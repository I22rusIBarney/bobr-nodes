"""Очередь задач: покупка в Bobr Cloud → ожидание IP → установка ноды. Каждая задача — одно сообщение,
которое обновляется по мере прохождения этапов. Незавершённые задачи продолжаются после перезапуска бота."""
from __future__ import annotations

import asyncio
import html
import logging
import time
from pathlib import Path

from . import engine, upgrade
from .bcloud import Bcloud, BcloudError, BcloudUnavailable
from .store import Store

log = logging.getLogger(__name__)

STAGES = [('buy', 'Покупка в Bobr Cloud'), ('ip', 'Создание сервера'), ('dns', 'DNS в Cloudflare'),
          ('ssh', 'Вход по SSH'), ('install', 'Установка ноды'), ('extras', 'Дополнения на ноде'), ('panel', 'Регистрация в панели'),
          ('check', 'Проверка')]
STAGE_ORDER = [s for s, _ in STAGES]
SERVICE_KINDS = ('node', 'reip', 'upgrade')  # задачи по уже работающей ноде: без покупки и сервера в Bobr Cloud


def xhttp_label(x) -> str:
    mode = x.get('mode') if isinstance(x, dict) else None
    return {'direct': 'XHTTP напрямую', 'cdn': 'XHTTP через CDN'}.get(mode, 'XHTTP')


def extras_label(d: dict) -> str:
    port = (d.get('host') or {}).get('port') if d.get('kind') != 'node' else d.get('port')
    parts = (['Psiphon'] if d.get('psiphon') else []) + (['WARP'] if d.get('warp') else []) + ([xhttp_label(d['xhttp'])] if d.get('xhttp') else []) + \
        ([f'порт {port}'] if port not in (None, '', 443, '443') else [])
    return ', '.join(parts)


REIP_IP = {'bcloud': 'Смена IP в Bobr Cloud', 'sync': 'Новый IP из Bobr Cloud', 'manual': 'Новый IP'}


def stages_for(d: dict) -> list[tuple[str, str]]:
    """Этапы задачи с подписями. Дополнения — только если они есть; у задач по готовой ноде — без покупки и установки."""
    if d.get('kind') == 'upgrade':
        return [('ssh', 'Вход по SSH'), ('install', f'Образ ноды {d.get("tag") or "latest"}'), ('check', 'Нода на связи')]
    if d.get('kind') == 'reip':
        return [('ip', REIP_IP.get(d.get('mode'), 'Новый IP')), ('dns', 'DNS в Cloudflare'), ('ssh', 'Вход по SSH на новом IP'),
                ('panel', 'Адрес в панели'), ('check', 'Нода на связи')]
    extra = extras_label(d)
    if d.get('kind') == 'node' and d.get('rollback'):
        return [('panel', 'Исходный профиль в панели'), ('check', 'Нода на связи')]
    if d.get('kind') == 'node':
        out = ([('ssh', 'Вход по SSH'), ('extras', 'Установка: ' + extra)] if extra else []) + \
            [('panel', 'Профиль и хосты в панели')] + ([('check', 'Нода на связи')] if d.get('psiphon') or d.get('warp') or d.get('xhttp') else [])
        return out
    skip = set(d.get('skipped_stages') or []) | (set() if extra else {'extras'})
    names = {'extras': 'Дополнения: ' + extra, **({'ip': 'Сервер из Bobr Cloud'} if d.get('existing') else {})}
    return [(c, names.get(c, lbl)) for c, lbl in STAGES if c not in skip]


# доступность новой ноды из России — по чекерам мониторинга, уже после установки
RU_CHECK = {'wait': ('⏳', 'проверяю на чекерах, обычно 2–5 минут'), 'ok': ('🟢', 'доступна'),
            'bad': ('🔴', 'не проходит проверку'), 'none': ('▫️', 'чекеры не успели проверить')}

STATUS_ICON = {'queued': '🕓', 'buying': '⏳', 'provisioning': '⏳', 'deploying': '⏳', 'done': '✅', 'failed': '❌'}


CREATE_TRIES = 2  # сколько раз бот сам покупает сервер, если Bobr Cloud не смог его создать


class Fail(Exception):
    pass


def bcloud_from(store: Store, tg_id: int | str | None = None) -> Bcloud:
    """Клиент Bobr Cloud от имени tg_id (владелец группы); по умолчанию — аккаунт из настроек."""
    return Bcloud(store.get('bcloud_url'), store.get('bcloud_token'), int(tg_id or store.get('bcloud_tg_id') or 0))


def buyer_of(store: Store, prof: dict) -> int:
    """Чей баланс у группы: владелец группы или аккаунт из настроек. Группа приглашённого пользователя —
    только с его баланса, никогда с аккаунта администратора."""
    return int(prof.get('owner_tg_id') or prof.get('created_by') or store.get('bcloud_tg_id') or 0)


def cf_token_of(store: Store, prof: dict) -> str:
    """Токен Cloudflare группы; общий из «Подключений» — только у групп администратора, не у групп пользователей."""
    if prof.get('cf_token'):
        return prof['cf_token']
    return '' if prof.get('created_by') else store.get('cf_token')


def job_text(job: dict, profile_name: str = '', last: str = '') -> str:
    d = job['data']
    icon = STATUS_ICON.get(job['status'], '•')
    head = f'{icon} <b>{html.escape(d["domain"])}</b>  #{job["id"]}\n{d.get("flag", "")} {html.escape(d.get("loc_name", ""))} · {html.escape(d.get("spec", ""))}'
    if profile_name:
        head += f'\n🗂 {html.escape(profile_name)}'
    lines = [head, '']
    cur = job['stage']
    cur_i = STAGE_ORDER.index(cur) if cur in STAGE_ORDER else -1
    for code, label in stages_for(d):
        i = STAGE_ORDER.index(code)
        if job['status'] == 'done' or i < cur_i:
            mark = '✅'
        elif i == cur_i:
            mark = '❌' if job['status'] == 'failed' else '⏳'
        else:
            mark = '▫️'
        extra = ''
        if code == 'ip' and d.get('ip') and (i < cur_i or job['status'] == 'done'):
            extra = f' · <code>{d["ip"]}</code>'
        lines.append(f'{mark} {label}{extra}')
    ru = RU_CHECK.get(d.get('ru_check') or '')
    if job['status'] == 'done' and ru:
        lines.append(f'{ru[0]} Из России: {html.escape(d.get("ru_note") or ru[1])}')
    if job['status'] == 'failed' and job['error']:
        lines += ['', f'<b>Ошибка:</b> {html.escape(job["error"][:600])}']
    elif job['status'] in ('provisioning', 'deploying', 'buying') and last:
        lines += ['', f'<i>{html.escape(last[:160])}</i>']
    elif job['status'] == 'done' and d.get('kind') == 'upgrade' and d.get('result'):
        lines += ['', html.escape(d['result'])]
    elif job['status'] == 'done' and d.get('kind') not in SERVICE_KINDS:
        lines += ['', f'Root-пароль: <tg-spoiler><code>{html.escape(d.get("password", ""))}</code></tg-spoiler>']
    return '\n'.join(lines)


class Runner:
    def __init__(self, store: Store, bot, logdir: str | Path = 'data/logs', *, deploy=engine.deploy,
                 deploy_recipe=engine.deploy_recipe, update_node=engine.update_node, reip=engine.reip,
                 upgrade_node=upgrade.upgrade_node,
                 bcloud_factory=bcloud_from, poll_every: float = 10, ip_timeout: float = 20 * 60, edit_every: float = 3):
        self.store = store
        self.bot = bot
        self.logdir = Path(logdir)
        self.logdir.mkdir(parents=True, exist_ok=True)
        self.deploy = deploy
        self.deploy_recipe = deploy_recipe
        self.update_node = update_node
        self.upgrade_node = upgrade_node
        self.reip = reip
        self.reip_timeout = 15 * 60
        self.keydir = self.logdir.parent / 'ssh'
        self.bcloud_factory = bcloud_factory
        self.poll_every = poll_every
        self.ip_timeout = ip_timeout
        self.rebuy_wait = min(5.0, poll_every)
        self.after_deploy = None  # (jid) -> None: мониторинг проверяет новую ноду из России  # Bobr Cloud возвращает деньги за несозданный сервер сразу
        self.edit_every = edit_every
        self.tasks: dict[int, asyncio.Task] = {}
        self.last: dict[int, str] = {}
        self._edited: dict[int, float] = {}
        self._pending: set[int] = set()
        self._sem: asyncio.Semaphore | None = None
        self._sem_n = 0

    def sem(self) -> asyncio.Semaphore:
        n = max(1, int(self.store.get('parallel', '3') or 3))
        if self._sem is None or n != self._sem_n:
            self._sem, self._sem_n = asyncio.Semaphore(n), n
        return self._sem

    def bot_key(self) -> tuple[str, str]:
        from . import sshkey
        return sshkey.ensure(self.keydir)

    def known_hosts(self) -> str:
        self.keydir.mkdir(parents=True, exist_ok=True)
        return str(self.keydir / 'known_hosts')

    def pubkeys(self, prof: dict | None = None) -> str:
        """Ключи для authorized_keys новой ноды: ваш (из настроек) и ключ бота. На серверы групп приглашённых
        пользователей ваш личный ключ не кладём — только ключ бота (он нужен для починки и дополнений)."""
        mine = '' if (prof or {}).get('created_by') else self.store.get('ssh_pubkey')
        return '\n'.join(x for x in (mine, self.bot_key()[1]) if x and x != '-')

    def log_path(self, jid: int) -> Path:
        return self.logdir / f'job-{jid}.log'

    # ── запуск ──
    def start(self, jid: int) -> None:
        t = self.tasks.get(jid)
        if t and not t.done():
            return
        self.tasks[jid] = asyncio.create_task(self._run(jid))

    # ── серверы, удалённые в bcloud ──
    async def sync_bcloud(self) -> list[int]:
        """Готовые ноды, чей сервер удалён в Bobr Cloud (вручную или по окончании срока): нода и хосты — из панели,
        A-запись — из Cloudflare, сервер — из списка бота. Профиль остаётся. Возвращает id таких задач."""
        done = []
        for j in self.store.jobs(5000):
            d = j['data']
            if j['status'] != 'done' or d.get('kind') in SERVICE_KINDS or not d.get('server_id') or d.get('removed'):
                continue
            prof = self.store.profile(j['profile_id'])
            if not prof:
                continue
            try:
                bc = self.bcloud_factory(self.store, d.get('buyer') or buyer_of(self.store, prof))
                bc.RETRY_FOR = 0  # фоновая сверка: bcloud недоступен — просто проверим в следующий раз
                srv = await bc.server(int(d['server_id']))
            except BcloudError as e:  # 404 — сервер передан другому клиенту или bcloud недоступен: ничего не удаляем
                log.info('sync bcloud job %s: %s', j['id'], e)
                continue
            if srv.get('status') != 'deleted':
                continue
            lines: list[str] = []
            pconf = {**prof, 'cf_token': cf_token_of(self.store, prof)}
            try:
                res = await asyncio.to_thread(engine.remove_node, pconf, d.get('ip', ''), d.get('domain', ''), lines.append)
            except engine.StepError as e:
                log.warning('remove node job %s: %s', j['id'], e)
                continue  # панель недоступна — попробуем в следующий раз
            with open(self.log_path(j['id']), 'a', encoding='utf-8') as lf:
                lf.write(f'\n\n===== {time.strftime("%Y-%m-%d %H:%M:%S")} сервер удалён в Bobr Cloud\n' +
                         ''.join(f'### {x}\n' for x in lines))
            self.store.patch_job_data(j['id'], removed=True, removed_at=int(time.time()))
            self.store.update_job(j['id'], hidden=1)
            done.append(j['id'])
            try:
                await self.bot.send_message(j['chat_id'], (
                    f'🗑 <b>Сервер {html.escape(d.get("domain") or d.get("name", ""))} удалён в Bobr Cloud</b>\n\n'
                    f'Из панели «{html.escape(prof.get("name", ""))}» убраны нода и хосты: {res["nodes"]} / {res["hosts"]}'
                    + (', A-запись в Cloudflare удалена' if res['dns'] else '') + '. Профиль не тронут.'), parse_mode='HTML')
            except Exception as e:  # noqa: BLE001
                log.info('notify removed %s: %s', j['id'], e)
        return done

    async def sync_loop(self, every: float = 300) -> None:
        while True:
            await asyncio.sleep(every)
            try:
                await self.sync_bcloud()
            except Exception:  # noqa: BLE001 — сверка не должна ронять бота
                log.exception('sync bcloud')

    async def resume_all(self) -> int:
        jobs = self.store.active_jobs()
        for j in jobs:
            self.start(j['id'])
        return len(jobs)

    async def wait_all(self) -> None:
        while any(not t.done() for t in self.tasks.values()):
            await asyncio.gather(*[t for t in self.tasks.values() if not t.done()], return_exceptions=True)

    # ── сообщение задачи ──
    async def render(self, jid: int, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._edited.get(jid, 0) < self.edit_every:
            if jid not in self._pending:
                self._pending.add(jid)
                asyncio.get_running_loop().call_later(self.edit_every, lambda: asyncio.ensure_future(self._flush(jid)))
            return
        self._edited[jid] = now
        job = self.store.job(jid)
        if not job or not job['chat_id']:  # без чата — тихая задача (автообновление ночью): итог придёт сводкой
            return
        prof = self.store.profile(job['profile_id']) or {}
        text = job_text(job, prof.get('name', ''), self.last.get(jid, ''))
        from .handlers import job_kb  # отложенный импорт: handlers импортирует runner
        try:
            if job['msg_id']:
                await self.bot.edit_message_text(text, chat_id=job['chat_id'], message_id=job['msg_id'],
                                                 reply_markup=job_kb(job), parse_mode='HTML')
            else:
                m = await self.bot.send_message(job['chat_id'], text, reply_markup=job_kb(job), parse_mode='HTML')
                self.store.update_job(jid, msg_id=m.message_id)
        except Exception as e:  # сообщение не изменилось / удалено — не мешает работе
            if 'not modified' not in str(e):
                log.warning('render job %s: %s', jid, e)

    async def _flush(self, jid: int) -> None:
        self._pending.discard(jid)
        await self.render(jid, force=True)

    # ── этапы ──
    async def _set(self, jid: int, **fields) -> None:
        self.store.update_job(jid, **fields)
        await self.render(jid, force='status' in fields)

    def ssh_of(self, pid: int, node_uuid: str) -> dict:
        """Как заходить на ноду панели: сохранённый доступ → пароль из задачи бота, купившей её → пусто."""
        import json as _json
        try:
            saved = _json.loads(self.store.get(f'node_ssh:{pid}:{node_uuid}') or '{}')
        except ValueError:
            saved = {}
        return saved

    def _node_ssh(self, d: dict, prof: dict) -> dict:
        """Поля входа для задачи по ноде: сохранённый доступ, иначе — из задачи (пароль root из покупки)."""
        from . import sshaccess
        ssh = self.ssh_of(prof['id'], d['node_uuid']) or d.get('ssh') or {}
        return {**sshaccess.to_node(ssh, self.bot_key()[0]), 'known_hosts': self.known_hosts()}

    async def _run_reip(self, jid: int, d: dict, prof: dict) -> None:
        """Новый IP ноды: сменить в Bobr Cloud (или взять уже сменённый), затем DNS, SSH и адрес в панели."""
        loop = asyncio.get_running_loop()
        await self._set(jid, status='deploying', stage='ip', error='')
        if not d.get('new_ip'):
            bc = self.bcloud_factory(self.store, d.get('buyer') or buyer_of(self.store, prof))
            if d['mode'] == 'bcloud' and not d.get('change_sent'):
                # отмечаем до запроса: повтор задачи не должен списать деньги второй раз
                d = self.store.patch_job_data(jid, change_sent=True)
                try:
                    r = await bc.change_ip(d['server_id'])
                except BcloudError:
                    self.store.patch_job_data(jid, change_sent=False)
                    raise
                self.last[jid] = r.get('message') or 'Bobr Cloud меняет IP'
            end = time.monotonic() + self.reip_timeout
            while True:
                srv = await bc.server(d['server_id'], sync=True)
                if srv.get('ip') and srv['ip'] != d['old_ip']:
                    d = self.store.patch_job_data(jid, new_ip=srv['ip'])
                    break
                if d['mode'] == 'sync':
                    raise Fail(f'в Bobr Cloud у сервера прежний IP {d["old_ip"]} — сначала смените его в Bobr Cloud или выберите «Сменить IP через Bobr Cloud»')
                if time.monotonic() > end:
                    raise Fail('Bobr Cloud так и не сообщил новый IP — проверьте сервер в Bobr Cloud и нажмите «Повторить»')
                self.last[jid] = 'ждём новый IP от Bobr Cloud'
                await self.render(jid)
                await asyncio.sleep(self.poll_every * 3)
        self.last[jid] = f'новый IP: {d["new_ip"]}'
        node = {'node_uuid': d['node_uuid'], 'name': d.get('name', ''), 'old_ip': d['old_ip'], 'new_ip': d['new_ip'],
                'domains': d.get('domains') or [], **self._node_ssh(d, prof)}
        pconf = {**prof, 'cf_token': cf_token_of(self.store, prof)}
        async with self.sem():
            def on_log(text: str) -> None:
                self.last[jid] = text
                lf.write(f'\n### {time.strftime("%H:%M:%S")} {text}\n')
                lf.flush()
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.render(jid)))

            def on_stage(name: str) -> None:
                self.store.update_job(jid, stage=name)
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.render(jid)))

            with open(self.log_path(jid), 'a', encoding='utf-8') as lf:
                lf.write(f'\n\n===== {time.strftime("%Y-%m-%d %H:%M:%S")} смена IP #{jid}: {d.get("name")} {d["old_ip"]} → {d["new_ip"]}\n')
                await asyncio.to_thread(self.reip, node, pconf, lf, on_log, on_stage)
        if d.get('deploy_job'):  # задача покупки помнит IP сервера — для SSH и поиска ноды дальше
            self.store.patch_job_data(int(d['deploy_job']), ip=d['new_ip'])

    async def _run_node(self, jid: int, d: dict, prof: dict) -> None:
        """Дополнения на уже работающей ноде панели (без покупки и установки)."""
        loop = asyncio.get_running_loop()
        node = {'node_uuid': d['node_uuid'], 'name': d.get('name', ''), 'ip': d.get('ip', ''),
                'domains': d.get('domains') or [], 'psiphon': d.get('psiphon'), 'warp': d.get('warp'), 'xhttp': d.get('xhttp'),
                'port': d.get('port'), 'final_mask': d.get('final_mask'), 'rollback': d.get('rollback'),
                **self._node_ssh(d, prof)}
        pconf = {**prof, 'ssh_pubkey': self.pubkeys(prof), 'cf_token': cf_token_of(self.store, prof)}
        self.last[jid] = 'ждём свободный слот'
        await self.render(jid)
        async with self.sem():
            await self._set(jid, status='deploying', stage=stages_for(d)[0][0], error='')

            def on_log(text: str) -> None:
                self.last[jid] = text
                lf.write(f'\n### {time.strftime("%H:%M:%S")} {text}\n')
                lf.flush()
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.render(jid)))

            def on_stage(name: str) -> None:
                self.store.update_job(jid, stage=name)
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.render(jid)))

            with open(self.log_path(jid), 'a', encoding='utf-8') as lf:
                lf.write(f'\n\n===== {time.strftime("%Y-%m-%d %H:%M:%S")} дополнения на ноде #{jid}: {d.get("name")} {d.get("ip")}\n')
                await asyncio.to_thread(self.update_node, node, pconf, lf, on_log, on_stage)

    async def _run_upgrade(self, jid: int, d: dict, prof: dict) -> None:
        """Новая версия remnawave/node на ноде: образ, перезапуск, проверка связи с панелью, откат при неудаче."""
        loop = asyncio.get_running_loop()
        node = {'node_uuid': d['node_uuid'], 'name': d.get('name', ''), 'ip': d.get('ip', ''), 'tag': d.get('tag') or 'latest',
                **self._node_ssh(d, prof)}
        self.last[jid] = 'ждём свободный слот'
        await self.render(jid)
        async with self.sem():
            await self._set(jid, status='deploying', stage='ssh', error='')

            def on_log(text: str) -> None:
                self.last[jid] = text
                lf.write(f'\n### {time.strftime("%H:%M:%S")} {text}\n')
                lf.flush()
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.render(jid)))

            def on_stage(name: str) -> None:
                self.store.update_job(jid, stage=name)
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.render(jid)))

            with open(self.log_path(jid), 'a', encoding='utf-8') as lf:
                lf.write(f'\n\n===== {time.strftime("%Y-%m-%d %H:%M:%S")} обновление ноды #{jid}: {d.get("name")} → {node["tag"]}\n')
                res = await asyncio.to_thread(self.upgrade_node, node, prof, lf, on_log, on_stage)
        self.store.patch_job_data(jid, result=res)

    async def _run(self, jid: int) -> None:
        job = self.store.job(jid)
        if not job:
            return
        d = job['data']
        loop = asyncio.get_running_loop()
        try:
            prof = self.store.profile(job['profile_id'])
            if not prof:
                raise Fail('группа удалена')
            if d.get('kind') == 'reip':
                await self._run_reip(jid, d, prof)
                await self._set(jid, status='done', error='')
                return
            if d.get('kind') == 'upgrade':
                await self._run_upgrade(jid, d, prof)
                await self._set(jid, status='done', error='')
                return
            if d.get('kind') == 'node':
                await self._run_node(jid, d, prof)
                await self._set(jid, status='done', error='')
                return
            bc = self.bcloud_factory(self.store, d.get('buyer') or buyer_of(self.store, prof))

            # Bobr Cloud (Astra) иногда не выдаёт сервер — деньги возвращаются, а бот сам пробует купить ещё раз;
            # после второй неудачи предлагает другую конфигурацию или локацию
            while True:
                if not d.get('server_id'):
                    if job['status'] == 'buying' and not d.get('create_tries'):
                        raise Fail('бот перезапустился во время покупки — проверьте «Мои серверы» в Bobr Cloud. '
                                   'Повтор купит новый сервер.')
                    await self._set(jid, status='buying', stage='buy', error='')
                    srv = await bc.buy(d['location'], d['os'], d['idx'], d.get('period', '1m'), d['password'], d.get('custom'))
                    d = self.store.patch_job_data(jid, server_id=srv['id'])

                await self._set(jid, status='provisioning', stage='ip', error='')
                self.last[jid] = 'ждём, пока Bobr Cloud создаст сервер'
                end = time.monotonic() + self.ip_timeout
                rebuy = False
                while not d.get('ip'):  # IP уже известен (повтор, перезапуск бота) — bcloud больше не нужен
                    try:
                        srv = await bc.server(d['server_id'])
                    except BcloudUnavailable as e:
                        if time.monotonic() > end:
                            raise Fail(f'{e} — проверьте, что Bobr Cloud запущен, и нажмите «Повторить»') from e
                        self.last[jid] = 'Bobr Cloud не отвечает — жду'
                        await self.render(jid)
                        await asyncio.sleep(self.poll_every)
                        continue
                    if srv['status'] == 'active' and srv.get('ip'):
                        if d.get('ip') != srv['ip']:  # новый сервер: прежний ключ этого IP (если был) больше не его
                            engine.forget_host(self.known_hosts(), srv['ip'])
                        d = self.store.patch_job_data(jid, ip=srv['ip'], password=srv.get('root_password') or d['password'])
                        break
                    if srv['status'] in ('deleted', 'failed', 'error'):
                        if d.get('existing'):
                            raise Fail(f'сервер в Bobr Cloud недоступен (статус {srv["status"]})')
                        tries = int(d.get('create_tries') or 0) + 1
                        if tries < CREATE_TRIES:
                            d = self.store.patch_job_data(jid, server_id=None, create_tries=tries)
                            self.last[jid] = 'Bobr Cloud не смог создать сервер — покупаю ещё раз'
                            await self.render(jid)
                            await asyncio.sleep(self.rebuy_wait)  # пусть Bobr Cloud вернёт деньги за первую попытку
                            rebuy = True
                            break
                        # следующий «Повторить» — снова с двумя попытками
                        d = self.store.patch_job_data(jid, server_id=None, create_tries=0, create_failed=True)
                        raise Fail(f'Bobr Cloud дважды не смог создать сервер (статус {srv["status"]}) — похоже, в этой локации '
                                   'сейчас нет мощностей под такую конфигурацию. Выберите другую конфигурацию или локацию. '
                                   'Деньги за обе попытки Bobr Cloud возвращает сам')
                    if time.monotonic() > end:
                        raise Fail('сервер слишком долго создаётся — проверьте его в Bobr Cloud и нажмите «Повторить»')
                    await asyncio.sleep(self.poll_every)
                if not rebuy:
                    break
            if not d.get('renamed'):
                try:
                    await bc.rename(d['server_id'], d['name'])
                    d = self.store.patch_job_data(jid, renamed=True)
                except BcloudError as e:  # имя в bcloud — удобство, не повод останавливать установку
                    log.info('rename %s: %s', jid, e)

            self.last[jid] = 'ждём свободный слот установки'
            await self.render(jid)
            async with self.sem():
                await self._set(jid, status='deploying', stage='dns')
                node = {'name': d['name'], 'ip': d['ip'], 'domain': d['domain'], 'country': d.get('country', ''),
                        'remark': d.get('remark', ''), 'ssh_user': 'root', 'ssh_password': d['password'],
                        'host': d.get('host'), 'squads': d.get('squads'), 'place': d.get('place'),
                        'psiphon': d.get('psiphon'), 'warp': d.get('warp'), 'xhttp': d.get('xhttp'),
                        'traffic_control': prof.get('traffic_control', True), 'known_hosts': self.known_hosts()}
                pconf = {**prof, 'cf_token': cf_token_of(self.store, prof),
                         'ssh_pubkey': self.pubkeys(prof)}
                fn = self.deploy
                sid = d.get('script_id', prof.get('script_id'))
                if sid:
                    script = self.store.script(int(sid))
                    if not script or script.get('status') != 'approved' or not script.get('recipe'):
                        raise Fail('скрипт установки группы удалён или не утверждён — проверьте «Мои скрипты»')
                    pconf['script'] = script
                    fn = self.deploy_recipe

                def on_log(text: str) -> None:
                    self.last[jid] = text
                    lf.write(f'\n### {time.strftime("%H:%M:%S")} {text}\n')
                    lf.flush()
                    loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.render(jid)))

                def on_stage(name: str) -> None:
                    self.store.update_job(jid, stage=name)
                    loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.render(jid)))

                with open(self.log_path(jid), 'a', encoding='utf-8') as lf:
                    lf.write(f'\n\n===== {time.strftime("%Y-%m-%d %H:%M:%S")} запуск задачи #{jid}: {d["domain"]} {d["ip"]}\n')
                    await asyncio.to_thread(fn, node, pconf, lf, on_log, on_stage)
            await self._set(jid, status='done', error='')
            if self.after_deploy is not None:  # проверка из России — фоном, задача уже готова
                try:
                    self.after_deploy(jid)
                except Exception:  # noqa: BLE001
                    log.exception('after_deploy %s', jid)
        except (Fail, engine.StepError, BcloudError) as e:
            await self._set(jid, status='failed', error=str(e))
        except Exception as e:  # noqa: BLE001
            log.exception('job %s', jid)
            await self._set(jid, status='failed', error=f'{type(e).__name__}: {e}')
