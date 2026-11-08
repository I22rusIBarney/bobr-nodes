"""Диагноз ноды по фактам: отличить выключенный сервер, упавшую ноду, DNS и блокировку из России."""
import asyncio
import time

from bot import diagnose as dg

BASE = {'panel': True, 'domain': 'de1.ex.com', 'ip': '10.0.0.1', 'dns': ['10.0.0.1'], 'tcp': True, 'ssh': True,
        'tls': {'tls': 'ok', 'days': 60}, 'russia': {'c': ['ok'], 'nw': ['ok']}, 'checked_at': time.time()}


def v(**kw):
    return dg.verdict({**BASE, **kw})


def test_all_good():
    r = v()
    assert r['code'] == 'ok' and not r['fixes']


def test_server_down_is_not_a_block():
    r = v(tcp=False, ssh=False, panel=False, russia={'c': ['bad']})
    assert r['code'] == 'down' and 'не блокировка' in r['text']
    assert [x['id'] for x in r['fixes']] == ['hoster', 'replace']     # маскировку не предлагаем: бесполезно


def test_server_alive_node_down():
    r = v(tcp=False, ssh=True, panel=False)
    assert r['code'] == 'node_down' and r['fixes'][0]['id'] == 'upgrade' and not r['fixes'][0]['paid']


def test_dns_points_elsewhere_free_fix():
    r = v(dns=['10.9.9.9'])
    assert r['code'] == 'dns' and r['fixes'] == [{'id': 'sync_ip', 'label': 'Поправить DNS', 'paid': False, 'why': r['fixes'][0]['why']}]


def test_full_russia_block_free_fixes_first():
    r = v(russia={'c': ['bad'], 'nw': ['bad']})
    assert r['code'] == 'ru_block' and [x['id'] for x in r['fixes']] == ['fix', 'reip', 'replace']
    assert [x['paid'] for x in r['fixes']] == [False, True, True]


def test_partial_block_names_regions():
    r = v(russia={'c': ['bad'], 'nw': ['ok'], 'v': ['ok']})
    assert r['code'] == 'ru_partial' and 'Центр' in r['text'] and 'Северо-Запад' not in r['text']


def test_bcloud_server_not_active():
    r = v(bcloud='suspended')
    assert r['code'] == 'server_off' and 'suspended' in r['title']


def test_no_checkers_and_stale_data():
    assert v(russia={})['code'] == 'no_ru'
    r = dg.verdict({**BASE, 'checked_at': time.time() - 3600}, every=300)
    assert r['code'] == 'ok' and 'устарели' in r['text']


def test_cert_expiring_soon():
    assert v(tls={'tls': 'ok', 'days': 3})['code'] == 'cert'


def test_collect_against_local_port():
    async def go():
        srv = await asyncio.start_server(lambda r, w: w.close(), '127.0.0.1', 0)
        port = srv.sockets[0].getsockname()[1]
        f = await dg.collect({'address': '127.0.0.1', 'isConnected': True}, {}, {'by': {'1': [0, 1, None]}, 'checked_at': 1},
                             [{'id': 1, 'region': 'c'}], port, 1)
        srv.close()
        return f
    f = asyncio.run(go())
    assert f['tcp'] is True and f['ssh'] is False and f['russia'] == {'c': ['bad']} and f['panel'] is True


def test_cloudflare_proxied_domain_is_not_a_dns_error():
    assert dg.cloudflare('104.21.3.4') and not dg.cloudflare('10.0.0.1')
    assert v(dns=['104.21.3.4', '172.67.1.1'])['code'] == 'ok'


def test_monitor_cause_block_vs_down(tmp_path, monkeypatch):
    """Мониторинг отличает блокировку от упавшего сервера; «сервер лёг» — только при трёх признаках сразу."""
    from bot import monitor as M
    from bot.store import Store
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'g', 'panel_url': 'https://p', 'panel_token': 't'})
    mon = M.Monitor(store, None, None, {1})
    prof = store.profile(pid)
    opened = {}

    async def tcp(host, port, timeout=5):
        return opened.get(port, False)
    monkeypatch.setattr(dg, 'tcp_open', tcp)
    monkeypatch.setattr(dg, 'tls_probe', lambda *a, **k: {'tls': 'ok', 'days': 60})
    st = {'addr': '10.0.0.1', 'uuid': 'u', 'connected': True}
    opened.update({443: True, 22: True})
    assert asyncio.run(mon.cause(prof, st)) == 'block'
    opened.clear()
    assert asyncio.run(mon.cause(prof, {**st, 'connected': False})) == 'down'
    assert asyncio.run(mon.cause(prof, st)) == ''                     # панель видит ноду — не решаем, что сервер лёг
    opened[22] = True
    assert asyncio.run(mon.cause(prof, {**st, 'connected': False})) == ''   # SSH жив — не «сервер лёг»


def test_user_group_does_not_probe_private_addresses():
    """В группе пользователя адрес ноды задаёт он — бот не стучится во внутреннюю сеть и честно говорит, что проверить нечего."""
    async def go():
        srv = await asyncio.start_server(lambda r, w: w.close(), '127.0.0.1', 0)
        port = srv.sockets[0].getsockname()[1]
        f = await dg.collect({'address': '127.0.0.1', 'isConnected': True}, {}, None, [], port, 1, public_only=True)
        srv.close()
        return f
    f = asyncio.run(go())
    assert f['tcp'] is None and f['target'] == '' and dg.verdict(f)['code'] == 'no_target'


def test_alert_carries_full_diagnosis(tmp_path, monkeypatch):
    """Тревога о ноде сразу говорит, что случилось и что делать, — как на странице ноды."""
    from bot import monitor as M
    from bot.store import Store
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'g', 'panel_url': 'https://p', 'panel_token': 't'})
    mon = M.Monitor(store, None, None, {1})
    prof = store.profile(pid)
    sent = []

    async def say(prof, text, markup=None, st=None, type_=''):
        sent.append(text)
    mon.say = say

    async def closed(host, port, timeout=5):
        return False
    monkeypatch.setattr(dg, 'tcp_open', closed)
    st = {'addr': '10.0.0.1', 'uuid': 'u', 'tok': 't', 'connected': False, 'fail': 2, 'data': 2, 'fails': 3, 'by': {}}
    store.set_mon(f'{pid}:10.0.0.1', st)
    asyncio.run(mon.alert(prof, st, M.settings_of(prof) | {'mon_mode': 'ask'}))
    assert 'Сервер не отвечает' in sent[0] and 'Проверить у хостера' in sent[0] and 'Новая нода (платно)' in sent[0]
    assert store.mon(f'{pid}:10.0.0.1')['cause'] == 'down'


def test_degradation_warns_once_before_block(tmp_path):
    """Задержка выросла вдвое против обычной — одно предупреждение за 12 часов; часть чекеров не проходит трижды — тоже."""
    from bot import monitor as M
    from bot.store import Store
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'g', 'panel_url': 'https://p', 'panel_token': 't'})
    store.save_checker({'name': 'Москва', 'region': 'c'})
    mon = M.Monitor(store, None, None, {1})
    prof, cfg = store.profile(pid), M.settings_of(store.profile(pid)) | {'mon_mode': 'ask'}
    sent = []

    async def say(prof, text, markup=None, st=None, type_=''):
        sent.append((type_, text))
    mon.say = say
    st = {'addr': 'a', 'name': 'de1', 'fail': 0, 'data': 1, 'by': {'1': [1, 1, 60]}}
    for i in range(20):
        asyncio.run(mon.degradation(prof, st, cfg, 1000 + i))
    assert not sent                                                    # обычная задержка — тишина
    st['by'] = {'1': [1, 1, 400]}
    for i in range(5):
        asyncio.run(mon.degradation(prof, st, cfg, 2000 + i))
    assert len(sent) == 1 and sent[0][0] == 'node.degraded' and '400 мс вместо обычных 60 мс' in sent[0][1]
    st.update(lat=[], fail=1, data=2, by={'1': [0, 1, None]}, degr_at=0)
    for i in range(3):
        asyncio.run(mon.degradation(prof, st, cfg, 90000 + i))
    assert len(sent) == 2 and 'не проходит у 1 из 2 чекеров (Центр)' in sent[1][1]
