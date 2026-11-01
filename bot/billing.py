"""Подписка BobrNet для приглашённых пользователей.

Как это работает:
- Администратор приглашает человека и выбирает срок бесплатного доступа (или бессрочно). Может поставить скидку
  или сделать доступ бесплатным навсегда — в карточке пользователя.
- Платный режим включается в «Тарифе»: цена за месяц, за сколько дней напоминать.
- За `remind_days` до конца срока — сообщение: сколько стоит, хватает ли денег на балансе Bobr Cloud.
- В день окончания — автопродление с баланса Bobr Cloud (если включено у пользователя и денег хватает).
- Не продлилось — пауза: ноды пользователя работают, но бот ими не управляет (нет панели, мониторинга, починки,
  уведомлений), пользователь видит только экран «Продлить». Пополнил и продлил — всё сразу возвращается.
Оплата списывается через Bobr Cloud (служебный API /charge) — с того же баланса, с которого покупаются серверы.
"""
from __future__ import annotations

import html
import json
import logging
import time

from .bcloud import BcloudError, NoMoney
from .runner import bcloud_from

log = logging.getLogger(__name__)

KEY = 'billing'
DAY = 86400
DEFAULTS = {'on': False, 'price_rub': 300, 'period_days': 30, 'remind_days': 3, 'trial_days': 14}
RETRY_EVERY = 6 * 3600   # на паузе с автопродлением — пробовать списать снова (вдруг пополнил)


def cfg(store) -> dict:
    try:
        raw = json.loads(store.get(KEY) or '{}')
    except ValueError:
        raw = {}
    out = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    out['on'] = bool(out['on'])
    for k in ('price_rub', 'period_days', 'remind_days', 'trial_days'):
        out[k] = int(out[k])
    return out


def clean(raw: dict, cur: dict) -> dict:
    out = dict(cur)
    if 'on' in raw:
        out['on'] = bool(raw['on'])
    for k, lo, hi, what in (('price_rub', 0, 100_000, 'Цена: от 0 до 100 000 ₽'), ('period_days', 1, 366, 'Период: от 1 до 366 дней'),
                            ('remind_days', 1, 30, 'Напоминать: за 1–30 дней'), ('trial_days', 0, 366, 'Бесплатно: 0–366 дней')):
        if k in raw:
            v = raw[k]
            if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                raise ValueError(what)
            out[k] = v
    return out


def price_kop(c: dict, u: dict) -> int:
    """Цена периода для пользователя с его скидкой, в копейках (округление до рубля)."""
    d = max(0, min(100, int(u.get('discount') or 0)))
    return int(round(c['price_rub'] * (100 - d) / 100)) * 100


def state(c: dict, u: dict, now: float | None = None) -> str:
    """free — без оплаты (платный режим выключен, бесплатно навсегда или срок не задан); active; paused."""
    now = now or time.time()
    if not c['on'] or u.get('free') or not u.get('access_until'):
        return 'free'
    return 'active' if u['access_until'] > now else 'paused'


def paused(store, uid: int, now: float | None = None) -> bool:
    u = store.user(int(uid or 0))
    return bool(u) and not u.get('blocked') and state(cfg(store), u, now) == 'paused'


def paused_profiles(store, now: float | None = None) -> set[int]:
    """Группы, которыми бот не занимается, пока владелец на паузе (группы, созданные этим пользователем)."""
    c = cfg(store)
    if not c['on']:
        return set()
    stopped = {u['tg_id'] for u in store.users() if not u.get('blocked') and state(c, u, now) == 'paused'}
    return {p['id'] for p in store.profiles() if str(p.get('created_by') or '').isdigit() and int(p['created_by']) in stopped}


def view(store, u: dict, now: float | None = None) -> dict:
    c = cfg(store)
    now = now or time.time()
    st = state(c, u, now)
    until = u.get('access_until')
    return {'state': st, 'on': c['on'], 'until': until, 'days_left': max(0, int((until - now) // DAY)) if until and until > now else 0,
            'price_kop': price_kop(c, u), 'base_kop': c['price_rub'] * 100, 'discount': int(u.get('discount') or 0),
            'free': bool(u.get('free')), 'autorenew': u.get('autorenew', True) is not False, 'period_days': c['period_days'],
            'payments': (u.get('payments') or [])[-20:][::-1]}


def set_access(store, uid: int, *, until: int | None | str = '-', add_days: int = 0, discount: int | None = None,
               free: bool | None = None, autorenew: bool | None = None) -> dict:
    """Правки администратора (срок, скидка, бесплатно) и пользователя (автопродление)."""
    u = store.user(int(uid))
    if not u:
        raise ValueError('Пользователь не найден')
    was = u.get('access_until')
    if until != '-':
        u['access_until'] = int(until) if until else None
    if add_days:
        if not -366 <= int(add_days) <= 3660:
            raise ValueError('Дней: до 3660')
        base = max(time.time(), u.get('access_until') or time.time())
        u['access_until'] = int(base + int(add_days) * DAY)
    if discount is not None:
        if not 0 <= int(discount) <= 100:
            raise ValueError('Скидка: от 0 до 100%')
        u['discount'] = int(discount)
    if free is not None:
        u['free'] = bool(free)
    if autorenew is not None:
        u['autorenew'] = bool(autorenew)
    if u.get('access_until') != was:  # новый срок — напомнить и сообщить о паузе заново, когда подойдёт
        for k in ('reminded', 'paused_told', 'tried_at'):
            u.pop(k, None)
    store.save_user(u)
    return u


class Billing:
    def __init__(self, store, notify, button=None):
        self.store, self.notify = store, notify
        self.button = button or (lambda: None)

    async def renew(self, uid: int, how: str = 'manual', now: float | None = None) -> dict:
        """Списать оплату периода с баланса Bobr Cloud и продлить доступ. NoMoney — не хватило денег."""
        now = now or time.time()
        c = cfg(self.store)
        u = self.store.user(int(uid))
        if not u or u.get('blocked'):
            raise ValueError('Пользователь не найден')
        amount = price_kop(c, u)
        start = max(now, u.get('access_until') or now)
        ref = f'bn:{uid}:{int(start) // 3600}'  # один период — одно списание, даже если кнопку нажали дважды
        if amount:
            r = await bcloud_from(self.store, uid).charge(amount, ref, f'BobrNet: подписка на {c["period_days"]} дн.')
            repeat = bool(r.get('repeat'))
        else:
            repeat = False
        u = self.store.user(int(uid))  # могли поменять, пока шло списание
        if repeat and (u.get('access_until') or 0) > start:
            return u  # уже продлено этим же списанием
        u['access_until'] = int(start + c['period_days'] * DAY)
        u['payments'] = ((u.get('payments') or []) + [{'at': int(now), 'kop': amount, 'days': c['period_days'], 'how': how, 'ref': ref}])[-50:]
        u.pop('reminded', None)
        u.pop('paused_told', None)
        self.store.save_user(u)
        return u

    async def tick(self, now: float | None = None) -> None:
        """Раз в час: напоминания, автопродление в день окончания, пауза."""
        c = cfg(self.store)
        if not c['on']:
            return
        now = now or time.time()
        for u in self.store.users():
            if u.get('blocked') or u.get('free') or not u.get('access_until'):
                continue
            try:
                await self.check_user(c, u, now)
            except Exception:  # noqa: BLE001 — один пользователь не должен остановить остальных
                log.exception('billing %s', u.get('tg_id'))

    async def check_user(self, c: dict, u: dict, now: float) -> None:
        uid, until = int(u['tg_id']), int(u['access_until'])
        amount = price_kop(c, u)
        auto = u.get('autorenew', True) is not False
        left = until - now
        if 0 < left <= c['remind_days'] * DAY and u.get('reminded') != until:
            u['reminded'] = until
            self.store.save_user(u)
            await self.remind(uid, c, u, amount, auto, until)
            return
        if left > 0:
            return
        # срок вышел: автопродление, иначе пауза (раз в RETRY_EVERY пробуем снова, если автопродление включено)
        if auto and now - float(u.get('tried_at') or 0) >= RETRY_EVERY:
            u['tried_at'] = now
            self.store.save_user(u)
            try:
                await self.renew(uid, 'auto', now)
                await self.say(uid, f'✅ <b>Подписка продлена</b> на {c["period_days"]} дн.: списано {amount // 100} ₽ с баланса Bobr Cloud. '
                                    f'Следующее продление — {self.date(self.store.user(uid)["access_until"])}.')
                return
            except NoMoney as e:
                why = f'на балансе Bobr Cloud {e.balance_kop // 100} ₽, нужно {amount // 100} ₽'
            except BcloudError as e:
                why = f'Bobr Cloud не ответил ({e}) — попробую ещё раз позже'
        elif auto:
            return
        else:
            why = 'автопродление выключено'
        if u.get('paused_told') != until:
            u = self.store.user(uid)
            u['paused_told'] = until
            self.store.save_user(u)
            await self.say(uid, f'⏸ <b>Доступ к BobrNet на паузе</b>: {html.escape(why)}.\n\n'
                                'Ваши ноды работают как прежде, но бот пока ими не управляет: нет мониторинга, починки и '
                                f'уведомлений. Продлите за {amount // 100} ₽ — всё вернётся сразу.', self.pay_kb(amount))

    async def remind(self, uid: int, c: dict, u: dict, amount: int, auto: bool, until: int) -> None:
        try:
            bal = await bcloud_from(self.store, uid).balance()
        except BcloudError:
            bal = None
        enough = bal is not None and bal >= amount
        text = (f'📅 <b>Доступ к BobrNet до {self.date(until)}</b>\n\n'
                f'Дальше бот работает по подписке: {amount // 100} ₽ за {c["period_days"]} дн.'
                + (f' (скидка {u["discount"]}%)' if u.get('discount') else '') + '. ')
        if auto:
            text += ('Спишу автоматически с баланса Bobr Cloud в день окончания — ' +
                     (f'на балансе {bal // 100} ₽, хватает.' if enough else
                      f'на балансе {bal // 100 if bal is not None else "?"} ₽ — пополните, иначе доступ встанет на паузу.'))
        else:
            text += 'Автопродление выключено — продлите вручную, иначе доступ встанет на паузу.'
        await self.say(uid, text, self.pay_kb(amount))

    def pay_kb(self, amount: int):
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        rows = [[InlineKeyboardButton(text=f'💳 Продлить за {amount // 100} ₽', callback_data='bill:pay')]]
        b = self.button()
        if b:
            rows += b.inline_keyboard
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def say(self, uid: int, text: str, markup=None) -> None:
        await self.notify.emit('billing.notice', text, None, markup, to=[uid])

    @staticmethod
    def date(ts: float) -> str:
        return time.strftime('%d.%m.%Y', time.localtime(ts))

    def stats(self, now: float | None = None) -> dict:
        c, now = cfg(self.store), now or time.time()
        users = [u for u in self.store.users() if not u.get('blocked')]
        month = now - 30 * DAY
        return {'users': len(users), 'active': sum(1 for u in users if state(c, u, now) == 'active'),
                'paused': sum(1 for u in users if state(c, u, now) == 'paused'),
                'free': sum(1 for u in users if state(c, u, now) == 'free'),
                'month_kop': sum(p['kop'] for u in users for p in (u.get('payments') or []) if p['at'] >= month),
                'soon': sum(1 for u in users if state(c, u, now) == 'active' and u['access_until'] - now <= c['remind_days'] * DAY)}
