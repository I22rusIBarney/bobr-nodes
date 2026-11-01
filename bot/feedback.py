"""Отзывы и ошибки: чтобы о проблеме узнавал администратор, а пользователь знал, что сообщать не нужно.

- Задача упала — отчёт уходит администраторам сам (ошибка, этап, хвост журнала); пользователь видит в сообщении
  задачи, что отчёт уже отправлен, и может добавить подробности.
- Установка прошла — через 15 минут опрос «всё работает?»; «есть проблема» — тоже отчёт, с комментарием.
- «Сообщить о проблеме» — отчёт своими словами из панели управления.
- Администратор видит всё в «Отзывах и ошибках»: статистика установок и опросов, отчёты, ответ пользователю.
"""
from __future__ import annotations

import html
import logging
import time
from pathlib import Path

from .notify import Notifier
from .runner import SERVICE_KINDS

log = logging.getLogger(__name__)

SURVEY_DELAY = 15 * 60          # через сколько после установки спросить, всё ли работает
SURVEY_KINDS = ('install', 'upgrade', 'reip')
KIND_NAME = {'install': 'Установка ноды', 'upgrade': 'Обновление ноды', 'reip': 'Смена IP', 'node': 'Дополнения на ноде'}


def job_kind(d: dict) -> str:
    k = d.get('kind')
    return 'install' if k not in SERVICE_KINDS else k


class Feedback:
    def __init__(self, store, bot, access=None, logdir: str | Path = 'data/logs', button=None):
        self.store, self.bot, self.access = store, bot, access
        self.logdir = Path(logdir)
        self.notify = Notifier(store, bot, access)
        self.button = button or (lambda: None)

    # ───────── кто есть кто ─────────
    def admins(self) -> list[int]:
        return sorted(self.access.admins) if self.access is not None else []

    def is_admin(self, uid: int) -> bool:
        return int(uid or 0) in set(self.admins())

    def who(self, uid: int) -> str:
        u = self.store.user(int(uid)) if uid else None
        if u and u.get('username'):
            return '@' + u['username']
        if u and u.get('first_name'):
            return u['first_name']
        return 'администратор' if self.is_admin(uid) else f'id {uid}'

    def log_tail(self, jid: int, n: int = 40) -> str:
        try:
            lines = (self.logdir / f'job-{jid}.log').read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            return ''
        return '\n'.join(lines[-n:])[-6000:]

    async def tell_admins(self, uid: int, text: str, prof: dict | None = None) -> None:
        """Администраторам — кроме самого автора, если он администратор (свою ошибку он и так видит)."""
        to = [a for a in self.admins() if a != int(uid or 0)]
        if to:
            await self.notify.emit('report.new', text, prof, self.button(), to=to)

    # ───────── задача упала ─────────
    async def job_failed(self, jid: int) -> int | None:
        j = self.store.job(jid)
        if not j:
            return None
        d, uid = j['data'], int(j['chat_id'] or 0)
        prof = self.store.profile(j['profile_id']) or {}
        info = {'error': (j.get('error') or '')[:1500], 'stage': j.get('stage') or '', 'domain': d.get('domain', ''),
                'kind': job_kind(d), 'group': prof.get('name', ''), 'log': self.log_tail(jid)}
        r = self.store.report_for_job(jid)
        if r and r['status'] == 'open':  # повтор той же задачи упал снова — тот же отчёт, без нового сообщения
            info['count'] = int(r['data'].get('count') or 1) + 1
            self.store.update_report(r['id'], data={**r['data'], **info})
            rid, fresh = r['id'], False
        else:
            rid, fresh = self.store.add_report(uid, 'auto', j['profile_id'], jid, data={**info, 'count': 1}), True
        self.store.patch_job_data(jid, report=rid, report_to_admin=not self.is_admin(uid))
        if fresh:
            await self.tell_admins(uid, f'🐞 <b>Ошибка у {html.escape(self.who(uid))}</b> · {html.escape(prof.get("name", ""))}\n'
                                        f'{KIND_NAME.get(info["kind"], "Задача")} <code>{html.escape(info["domain"])}</code>, задача #{jid}, '
                                        f'этап «{html.escape(info["stage"] or "?")}»\n\n<code>{html.escape(info["error"][:400])}</code>\n\n'
                                        f'Отчёт #{rid} — в «Отзывах и ошибках».', prof)
        return rid

    # ───────── установка прошла: опрос позже ─────────
    def job_done(self, jid: int, now: float | None = None) -> None:
        j = self.store.job(jid)
        if not j or job_kind(j['data']) not in SURVEY_KINDS or j['data'].get('monitor') or j['data'].get('autoscale'):
            return  # починка и автодокупка — не просьба пользователя, его не спрашиваем
        due = self.pending()
        due[str(jid)] = (now or time.time()) + SURVEY_DELAY
        self.store.set('fb:pending', __import__('json').dumps(due))

    def pending(self) -> dict:
        import json
        try:
            v = json.loads(self.store.get('fb:pending') or '{}')
        except ValueError:
            v = {}
        return v if isinstance(v, dict) else {}

    async def surveys(self, now: float | None = None) -> int:
        """Пора спросить: одна анкета на партию (несколько нод одной установки — одно сообщение)."""
        import json
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        now = now or time.time()
        due = self.pending()
        ready = [int(k) for k, t in due.items() if t <= now]
        if not ready:
            return 0
        batches: dict[tuple, list[dict]] = {}
        for jid in ready:
            due.pop(str(jid), None)
            j = self.store.job(jid)
            if j and j['status'] == 'done' and not j['data'].get('survey'):
                batches.setdefault((j['chat_id'], j['batch'], job_kind(j['data'])), []).append(j)
        self.store.set('fb:pending', json.dumps(due))
        sent = 0
        for (uid, _, kind), js in batches.items():
            first = js[0]
            self.store.set(f'fb:batch:{first["id"]}', json.dumps([x['id'] for x in js]))
            names = ', '.join(f'<code>{html.escape(x["data"].get("domain", ""))}</code>' for x in js[:8])
            what = {'install': 'установлены' if len(js) > 1 else 'установлена', 'upgrade': 'обновлены' if len(js) > 1 else 'обновлена',
                    'reip': 'с новым IP'}[kind]
            text = (f'🧪 <b>Всё работает?</b>\n{"Ноды" if len(js) > 1 else "Нода"} {names} {what} 15 минут назад. '
                    'Подключитесь через VPN и проверьте — ответ поможет быстрее находить ошибки.')
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text='👍 Всё работает', callback_data=f'fb:y:{first["id"]}'),
                InlineKeyboardButton(text='👎 Есть проблема', callback_data=f'fb:n:{first["id"]}')]])
            await self.notify.emit('survey.ask', text, self.store.profile(first['profile_id']), kb, to=[int(uid)])
            sent += 1
        return sent

    async def answer(self, uid: int, jid: int, ok: bool) -> tuple[str, int | None]:
        """Ответ на опрос. Плохо — отчёт администраторам; вернём его номер, чтобы попросить подробности."""
        import json
        try:
            jids = json.loads(self.store.get(f'fb:batch:{jid}') or '[]') or [jid]
        except ValueError:
            jids = [jid]
        js = [j for j in (self.store.job(x) for x in jids) if j]
        if not js:
            return 'Задача не найдена', None
        if any(j['data'].get('survey') for j in js):
            return 'Ответ уже записан, спасибо!', None
        for j in js:
            self.store.patch_job_data(j['id'], survey='ok' if ok else 'bad')
        first = js[0]
        prof = self.store.profile(first['profile_id']) or {}
        doms = [j['data'].get('domain', '') for j in js]
        rid = self.store.add_report(uid, 'survey', first['profile_id'], first['id'], rating=1 if ok else -1,
                                    status='done' if ok else 'open',
                                    data={'jobs': [j['id'] for j in js], 'domains': doms, 'kind': job_kind(first['data']),
                                          'group': prof.get('name', '')})
        if ok:
            return 'Спасибо! Рады, что всё работает 🙌', None
        await self.tell_admins(uid, f'👎 <b>{html.escape(self.who(uid))}: не работает</b> · {html.escape(prof.get("name", ""))}\n'
                                    + ', '.join(f'<code>{html.escape(d)}</code>' for d in doms[:8]) + f'\n\nОтчёт #{rid}.', prof)
        return 'Отчёт отправлен администратору — писать ему отдельно не нужно.', rid

    async def comment(self, uid: int, rid: int, text: str) -> str:
        r = self.store.report(rid)
        if not r or (int(r['uid']) != int(uid) and not self.is_admin(uid)):
            return 'Отчёт не найден'
        text = text.strip()[:2000]
        if not text:
            return 'Пустой комментарий'
        stamp = time.strftime('%d.%m %H:%M')
        self.store.update_report(rid, text=(r['text'] + '\n\n' if r['text'] else '') + f'[{stamp}] {text}', status='open')
        prof = self.store.profile(r['pid']) if r['pid'] else None
        await self.tell_admins(uid, f'💬 <b>Подробности к отчёту #{rid}</b> от {html.escape(self.who(uid))}\n\n{html.escape(text[:800])}', prof)
        return 'Добавлено к отчёту — администратор увидит.'

    async def manual(self, uid: int, text: str, pid: int | None = None) -> int:
        text = text.strip()[:2000]
        if len(text) < 5:
            raise ValueError('Опишите проблему хотя бы парой слов')
        prof = self.store.profile(pid) if pid else None
        rid = self.store.add_report(uid, 'user', pid, None, text=f'[{time.strftime("%d.%m %H:%M")}] {text}',
                                    data={'group': (prof or {}).get('name', '')})
        await self.tell_admins(uid, f'📝 <b>{html.escape(self.who(uid))} сообщает о проблеме</b>'
                                    + (f' · {html.escape(prof["name"])}' if prof else '') + f'\n\n{html.escape(text[:800])}\n\nОтчёт #{rid}.', prof)
        return rid

    async def resolve(self, rid: int, reply: str = '', done: bool = True) -> dict:
        r = self.store.report(rid)
        if not r:
            raise ValueError('Отчёт не найден')
        reply = reply.strip()[:2000]
        data = dict(r['data'])
        if reply:
            data.setdefault('replies', []).append({'at': int(time.time()), 'text': reply})
        self.store.update_report(rid, status='done' if done else 'open', data=data)
        if (reply or done) and not self.is_admin(r['uid']):
            msg = f'✅ <b>Отчёт #{rid}</b>: ' + ('проблема решена.' if done else 'администратор ответил.')
            if reply:
                msg += f'\n\n{html.escape(reply)}'
            await self.notify.emit('report.reply', msg, self.store.profile(r['pid']) if r['pid'] else None, to=[int(r['uid'])])
        return self.store.report(rid)

    # ───────── статистика ─────────
    def stats(self, days: int = 30, now: float | None = None) -> dict:
        since = (now or time.time()) - days * 86400
        by: dict[str, dict] = {}
        for j in self.store.jobs(5000, include_hidden=True):
            if (j.get('created_at') or 0) < since or j['status'] not in ('done', 'failed'):
                continue
            k = job_kind(j['data'])
            c = by.setdefault(k, {'kind': k, 'title': KIND_NAME.get(k, k), 'done': 0, 'failed': 0, 'good': 0, 'bad': 0})
            c[j['status']] += 1
            if j['data'].get('survey') == 'ok':
                c['good'] += 1
            elif j['data'].get('survey') == 'bad':
                c['bad'] += 1
        surveys = [r for r in self.store.reports(kind='survey', limit=1000) if r['ts'] >= since]
        good = sum(1 for r in surveys if r['rating'] == 1)
        return {'days': days, 'kinds': sorted(by.values(), key=lambda x: -(x['done'] + x['failed'])),
                'done': sum(x['done'] for x in by.values()), 'failed': sum(x['failed'] for x in by.values()),
                'surveys': len(surveys), 'good': good, 'bad': len(surveys) - good,
                'open': len(self.store.reports(status='open', limit=1000))}
