"""Мониторинг доступности нод из России и починка.

Цикл (раз в ~5 минут): опросить все чекеры → по каждой группе с подпиской сопоставить проверки с нодами панели →
нода «недоступна», если её не пропускает большинство исправных чекеров. После FAILS_TO_ALERT таких циклов подряд —
сообщение и, по настройке группы, починка по шагам: Final Mask и свой порт → переустановка другим скриптом →
запасной XHTTP через CDN → смена IP в bcloud (платно) → новая нода в той же локации. После каждого шага ждём RECOVER_CYCLES
циклов: нода ожила — готово, нет — следующий шаг.

Состояние ноды хранится по ключу «группа:адрес ноды» (домен не меняется при смене IP и переустановке)."""
from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import checker as ck
from . import engine
from . import services as svc
from . import sshaccess
from .bcloud import BcloudError
from .store import ACTIVE, Store

log = logging.getLogger(__name__)

STEPS = ['mask', 'script', 'xhttp', 'reip', 'replace']  # смена IP платная — после бесплатных шагов, перед покупкой
STEP_LABEL = {'reip': 'смена IP в bcloud', 'mask': 'Final Mask и свой порт', 'script': 'переустановка другим скриптом',
              'xhttp': 'запасной XHTTP через CDN', 'replace': 'новая нода в той же локации'}
MODES = {'watch': 'только показывать', 'notify': 'сообщать', 'ask': 'сообщать и предлагать починку', 'auto': 'чинить сам'}
OLD = {'panel': 'убрать из панели, сервер оставить', 'delete': 'убрать из панели и удалить в bcloud', 'keep': 'ничего не трогать'}
DEFAULTS = {'mon_sub': '', 'mon_mode': 'ask', 'mon_steps': list(STEPS), 'mon_script': None, 'mon_port': 8443,
            'mon_old': 'panel', 'mon_max_buys': 2}
FAILS_TO_ALERT = 3
RECOVER_CYCLES = 2


def settings_of(prof: dict) -> dict:
    out = {k: prof[k] if prof.get(k) is not None else v for k, v in DEFAULTS.items()}
    out['mon_steps'] = [x for x in STEPS if x in (out['mon_steps'] or [])]
    return out


def clean_settings(raw: dict, cur: dict, scripts: list[dict]) -> dict:
    """Настройки мониторинга группы из админки → проверенные значения (ValueError с понятным текстом)."""
    out = settings_of(cur)
    if 'mon_sub' in raw and raw['mon_sub'] is not None:
        v = str(raw['mon_sub']).strip()
        if v or not cur.get('mon_sub'):  # пусто — оставить текущую (поле секретное); «-» — убрать
            out['mon_sub'] = ck.check_sub_url(v)
    if raw.get('mon_mode') is not None:
        if raw['mon_mode'] not in MODES:
            raise ValueError('режим мониторинга: watch, notify, ask или auto')
        out['mon_mode'] = raw['mon_mode']
    if raw.get('mon_steps') is not None:
        steps = raw['mon_steps']
        if not isinstance(steps, list) or any(x not in STEPS for x in steps):
            raise ValueError('шаги починки: ' + ', '.join(STEPS))
        out['mon_steps'] = [x for x in STEPS if x in steps]
    if 'mon_script' in raw:
        sid = raw['mon_script']
        if sid not in (None, '', 0):
            sc = next((x for x in scripts if x['id'] == int(sid)), None)
            if not sc or sc.get('status') != 'approved':
                raise ValueError('скрипт для переустановки: только утверждённый из «Моих скриптов»')
            out['mon_script'] = int(sid)
        else:
            out['mon_script'] = None
    if raw.get('mon_port') is not None:
        try:
            out['mon_port'] = engine.public_port(raw['mon_port'], int(cur.get('node_port') or 2222)) or 443
        except engine.StepError as e:
            raise ValueError(str(e)) from None
    if raw.get('mon_old') is not None:
        if raw['mon_old'] not in OLD:
            raise ValueError('старый сервер: panel, delete или keep')
        out['mon_old'] = raw['mon_old']
    if raw.get('mon_max_buys') is not None:
        try:
            n = int(raw['mon_max_buys'])
        except (TypeError, ValueError):
            n = -1
        if not 0 <= n <= 20:
            raise ValueError('покупок в сутки: от 0 до 20')
        out['mon_max_buys'] = n
    return out


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r] for r in rows if r])


class Monitor:
    def __init__(self, store: Store, runner, bot, admins: set[int], every: float = ck.INTERVAL):
        self.store, self.runner, self.bot, self.admins = store, runner, bot, admins
        self.every = every
        self.results: dict[int, list[dict] | None] = {}  # последний опрос каждого чекера
        self.polled_at = 0.0
        self._lock = asyncio.Lock()
        self._bg: set[asyncio.Task] = set()

    # ───────── чекеры ─────────
    def subs(self) -> list[tuple[int, str]]:
        return [(p['id'], p['mon_sub']) for p in self.store.profiles() if p.get('mon_sub')]

    def keyfiles(self) -> tuple[str, str, str]:
        path, pub = self.runner.bot_key()
        return path, pub, self.runner.known_hosts()

    def _bg_task(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    def install(self, cid: int) -> asyncio.Task:
        """Поставить или обновить чекер в фоне; ход — в поле log чекера."""
        return self._bg_task(self._install(cid))

    async def _install(self, cid: int) -> None:
        ch = self.store.checker(cid)
        if not ch:
            return
        lines: list[str] = []

        def say(t: str) -> None:
            lines.append(t)
            self.store.patch_checker(cid, log=lines[-30:])

        self.store.patch_checker(cid, status='installing', error='', log=[])
        path, pub, kh = self.keyfiles()
        try:
            fields = await asyncio.to_thread(ck.install, ch, self.subs(), path, pub, kh, say, _Sink())
            self.store.patch_checker(cid, **fields, status='running' if self.subs() else 'idle', error='',
                                     installed_at=int(time.time()))
        except Exception as e:  # noqa: BLE001 — любая ошибка установки видна в админке, а не теряется в фоне
            say(f'❌ {e}')
            self.store.patch_checker(cid, status='failed', error=str(e)[:300])

    def resync(self) -> asyncio.Task:
        """Подписки групп изменились — записать их на все установленные чекеры."""
        return self._bg_task(self._resync())

    async def _resync(self) -> None:
        subs = self.subs()
        path, _, kh = self.keyfiles()
        for ch in self.store.checkers():
            if ch.get('status') not in ('running', 'idle') or not ch.get('api_password'):
                continue
            try:
                st = await asyncio.to_thread(ck.apply_subs, ch, subs, path, kh, lambda t: None)
                self.store.patch_checker(ch['id'], status=st, error='')
            except (engine.StepError, OSError) as e:
                self.store.patch_checker(ch['id'], error=f'не удалось обновить подписки: {e}')

    async def poll_all(self) -> dict[int, list[dict] | None]:
        path, _, kh = self.keyfiles()
        out: dict[int, list[dict] | None] = {}

        async def one(ch):
            try:
                res = await asyncio.to_thread(ck.poll, ch, path, kh)
                out[ch['id']] = res
                self.store.patch_checker(ch['id'], polled_at=int(time.time()), poll_error='', proxies=len(res),
                                         online=sum(1 for p in res if p.get('online')))
            except (engine.StepError, OSError) as e:
                out[ch['id']] = None
                self.store.patch_checker(ch['id'], poll_error=str(e)[:200], polled_at=int(time.time()))
        await asyncio.gather(*[one(ch) for ch in self.store.checkers() if ch.get('status') == 'running'])
        self.results, self.polled_at = out, time.time()
        return out

    @staticmethod
    def healthy(res: list[dict] | None) -> bool:
        """Чекер исправен: ответил, и если проверок много — хоть одна прошла (иначе лежит сеть самого чекера)."""
        return res is not None and (len(res) < 3 or any(p.get('online') for p in res))

    # ───────── цикл ─────────
    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.every)
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — мониторинг не должен ронять бота
                log.exception('monitor tick')

    async def tick(self) -> None:
        async with self._lock:
            results = await self.poll_all()
            for prof in self.store.profiles():
                if prof.get('mon_sub'):
                    try:
                        await self.check_group(prof, results)
                    except engine.StepError as e:  # панель группы недоступна — в следующий раз
                        log.info('monitor group %s: %s', prof['id'], e)

    def votes(self, prof: dict, results: dict, nodes: list[dict]) -> dict[str, dict]:
        """Адрес ноды → {data, fail, uuid, name, connected}: сколько исправных чекеров её видят и у скольких она не проходит.
        Хосты через CDN (cdn-…) не считаются: нода жива, если проходит хоть один прямой вход."""
        name = ck.sub_name(prof['id'])
        bj = svc.bot_jobs_by_ip(self.store, prof['id'])
        out = {}
        for n in nodes:
            aliases = {n['address']}
            d = bj.get(n['address'])
            if d:
                aliases |= {d.get('ip') or '', d.get('domain') or ''} - {''}
            data = fail = 0
            for res in results.values():
                if not self.healthy(res):
                    continue
                mine = [p for p in res if p.get('subName') == name and p.get('server') in aliases
                        and not str(p.get('server') or '').startswith(engine.CDN_PREFIX)]
                if not mine:
                    continue
                data += 1
                if not any(p.get('online') for p in mine):
                    fail += 1
            out[n['address']] = {'data': data, 'fail': fail, 'uuid': n['uuid'], 'name': n.get('name') or n['address'],
                                 'connected': bool(n.get('isConnected')), 'country': n.get('countryCode') or ''}
        return out

    async def check_group(self, prof: dict, results: dict) -> None:
        panel = engine.Panel(prof['panel_url'], prof['panel_token'], prof.get('panel_cookie', ''))
        nodes = await asyncio.to_thread(panel.call, 'GET', '/api/nodes')
        votes = self.votes(prof, results, nodes)
        cfg = settings_of(prof)
        now = time.time()
        seen = set()
        for addr, v in votes.items():
            key = f'{prof["id"]}:{addr}'
            seen.add(key)
            st = self.store.mon(key) or {'status': 'ok', 'fails': 0}
            st.update(uuid=v['uuid'], name=v['name'], addr=addr, data=v['data'], fail=v['fail'],
                      connected=v['connected'], country=v['country'], checked_at=int(now))
            down = v['data'] > 0 and v['fail'] * 2 > v['data']
            if v['data'] == 0:  # ни один исправный чекер ноду не видит — нет данных, ничего не решаем
                self.store.set_mon(key, st)
                continue
            if not down:
                if st.get('status') in ('down', 'repairing', 'gave_up', 'ignored'):
                    await self.recovered(prof, st)
                st.update(status='ok', fails=0, step=None, job=None, job_done=False, steps_done=[])
                self.store.set_mon(key, st)
                continue
            st['fails'] = int(st.get('fails') or 0) + 1
            if st.get('status', 'ok') == 'ok' and st['fails'] >= FAILS_TO_ALERT:
                st.update(status='down', since=int(now), tok=secrets.token_hex(4), steps_done=[])
                self.store.set_mon(key, st)
                await self.alert(prof, st, cfg)
                if cfg['mon_mode'] == 'auto':
                    st = self.store.mon(key)
                    await self.start_repair(prof, st)
                continue
            if st.get('status') == 'repairing':
                await self.drive(prof, st)
                continue
            self.store.set_mon(key, st)
        for key, st in self.store.mon_all(f'{prof["id"]}:').items():  # ноды, которых больше нет в панели
            if key not in seen and st.get('status') == 'repairing':  # переустановка: нода вернётся под тем же адресом
                job = self.store.job(int(st['job'])) if st.get('job') else None
                if job and job['status'] == 'failed':
                    await self.drive(prof, st)
                continue
            if key not in seen and not (st.get('status') == 'repairing' or
                                        st.get('status') == 'replaced' and now - (st.get('checked_at') or 0) < 86400):
                self.store.set_mon(key, None)

    # ───────── сообщения ─────────
    async def say(self, text: str, markup=None) -> None:
        for uid in sorted(self.admins):
            try:
                await self.bot.send_message(uid, text, reply_markup=markup, parse_mode='HTML', disable_web_page_preview=True)
            except Exception as e:  # noqa: BLE001
                log.info('monitor notify %s: %s', uid, e)

    def head(self, prof: dict, st: dict) -> str:
        return f'<b>{html.escape(st.get("name") or st["addr"])}</b> · {html.escape(prof["name"])}'

    async def alert(self, prof: dict, st: dict, cfg: dict) -> None:
        if cfg['mon_mode'] == 'watch':
            return
        why = f'не проходит проверку у {st["fail"]} из {st["data"]} чекеров в России уже {st["fails"] * self.every // 60} мин'
        why += '' if st.get('connected') else '; с панелью тоже нет связи — похоже, сервер лежит'
        text = f'🔴 {self.head(prof, st)}\n{why}.'
        steps = [STEP_LABEL[s] for s in cfg['mon_steps']]
        if cfg['mon_mode'] == 'auto':
            text += '\n\nЧиню по шагам: ' + (', '.join(steps) if steps else 'шагов не выбрано') + '.'
            return await self.say(text)
        if cfg['mon_mode'] == 'ask':
            text += '\n\nШаги починки: ' + (', '.join(steps) if steps else 'не выбраны') + '.'
            rows = [[('🛠 Починить', f'mon:f:{st["tok"]}')]] if steps else []
            if 'replace' in cfg['mon_steps']:
                rows.append([('🆕 Сразу новая нода', f'mon:r:{st["tok"]}')])
            rows.append([('🙈 Не трогать', f'mon:i:{st["tok"]}')])
            return await self.say(text, kb(*rows))
        await self.say(text)

    async def recovered(self, prof: dict, st: dict) -> None:
        if settings_of(prof)['mon_mode'] == 'watch':
            return
        done = st.get('steps_done') or []
        how = f' после шага «{STEP_LABEL[done[-1]]}»' if done and st.get('status') == 'repairing' else ''
        await self.say(f'🟢 {self.head(prof, st)}\nснова доступна из России{how}.')

    # ───────── починка ─────────
    def find(self, tok: str) -> tuple[dict, str, dict] | None:
        for key, st in self.store.mon_all().items():
            if st.get('tok') == tok:
                prof = self.store.profile(int(key.split(':', 1)[0]))
                if prof:
                    return prof, key, st
        return None

    async def action(self, tok: str, what: str) -> str:
        """Кнопка из сообщения или админки: fix — чинить по шагам, replace — сразу новая нода, ignore — не трогать."""
        found = self.find(tok)
        if not found:
            return 'Нода уже в порядке или удалена'
        prof, key, st = found
        if st.get('status') not in ('down', 'gave_up', 'ignored', 'repairing'):
            return 'Нода уже в порядке'
        if what == 'ignore':
            st['status'] = 'ignored'
            self.store.set_mon(key, st)
            return 'Не трогаю, пока нода не оживёт'
        if st.get('status') == 'repairing':
            return 'Починка уже идёт'
        st['steps_done'] = [x for x in STEPS if x != 'replace'] if what == 'replace' else []
        self.store.set_mon(key, st)
        return await self.start_repair(prof, st, only_replace=what == 'replace')

    async def start_repair(self, prof: dict, st: dict, only_replace: bool = False) -> str:
        st.update(status='repairing', step=None, job=None, job_done=False)
        self.store.set_mon(f'{prof["id"]}:{st["addr"]}', st)
        return await self.next_step(prof, st, only_replace=only_replace)

    async def next_step(self, prof: dict, st: dict, only_replace: bool = False) -> str:
        cfg = settings_of(prof)
        key = f'{prof["id"]}:{st["addr"]}'
        done = st.get('steps_done') or []
        plan = ['replace'] if only_replace else [x for x in cfg['mon_steps'] if x not in done]
        for step in plan:
            done.append(step)
            st['steps_done'] = done
            try:
                jid, note = await self.run_step(prof, st, step, cfg)
            except (ValueError, engine.StepError, BcloudError) as e:
                await self.say(f'⚠️ {self.head(prof, st)}\nшаг «{STEP_LABEL[step]}» пропущен: {html.escape(str(e))}')
                continue
            st.update(step=step, job=jid, job_done=False, fails=0)
            self.store.set_mon(key, st)
            await self.say(f'🛠 {self.head(prof, st)}\nшаг «{STEP_LABEL[step]}»: {html.escape(note)}')
            return f'Шаг: {STEP_LABEL[step]}'
        st.update(status='gave_up', step=None, job=None)
        self.store.set_mon(key, st)
        await self.say(f'❌ {self.head(prof, st)}\nвсе шаги починки испробованы — нода всё ещё недоступна. Посмотрите её сами.')
        return 'Шагов больше нет'

    async def drive(self, prof: dict, st: dict) -> None:
        """Нода в починке и всё ещё недоступна: ждём задачу шага, потом RECOVER_CYCLES циклов, потом следующий шаг."""
        key = f'{prof["id"]}:{st["addr"]}'
        job = self.store.job(int(st['job'])) if st.get('job') else None
        if job and job['status'] in ACTIVE:
            st['fails'] = 0
            self.store.set_mon(key, st)
            return
        if job and not st.get('job_done'):
            st.update(job_done=True, fails=0)
            if job['status'] == 'failed':
                self.store.set_mon(key, st)
                await self.say(f'⚠️ {self.head(prof, st)}\nшаг «{STEP_LABEL[st["step"]]}» не удался: {html.escape(job["error"][:200])}')
                await self.next_step(prof, st)
                return
            if st['step'] == 'replace':
                await self.finish_replace(prof, st, job)
                return
            self.store.set_mon(key, st)
            return
        if int(st.get('fails') or 0) >= RECOVER_CYCLES:
            await self.next_step(prof, st)
        else:
            self.store.set_mon(key, st)

    # ── шаги ──
    def chat(self) -> int:
        return sorted(self.admins)[0] if self.admins else 0

    async def run_step(self, prof: dict, st: dict, step: str, cfg: dict) -> tuple[int, str]:
        pid = prof['id']
        bd = svc.bot_jobs_by_ip(self.store, pid).get(st['addr']) or {}
        if any(j['status'] in ACTIVE and j['profile_id'] == pid and (j['data'].get('node_uuid') == st['uuid']
               or j['data'].get('domain') == st['addr']) for j in self.store.active_jobs()):
            raise ValueError('по этой ноде уже идёт задача')
        ip = bd.get('ip') or (st['addr'] if engine.is_ip(st['addr']) else '')
        if step == 'replace':
            return await self.replace(prof, st, bd, cfg)
        if step == 'script':
            return await self.reinstall(prof, st, bd, cfg)
        ssh = svc.node_ssh(self.store, self.runner, pid, st['uuid'], st['addr'])
        if not ssh:
            raise ValueError('нет доступа по SSH к ноде (задайте его на странице ноды)')
        doms = [bd['domain']] if bd.get('domain') else ([] if engine.is_ip(st['addr']) else [st['addr']])
        base = {'node_uuid': st['uuid'], 'name': st['name'], 'domain': doms[0] if doms else st['name'], 'domains': doms,
                'loc_name': st['name'], 'ssh': ssh, 'monitor': True}
        if step == 'reip':
            if not bd.get('server_id'):
                raise ValueError('нода куплена не через бота — сменить IP в bcloud нельзя')
            if not ip:
                raise ValueError('не знаю текущий IP ноды')
            data = {**base, 'kind': 'reip', 'mode': 'bcloud', 'ip': ip, 'old_ip': ip, 'new_ip': '', 'flag': '🔁',
                    'spec': 'смена IP', 'server_id': bd['server_id'], 'buyer': bd.get('buyer'), 'deploy_job': bd.get('_job')}
            note = 'меняю IP в bcloud'
        elif step == 'mask':
            port = cfg['mon_port']
            data = {**base, 'kind': 'node', 'ip': ip or st['addr'], 'flag': '🛠', 'spec': 'починка: Final Mask и порт',
                    'port': port if port != 443 else '', 'final_mask': engine.final_mask(None)}
            note = 'Final Mask на хостах' + (f', порт {port}' if port != 443 else '')
        elif step == 'xhttp':
            if prof.get('script_id'):
                raise ValueError('группа ставит ноды своим скриптом — запасной XHTTP только у встроенного установщика')
            if not (prof.get('cf_token') or self.store.get('cf_token')):
                raise ValueError('нет токена Cloudflare')
            if not doms:
                raise ValueError('у ноды нет домена')
            data = {**base, 'kind': 'node', 'ip': ip or st['addr'], 'flag': '🛠', 'spec': 'починка: XHTTP через CDN',
                    'xhttp': {'port': engine.XHTTP_PORT, 'mode': 'cdn'}, 'port': ''}
            note = 'ставлю запасной вход через Cloudflare CDN'
        else:
            raise ValueError('неизвестный шаг')
        jid = self.store.add_job('mon-' + st['uuid'][:8], pid, self.chat(), data)
        await self.runner.render(jid, force=True)
        self.runner.start(jid)
        return jid, note

    async def bcloud_server(self, prof: dict, bd: dict, ip: str) -> dict | None:
        """Сервер ноды в bcloud у покупателя группы: по задаче бота или по IP."""
        try:
            servers = {x['id']: x for x in await svc.own_servers(self.store, prof)}
        except BcloudError:
            return None
        if bd.get('server_id') and int(bd['server_id']) in servers:
            return servers[int(bd['server_id'])]
        return next((x for x in servers.values() if ip and x.get('ip') == ip), None)

    async def reinstall(self, prof: dict, st: dict, bd: dict, cfg: dict) -> tuple[int, str]:
        """Переустановка на том же сервере другим скриптом: нода и хосты — из панели, затем установка с тем же доменом."""
        ip = bd.get('ip') or (st['addr'] if engine.is_ip(st['addr']) else '')
        dom = bd.get('domain') or ('' if engine.is_ip(st['addr']) else st['addr'])
        if not dom:
            raise ValueError('у ноды нет домена — переустановке он нужен для сертификата')
        sid = cfg['mon_script']
        if (sid or None) == (prof.get('script_id') or None):
            raise ValueError('скрипт для переустановки совпадает со скриптом группы — выберите другой в настройках мониторинга')
        if sid:
            sc = self.store.script(int(sid))
            if not sc or sc.get('status') != 'approved':
                raise ValueError('скрипт для переустановки удалён или не утверждён')
        srv = await self.bcloud_server(prof, bd, ip)
        if not srv or srv.get('status') != 'active':
            raise ValueError('сервера ноды нет в bcloud у покупателя группы — переустановить не на что')
        pconf = {**prof, 'cf_token': prof.get('cf_token') or self.store.get('cf_token')}
        await asyncio.to_thread(engine.remove_node, pconf, srv['ip'], dom, lambda t: None)
        if bd.get('_job'):
            self.store.patch_job_data(int(bd['_job']), removed=True, removed_at=int(time.time()))
        engine.forget_host(self.runner.known_hosts(), srv['ip'])
        cat = {'locations': [], 'os_code': srv.get('os') or 'debian12'}
        ids = await svc.create_jobs(self.store, self.runner, self.chat(), prof['id'], cat, srv['location'], None, '1m', [dom],
                                    squads=bd.get('squads'), host=bd.get('host'), servers=[srv], script_id=sid,
                                    extras={k: bd.get(k) for k in ('psiphon', 'warp') if bd.get(k)},
                                    note={'monitor': True})
        title = (self.store.script(int(sid)) or {}).get('title') if sid else 'Nuvrion Vision'
        return ids[0], f'переустанавливаю на том же сервере скриптом «{title}»'

    def buys_today(self, pid: int) -> list[float]:
        import json
        try:
            ts = json.loads(self.store.get(f'mon_buys:{pid}') or '[]')
        except ValueError:
            ts = []
        return [t for t in ts if t > time.time() - 86400]

    async def replace(self, prof: dict, st: dict, bd: dict, cfg: dict) -> tuple[int, str]:
        import json
        buys = self.buys_today(prof['id'])
        if len(buys) >= cfg['mon_max_buys']:
            raise ValueError(f'лимит покупок за сутки исчерпан ({cfg["mon_max_buys"]}) — поменяйте его в настройках мониторинга')
        cat = await svc.load_catalog(self.store, prof)
        loc = bd.get('location') or (st.get('country') or '').lower()
        L = next((x for x in cat['locations'] if x['code'] == loc), None)
        if not L:
            raise ValueError(f'локации «{loc or "?"}» сейчас нет в bcloud')
        idx, custom = bd.get('idx'), None
        if bd.get('custom'):
            custom = await svc.custom_offer(self.store, prof, cat, loc, bd['custom'])
            idx = None
        elif idx is None or not any(o['idx'] == idx for o in L['offers']):
            idx = min(L['offers'], key=lambda o: o['price_kop'])['idx']
        doms = await svc.auto_domains(self.store, prof, loc, 1)
        extras = {k: bd.get(k) for k in ('psiphon', 'warp', 'xhttp') if bd.get(k)}
        ids = await svc.create_jobs(self.store, self.runner, self.chat(), prof['id'], cat, loc, idx, '1m', doms,
                                    squads=bd.get('squads'), host=bd.get('host'), extras=extras, custom=custom,
                                    note={'monitor': True, 'replaces': st['addr']})
        self.store.set(f'mon_buys:{prof["id"]}', json.dumps(buys + [time.time()]))
        return ids[0], f'покупаю новую ноду {doms[0]} в {L["flag"]} {L["name"]}'

    async def finish_replace(self, prof: dict, st: dict, job: dict) -> None:
        """Новая нода готова: со старой — по настройке группы."""
        key = f'{prof["id"]}:{st["addr"]}'
        cfg = settings_of(prof)
        bd = svc.bot_jobs_by_ip(self.store, prof['id']).get(st['addr']) or {}
        ip = bd.get('ip') or (st['addr'] if engine.is_ip(st['addr']) else '')
        dom = bd.get('domain') or ('' if engine.is_ip(st['addr']) else st['addr'])
        what = 'старая нода оставлена как есть'
        if cfg['mon_old'] in ('panel', 'delete'):
            pconf = {**prof, 'cf_token': prof.get('cf_token') or self.store.get('cf_token')}
            try:
                await asyncio.to_thread(engine.remove_node, pconf, ip, dom, lambda t: None)
                what = 'старая нода убрана из панели'
            except engine.StepError as e:
                what = f'старую ноду убрать из панели не удалось: {e}'
            if bd.get('_job'):
                self.store.patch_job_data(int(bd['_job']), removed=True, removed_at=int(time.time()))
        if cfg['mon_old'] == 'delete':
            srv = await self.bcloud_server(prof, bd, ip)
            if srv:
                try:
                    await self.runner.bcloud_factory(self.store, bd.get('buyer') or svc.buyer_of(self.store, prof)).delete(srv['id'])
                    what += ', сервер удалён в bcloud'
                except BcloudError as e:
                    what += f', удалить сервер в bcloud не удалось: {e}'
            else:
                what += ', сервера в bcloud не нашёл'
        st.update(status='replaced', step=None, job=None, replaced_by=job['data'].get('domain'))
        self.store.set_mon(key, st)
        await self.say(f'✅ {self.head(prof, st)}\nзаменена новой нодой <code>{html.escape(job["data"].get("domain", ""))}</code>; '
                       f'{html.escape(what)}.')

    # ───────── для админки ─────────
    def view(self) -> dict:
        checkers = []
        for ch in self.store.checkers():
            checkers.append({**{k: ch.get(k) for k in ('id', 'name', 'ip', 'status', 'error', 'log',
                                                       'polled_at', 'poll_error', 'proxies', 'online', 'installed_at')},
                             'ssh': sshaccess.view(ch), 'access': sshaccess.describe(ch)})
        groups = []
        for p in self.store.profiles():
            cfg = settings_of(p)
            nodes = [{k: v for k, v in st.items() if k not in ('job_done',)} | {'key': key}
                     for key, st in sorted(self.store.mon_all(f'{p["id"]}:').items())]
            groups.append({'id': p['id'], 'name': p['name'], 'enabled': bool(p.get('mon_sub')),
                           **{k: v for k, v in cfg.items() if k != 'mon_sub'}, 'nodes': nodes,
                           'buys_today': len(self.buys_today(p['id']))})
        return {'checkers': checkers, 'groups': groups, 'polled_at': int(self.polled_at), 'every': int(self.every),
                'steps': STEP_LABEL, 'modes': MODES, 'old': OLD}


class _Sink:
    """Журнал установки чекера не нужен на диске: строки идут в поле log чекера."""
    def write(self, *_):
        pass

    def flush(self):
        pass

