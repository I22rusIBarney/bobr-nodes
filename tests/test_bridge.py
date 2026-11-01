import asyncio

from bot import services as svc
from bot.store import Store


def test_relay_servers_are_not_offered_for_nodes(tmp_path, monkeypatch):
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'g', 'panel_url': 'https://p', 'panel_token': 't'})
    store.save_relay({'pid': pid, 'node_uuid': 'n', 'ip': '203.0.113.22', 'listen': 443, 'target': 'a', 'target_port': 443,
                      'server_id': 12, 'ssh': {}})
    store.save_relay({'pid': pid, 'node_uuid': 'n', 'ip': '', 'listen': 443, 'target': 'a', 'target_port': 443,
                      'server_id': 14, 'status': 'buying', 'ssh': {}})   # ещё создаётся

    class BC:
        RETRY_FOR = 0

        async def servers(self):
            S = lambda i, ip, st='active': {'id': i, 'ip': ip, 'status': st, 'os': 'debian12'}  # noqa: E731
            return [S(11, '203.0.113.21'), S(12, '203.0.113.22'), S(14, '', 'provisioning')]
    monkeypatch.setattr(svc, 'bcloud_from', lambda store, tg: BC())
    out = {x['id']: x for x in asyncio.run(svc.own_servers(store, store.profile(pid)))}
    assert out[11]['ready'] and not out[11]['relay']
    assert out[12]['relay'] and out[12]['used'] and not out[12]['ready'] and 'мост' in out[12]['why']
    assert out[14]['relay'] and not out[14]['ready']
