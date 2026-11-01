import asyncio
import time

import pytest

from bot import autopilot as ap
from bot.store import Store


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, uid, text, reply_markup=None, **kw):
        self.sent.append((uid, text, reply_markup))


class Access:
    admins = {1}

    def recipients(self, prof):
        return [1]


def srv(price, days, auto=True, name='s'):
    return {'price_kop': price, 'days_left': days, 'auto_renew': auto, 'domain': name}


def test_forecast_walks_renewals():
    f = ap.forecast([srv(30000, 5, name='a'), srv(20000, 10, name='b')], 60000)
    # день 5: −300 → 300; день 10: −200 → 100; день 35: на «a» нужно 300, есть 100
    assert f['days'] == 35 and f['server'] == 'a' and f['need_kop'] == 20000 and f['monthly_kop'] == 50000
    assert ap.forecast([srv(30000, 5, auto=False)], 0)['days'] is None   # без автопродления — не тратит
    assert ap.forecast([srv(100, 1)], 10**9)['days'] is None


def test_clean_auto_new_fields():
    a = ap.clean_auto({'scale': 'ask', 'scale_max': 3, 'scale_floor': 1000, 'bal_days': 14}, {})
    assert (a['scale'], a['scale_max'], a['scale_floor'], a['bal_days']) == ('ask', 3, 1000, 14)
    assert ap.auto_cfg({})['scale'] == 'off' and ap.auto_cfg({})['bal_days'] == 7
    for bad in ({'scale': 'yes'}, {'scale_max': 0}, {'scale_floor': -1}, {'bal_days': 100}, {'scale_max': True}):
        with pytest.raises(ValueError):
            ap.clean_auto(bad, {})


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'g', 'panel_url': 'https://p', 'panel_token': 't', 'base_domain': 'ex.com',
                              'auto': {'scale': 'ask', 'scale_max': 1, 'scale_floor': 500}})
    bot = Bot()
    pilot = ap.Autopilot(store, None, bot, Access())
    cat = {'locations': [{'code': 'de', 'name': 'Германия', 'flag': '🇩🇪',
                          'offers': [{'idx': 0, 'cpu': 1, 'ram': 1, 'disk': 10, 'price_kop': 20000},
                                     {'idx': 1, 'cpu': 2, 'ram': 2, 'disk': 20, 'price_kop': 40000}]}],
           'periods': [{'code': '1m', 'months': 1, 'pct': 0}]}

    async def load_catalog(store, prof):
        return cat

    class BC:
        bal = 100000

        async def balance(self):
            return BC.bal
    created = []

    async def create_jobs(store, runner, chat, pid, cat, loc, idx, period, doms, **kw):
        jid = store.add_job('b', pid, chat, {'location': loc, 'idx': idx, 'domain': doms[0], **(kw.get('note') or {})})
        created.append((loc, idx, doms, kw))
        return [jid]

    async def auto_domains(store, prof, loc, n, *a, **k):
        return [f'de-{len(created) + 1}.ex.com']
    monkeypatch.setattr(ap.svc, 'load_catalog', load_catalog)
    monkeypatch.setattr(ap.svc, 'create_jobs', create_jobs)
    monkeypatch.setattr(ap.svc, 'auto_domains', auto_domains)
    monkeypatch.setattr(ap, 'bcloud_from', lambda store, tg: BC())
    return store, store.profile(pid), pilot, bot, BC, created


NODE = {'uuid': 'u1', 'address': 'hot.ex.com', 'name': 'hot'}
BD = {'location': 'de', 'idx': 1, 'squads': ['sq'], 'host': {'mode': 'simple'}, 'warp': {'domains': ['x']}}


def test_plan_same_offer_and_reasons(env):
    store, prof, pilot, bot, BC, created = env
    plan = asyncio.run(pilot.plan_scale(prof, NODE, BD))
    assert plan['idx'] == 1 and plan['price'] == 40000
    assert 'не через бота' in asyncio.run(pilot.plan_scale(prof, NODE, {}))
    BC.bal = 40000 + 49900                                   # после покупки останется 499 ₽ < 500 ₽
    assert 'порога' in asyncio.run(pilot.plan_scale(prof, NODE, BD))


def test_ask_then_confirm_buys_once_and_respects_limit(env):
    store, prof, pilot, bot, BC, created = env
    extra, markup = asyncio.run(pilot.scale_offer(prof, NODE, BD))
    assert 'Могу докупить' in extra and markup.inline_keyboard[0][0].callback_data.startswith('ap:s:')
    tok = markup.inline_keyboard[0][0].callback_data[5:]
    assert pilot.scale_owner(tok) == prof['id']
    j0 = store.add_job('b0', prof['id'], 1, {'domain': 'hot.ex.com', 'ip': '1.2.3.4', **BD})   # перегруженная нода — от бота
    store.update_job(j0, status='done')
    msg = asyncio.run(pilot.scale_confirm(tok))
    assert msg.startswith('Покупаю') and created[0][:2] == ('de', 1)
    assert created[0][3]['squads'] == ['sq'] and created[0][3]['extras'] == {'warp': {'domains': ['x']}}
    assert any('Докупаю ноду' in t for _, t, _ in bot.sent)
    assert 'устарело' in asyncio.run(pilot.scale_confirm(tok))   # кнопка одноразовая
    other = {**NODE, 'uuid': 'u2'}
    assert 'лимит' in asyncio.run(pilot.plan_scale(prof, other, BD))   # scale_max = 1
    assert 'уже докупал' in asyncio.run(pilot.plan_scale(prof, NODE, BD))


def test_auto_mode_buys_and_off_mode_silent(env):
    store, prof, pilot, bot, BC, created = env
    store.save_profile({**prof, 'auto': {**prof['auto'], 'scale': 'auto'}})
    extra, markup = asyncio.run(pilot.scale_offer(store.profile(prof['id']), NODE, BD))
    assert 'Докупаю ещё одну' in extra and markup is None and len(created) == 1
    store.save_profile({**prof, 'auto': {**prof['auto'], 'scale': 'off'}})
    assert asyncio.run(pilot.scale_offer(store.profile(prof['id']), NODE, BD)) == ('', None)


def test_balance_warning_once_a_day_per_account(env):
    store, prof, pilot, bot, BC, created = env
    r = {'servers': [srv(30000, 4, name='a')], 'balance_kop': 10000}
    now = time.time()
    asyncio.run(pilot.balance_warn(1, [(prof, r)], now))
    asyncio.run(pilot.balance_warn(1, [(prof, r)], now + 60))
    warns = [t for _, t, _ in bot.sent if 'хватит' in t]
    assert len(warns) == 1 and '~4 дн.' in warns[0]
    r2 = {'servers': [srv(30000, 2, name='a')], 'balance_kop': 0}   # ≤ 3 дней — это уже «Скоро конец аренды»
    store.set('ap:balwarn:2', '')
    asyncio.run(pilot.balance_warn(2, [(prof, r2)], now))
    assert len([t for _, t, _ in bot.sent if 'хватит' in t]) == 1
