import asyncio

import pytest

from bot import guard as gd
from bot.store import Store

NOW = 1_762_750_000.0  # 2025-11-10, около полудня UTC
DAY = 86400
GB = 2 ** 30


def user(uid, name, used=0, status='ACTIVE', **kw):
    return {'uuid': uid, 'username': name, 'status': status, 'userTraffic': {'lifetimeUsedTrafficBytes': used}, **kw}


def device(hwid, owner, ua='Happ/3.1', platform='iOS', model='iPhone 15', ip='', created=None):
    return {'hwid': hwid, 'userUuid': owner, 'userAgent': ua, 'platform': platform, 'osVersion': '18' if platform else '',
            'deviceModel': model, 'ip': ip, 'createdAt': created}


def data(users, devices, own=()):
    return {'users': [gd.norm_user(u) for u in users], 'devices': [gd.norm_device(d) for d in devices], 'own': set(own)}


CONF = gd.cfg({'guard': {'on': True}})


def test_server_device_is_critical():
    d = data([user('u1', 'alice'), user('u2', 'bob')],
             [device('3x-ui-server-92ff0059', 'u1', ua='3x-ui-outbound-sub/1.0', platform='', model=''),
              device('aaaaaaaaaa11', 'u2')])
    found = gd.analyze(d, CONF, {}, {}, NOW)
    assert [f['name'] for f in found] == ['alice']
    assert found[0]['level'] == 'crit' and found[0]['crit']
    assert '3x-ui' in found[0]['reasons'][0]['text']


def test_normal_family_is_quiet():
    devs = [device(f'hwid{i:08d}', 'u1', platform='Android' if i % 2 else 'Windows', model=f'm{i}', ip='5.18.0.1') for i in range(11)]
    d = data([user('u1', 'family', hwidDeviceLimit=12)], devs)
    assert gd.analyze(d, CONF, {}, {'5.18.0.1': {'holder': 'ROSTELECOM-AS'}}, NOW) == []


def test_hosting_ip_but_own_node_is_ignored():
    d = data([user('u1', 'alice')], [device('h1aaaaaaaaaa', 'u1', ip='1.1.1.1'), device('h2aaaaaaaaaa', 'u1', ip='9.9.9.9')],
             own={'9.9.9.9'})
    info = {'1.1.1.1': {'holder': 'HETZNER-AS'}, '9.9.9.9': {'holder': 'HETZNER-AS'}}
    found = gd.analyze(d, CONF, {}, info, NOW)
    assert len(found) == 1 and [r['code'] for r in found[0]['reasons']] == ['host:1.1.1.1']


def test_ban_evasion_by_hwid_and_ip():
    d = data([user('u1', 'banned', status='DISABLED'), user('u2', 'second'), user('u3', 'neighbour')],
             [device('samehwid0001', 'u1', ip='5.5.5.5'), device('samehwid0001', 'u2'),
              device('otherhwid001', 'u3', ip='5.5.5.5')])
    found = {f['name']: f for f in gd.analyze(d, CONF, {}, {}, NOW)}
    assert 'banned' not in found  # уже отключён
    assert found['second']['crit'] and 'banned' in found['second']['reasons'][0]['text']
    assert found['neighbour']['level'] == 'warn' and not found['neighbour']['crit']  # общий IP — только внимание


def test_burst_of_new_devices():
    iso = lambda t: __import__('datetime').datetime.fromtimestamp(t, __import__('datetime').timezone.utc).isoformat()  # noqa: E731
    devs = [device(f'new{i:09d}', 'u1', created=iso(NOW - 3600 * i)) for i in range(5)]
    found = gd.analyze(data([user('u1', 'alice')], devs), {**CONF, 'burst': 4}, {}, {}, NOW)
    assert found == []  # 30 баллов — меньше порога
    devs.append(device('bare00000001', 'u1', ua='', platform='', model=''))
    devs.append(device('bare00000002', 'u1', ua='', platform='', model=''))
    found = gd.analyze(data([user('u1', 'alice')], devs), {**CONF, 'burst': 4}, {}, {}, NOW)
    assert found and {'burst', 'bare:bare00000001'} <= {r['code'] for r in found[0]['reasons']}


def test_traffic_history_and_spike():
    hist = {}
    used = 0
    for day in range(8, -1, -1):  # неделя по 2 ГБ, вчера — 40 ГБ
        t = NOW - day * DAY
        hist = gd.remember_traffic(hist, [{'key': 'u1', 'used': used}], t, 0)
        used += (40 if day == 1 else 2) * GB
    y, base = gd.traffic(hist['u1'], NOW, 0)
    assert round(y / GB) == 40 and round(base / GB) == 2
    found = gd.analyze(data([user('u1', 'alice', used=used)], []), {**CONF, 'day_gb': 30}, hist, {}, NOW)
    assert {r['code'] for r in found[0]['reasons']} == {'day', 'spike'}
    assert len(hist['u1']) <= gd.HISTORY_DAYS


def test_fresh_dedup_and_whitelist():
    f = {'key': 'u1', 'reasons': [{'code': 'day', 'crit': False}]}
    assert gd.fresh([f], {}, set(), NOW) == [f]
    seen = {'u1': {'sig': gd.signature(f), 'ts': NOW, 'codes': ['day']}}
    assert gd.fresh([f], seen, set(), NOW + 3600) == []
    assert gd.fresh([f], seen, set(), NOW + gd.REMIND + 1) == [f]
    crit = {'key': 'u1', 'reasons': [{'code': 'day', 'crit': False}, {'code': 'server:x', 'crit': True}]}
    assert gd.fresh([f], seen, {'u1'}, NOW + gd.REMIND + 1) == []   # «всё в порядке» — не напоминаем
    assert gd.fresh([crit], seen, {'u1'}, NOW) == [crit]            # но новое критичное — пишем


def test_clean_validates():
    p = {'guard': {}}
    assert gd.clean({'on': True, 'auto': 'revoke', 'day_gb': 80}, p)['auto'] == 'revoke'
    with pytest.raises(ValueError):
        gd.clean({'auto': 'nuke'}, p)
    with pytest.raises(ValueError):
        gd.clean({'day_gb': -1}, p)


def test_pages_follow_total():
    calls = []

    def call(method, path):
        calls.append(path)
        start = int(path.split('start=')[1].split('&')[0])
        return {'users': [{'uuid': str(i)} for i in range(start, min(start + gd.PAGE, 1200))], 'total': 1200}
    assert len(gd.pages(call, '/api/users', 'users')) == 1200 and len(calls) == 3


class FakeNotify:
    def __init__(self):
        self.sent = []

    async def emit(self, type_, text, prof=None, markup=None, **kw):
        self.sent.append((type_, text))


class FakePanel:
    def __init__(self):
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path))
        return {}


def test_guard_check_alerts_once_and_auto_revokes(tmp_path, monkeypatch):
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'Main', 'panel_url': 'https://p', 'panel_token': 't', 'guard': {'on': True, 'auto': 'revoke'}})
    prof = store.profile(pid)
    n = FakeNotify()
    g = gd.Guard(store, n, ipinfo=lambda ip: {})
    panel = FakePanel()
    monkeypatch.setattr(gd.Guard, 'panel', staticmethod(lambda p: panel))
    d = data([user('u1', 'reseller')], [device('3x-ui-server-0001', 'u1', ua='3x-ui-outbound-sub/1.0', platform='', model='')])
    found = asyncio.run(g.check(prof, NOW, d))
    assert found and n.sent and n.sent[0][0] == 'security.guard' and 'reseller' in n.sent[0][1]
    assert ('POST', '/api/users/u1/actions/revoke') in panel.calls and 'перевыпущена' in n.sent[0][1]
    asyncio.run(g.check(prof, NOW + 3600, d))
    assert len(n.sent) == 1  # тот же набор признаков — не повторяем
    last = g.get(f'guard:last:{pid}', {})
    assert last['found'][0]['name'] == 'reseller' and last['users'] == 1
    tok = next(iter(g.get(f'guard:tok:{pid}', {})))
    assert 'всё в порядке' in asyncio.run(g.button(pid, tok, 'o'))
    assert 'u1' in g.get(f'guard:ok:{pid}', [])
    with pytest.raises(ValueError):
        asyncio.run(g.button(pid, 'deadbeef', 'r'))


def test_tick_skips_disabled_groups(tmp_path):
    store = Store(tmp_path / 'db.sqlite')
    store.save_profile({'name': 'Off', 'panel_url': 'https://p', 'panel_token': 't'})
    g = gd.Guard(store, FakeNotify())
    asyncio.run(g.tick(store.profiles(), NOW))
    assert g._at == {}


# ───────── IP подключений, история подписки, карточка ─────────
def iso(t):
    import datetime as dt
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()


def conn(owner, ip, ago=60, node='de1'):
    return {'owner': owner, 'ip': ip, 'last': NOW - ago, 'node': node}


def test_more_ips_than_devices_means_shared_keys():
    devs = [device('phone0000001', 'u1', ip='5.18.0.1')]
    ips = [conn('u1', f'31.173.0.{i}') for i in range(6)]
    d = {**data([user('u1', 'alice'), user('u2', 'bob')], devs + [device('phone0000002', 'u2')]), 'conn': ips + [conn('u2', '5.5.5.5')]}
    found = gd.analyze(d, CONF, {}, {}, NOW)
    assert [f['name'] for f in found] == ['alice'] and found[0]['ips'] == 6
    assert any(r['code'] == 'ips' and r['w'] == 60 for r in found[0]['reasons'])
    old = {**d, 'conn': [conn('u1', f'31.173.0.{i}', ago=5 * 3600) for i in range(6)]}  # давно — не считаем
    assert gd.analyze(old, CONF, {}, {}, NOW) == []


def test_relay_through_hosting_and_own_relay_ignored():
    d = {**data([user('u1', 'alice')], [device('phone0000001', 'u1')], own={'10.0.0.9'}),
         'conn': [conn('u1', '95.1.1.1'), conn('u1', '10.0.0.9')]}
    info = {'95.1.1.1': {'holder': 'AEZA-AS'}, '10.0.0.9': {'holder': 'AEZA-AS'}}
    found = gd.analyze(d, CONF, {}, info, NOW)
    assert [r['code'] for r in found[0]['reasons']] == ['conn-host:95.1.1.1']


def test_blocked_and_server_subscription_requests():
    subs = [{'owner': 'u1', 'at': NOW - 600, 'ip': '1.2.3.4', 'ua': 'v2rayNG/1.9', 'type': 'BLOCK', 'rule': 'Block everything else'},
            {'owner': 'u2', 'at': NOW - 600, 'ip': '1.2.3.5', 'ua': '3x-ui-outbound-sub/1.0', 'type': 'BLOCK', 'rule': ''},
            {'owner': 'u3', 'at': NOW - 600, 'ip': '1.2.3.6', 'ua': 'Happ/3.1', 'type': 'XRAY_BASE64', 'rule': 'Happ'}]
    d = {**data([user('u1', 'a'), user('u2', 'b'), user('u3', 'c')], []), 'subs': subs}
    f = {x['name']: x for x in gd.analyze(d, {**CONF, 'day_gb': 0}, {}, {}, NOW)}
    assert 'c' not in f and f['b']['crit']
    assert 'a' not in f  # 40 баллов — меньше порога: один запрет ещё не повод
    j = gd.judge(gd.index(d), 'u1', CONF, {}, {}, NOW)
    assert j['score'] == 40 and 'Block everything else' in j['reasons'][0]['text']


class JobPanel:
    """Панель с фоновыми задачами: 3.x (connections) или 2.7 (ip-control)."""
    def __init__(self, v3=True):
        self.v3, self.polls = v3, 0

    def call(self, method, path, body=None):
        from bot.engine import StepError
        if method == 'POST':
            if path.startswith('/api/connections/') and not self.v3:
                raise StepError(f'панель POST {path}: HTTP 404: Not Found')
            return {'jobId': 'j1'}
        self.polls += 1
        if self.polls < 2:
            return {'isCompleted': False}
        who = 'userId' if self.v3 else 'userUuid'
        return {'isCompleted': True, 'result': {'success': True, 'users': [
            {who: 7, 'ips': [{'ip': '1.1.1.1', 'lastSeen': iso(NOW)}, '2.2.2.2']}]}}


@pytest.mark.parametrize('v3', [True, False])
def test_node_connections_both_panel_versions(v3):
    pytest.importorskip('httpx')
    out = gd.node_connections(JobPanel(v3), {'uuid': 'n1', 'name': 'de1'}, sleep=lambda s: None)
    assert [(c['owner'], c['ip'], c['node']) for c in out] == [('7', '1.1.1.1', 'de1'), ('7', '2.2.2.2', 'de1')]
    assert out[0]['last'] == pytest.approx(NOW) and out[1]['last'] is None


def test_detail_and_search_from_snapshot(tmp_path):
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'Main', 'panel_url': 'https://p', 'panel_token': 't', 'guard': {'on': True}})
    prof = store.profile(pid)
    g = gd.Guard(store, FakeNotify(), ipinfo=lambda ip: {'holder': 'HETZNER-AS'} if ip == '95.1.1.1' else {'holder': 'MTS'})
    d = {**data([user('u1', 'alice', tg=1), user('u2', 'old', status='DISABLED')],
                [device('phone0000001', 'u1', ip='5.5.5.5'), device('phone0000001', 'u2', ip='5.5.5.5')]),
         'conn': [conn('u1', '95.1.1.1'), conn('u1', '31.1.1.1', node='nl1')],
         'subs': [{'owner': 'u1', 'at': NOW - 60, 'ip': '5.5.5.5', 'ua': 'Happ/3.1', 'type': 'XRAY_BASE64', 'rule': 'Happ'}]}
    asyncio.run(g.check(prof, NOW, d))
    card = g.detail(prof, 'u1', NOW)
    assert card['level'] == 'crit' and card['devices'] == 1 and card['ips'] == 2
    assert {c['ip'] for c in card['conn_list']} == {'95.1.1.1', '31.1.1.1'}
    assert next(c for c in card['conn_list'] if c['ip'] == '95.1.1.1')['net']['hosting']
    assert card['links'] == [{'key': 'u2', 'name': 'old', 'status': 'DISABLED', 'via': ['IP 5.5.5.5', 'устройство']}]
    assert card['subs'][0]['ua'] == 'Happ/3.1' and not card['subs'][0]['blocked']
    assert g.detail(prof, 'nobody', NOW) is None
    assert [x['name'] for x in g.search(pid, 'ALI')] == ['alice']
    daily = g.get(f'guard:daily:{pid}', {})
    assert list(daily.values())[0]['crit'] == 1
