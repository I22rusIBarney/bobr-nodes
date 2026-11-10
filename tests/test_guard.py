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


CONF = gd.cfg({'guard': {'names': '', 'on': True}})


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
    d = data([user('u1', 'alice')], [device('h1aaaaaaaaaa', 'u1', ip='1.1.1.1', ua='', platform='Linux'), device('h2aaaaaaaaaa', 'u1', ip='9.9.9.9')],
             own={'9.9.9.9'})
    info = {'1.1.1.1': {'holder': 'HETZNER-AS'}, '9.9.9.9': {'holder': 'HETZNER-AS'}}
    found = gd.analyze(d, CONF, {}, info, NOW)
    assert len(found) == 1 and [r['code'] for r in found[0]['reasons']] == ['host:1.1.1.1']
    # обычное приложение на телефоне через хостинг — у человека включён другой VPN: слабый признак, без сообщения
    app = data([user('u1', 'alice')], [device('h1aaaaaaaaaa', 'u1', ip='1.1.1.1')])
    assert gd.analyze(app, CONF, {}, info, NOW) == []
    assert gd.judge(gd.index(app), 'u1', CONF, {}, info, NOW)['score'] == 15


def test_ban_evasion_by_hwid_and_ip():
    d = data([user('u1', 'banned', status='DISABLED'), user('u2', 'second'), user('u3', 'neighbour')],
             [device('samehwid0001', 'u1', ip='5.5.5.5'), device('samehwid0001', 'u2'),
              device('otherhwid001', 'u3', ip='5.5.5.5')])
    found = {f['name']: f for f in gd.analyze(d, CONF, {}, {}, NOW)}
    assert 'banned' not in found  # уже отключён
    assert found['second']['crit'] and 'banned' in found['second']['reasons'][0]['text']
    assert 'neighbour' not in found  # общий IP — у оператора один адрес на тысячи людей: только отметка в карточке
    assert gd.judge(gd.index(d), 'u3', CONF, {}, {}, NOW)['score'] == 20


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
    pid = store.save_profile({'name': 'Main', 'panel_url': 'https://p', 'panel_token': 't', 'guard': {'names': '', 'on': True, 'auto': 'revoke'}})
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


def nets(ips, start=0):
    """Каждому IP — свой оператор."""
    return {ip: {'asn': f'AS{start + i}', 'holder': f'ISP{start + i}'} for i, ip in enumerate(ips)}


def test_many_ips_in_one_network_is_one_phone():
    # один телефон у оператора: 6 адресов за час, но сеть одна — не признак
    ips = [f'31.173.0.{i}' for i in range(6)]
    d = {**data([user('u1', 'alice', hwidDeviceLimit=3)], [device('phone0000001', 'u1')]), 'conn': [conn('u1', ip) for ip in ips]}
    one_net = {ip: {'asn': 'AS8359', 'holder': 'MTS'} for ip in ips}
    j = gd.judge(gd.index(d), 'u1', CONF, {}, one_net, NOW)
    assert j['reasons'] == [] and j['nets'] == 1 and j['ips'] == 6
    assert gd.judge(gd.index(d), 'u1', CONF, {}, {}, NOW)['nets'] == 1   # сети не узнали — тоже одна куча, не обвиняем


def test_concurrent_means_seen_together_not_within_the_hour():
    # телефон за час сменил 5 сетей (вышки, кафе, дом) — одновременно он только в последней
    ips = [f'31.{i}.0.1' for i in range(5)]
    d = {**data([user('u1', 'a', hwidDeviceLimit=1)], [device('phone0000001', 'u1')]),
         'conn': [conn('u1', ip, ago=60 + i * 600) for i, ip in enumerate(ips)]}
    j = gd.judge(gd.index(d), 'u1', CONF, {}, nets(ips), NOW)
    assert j['ips'] == 1 and j['nets'] == 1 and j['reasons'] == []


def test_more_networks_than_limit_needs_repeats(tmp_path):
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'A', 'panel_url': 'https://a', 'panel_token': 't', 'guard': {'names': '', 'on': True}})
    ips = [f'31.{i}.0.1' for i in range(5)]
    info = nets(ips)
    n = FakeNotify()
    g = gd.Guard(store, n, ipinfo=lambda ip: info.get(ip) or {})
    for hour in range(3):
        t = NOW + hour * 3600
        d = {**data([user('u1', 'reseller', hwidDeviceLimit=3)], [device('phone0000001', 'u1')]),
             'conn': [{'owner': 'u1', 'ip': ip, 'last': t - 30, 'node': 'de1'} for ip in ips]}
        found = asyncio.run(g.check(store.profile(pid), t, d))
        if hour < 2:   # одна-две проверки — может быть совпадение: наблюдаем молча
            assert found == [] and n.sent == []
    assert found[0]['reasons'][0]['code'] == 'nets-limit!' and found[0]['nets'] == 5 and len(n.sent) == 1
    assert '5 разных сетей при лимите 3' in n.sent[0][1] and 'из 5 сетей (5 IP)' in n.sent[0][1]
    card = g.detail(store.profile(pid), 'u1', t)
    assert card['nets_checks'] == 3 and len(card['conc_history']) == 3 and all(c['live'] for c in card['conn_list'])


def test_keys_outside_subscription_but_within_limit_is_only_a_note():
    ips = [f'31.{i}.0.1' for i in range(3)]
    d = {**data([user('u1', 'a', hwidDeviceLimit=5)], [device('phone0000001', 'u1')]), 'conn': [conn('u1', ip) for ip in ips]}
    ix = gd.index(d)
    past = {'conc': {'u1': [[NOW - 7200, 3, 3, []], [NOW - 3600, 3, 3, []], [NOW, 3, 3, []]]}}
    j = gd.judge(ix, 'u1', CONF, {}, nets(ips), NOW, past)
    assert [(r['code'], r['w']) for r in j['reasons']] == [('nets-dev', 20)] and j['level'] == ''


def test_relay_through_hosting_needs_repeats_and_own_relay_ignored():
    d = {**data([user('u1', 'alice')], [device('phone0000001', 'u1')], own={'10.0.0.9'}),
         'conn': [conn('u1', '95.1.1.1'), conn('u1', '10.0.0.9')]}
    info = {'95.1.1.1': {'holder': 'AEZA-AS'}, '10.0.0.9': {'holder': 'AEZA-AS'}}
    ix = gd.index(d)
    once = gd.judge(ix, 'u1', CONF, {}, info, NOW)
    assert [(r['code'], r['w']) for r in once['reasons']] == [('conn-host:95.1.1.1', 15)] and once['level'] == ''
    past = {'conc': {'u1': [[NOW - 7200, 1, 1, ['95.1.1.1']], [NOW - 3600, 1, 1, ['95.1.1.1']], [NOW, 1, 1, ['95.1.1.1']]]}}
    held = gd.judge(ix, 'u1', CONF, {}, info, NOW, past)
    assert [(r['code'], r['w']) for r in held['reasons']] == [('conn-host!:95.1.1.1', 70)] and held['level'] == 'warn'
    assert gd.remember_conc({}, ix, info, NOW)['u1'] == [[NOW, 1, 1, ['95.1.1.1']]]


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
    pid = store.save_profile({'name': 'Main', 'panel_url': 'https://p', 'panel_token': 't', 'guard': {'names': '', 'on': True}})
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


# ───────── разбор нейросетью ─────────
class FakeGemini:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    async def ask(self, system, turns):
        self.asked.append(turns[0]['text'])
        return self.answer


def test_ai_card_is_anonymous_cached_and_in_alert(tmp_path):
    import json
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'Main', 'panel_url': 'https://p', 'panel_token': 't', 'guard': {'names': '', 'on': True, 'ai': True}})
    prof = store.profile(pid)
    gem = FakeGemini({'verdict': 'reselling', 'confidence': 180, 'summary': 'Подписка подключена к серверу.',
                      'evidence': ['3x-ui'], 'action': 'revoke', 'junk': 1})
    n = FakeNotify()
    g = gd.Guard(store, n, ipinfo=lambda ip: {'holder': 'AEZA'}, gemini=gem)
    d = {**data([user('u1', 'secret-name', tg=424242)],
                [device('3x-ui-server-0001', 'u1', ua='3x-ui-outbound-sub/1.0', platform='', model='', ip='95.142.10.20')]),
         'conn': [conn('u1', '95.142.10.21')]}
    asyncio.run(g.check(prof, NOW, d))
    assert len(gem.asked) == 1
    sent = gem.asked[0]
    for secret in ('secret-name', '424242', '3x-ui-server-0001', '95.142.10.20', '95.142.10.21'):
        assert secret not in sent
    assert '95.142.x.x' in sent and json.loads(sent)['device_list'][0]['server_like']
    assert 'Нейросеть' in n.sent[0][1] and 'перепродаёт' in n.sent[0][1]
    a = asyncio.run(g.ai(prof, 'u1', NOW + 60))
    assert a['confidence'] == 100 and a['verdict'] == 'reselling' and a['doubts'] == [] and 'junk' not in a
    assert len(gem.asked) == 1                      # тот же набор признаков — из кэша
    asyncio.run(g.ai(prof, 'u1', NOW + 60, force=True))
    assert len(gem.asked) == 2
    assert g.detail(prof, 'u1', NOW)['ai']['verdict'] == 'reselling'


def test_ai_errors_and_daily_cap(tmp_path, monkeypatch):
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'Main', 'panel_url': 'https://p', 'panel_token': 't', 'guard': {'names': '', 'on': True}})
    prof = store.profile(pid)
    g = gd.Guard(store, FakeNotify())
    with pytest.raises(ValueError, match='Gemini'):
        asyncio.run(g.ai(prof, 'u1'))
    g = gd.Guard(store, FakeNotify(), gemini=FakeGemini({'verdict': '???'}))
    with pytest.raises(ValueError, match='Проверить сейчас'):
        asyncio.run(g.ai(prof, 'u1'))
    monkeypatch.setattr(gd, 'AI_DAY', 0)
    assert asyncio.run(g.ai_auto(prof, 'u1', NOW)) is None
    assert gd.ai_clean({'verdict': '???', 'confidence': 'x'}) | {} == {
        'verdict': 'unclear', 'confidence': 0, 'summary': '', 'evidence': [], 'doubts': [], 'action': 'watch', 'check': []}


def test_scrub_hides_names_and_ips():
    t = gd._scrub('то же устройство, что у vasya_99 · IP 31.173.5.6 · h-123456789', ['vasya_99', 'h-123456789', 'ab'])
    assert t == 'то же устройство, что у «скрыто» · IP 31.173.x.x · «скрыто»'


# ───────── «Проверить сейчас» в фоне и сводка вместо пачки сообщений ─────────
def test_start_runs_in_background_and_reports_errors(tmp_path, monkeypatch):
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'Main', 'panel_url': 'https://p', 'panel_token': 't', 'guard': {'names': '', 'on': True}})
    prof = store.profile(pid)
    g = gd.Guard(store, FakeNotify(), ipinfo=lambda ip: {})
    gate = asyncio.Event()

    async def slow_check(p, now, data=None):
        g.stage(pid, 'Спрашиваю ноды')
        await gate.wait()
        raise RuntimeError('панель упала')
    monkeypatch.setattr(g, 'check', slow_check)

    async def scenario():
        st = g.start(prof)
        assert st['stage'] == 'Начинаю'
        await asyncio.sleep(0)
        assert g.running[pid]['stage'] == 'Спрашиваю ноды'
        assert g.start(prof)['stage'] == 'Спрашиваю ноды'   # вторая не запускается
        assert len(g._tasks) == 1
        gate.set()
        await asyncio.gather(*list(g._tasks))
        assert pid not in g.running
        assert g.get(f'guard:last:{pid}', {})['error'] == 'панель упала'
    asyncio.run(scenario())


def test_many_suspects_become_one_digest(tmp_path):
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'Main', 'panel_url': 'https://p', 'panel_token': 't', 'guard': {'names': '', 'on': True}})
    n = FakeNotify()
    g = gd.Guard(store, n, ipinfo=lambda ip: {})
    users = [user(f'u{i}', f'name{i}') for i in range(12)]
    devs = [device(f'3x-ui-server-{i:04d}', f'u{i}', ua='3x-ui-outbound-sub/1.0', platform='', model='') for i in range(12)]
    asyncio.run(g.check(store.profile(pid), NOW, data(users, devs)))
    assert len(n.sent) == gd.ALERTS_EACH + 1
    assert 'ещё 7 подозрительных' in n.sent[-1][1] and 'критично: 7' in n.sent[-1][1]


# ───────── то же устройство: HWID и имя компьютера ─────────
def test_pc_name_only_for_unique_computer_names():
    assert gd.pc_name({'platform': 'Windows', 'model': 'DESKTOP-7F3K2LQ'}) == 'desktop-7f3k2lq'
    assert gd.pc_name({'platform': 'windows', 'model': 'VASYA-PC'}) == 'vasya-pc'
    assert gd.pc_name({'platform': '', 'model': 'LAPTOP-AB12CD3'}) == 'laptop-ab12cd3'
    for model, plat in (('iPhone 15', 'iOS'), ('SM-S918B', 'Android'), ('MacBookPro18,1', 'macOS'), ('PC', 'Windows'),
                        ('System Product Name', 'Windows'), ('Pixel 8', 'Android'), ('MyPhone123', 'Android')):
        assert gd.pc_name({'platform': plat, 'model': model}) == '', model


def test_twin_by_hwid_not_by_model_and_pc_name_link():
    users = [user(f'u{i}', f'n{i}') for i in range(4)] + [user('b', 'banned', status='DISABLED')]
    devs = [device('samehwid0001', 'u0'), device('samehwid0001', 'u1'),               # одно приложение на одном телефоне
            device('iphone000001', 'u2'), device('iphone000002', 'u3'),               # просто два iPhone 15 — не связь
            device('pchwid000001', 'u3', platform='Windows', model='DESKTOP-7F3K2LQ'),
            device('pchwid000002', 'b', platform='Windows', model='DESKTOP-7F3K2LQ')]  # тот же ПК, другой HWID
    ix = gd.index(data(users, devs))
    j = {k: gd.judge(ix, k, CONF, {}, {}, NOW) for k in ('u0', 'u2', 'u3')}
    assert [r['w'] for r in j['u0']['reasons']] == [30] and j['u0']['level'] == ''   # одна пара — не тревога
    assert j['u2']['reasons'] == []
    assert [r['code'] for r in j['u3']['reasons']] == ['ban-pc:b'] and j['u3']['level'] == 'warn'
    devs.append(device('samehwid0001', 'u2'))
    j = gd.judge(gd.index(data(users, devs)), 'u0', CONF, {}, {}, NOW)
    assert j['reasons'][0]['w'] == 50 and 'n1, n2' in j['reasons'][0]['text']


def test_pc_name_ignores_architecture_suffix():
    happ = {'platform': 'Windows', 'model': 'DESKTOP-FR15BBH_x86_64'}  # так присылает Happ 4.3 на Windows
    assert gd.pc_name(happ) == gd.pc_name({'platform': 'windows', 'model': 'DESKTOP-FR15BBH'}) == 'desktop-fr15bbh'
    assert gd.pc_name({'platform': 'Windows', 'model': 'PC_x86_64'}) == ''


# ───────── смена устройств, IP подписки, круглосуточный трафик, ферма, торренты, общий чёрный список ─────────
def test_device_churn_over_a_week():
    users = [user('u1', 'reseller', hwidDeviceLimit=3)]
    hw = {}
    for day in range(7):  # каждый день — два новых устройства, старые удалены
        devs = [device(f'dev{day:02d}{i:06d}', 'u1') for i in range(2)]
        hw = gd.remember_devices(hw, gd.index(data(users, devs)), NOW - (6 - day) * DAY)
    ix = gd.index(data(users, devs))
    j = gd.judge(ix, 'u1', CONF, {}, {}, NOW, {'hw': hw})
    assert j['week_devices'] == 14 and [r['code'] for r in j['reasons']] == ['churn'] and j['reasons'][0]['w'] == 50
    old = gd.remember_devices(hw, ix, NOW + 40 * DAY)  # через 40 дней старые забыты
    assert set(old['u1']) == {d['hwid'] for d in devs}


def test_subscription_link_counts_networks_not_ips():
    ips = [f'31.{i}.0.1' for i in range(13)]
    subs = [{'owner': 'u1', 'at': NOW - 600 * i, 'ip': ip, 'ua': 'Happ/4', 'type': 'XRAY_BASE64', 'rule': ''} for i, ip in enumerate(ips)]
    d = {**data([user('u1', 'a', hwidDeviceLimit=3)], [device('dev000000001', 'u1')]), 'subs': subs}
    ix = gd.index(d)
    mem = gd.remember_subips({}, ix, NOW)
    one = {ip: {'asn': 'AS8359'} for ip in ips}   # 13 адресов одного оператора — один телефон в дороге
    j = gd.judge(ix, 'u1', CONF, {}, one, NOW, {'subip': mem})
    assert j['sub_ips'] == 13 and j['sub_nets'] == 1 and j['reasons'] == []
    j = gd.judge(ix, 'u1', CONF, {}, nets(ips), NOW, {'subip': mem})
    assert j['sub_nets'] == 13 and [(r['code'], r['w']) for r in j['reasons']] == [('subnets', 25)]


def test_traffic_around_the_clock():
    pts, used = {}, 0
    for h in range(25, -1, -1):
        used += 300 * 2**20
        pts = gd.remember_hourly(pts, [{'key': 'u1', 'used': used}], NOW - h * 3600)
    hrs = gd.active_hours(pts['u1'], NOW)
    assert hrs['active'] == 24 and len(hrs['hours']) == 24
    ix = gd.index(data([user('u1', 'a', used=used)], []))
    assert gd.judge(ix, 'u1', CONF, {}, {}, NOW, {'hourly': pts})['reasons'][0]['code'] == 'flat'
    idle = gd.remember_hourly({}, [{'key': 'u2', 'used': 5}], NOW)
    idle = gd.remember_hourly(idle, [{'key': 'u2', 'used': 5}], NOW + 3600)
    assert idle['u2'] == [[NOW + 3600, 5]]  # без трафика храним одну отметку
    assert gd.active_hours(idle['u2'], NOW + 3600) is None


def test_trial_farm_and_torrents():
    users = [user('u0', 'main')] + [user(f't{i}', f'trial{i}', status='EXPIRED') for i in range(3)]
    devs = [device('farmhwid0001', o) for o in ('u0', 't0', 't1', 't2')]
    d = {**data(users, devs), 'torrents': [{'owner': 'name:main', 'at': NOW - 3600 * i} for i in range(4)]}
    j = gd.judge(gd.index(d), 'u0', CONF, {}, {}, NOW)
    codes = {r['code'] for r in j['reasons']}
    assert 'farm' in codes and 'torrent' in codes and j['torrents'] == 4


def test_shared_blacklist_only_inside_one_owner(tmp_path):
    store = Store(tmp_path / 'db.sqlite')
    a = store.save_profile({'name': 'A', 'panel_url': 'https://a', 'panel_token': 't', 'guard': {'names': '', 'on': True}})
    b = store.save_profile({'name': 'B', 'panel_url': 'https://b', 'panel_token': 't', 'guard': {'names': '', 'on': True}})
    c = store.save_profile({'name': 'C', 'panel_url': 'https://c', 'panel_token': 't', 'guard': {'names': '', 'on': True}, 'created_by': 777})
    g = gd.Guard(store, FakeNotify(), ipinfo=lambda ip: {})
    asyncio.run(g.check(store.profile(a), NOW, data([user('x', 'banned', status='DISABLED')],
                                                     [device('evilhwid0001', 'x'), device('pc0000000001', 'x', platform='Windows', model='DESKTOP-EVIL123')])))
    newcomer = [user('y', 'newcomer')], [device('evilhwid0001', 'y')]
    found = asyncio.run(g.check(store.profile(b), NOW, data(*newcomer)))
    assert found and found[0]['crit'] and 'другой вашей группе' in found[0]['reasons'][0]['text']
    assert asyncio.run(g.check(store.profile(c), NOW, data(*newcomer))) == []  # чужой владелец — список не общий
    pc = asyncio.run(g.check(store.profile(b), NOW + 1, data([user('z', 'pcguy')], [device('pc0000000002', 'z', platform='Windows', model='DESKTOP-EVIL123_x86_64')])))
    assert pc[0]['reasons'][0]['code'] == 'xban-pc'


def test_sub_history_pages_newest_first_and_oldest_first():
    rec = lambda i: {'userId': 1, 'requestAt': iso(NOW - i * 60), 'requestIp': f'1.1.{i // 250}.{i % 250}', 'userAgent': 'Happ'}  # noqa: E731
    newest = [rec(i) for i in range(1500)]          # свежие сначала, 1500 записей по минуте

    def call_new(method, path):
        start = int(path.split('start=')[1].split('&')[0])
        return {'records': newest[start:start + 500], 'total': 1500}
    out = gd.sub_history(call_new, NOW - 600 * 60)
    assert len(out) == 601 and max(r['at'] for r in out) == pytest.approx(NOW)
    oldest = list(reversed(newest))

    def call_old(method, path):
        start = int(path.split('start=')[1].split('&')[0])
        return {'records': oldest[start:start + 500], 'total': 1500}
    assert len(gd.sub_history(call_old, NOW - 600 * 60)) == 601


def test_card_shows_memory(tmp_path):
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'A', 'panel_url': 'https://a', 'panel_token': 't', 'guard': {'names': '', 'on': True}})
    g = gd.Guard(store, FakeNotify(), ipinfo=lambda ip: {'holder': 'MTS'})
    users = [user('u1', 'a')]
    asyncio.run(g.check(store.profile(pid), NOW - DAY, data(users, [device('olddevice001', 'u1')])))
    subs = [{'owner': 'u1', 'at': NOW - 60, 'ip': '5.5.5.5', 'ua': 'Happ', 'type': 'XRAY_BASE64', 'rule': ''}]
    asyncio.run(g.check(store.profile(pid), NOW, {**data(users, [device('newdevice001', 'u1')]), 'subs': subs}))
    card = g.detail(store.profile(pid), 'u1', NOW)
    assert [(x['hwid'], x['current']) for x in card['device_history']] == [('newdevice001', True), ('olddevice001', False)]
    assert card['week_devices'] == 2 and card['sub_ips'][0]['ip'] == '5.5.5.5' and card['sub_ips'][0]['net']['holder'] == 'MTS'
    assert card['hourly'] is None and card['torrent_list'] == []


# ───────── только подписки бота продаж, понятные сообщения ─────────
def test_only_sales_bot_names_are_checked():
    conf = gd.cfg({'guard': {'on': True}})
    assert conf['names'] == gd.NAMES_DEF
    users = [user('1', 'BOBR_1364999314'), user('2', '160_1195917992'), user('3', 'test', status='DISABLED'),
             user('4', 'statistics'), user('5', 'bridge_ab12')]
    ix = gd.index(data(users, []), conf['names'])
    assert sorted(u['name'] for u in ix['users'].values()) == ['160_1195917992', 'BOBR_1364999314']
    shared = [device('samehwid0001', '1', ip='95.85.248.151'), device('otherhwid001', '3', ip='95.85.248.151')]
    assert gd.analyze(data(users, shared), conf, {}, {}, NOW) == []  # отключённый test — системный, не связь
    with pytest.raises(ValueError):
        gd.clean({'names': '(['}, {})
    assert gd.clean({'names': ''}, {})['names'] == ''


def test_warp_and_private_relay_are_not_hosting():
    assert not gd.hosting('CLOUDFLARENET - Cloudflare, Inc.') and not gd.hosting('AKAMAI-AS') and not gd.hosting('FASTLY')
    assert gd.hosting('HETZNER-AS') and gd.hosting('AS-3HCLOUD Newserverlife LLC')
    d = {**data([user('u1', 'a')], [device('phone0000001', 'u1')]), 'conn': [conn('u1', '104.22.219.101')]}
    assert gd.analyze(d, CONF, {}, {'104.22.219.101': {'holder': 'CLOUDFLARENET - Cloudflare, Inc.'}}, NOW) == []


def test_notification_is_informative_and_copyable():
    assert gd.handle_of('@alchern1999 | Алексей Чернышков') == '@alchern1999' and gd.handle_of('без ника') == ''
    users = [user('u1', 'BOBR_1364999314', tg=1364999314, description='@alchern1999 | Алексей', hwidDeviceLimit=5)]
    devs = [device('3x-ui-server-92ff0059', 'u1', ua='3x-ui-outbound-sub/1.0', platform='', model='', ip='45.38.3.215'),
            device('97351eb0-2c50-4ef8', 'u1', ua='Happ/4.3.0/Windows', platform='Windows', model='DESKTOP-FR15BBH_x86_64', ip='5.18.1.1')]
    f = gd.analyze(data(users, devs), CONF, {}, {'45.38.3.215': {'holder': 'AS-3HCLOUD'}}, NOW)[0]
    t = gd.text({'name': 'Barney'}, f)
    assert '<code>BOBR_1364999314</code> · <code>@alchern1999</code>' in t and 'TG <code>' not in t
    assert '<code>3x-ui-server-92ff0059</code>' in t and '<code>45.38.3.215</code>' in t   # устройство признака и IP
    assert 'DESKTOP-FR15BBH_x86_64' in t and '<code>97351eb0-2c50-4ef8</code>' in t         # остальные устройства
    assert '🔴' in t and '🖥' in t and len(t) < 3900


# ───────── «Windows 10 amd64» — не имя компьютера; не уникальные HWID ─────────
def test_os_strings_are_not_pc_names():
    for model, plat in (('Windows 10 amd64', 'Windows'), ('Windows 11 amd64', 'Windows'), ('Mac OS X aarch64', 'macOS'),
                        ('Windows_10_x64', 'Windows'), ('Linux 6.1 x86_64', 'Linux'), ('My Computer', 'Windows')):
        assert gd.pc_name({'platform': plat, 'model': model}) == '', model
    assert gd.pc_name({'platform': 'Windows', 'model': 'DESKTOP-FR15BBH_x86_64'}) == 'desktop-fr15bbh'
    assert gd.pc_name({'platform': 'macOS', 'model': 'Ivans-MacBook-Pro'}) == 'ivans-macbook-pro'


def test_hwid_or_pc_name_shared_by_many_is_not_a_link():
    users = [user(f'u{i}', f'n{i}') for i in range(8)] + [user('b', 'banned', status='DISABLED')]
    devs = [device('constanthwid', f'u{i}') for i in range(8)] + [device('constanthwid', 'b')]
    devs += [device(f'pc{i:010d}', f'u{i}', platform='Windows', model='DESKTOP-SAMEIMG') for i in range(7)]
    ix = gd.index(data(users, devs))
    assert ix['common_hw'] == {'constanthwid'} and ix['common_pc'] == {'desktop-sameimg'}
    assert gd.judge(ix, 'u0', CONF, {}, {}, NOW)['reasons'] == []
    two = data([user('a', 'a'), user('b2', 'b'), user('c', 'c')],
               [device(f'pc{i:010d}', o, platform='Windows', model='DESKTOP-7F3K2LQ') for i, o in enumerate(('a', 'b2', 'c'))])
    r = gd.judge(gd.index(two), 'a', CONF, {}, {}, NOW)['reasons']
    assert [x['w'] for x in r] == [20] and 'b, c' in r[0]['text']   # одной строкой, а не по 20 за каждого


def test_sales_names_with_suffix():
    rx = __import__('re').compile(gd.NAMES_DEF, __import__('re').I)
    assert rx.search('BOBR_774911976ekaterinamamont') and rx.search('343_1276251629') and not rx.search('statistics')
    assert gd.copyable('у BOBR_774911976ekaterinamamont') == 'у <code>BOBR_774911976ekaterinamamont</code>'


# ───────── итог дня от нейросети ─────────
def test_night_report_anonymous_sorted_and_cached(tmp_path):
    import json
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'Main', 'panel_url': 'https://p', 'panel_token': 't', 'guard': {'names': '', 'on': True, 'night': True}})
    prof = store.profile(pid)
    gem = FakeGemini({'summary': 'Один перепродавец, остальное — шум.', 'users': [
        {'id': 'S2', 'verdict': 'family', 'confidence': 70, 'action': 'none', 'why': 'Обычные телефоны.'},
        {'id': 'S1', 'verdict': 'reselling', 'confidence': 90, 'action': 'revoke', 'why': 'Подписка на сервере 3x-ui.'},
        {'id': 'S9', 'verdict': 'reselling', 'confidence': 99, 'action': 'disable', 'why': 'чужой id'}]})
    n = FakeNotify()
    g = gd.Guard(store, n, ipinfo=lambda ip: {}, gemini=gem)
    users = [user('u1', 'BOBR_111', description='@reseller_man'), user('u2', 'BOBR_222'), user('u3', 'BOBR_333'), user('u4', 'BOBR_444')]
    devs = [device('3x-ui-server-0001', 'u1', ua='3x-ui-outbound-sub/1.0', platform='', model=''),
            device('samehwid0001', 'u2'), device('samehwid0001', 'u3'), device('samehwid0001', 'u4')]
    asyncio.run(g.check(prof, NOW, data(users, devs)))
    n.sent.clear()
    rep = asyncio.run(g.night(prof, NOW + 3600))
    sent = gem.asked[-1]
    for secret in ('BOBR_111', 'reseller_man', '3x-ui-server-0001'):
        assert secret not in sent
    assert [x['id'] for x in json.loads(sent)][:1] == ['S1']
    assert [u['name'] for u in rep['users']] == ['BOBR_111', next(u['name'] for u in rep['users'] if u['action'] == 'none')]
    assert len(rep['users']) == 2  # чужой id S9 отброшен
    t = n.sent[0][1]
    assert n.sent[0][0] == 'security.guard_night' and 'Действовать (1)' in t and '<code>BOBR_111</code> · <code>@reseller_man</code>' in t
    assert 'ложная тревога (1)' in t and 'Один перепродавец' in t
    assert g.ai_cached(pid, 'u1')['verdict'] == 'reselling' and g.get(f'guard:nights:{pid}', [])[0]['users']
    assert g.detail(prof, 'u1', NOW)['ai']['night']


def test_night_runs_once_after_midnight_and_not_right_after_enabling(tmp_path, monkeypatch):
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'Main', 'panel_url': 'https://p', 'panel_token': 't', 'guard': {'names': '', 'on': True, 'night': True, 'tz': 0}})
    prof = store.profile(pid)
    g = gd.Guard(store, FakeNotify(), gemini=FakeGemini({'summary': '', 'users': []}))
    runs = []

    async def fake_night(p, now=None):
        runs.append(now)
    monkeypatch.setattr(g, 'night', fake_night)
    day = (NOW // 86400) * 86400
    asyncio.run(g.night_due(prof, day + 15 * 3600))     # включили днём — не сразу
    asyncio.run(g.night_due(prof, day + 23 * 3600))
    assert runs == []
    asyncio.run(g.night_due(prof, day + 86400 + 60))    # полночь
    asyncio.run(g.night_due(prof, day + 86400 + 3600))  # тот же день — повторно не надо
    assert runs == [day + 86400 + 60]
    off = {**prof, 'guard': {**prof['guard'], 'night': False}}
    asyncio.run(g.night_due(off, day + 2 * 86400 + 60))
    assert len(runs) == 1


def test_night_without_suspects_still_reports(tmp_path):
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'Main', 'panel_url': 'https://p', 'panel_token': 't', 'guard': {'names': '', 'on': True}})
    gem = FakeGemini({})
    n = FakeNotify()
    g = gd.Guard(store, n, gemini=gem)
    rep = asyncio.run(g.night(store.profile(pid), NOW))
    assert rep['users'] == [] and gem.asked == [] and 'подозрительных не было' in n.sent[0][1]
    with pytest.raises(ValueError, match='Gemini'):
        asyncio.run(gd.Guard(store, n).night(store.profile(pid), NOW))
