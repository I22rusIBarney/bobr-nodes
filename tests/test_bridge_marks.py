import asyncio

from bot import services as svc
from bot.store import Store


class FakeBc:
    servers_by: dict = {}
    calls: list = []

    def __init__(self, tg):
        self.tg = tg

    async def servers(self):
        return self.servers_by.get(self.tg, [])

    async def set_bridge(self, sid, role, peer='', link=None):
        self.calls.append((self.tg, sid, role, peer, link or []))


def srv(sid, name, role='', peer='', link=None):
    return {'id': sid, 'name': name, 'bridge_role': role, 'bridge_peer': peer, 'bridge_link': link or []}


def test_marks_entry_and_exit_and_clears_stale(tmp_path, monkeypatch):
    store = Store(tmp_path / 'db.sqlite')
    store.set('bcloud_url', 'https://bc.test')
    store.set('bcloud_token', 't')
    pid = store.save_profile({'name': 'g', 'panel_url': 'https://p', 'panel_token': 't', 'owner_tg_id': 5})
    store.add_job('b', pid, 0, {'domain': 'de.example', 'ip': '192.0.2.1', 'server_id': 10})  # нода-выход
    store.save_relay({'pid': pid, 'node_name': 'Germany', 'target': 'de.example', 'ip': '192.0.2.2', 'server_id': 11, 'status': 'ok'})
    store.save_relay({'pid': pid, 'node_name': 'Germany', 'target': 'de.example', 'ip': '203.0.113.9', 'status': 'ok'})  # свой сервер
    FakeBc.servers_by = {5: [srv(10, 'de-exit'), srv(11, 'de-in'), srv(12, 'old', 'entry', 'Latvia')]}
    FakeBc.calls = []
    monkeypatch.setattr(svc, 'bcloud_from', lambda st, tg: FakeBc(tg))
    assert asyncio.run(svc.sync_bridge_marks(store)) == 3
    assert sorted(FakeBc.calls) == [(5, 10, 'exit', 'de-in, 203.0.113.9', [11]), (5, 11, 'entry', 'Germany', [10]), (5, 12, '', '', [])]
    # совпадает — ничего не трогаем
    FakeBc.servers_by = {5: [srv(10, 'de-exit', 'exit', 'de-in, 203.0.113.9', [11]), srv(11, 'de-in', 'entry', 'Germany', [10]), srv(12, 'old')]}
    FakeBc.calls = []
    assert asyncio.run(svc.sync_bridge_marks(store)) == 0 and FakeBc.calls == []


def test_old_bcloud_without_marks_is_skipped(tmp_path, monkeypatch):
    store = Store(tmp_path / 'db.sqlite')
    store.set('bcloud_url', 'https://bc.test')
    store.set('bcloud_token', 't')
    pid = store.save_profile({'name': 'g', 'panel_url': 'https://p', 'panel_token': 't', 'owner_tg_id': 5})
    store.save_relay({'pid': pid, 'node_name': 'Germany', 'target': 'de.example', 'ip': '192.0.2.2', 'server_id': 11, 'status': 'ok'})
    FakeBc.servers_by = {5: [{'id': 11, 'name': 'de-in'}]}
    FakeBc.calls = []
    monkeypatch.setattr(svc, 'bcloud_from', lambda st, tg: FakeBc(tg))
    assert asyncio.run(svc.sync_bridge_marks(store)) == 0 and FakeBc.calls == []


def test_reinstalled_server_is_free_and_old_node_dropped(tmp_path, monkeypatch):
    import time
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'g', 'panel_url': 'https://p', 'panel_token': 't', 'owner_tg_id': 5})
    jid = store.add_job('b', pid, 0, {'domain': 'old.example', 'ip': '192.0.2.7', 'server_id': 20})
    store.add_job('b', pid, 0, {'domain': 'busy.example', 'ip': '192.0.2.8', 'server_id': 21})
    later = time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(time.time() + 60))
    base = {'status': 'active', 'os': 'debian12', 'location': 'de'}

    class Bc(FakeBc):
        async def servers(self):
            return [{**base, 'id': 20, 'ip': '192.0.2.7', 'reinstalled_at': later},
                    {**base, 'id': 21, 'ip': '192.0.2.8', 'reinstalled_at': None}]
    monkeypatch.setattr(svc, 'bcloud_from', lambda st, tg: Bc(tg))
    out = {x['id']: x for x in asyncio.run(svc.own_servers(store, store.profile(pid)))}
    assert out[20]['ready'] and not out[20]['used'] and out[20]['stale']['job'] == jid
    assert out[21]['used'] and not out[21]['ready'] and out[21]['stale'] is None
    removed = []
    monkeypatch.setattr(svc.engine, 'remove_node', lambda prof, ip, dom, log: removed.append((ip, dom)))
    asyncio.run(svc.drop_stale(store, out[20]['stale'], lambda t: None))
    assert removed == [('192.0.2.7', 'old.example')]
    assert store.job(jid)['data']['removed'] and store.job(jid)['data']['os_reinstalled']


def test_bridge_remark_uses_exit_country():
    assert svc.bridge_remark({'name': 'Germany test', 'countryCode': 'DE'}) == '🇩🇪 Germany ⇄'
    assert svc.bridge_remark({'name': 'swe', 'country': 'se'}) == '🇸🇪 Sweden ⇄'
    assert svc.bridge_remark({'name': 'Somewhere', 'countryCode': 'XX'}) == 'Somewhere ⇄'
