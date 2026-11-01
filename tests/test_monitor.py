import pytest

from bot import checker as ck
from bot import monitor as M


def test_sub_names_and_env():
    assert ck.sub_name(7) == 'bnodes-g7' and ck.pid_of('bnodes-g7') == 7 and ck.pid_of('чужая') is None
    env = ck.env_text([(1, 'https://a.example/s/1'), (2, 'https://b.example/s/2')], 'pw')
    assert 'SUBSCRIPTION_URL="https://a.example/s/1#bnodes-g1,https://b.example/s/2#bnodes-g2"' in env
    assert 'METRICS_PROTECTED="true"' in env and 'METRICS_PASSWORD="pw"' in env and 'SUBSCRIPTION_JSON_FORMAT="true"' in env
    assert '127.0.0.1:2112:2112' in ck.COMPOSE  # API чекера наружу не публикуется


@pytest.mark.parametrize('bad', ['ftp://x', 'https://a b', 'https://a/x#y', 'https://a/x,y'])
def test_sub_url_rejected(bad):
    with pytest.raises(ValueError):
        ck.check_sub_url(bad)


def test_settings():
    cfg = M.clean_settings({'mon_sub': 'https://sub.example/AbC', 'mon_mode': 'auto', 'mon_steps': ['replace', 'reip'],
                            'mon_port': '443', 'mon_old': 'keep', 'mon_max_buys': '0'}, {}, [])
    assert cfg['mon_steps'] == ['replace', 'reip'] and cfg['mon_port'] == 443 and cfg['mon_max_buys'] == 0  # порядок — как задан
    with pytest.raises(ValueError):
        M.clean_settings({'mon_steps': ['mask', 'mask']}, {}, [])
    buy = M.clean_settings({'mon_buy': [{'type': 'preset', 'cpu': '2', 'ram': 4, 'disk': 40}, {'type': 'same'},
                                        {'type': 'custom', 'cpu': 1, 'ram': 2, 'disk': 30}, {'type': 'same'}],
                            'mon_buy_last': 'none'}, {}, [])
    assert buy['mon_buy'] == [{'type': 'preset', 'cpu': 2, 'ram': 4, 'disk': 40}, {'type': 'same'},
                              {'type': 'custom', 'cpu': 1, 'ram': 2, 'disk': 30}] and buy['mon_buy_last'] == 'none'
    for bad in ({'mon_buy': []}, {'mon_buy': [{'type': 'gold'}]}, {'mon_buy': [{'type': 'preset', 'cpu': 'x'}]},
                {'mon_buy_last': 'maybe'}):
        with pytest.raises(ValueError):
            M.clean_settings(bad, {}, [])
    assert M.spec_label({'type': 'custom', 'cpu': 1, 'ram': 2, 'disk': 30}) == '1 vCPU · 2 ГБ · 30 ГБ (своя)'
    # пустая ссылка — оставить текущую, «-» — убрать
    assert M.clean_settings({'mon_sub': ''}, {'mon_sub': 'https://x/1'}, [])['mon_sub'] == 'https://x/1'
    assert M.clean_settings({'mon_sub': '-'}, {'mon_sub': 'https://x/1'}, [])['mon_sub'] == ''
    for bad in ({'mon_mode': 'yolo'}, {'mon_steps': ['dance']}, {'mon_old': 'burn'}, {'mon_max_buys': 99},
                {'mon_port': '2222'}, {'mon_script': 5}):
        with pytest.raises(ValueError):
            M.clean_settings(bad, {}, [{'id': 5, 'status': 'draft'}])
    assert M.settings_of({})['mon_steps'] == M.STEPS and M.settings_of({})['mon_mode'] == 'ask'
    assert M.settings_of({})['mon_buy'] == [{'type': 'same'}] and M.settings_of({})['mon_buy_last'] == 'cheapest'
    assert M.STEPS.index('reip') == M.STEPS.index('replace') - 1  # платная смена IP — прямо перед покупкой


def test_healthy():
    on = {'online': True, 'lastCheck': 1}
    off = {'online': False, 'lastCheck': 1}
    assert not M.Monitor.healthy([{'online': False, 'lastCheck': 0}])  # чекер только запустился — ещё не судим
    assert M.Monitor.healthy([off, off])  # мало проверок — голосует
    assert not M.Monitor.healthy([off, off, off])  # ни одна не прошла — лежит сеть самого чекера
    assert M.Monitor.healthy([off, off, on]) and not M.Monitor.healthy(None)


def test_assign_hosts():
    pl = {'uuid': 'n-pl', 'address': '201.10.91.118', 'configProfile': {'activeInbounds': ['i-pl']}}
    ee = {'uuid': 'n-ee', 'address': 'ee.example.com', 'configProfile': {'activeInbounds': ['i-ee', 'i-ee-xh', 'i-ws']}}
    ya = {'uuid': 'n-ya', 'address': '203.0.113.9', 'configProfile': {'activeInbounds': ['i-ya']}}
    H = lambda a, inb, **kw: {'uuid': a, 'address': a, 'port': 443, 'inbound': {'configProfileInboundUuid': inb}, **kw}  # noqa: E731
    hosts = [H('pl.example.com', 'i-pl'), H('pl2.example.com', 'i-pl'),
             H('cdn-ee.example.com', 'i-ee-xh'),                       # CDN: адрес другой, inbound — эстонский
             H('vk.com', 'i-ws', xrayJsonTemplateUuid='t-bal'),         # балансировщик Poland на эстонском inbound
             H('full.example.com', 'i-yacdn', nodes=['n-ya']),          # явная привязка «Ноды»
             H('188.68.218.46', 'i-arenda', remark='Обход 3'),          # хост без ноды: свой xray.json
             H('off.example.com', 'i-pl', isDisabled=True)]
    dns = {'pl.example.com': {'201.10.91.118'}, 'pl2.example.com': {'201.10.91.118'}, 'cdn-ee.example.com': {'104.16.0.1'},
           'ee.example.com': {'198.51.100.7'}, 'full.example.com': {'87.250.0.1'}}
    out, loose = M.Monitor.assign([pl, ee, ya], hosts, dns, {}, {'t-bal'})
    got = {k: sorted((h['address'], d) for h, d in v) for k, v in out.items()}
    assert got['201.10.91.118'] == [('pl.example.com', True), ('pl2.example.com', True)]  # нода по IP — хосты по домену
    assert got['ee.example.com'] == [('cdn-ee.example.com', False)]  # CDN — к своей ноде, не прямой вход
    assert got['203.0.113.9'] == [('full.example.com', False)]
    assert [h['address'] for h in loose] == ['188.68.218.46']  # балансировщик и выключенный — ни к кому


def test_skip():
    assert M.Monitor.skip_group('🌀 АВТО-ВЫБОР', M.SKIP_DEFAULT) and not M.Monitor.skip_group('Poland', M.SKIP_DEFAULT)
    assert not M.Monitor.skip_group('', M.SKIP_DEFAULT) and not M.Monitor.skip_group('АВТО-ВЫБОР', '')
    assert M.clean_settings({'mon_skip': ' NORMAL ,, авто '}, {}, [])['mon_skip'] == 'NORMAL, авто'


def test_votes_per_checker_and_regions(tmp_path):
    from bot.store import Store
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'g', 'panel_url': 'https://p', 'panel_token': 't'})
    prof = store.profile(pid)
    mon = M.Monitor(store, None, None, {1})
    sub = ck.sub_name(pid)
    P = lambda ok, ms=None: {'subName': sub, 'server': '201.10.91.118', 'port': 443, 'online': ok, 'lastCheck': 1, 'latencyMs': ms}  # noqa: E731
    node = {'uuid': 'n1', 'address': '201.10.91.118', 'name': 'Poland', 'isConnected': True, 'configProfile': {'activeInbounds': ['i1']}}
    host = {'uuid': 'h1', 'address': '201.10.91.118', 'port': 443, 'inbound': {'configProfileInboundUuid': 'i1'}}
    results = {1: [P(True, 80), P(True, 40)], 2: [P(False), P(False), P(True, 90)], 3: [{'subName': 'other', 'online': True, 'lastCheck': 1}]}
    v = mon.votes(prof, results, [node], [host], {})['201.10.91.118']
    assert v['by'] == {'1': [2, 2, 40], '2': [1, 3, 90]} and v['data'] == 2 and v['fail'] == 0
    assert M.guess_region('Сбер Клауд мск') == 'c' and M.guess_region('Новосибирск') == 'sib'
    assert M.guess_region('Владивосток') == 'fe' and M.guess_region('Мой чекер') == ''


def test_default_zone_from_old_node(tmp_path, monkeypatch):
    import asyncio

    from bot import services as svc
    from bot.store import Store
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'g', 'panel_url': 'https://p', 'panel_token': 't'})
    mon = M.Monitor(store, None, None, {1})

    class CF:
        def zone_names(self):
            return ['example.com', 'bobr-cloud.online', 'cloud.online']
    monkeypatch.setattr(svc, 'cf_for', lambda store, prof: CF())
    prof = store.profile(pid)
    z = lambda addr, bd=None: asyncio.run(mon.default_zone(prof, {'addr': addr}, bd or {}))  # noqa: E731
    assert z('swe.bobr-cloud.online') == 'bobr-cloud.online'          # самая длинная подходящая зона
    assert z('203.0.113.5', {'domain': 'de-1.example.com'}) == 'example.com'
    assert z('203.0.113.5') == '' and z('node.other.net') == ''
    store.save_profile({**prof, 'base_domain': 'example.com'})
    assert asyncio.run(mon.default_zone(store.profile(pid), {'addr': 'swe.bobr-cloud.online'}, {})) == 'example.com'
