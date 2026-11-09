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

    async def set_bridge(self, sid, role, peer=''):
        self.calls.append((self.tg, sid, role, peer))


def srv(sid, name, role='', peer=''):
    return {'id': sid, 'name': name, 'bridge_role': role, 'bridge_peer': peer}


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
    assert sorted(FakeBc.calls) == [(5, 10, 'exit', 'de-in, 203.0.113.9'), (5, 11, 'entry', 'Germany'), (5, 12, '', '')]
    # совпадает — ничего не трогаем
    FakeBc.servers_by = {5: [srv(10, 'de-exit', 'exit', 'de-in, 203.0.113.9'), srv(11, 'de-in', 'entry', 'Germany'), srv(12, 'old')]}
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
