"""Очередь задач: покупка в bcloud → ожидание IP → установка ноды. Каждая задача — одно сообщение,
которое обновляется по мере прохождения этапов. Незавершённые задачи продолжаются после перезапуска бота."""
from __future__ import annotations

import asyncio
import html
import logging
import time
from pathlib import Path

from . import engine
from .bcloud import Bcloud, BcloudError
from .store import Store

log = logging.getLogger(__name__)

STAGES = [('buy', 'Покупка в bcloud'), ('ip', 'Создание сервера'), ('dns', 'DNS в Cloudflare'),
          ('ssh', 'Вход по SSH'), ('install', 'Установка ноды'), ('panel', 'Регистрация в панели'),
          ('check', 'Проверка')]
STAGE_ORDER = [s for s, _ in STAGES]
STATUS_ICON = {'queued': '🕓', 'buying': '⏳', 'provisioning': '⏳', 'deploying': '⏳', 'done': '✅', 'failed': '❌'}


class Fail(Exception):
    pass


def bcloud_from(store: Store) -> Bcloud:
    return Bcloud(store.get('bcloud_url'), store.get('bcloud_token'), int(store.get('bcloud_tg_id') or 0))


def job_text(job: dict, profile_name: str = '', last: str = '') -> str:
    d = job['data']
    icon = STATUS_ICON.get(job['status'], '•')
    head = f'{icon} <b>{html.escape(d["domain"])}</b>  #{job["id"]}\n{d.get("flag", "")} {html.escape(d.get("loc_name", ""))} · {html.escape(d.get("spec", ""))}'
    if profile_name:
        head += f'\n🗂 {html.escape(profile_name)}'
    lines = [head, '']
    cur = job['stage']
    cur_i = STAGE_ORDER.index(cur) if cur in STAGE_ORDER else -1
    skip = set(d.get('skipped_stages') or [])
    for i, (code, label) in enumerate(STAGES):
        if code in skip:
            continue
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
    if job['status'] == 'failed' and job['error']:
        lines += ['', f'<b>Ошибка:</b> {html.escape(job["error"][:600])}']
    elif job['status'] in ('provisioning', 'deploying', 'buying') and last:
        lines += ['', f'<i>{html.escape(last[:160])}</i>']
    elif job['status'] == 'done':
        lines += ['', f'Root-пароль: <tg-spoiler><code>{html.escape(d.get("password", ""))}</code></tg-spoiler>']
    return '\n'.join(lines)


class Runner:
    def __init__(self, store: Store, bot, logdir: str | Path = 'data/logs', *, deploy=engine.deploy,
                 bcloud_factory=bcloud_from, poll_every: float = 10, ip_timeout: float = 20 * 60, edit_every: float = 3):
        self.store = store
        self.bot = bot
        self.logdir = Path(logdir)
        self.logdir.mkdir(parents=True, exist_ok=True)
        self.deploy = deploy
        self.bcloud_factory = bcloud_factory
        self.poll_every = poll_every
        self.ip_timeout = ip_timeout
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

    def log_path(self, jid: int) -> Path:
        return self.logdir / f'job-{jid}.log'

    # ── запуск ──
    def start(self, jid: int) -> None:
        t = self.tasks.get(jid)
        if t and not t.done():
            return
        self.tasks[jid] = asyncio.create_task(self._run(jid))

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
        if not job:
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

    async def _run(self, jid: int) -> None:
        job = self.store.job(jid)
        if not job:
            return
        d = job['data']
        loop = asyncio.get_running_loop()
        try:
            prof = self.store.profile(job['profile_id'])
            if not prof:
                raise Fail('профиль панели удалён')
            bc = self.bcloud_factory(self.store)

            if not d.get('server_id'):
                if job['status'] == 'buying':
                    raise Fail('бот перезапустился во время покупки — проверьте «Мои серверы» в bcloud. '
                               'Повтор купит новый сервер.')
                await self._set(jid, status='buying', stage='buy', error='')
                srv = await bc.buy(d['location'], d['os'], d['idx'], d.get('period', '1m'), d['password'])
                d = self.store.patch_job_data(jid, server_id=srv['id'])

            await self._set(jid, status='provisioning', stage='ip', error='')
            self.last[jid] = 'ждём, пока bcloud создаст сервер'
            end = time.monotonic() + self.ip_timeout
            while True:
                srv = await bc.server(d['server_id'])
                if srv['status'] == 'active' and srv.get('ip'):
                    d = self.store.patch_job_data(jid, ip=srv['ip'], password=srv.get('root_password') or d['password'])
                    break
                if srv['status'] in ('deleted', 'failed', 'error'):
                    raise Fail(f'bcloud не смог создать сервер (статус {srv["status"]}); деньги bcloud возвращает сам')
                if time.monotonic() > end:
                    raise Fail('сервер слишком долго создаётся — проверьте его в bcloud и нажмите «Повторить»')
                await asyncio.sleep(self.poll_every)
            try:
                await bc.rename(d['server_id'], d['name'])
            except BcloudError as e:
                log.info('rename %s: %s', jid, e)

            self.last[jid] = 'ждём свободный слот установки'
            await self.render(jid)
            async with self.sem():
                await self._set(jid, status='deploying', stage='dns')
                node = {'name': d['name'], 'ip': d['ip'], 'domain': d['domain'], 'country': d.get('country', ''),
                        'remark': d.get('remark', ''), 'ssh_user': 'root', 'ssh_password': d['password'],
                        'traffic_control': prof.get('traffic_control', True)}
                pconf = {**prof, 'cf_token': prof.get('cf_token') or self.store.get('cf_token'),
                         'ssh_pubkey': self.store.get('ssh_pubkey')}

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
                    await asyncio.to_thread(self.deploy, node, pconf, lf, on_log, on_stage)
            await self._set(jid, status='done', error='')
        except (Fail, engine.StepError, BcloudError) as e:
            await self._set(jid, status='failed', error=str(e))
        except Exception as e:  # noqa: BLE001
            log.exception('job %s', jid)
            await self._set(jid, status='failed', error=f'{type(e).__name__}: {e}')
