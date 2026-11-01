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


def test_node_server_info_for_conversion(tmp_path, monkeypatch):
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'g', 'panel_url': 'https://p', 'panel_token': 't'})
    j = store.add_job('b', pid, 1, {'server_id': 21, 'ip': '203.0.113.31', 'domain': 'ch1.example.com'})
    store.update_job(j, status='done')

    class BC:
        RETRY_FOR = 0

        async def servers(self):
            return [{'id': 21, 'ip': '203.0.113.31', 'status': 'active', 'os': 'debian12'}]
    monkeypatch.setattr(svc, 'bcloud_from', lambda store, tg: BC())
    x = asyncio.run(svc.own_servers(store, store.profile(pid)))[0]
    assert x['used'] and not x['relay'] and x['node'] == {'job': j, 'pid': pid, 'domain': 'ch1.example.com', 'ip': '203.0.113.31'}


def test_convert_cmd_stops_node_but_keeps_files():
    from bot import relay as rl
    c = rl.convert_cmd('ch1.example.com')
    assert 'docker compose --project-directory /opt/remnanode' in c and 'down' in c
    assert 'mv /opt/remnanode "/opt/remnanode.bridge-$ts"' in c and 'rm -rf' not in c
    for u in ('nuvrion-decoy', 'vps-psiphon', 'wg-quick@warp', 'bnodes-port-'):
        assert u in c
    assert "certbot delete --non-interactive --cert-name ch1.example.com" in c and 'CONVERT_OK' in c
    assert "''" in rl.convert_cmd('')


def test_bridge_verdict():
    from bot import relay as rl
    OK = {'tcp': 'ok', 'tls': 'ok'}
    base = {'sni': 'swe.example.com', 'from_entry': OK, 'from_bot': OK}
    assert 'фильтр по IP' in rl.verdict({**base, 'from_entry': {'tcp': 'ok', 'tls': 'fail'}, 'checkers': []})[0]
    assert 'пробросе' in rl.verdict({**base, 'from_bot': {'tcp': '?', 'tls': 'stall'}, 'checkers': []})[0]
    sni = rl.verdict({**base, 'checkers': [{'entry': {'tcp': 'ok', 'tls': 'stall'}, 'node': {'tcp': 'ok', 'tls': 'stall'}}]})
    assert 'SNI' in sni[0] and 'swe.example.com' in sni[0] and 'Напрямую' in sni[1]
    assert 'IP входа недоступен' in rl.verdict({**base, 'checkers': [{'entry': {'tcp': 'fail', 'tls': 'fail'}}]})[0]
    assert 'мост работает' in rl.verdict({**base, 'checkers': [{'entry': OK, 'node': {'tcp': 'fail', 'tls': 'fail'}}]})[0]
    assert rl.parse_probe('PROBE entry t=ok s=cert\nPROBE node t=fail s=fail') == {'entry': {'tcp': 'ok', 'tls': 'cert'},
                                                                                   'node': {'tcp': 'fail', 'tls': 'fail'}}
