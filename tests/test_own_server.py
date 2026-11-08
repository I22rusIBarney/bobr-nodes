"""Нода на своём сервере (другой хостер): задача без Bobr Cloud, вход по присланному SSH, хостер — сразу в статистике."""
import asyncio

from bot import services as svc
from bot.access import Access
from bot.runner import Runner
from bot.store import Store
from tests.test_tenant_guard import ADMIN, init_data


def test_deploy_own_server(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from bot.web import build_web
    store = Store(tmp_path / 'bot.db')
    pid = store.save_profile({'name': 'g', 'panel_url': 'https://p', 'panel_token': 't', 'base_domain': 'example.com'})
    calls = {'deploy': [], 'bcloud': 0}

    def deploy(node, prof, lf, log, stage):
        calls['deploy'].append(node)
        return 'готово'

    def no_bcloud(*a, **k):
        calls['bcloud'] += 1
        raise AssertionError('Bobr Cloud для своего сервера не нужен')
    runner = Runner(store, None, tmp_path / 'logs', deploy=deploy, bcloud_factory=no_bcloud)

    async def no_catalog(*a, **k):
        raise RuntimeError('Bobr Cloud не подключён')

    async def check(store_, p, doms, n, warns=None):
        return doms
    monkeypatch.setattr(svc, 'load_catalog', no_catalog)
    monkeypatch.setattr(svc, 'check_domains', check)

    async def go():
        client = TestClient(TestServer(build_web(store, runner, '42:TEST', {ADMIN}, access=Access(store, {ADMIN}), data_dir=tmp_path)))
        await client.start_server()
        try:
            hd = {'X-Init-Data': init_data(ADMIN)}
            base = {'pid': pid, 'ip': '185.215.4.10', 'country': 'DE', 'hoster': 'Aeza', 'user': 'root', 'port': 2200,
                    'password': 'pw', 'domain': 'de1.example.com', 'squads': ['s1'], 'port_client': 8443}
            bad = [(await client.post('/api/deploy/own', json={**base, **x}, headers=hd)).status
                   for x in ({'ip': '10.0.0.1'}, {'country': 'Germany'}, {'password': ''}, {'squads': []})]
            r = await client.post('/api/deploy/own', json=base, headers=hd)
            j = await r.json()
            for _ in range(100):
                if store.job(j['ids'][0])['status'] in ('done', 'failed'):
                    break
                await asyncio.sleep(0.05)
            return bad, r.status, j
        finally:
            await client.close()
    bad, status, j = asyncio.run(go())
    assert bad == [400, 400, 400, 400] and status == 200
    job = store.job(j['ids'][0])
    d = job['data']
    assert job['status'] == 'done', job['error']
    assert d['own'] and d['ip'] == '185.215.4.10' and d['location'] == 'de' and d['flag'] == '🇩🇪' and 'Aeza' in d['spec']
    assert d['host']['port'] == 8443 and calls['bcloud'] == 0
    node = calls['deploy'][0]
    assert (node['ssh_user'], node['ssh_port'], node['ssh_password']) == ('root', 2200, 'pw')
    assert store.get(f'hoster:{pid}:de1.example.com') == 'Aeza'
    assert svc.node_ssh(store, runner, pid, 'u', '185.215.4.10')['port'] == 2200   # доступ — тот, с которым ставили
