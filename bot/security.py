"""Безопасность сервера бота: входы по SSH и блокировки fail2ban (как в Bobr Cloud).

bnodes harden ставит на сервер маленький хук: он дописывает события в data/host-events.jsonl
(папка data общая у сервера и контейнера бота). Бот читает файл каждые 30 секунд и пишет админам:
о каждом входе по SSH — сразу (с пометкой «новый IP»), при массовом подборе пароля — отдельным предупреждением.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from .store import Store

log = logging.getLogger(__name__)

ATTACK_BANS, ATTACK_WINDOW = 20, 600   # 20+ блокировок за 10 минут — похоже на атаку


class Watch:
    def __init__(self, store: Store, bot, admins: set[int], data_dir: str | Path, every: float = 30):
        self.store, self.bot, self.admins = store, bot, sorted(admins)
        self.dir = Path(data_dir)
        self.every = every
        self.bans: list[float] = []
        self.logins: list[dict] = []
        self.attack_ts = 0.0

    @property
    def events_file(self) -> Path:
        return self.dir / 'host-events.jsonl'

    def hardened(self) -> dict | None:
        try:
            return json.loads((self.dir / '.hardened').read_text())
        except (OSError, ValueError):
            return None

    def _read_and_clear(self) -> list[dict]:
        if not self.events_file.exists():
            return []
        with open(self.events_file, 'r+', encoding='utf-8', errors='replace') as f:
            data = f.read()
            f.seek(0)
            f.truncate()
        out = []
        for line in data.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict):
                out.append(ev)
        return out

    def _known(self) -> list[str]:
        try:
            return json.loads(self.store.get('sec_known_ips') or '[]')
        except ValueError:
            return []

    async def _say(self, type_: str, text: str) -> None:
        from .notify import Notifier
        await Notifier(self.store, self.bot, admins=self.admins).emit(type_, text, to=list(self.admins))

    @staticmethod
    def _when(ts: float) -> str:
        return datetime.fromtimestamp(ts, timezone.utc).strftime('%d.%m %H:%M UTC')

    async def tick(self) -> None:
        try:
            evs = self._read_and_clear()
        except OSError:
            log.exception('host events')
            return
        now = time.time()
        for ev in evs:
            ts = float(ev.get('ts') or now)
            if ev.get('type') == 'login':
                user, ip = str(ev.get('user', '?'))[:32], str(ev.get('ip', '?'))[:64]
                known = self._known()
                new = ip not in known
                self.logins.append({'user': user, 'ip': ip, 'ts': ts, 'new': new})
                del self.logins[:-20]
                await self._say('security.login', '🔐 <b>Вход по SSH на сервер Bobr Nodes</b>' + (' · <b>новый IP</b>' if new else '') +
                                f'\n\n👤 {html.escape(user)}\n🌐 <code>{html.escape(ip)}</code>\n🕐 {self._when(ts)}' +
                                ('\n\nЕсли это были не вы — срочно смените пароль и SSH-ключи сервера: '
                                 'в базе бота root-пароли нод и токены панелей.' if new else ''))
                if new:
                    self.store.set('sec_known_ips', json.dumps([x for x in known if x != ip][-49:] + [ip]))
            elif ev.get('type') == 'ban':
                self.bans.append(ts)
        self.bans[:] = [t for t in self.bans if t > now - 24 * 3600]
        recent = sum(1 for t in self.bans if t > now - ATTACK_WINDOW)
        if recent >= ATTACK_BANS and now - self.attack_ts > 3600:
            self.attack_ts = now
            await self._say('security.brute', f'⚠️ <b>Подбор паролей к серверу Bobr Nodes</b>\n\nЗа 10 минут fail2ban заблокировал {recent} IP. '
                            'Защита работает, но если атака не стихает — проверьте, что вход по паролю отключён (bnodes security).')

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — наблюдатель не должен падать
                log.exception('security tick')
            await asyncio.sleep(self.every)

    def info(self) -> dict:
        now = time.time()
        return {'hardened': self.hardened(), 'bans_24h': sum(1 for t in self.bans if t > now - 24 * 3600),
                'logins': list(reversed(self.logins))[:10]}
