"""Клиент служебного API bcloud (/api/svc). Покупка идёт с баланса указанного админа."""
from __future__ import annotations

import asyncio
import time

import httpx


class BcloudError(Exception):
    pass


class BcloudUnavailable(BcloudError):
    """bcloud не ответил (перезапускается, сеть) — запрос можно повторить позже."""


class Bcloud:
    # сколько ждать bcloud, если он недоступен (например, bcloud update пересобирает контейнер)
    RETRY_FOR = 180.0

    def __init__(self, url: str, token: str, tg_id: int, timeout: float = 60):
        if not (url and token and tg_id):
            raise BcloudError('bcloud не настроен: нужны адрес, токен и аккаунт для покупок (панель → Подключения → bcloud)')
        self.base = url.rstrip('/')
        if not self.base.endswith('/api/svc'):
            self.base += '/api/svc'
        self.tg_id = int(tg_id)
        self.h = {'Authorization': f'Bearer {token}'}
        self.timeout = timeout

    async def _call(self, method: str, path: str, **kw):
        # Повторяем, только когда запрос точно не дошёл (нет соединения, имя не разрешилось) — это безопасно и для
        # покупки. Чтение (GET) повторяем при любой сетевой ошибке. Ответ bcloud с ошибкой не повторяем.
        end, wait = time.monotonic() + self.RETRY_FOR, 2.0
        while True:
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as c:
                    r = await c.request(method, self.base + path, headers=self.h, **kw)
                break
            except httpx.HTTPError as e:
                safe = method == 'GET' or isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout))
                if not safe or time.monotonic() + wait > end:
                    raise BcloudUnavailable(f'bcloud недоступен: {e}') from e
                await asyncio.sleep(wait)
                wait = min(wait * 2, 20.0)
        try:
            payload = r.json()
        except ValueError:
            payload = None
        if r.status_code < 300 and payload is None:
            raise BcloudError(f'bcloud ответил не JSON (HTTP {r.status_code}) — проверьте адрес bcloud')
        if r.status_code >= 300:
            try:
                detail = r.json().get('detail')
            except ValueError:
                detail = r.text[:200]
            hint = {401: ' — неверный токен', 404: ' — API выключен или неверный адрес (bcloud api-token)',
                    403: ' — доступ запрещён'}.get(r.status_code, '')
            raise BcloudError(f'bcloud {r.status_code}{hint}: {detail}')
        return payload

    async def resolve(self, q: str) -> dict:
        return await self._call('GET', '/users/resolve', params={'q': q})

    async def balance(self) -> int:
        return (await self._call('GET', '/balance', params={'tg_id': self.tg_id}))['balance_kop']

    async def catalog(self) -> dict:
        return await self._call('GET', '/catalog', params={'tg_id': self.tg_id})

    async def buy(self, location: str, os_: str, idx: int, period: str, password: str) -> dict:
        return await self._call('POST', '/servers', json={'tg_id': self.tg_id, 'location': location, 'os': os_,
                                                           'idx': idx, 'period': period, 'password': password})

    async def server(self, sid: int, sync: bool = False) -> dict:
        """sync — сначала обновить данные у провайдера (новый IP после смены в кабинете bcloud)."""
        params = {'tg_id': self.tg_id, **({'sync': 1} if sync else {})}
        return await self._call('GET', f'/servers/{sid}', params=params)

    async def change_ip(self, sid: int) -> dict:
        """Смена IP у провайдера с баланса владельца сервера. pending — итог придёт позже (проверять server(sync))."""
        return await self._call('POST', f'/servers/{sid}/change-ip', json={'tg_id': self.tg_id})

    async def rename(self, sid: int, name: str) -> None:
        await self._call('POST', f'/servers/{sid}/rename', json={'tg_id': self.tg_id, 'name': name})
