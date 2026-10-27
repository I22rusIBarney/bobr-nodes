"""Клиент служебного API bcloud (/api/svc). Покупка идёт с баланса указанного админа."""
from __future__ import annotations

import httpx


class BcloudError(Exception):
    pass


class Bcloud:
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
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.request(method, self.base + path, headers=self.h, **kw)
        except httpx.HTTPError as e:
            raise BcloudError(f'bcloud недоступен: {e}') from e
        if r.status_code >= 300:
            try:
                detail = r.json().get('detail')
            except ValueError:
                detail = r.text[:200]
            hint = {401: ' — неверный токен', 404: ' — API выключен или неверный адрес (bcloud api-token)',
                    403: ' — доступ запрещён'}.get(r.status_code, '')
            raise BcloudError(f'bcloud {r.status_code}{hint}: {detail}')
        return r.json()

    async def resolve(self, q: str) -> dict:
        return await self._call('GET', '/users/resolve', params={'q': q})

    async def balance(self) -> int:
        return (await self._call('GET', '/balance', params={'tg_id': self.tg_id}))['balance_kop']

    async def catalog(self) -> dict:
        return await self._call('GET', '/catalog', params={'tg_id': self.tg_id})

    async def buy(self, location: str, os_: str, idx: int, period: str, password: str) -> dict:
        return await self._call('POST', '/servers', json={'tg_id': self.tg_id, 'location': location, 'os': os_,
                                                           'idx': idx, 'period': period, 'password': password})

    async def server(self, sid: int) -> dict:
        return await self._call('GET', f'/servers/{sid}', params={'tg_id': self.tg_id})

    async def rename(self, sid: int, name: str) -> None:
        await self._call('POST', f'/servers/{sid}/rename', json={'tg_id': self.tg_id, 'name': name})
