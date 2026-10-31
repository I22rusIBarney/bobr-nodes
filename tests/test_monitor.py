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


def test_node_hosts_and_skip():
    nodes = [{'address': '201.10.91.118'}, {'address': 'de.example.com'}]
    hosts = [{'address': 'pl.example.com'}, {'address': 'pl2.example.com'}, {'address': 'cdn-pl.example.com'},
             {'address': 'vk.com'}, {'address': 'de.example.com'}, {'address': 'off.example.com', 'isDisabled': True}]
    dns = {'pl.example.com': {'201.10.91.118'}, 'pl2.example.com': {'201.10.91.118'}, 'cdn-pl.example.com': {'104.16.0.1'},
           'vk.com': {'87.240.0.1'}, 'de.example.com': {'198.51.100.7'}, 'off.example.com': {'201.10.91.118'}}
    m = M.Monitor.node_hosts(nodes, hosts, dns, {})
    assert m['201.10.91.118'] == {'201.10.91.118', 'pl.example.com', 'pl2.example.com'}  # нода по IP — хосты по домену
    assert m['de.example.com'] == {'de.example.com'}  # CDN, подменный адрес и выключенный хост — ни к кому
    assert M.Monitor.skip_group('🌀 АВТО-ВЫБОР', M.SKIP_DEFAULT) and not M.Monitor.skip_group('Poland', M.SKIP_DEFAULT)
    assert not M.Monitor.skip_group('', M.SKIP_DEFAULT) and not M.Monitor.skip_group('АВТО-ВЫБОР', '')
    assert M.clean_settings({'mon_skip': ' NORMAL ,, авто '}, {}, [])['mon_skip'] == 'NORMAL, авто'
