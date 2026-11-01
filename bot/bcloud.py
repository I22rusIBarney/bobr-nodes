"""Клиент служебного API Bobr Cloud (/api/svc). Покупка идёт с баланса указанного админа."""
from __future__ import annotations

import asyncio
import time

import httpx


class BcloudError(Exception):
    pass


class NoMoney(BcloudError):
    """Не хватает денег на балансе: balance_kop — сколько есть, need_kop — сколько нужно."""

    def __init__(self, balance_kop: int, need_kop: int):
        super().__init__(f'на балансе Bobr Cloud {balance_kop // 100} ₽, нужно {need_kop // 100} ₽')
        self.balance_kop, self.need_kop = balance_kop, need_kop


class BcloudUnavailable(BcloudError):
    """Bobr Cloud не ответил (перезапускается, сеть) — запрос можно повторить позже."""


class Bcloud:
    # сколько ждать bcloud, если он недоступен (например, bcloud update пересобирает контейнер)
    RETRY_FOR = 180.0

    def __init__(self, url: str, token: str, tg_id: int, timeout: float = 60):
        if not (url and token and tg_id):
            raise BcloudError('Bobr Cloud не настроен: нужны адрес, токен и аккаунт для покупок (панель → Подключения → Bobr Cloud)')
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
                    raise BcloudUnavailable(f'Bobr Cloud недоступен: {e}') from e
                await asyncio.sleep(wait)
                wait = min(wait * 2, 20.0)
        try:
            payload = r.json()
        except ValueError:
            payload = None
        if r.status_code < 300 and payload is None:
            raise BcloudError(f'Bobr Cloud ответил не JSON (HTTP {r.status_code}) — проверьте адрес Bobr Cloud')
        if r.status_code >= 300:
            try:
                detail = r.json().get('detail')
            except ValueError:
                detail = r.text[:200]
            if r.status_code == 402 and isinstance(detail, dict):
                raise NoMoney(int(detail.get('balance_kop') or 0), int(detail.get('need_kop') or 0))
            hint = {401: ' — неверный токен', 404: ' — API выключен или неверный адрес (bcloud api-token)',
                    403: ' — доступ запрещён'}.get(r.status_code, '')
            raise BcloudError(f'Bobr Cloud {r.status_code}{hint}: {detail}')
        return payload

    async def resolve(self, q: str) -> dict:
        return await self._call('GET', '/users/resolve', params={'q': q})

    async def balance(self) -> int:
        return (await self._call('GET', '/balance', params={'tg_id': self.tg_id}))['balance_kop']

    async def catalog(self) -> dict:
        return await self._call('GET', '/catalog', params={'tg_id': self.tg_id})

    async def buy(self, location: str, os_: str, idx: int | None, period: str, password: str, spec: dict | None = None) -> dict:
        """Готовый тариф (idx) или своя конфигурация из конструктора (idx=None, spec={cpu, ram, disk})."""
        body = {'tg_id': self.tg_id, 'location': location, 'os': os_, 'idx': idx, 'period': period, 'password': password}
        if idx is None:
            if not spec:
                raise BcloudError('не указана конфигурация сервера')
            body.update(idx=None, cpu=int(spec['cpu']), ram=int(spec['ram']), disk=int(spec['disk']))
        return await self._call('POST', '/servers', json=body)

    async def quote(self, location: str, cpu: int, ram: int, disk: int) -> dict:
        """Цена своей конфигурации за месяц: {price_kop, speed, cpu, ram, disk}."""
        try:
            return await self._call('GET', '/quote', params={'tg_id': self.tg_id, 'location': location,
                                                             'cpu': int(cpu), 'ram': int(ram), 'disk': int(disk)})
        except BcloudError as e:
            if str(e).startswith('Bobr Cloud 404') and str(e).endswith(': Not Found'):  # старый bcloud: конструктора в API нет
                raise BcloudError('Bobr Cloud не считает цену своей конфигурации — обновите его: bcloud update') from None
            raise

    async def servers(self) -> list[dict]:
        """Все серверы аккаунта в Bobr Cloud (кроме удалённых), где бы они ни были куплены: IP, root-пароль, локация."""
        try:
            return (await self._call('GET', '/servers', params={'tg_id': self.tg_id}))['servers']
        except BcloudError as e:
            if str(e).startswith('Bobr Cloud 405'):  # старый bcloud: списка серверов в служебном API ещё нет
                raise BcloudError('Bobr Cloud не отдаёт список серверов — обновите его: bcloud update') from None
            raise

    async def server(self, sid: int, sync: bool = False) -> dict:
        """sync — сначала обновить данные у провайдера (новый IP после смены в кабинете Bobr Cloud)."""
        params = {'tg_id': self.tg_id, **({'sync': 1} if sync else {})}
        return await self._call('GET', f'/servers/{sid}', params=params)

    async def change_ip(self, sid: int) -> dict:
        """Смена IP у провайдера с баланса владельца сервера. pending — итог придёт позже (проверять server(sync))."""
        return await self._call('POST', f'/servers/{sid}/change-ip', json={'tg_id': self.tg_id})

    async def rename(self, sid: int, name: str) -> None:
        await self._call('POST', f'/servers/{sid}/rename', json={'tg_id': self.tg_id, 'name': name})

    async def delete(self, sid: int) -> dict:
        """Удалить сервер владельца в Bobr Cloud (возврат остатка — по настройкам Bobr Cloud)."""
        return await self._call('POST', f'/servers/{sid}/delete', json={'tg_id': self.tg_id})

    async def renew(self, sid: int) -> dict:
        """Продлить сервер владельца на 30 дней с его баланса."""
        return await self._new_api(self._call('POST', f'/servers/{sid}/renew', json={'tg_id': self.tg_id}))

    async def set_autorenew(self, sid: int, value: bool) -> dict:
        return await self._new_api(self._call('POST', f'/servers/{sid}/autorenew', json={'tg_id': self.tg_id, 'value': bool(value)}))

    async def topup_options(self) -> dict:
        """Баланс, пределы суммы, способы оплаты и привязанные карты/счета СБП клиента."""
        return await self._new_api(self._call('GET', '/topup', params={'tg_id': self.tg_id}))

    async def topup(self, amount_rub: int, provider: str | None = None, method_id: int | None = None) -> dict:
        """Пополнение: {'url'} — оплатить на странице банка, {'status'} — списано привязанным способом."""
        return await self._new_api(self._call('POST', '/topup', json={'tg_id': self.tg_id, 'amount_rub': int(amount_rub),
                                                                       'provider': provider, 'method_id': method_id}))

    async def charge(self, amount_kop: int, ref: str, comment: str = '') -> dict:
        """Списать с баланса клиента оплату (подписка BobrNet). Повтор с тем же ref не спишет второй раз.
        Не хватает денег — NoMoney. Списание не повторяем при обрыве: повторит следующая попытка с тем же ref."""
        self.RETRY_FOR = 0
        return await self._new_api(self._call('POST', '/charge', json={'tg_id': self.tg_id, 'amount_kop': int(amount_kop),
                                                                        'ref': ref, 'comment': comment}))

    @staticmethod
    async def _new_api(coro):
        try:
            return await coro
        except BcloudError as e:
            if str(e).startswith(('Bobr Cloud 404', 'Bobr Cloud 405')) and str(e).endswith(('Not Found', 'Method Not Allowed')):
                raise BcloudError('Bobr Cloud не умеет этого через API — обновите его: bcloud update') from None
            raise
