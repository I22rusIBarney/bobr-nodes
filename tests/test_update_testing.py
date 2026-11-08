"""«Обновить сейчас», пока новая версия проходит тесты: обновление ставится в очередь (сервер дождётся тестов)."""
import asyncio

from bot import selfupdate as su
from bot.access import Access
from bot.runner import Runner
from bot.store import Store
from tests.test_tenant_guard import ADMIN, init_data


def test_update_now_while_testing(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from bot.web import build_web
    store = Store(tmp_path / 'bot.db')
    runner = Runner(store, None, tmp_path / 'logs')
    state = {'s': 'testing'}

    async def remote(data_dir, force=False, client=None):
        return {'state': state['s'], 'sha': 'abc1234', 'version': '9.9', 'changes': []}
    monkeypatch.setattr(su, 'check_remote', remote)

    async def go():
        client = TestClient(TestServer(build_web(store, runner, '42:TEST', {ADMIN}, access=Access(store, {ADMIN}), data_dir=tmp_path)))
        await client.start_server()
        try:
            hd = {'X-Init-Data': init_data(ADMIN)}
            r1 = await client.post('/api/selfupdate/now', headers=hd)
            j1 = await r1.json()
            state['s'] = 'failed'
            r2 = await client.post('/api/selfupdate/now', headers=hd)
            return r1.status, j1, r2.status
        finally:
            await client.close()
    s1, j1, s2 = asyncio.run(go())
    assert s1 == 200 and 'тесты' in j1['message'] and (tmp_path / su.NOW_FILE).read_text() == 'abc1234'
    assert s2 == 400


def test_hosts_watch_many(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from bot.web import build_web
    store = Store(tmp_path / 'bot.db')
    pid = store.save_profile({'name': 'g', 'panel_url': 'https://p', 'panel_token': 't'})
    runner = Runner(store, None, tmp_path / 'logs')
    a, b = '1a2b3c4d-0000-4000-8000-00000000000a', '1a2b3c4d-0000-4000-8000-00000000000b'

    async def go():
        client = TestClient(TestServer(build_web(store, runner, '42:TEST', {ADMIN}, access=Access(store, {ADMIN}), data_dir=tmp_path)))
        await client.start_server()
        try:
            hd = {'X-Init-Data': init_data(ADMIN)}
            ok = await client.post(f'/api/profiles/{pid}/hosts/watch', json={'uuids': [a, b], 'on': True}, headers=hd)
            one = await client.post(f'/api/profiles/{pid}/hosts/watch', json={'uuid': a, 'on': False}, headers=hd)
            bad = await client.post(f'/api/profiles/{pid}/hosts/watch', json={'uuids': [a, '../x'], 'on': True}, headers=hd)
            return ok.status, one.status, bad.status
        finally:
            await client.close()
    assert asyncio.run(go()) == (200, 200, 400)
    assert store.profile(pid)['mon_hosts'] == {a: False, b: True}
