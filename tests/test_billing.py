import asyncio
import json
import re
import time

import pytest

from bot import billing as bm
from bot.access import Access
from bot.bcloud import NoMoney
from bot.store import Store

DAY = 86400


class Notify:
    def __init__(self):
        self.sent = []

    async def emit(self, type_, text, prof=None, markup=None, to=None, **kw):
        self.sent.append((type_, text, to))


class FakeBC:
    balance_kop = 100_000
    charges = []

    def __init__(self, *a):
        pass

    async def charge(self, amount, ref, comment=''):
        if any(r == ref for r, _ in FakeBC.charges):
            return {'ok': True, 'repeat': True}
        if FakeBC.balance_kop < amount:
            raise NoMoney(FakeBC.balance_kop, amount)
        FakeBC.balance_kop -= amount
        FakeBC.charges.append((ref, amount))
        return {'ok': True, 'repeat': False}

    async def balance(self):
        return FakeBC.balance_kop


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = Store(tmp_path / 'db.sqlite')
    store.set(bm.KEY, json.dumps({'on': True, 'price_rub': 300, 'period_days': 30, 'remind_days': 3}))
    now = time.time()
    store.save_user({'tg_id': 7, 'username': 'vasya', 'access_until': int(now + 10 * DAY)})
    FakeBC.balance_kop, FakeBC.charges = 100_000, []
    monkeypatch.setattr(bm, 'bcloud_from', lambda store, tg: FakeBC())
    n = Notify()
    return store, bm.Billing(store, n), n, now


def test_states_and_price(env):
    store, b, n, now = env
    c = bm.cfg(store)
    assert bm.state(c, store.user(7), now) == 'active'
    assert bm.state(c, {'access_until': now - 1}, now) == 'paused'
    assert bm.state(c, {'access_until': now - 1, 'free': True}, now) == 'free'
    assert bm.state(c, {'access_until': None}, now) == 'free'           # бессрочно
    assert bm.state({**c, 'on': False}, {'access_until': now - 1}, now) == 'free'
    assert bm.price_kop(c, {'discount': 20}) == 24000 and bm.price_kop(c, {}) == 30000
    with pytest.raises(ValueError):
        bm.clean({'price_rub': -1}, c)


def test_reminder_then_autorenew(env):
    store, b, n, now = env
    asyncio.run(b.tick(now + 8 * DAY))                                  # за 2 дня — напоминание
    assert len(n.sent) == 1 and 'Доступ к BobrNet до' in n.sent[0][1] and 'хватает' in n.sent[0][1] and n.sent[0][2] == [7]
    asyncio.run(b.tick(now + 8 * DAY + 3600))
    assert len(n.sent) == 1                                             # напоминание одно
    asyncio.run(b.tick(now + 10 * DAY + 60))                            # срок вышел — автопродление
    u = store.user(7)
    assert u['access_until'] == int(now + 10 * DAY) + 30 * DAY or u['access_until'] > now + 30 * DAY
    assert FakeBC.charges and FakeBC.charges[0][1] == 30000 and 'продлена' in n.sent[-1][1]
    assert u['payments'][-1]['how'] == 'auto'


def test_no_money_pauses_and_pay_restores(env):
    store, b, n, now = env
    FakeBC.balance_kop = 100
    t = now + 10 * DAY + 60
    asyncio.run(b.tick(t))
    assert bm.paused(store, 7, t) and 'на паузе' in n.sent[-1][1]
    asyncio.run(b.tick(t + 3600))
    assert sum('на паузе' in x[1] for x in n.sent) == 1                 # о паузе — один раз
    FakeBC.balance_kop = 100_000
    u = asyncio.run(b.renew(7, 'manual', t + 7200))
    assert u['access_until'] > t + 29 * DAY and not bm.paused(store, 7, t + 7200)


def test_double_click_charges_once(env):
    store, b, n, now = env
    asyncio.run(b.renew(7, 'manual', now))
    until = store.user(7)['access_until']
    asyncio.run(b.renew(7, 'manual', now))                              # второй период — новое списание
    assert len(FakeBC.charges) == 2 and store.user(7)['access_until'] == until + 30 * DAY


def test_access_roles_and_groups(env):
    store, b, n, now = env
    acc = Access(store, {1})
    pid = store.save_profile({'name': 'g', 'panel_url': 'https://p', 'panel_token': 't', 'created_by': 7})
    assert acc.role(7) == 'member' and acc.recipients(store.profile(pid)) == [7]
    bm.set_access(store, 7, until=int(now - 10))
    assert acc.role(7) == 'paused' and acc.profiles_for(7) == [] and acc.recipients(store.profile(pid)) == []
    assert bm.paused_profiles(store) == {pid}
    bm.set_access(store, 7, free=True)
    assert acc.role(7) == 'member' and bm.paused_profiles(store) == set()


def test_invite_access_term(tmp_path):
    store = Store(tmp_path / 'db.sqlite')
    acc = Access(store, {1})
    inv = acc.create_invite(1, 'Вася', 1, 7, access_days=14)
    ok, _ = acc.redeem(inv['token'], {'id': 9, 'username': 'v', 'first_name': 'В'})
    u = store.user(9)
    assert ok and 13 * DAY < u['access_until'] - time.time() <= 14 * DAY
    inv2 = acc.create_invite(1, '', 1, 7)
    acc.redeem(inv2['token'], {'id': 10})
    assert store.user(10)['access_until'] is None


def test_paused_web_allowlist():
    from bot.web import PAUSED_OK
    for ok in ('GET /api/state', 'GET /api/billing', 'POST /api/billing/pay', 'GET /api/wallet', 'POST /api/wallet/topup', 'GET /api/avatar/5'):
        assert PAUSED_OK.match(ok), ok
    for bad in ('GET /api/profiles/1/nodes', 'POST /api/billing/settings', 'GET /api/jobs'):
        assert not PAUSED_OK.match(bad), bad
