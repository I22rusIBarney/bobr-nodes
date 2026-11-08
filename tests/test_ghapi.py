"""Токен GitHub из «Подключений»: формат, заголовок во всех запросах к API, проверка при сохранении."""
import pytest

from bot import forms, ghapi
from bot.store import Store


def test_token_format_and_headers(tmp_path):
    ok = 'github_pat_' + 'A' * 40
    assert forms.v_github(ok) == ok and forms.v_github('-') == '' and forms.v_github('ghp_' + 'b' * 36).startswith('ghp_')
    with pytest.raises(ValueError):
        forms.v_github('token123')
    store = Store(tmp_path / 'db.sqlite')
    ghapi.load(store)
    assert 'Authorization' not in ghapi.headers()
    store.set('github_token', ok)
    ghapi.load(store)
    assert ghapi.headers()['Authorization'] == f'Bearer {ok}'
    ghapi.TOKEN['v'] = ''


def test_bad_token_rejected_on_save(tmp_path, monkeypatch):
    import asyncio

    import httpx
    from aiohttp.test_utils import TestClient, TestServer

    from bot.access import Access
    from bot.runner import Runner
    from bot.web import build_web
    from tests.test_tenant_guard import ADMIN, init_data
    store = Store(tmp_path / 'bot.db')
    runner = Runner(store, None, tmp_path / 'logs')
    codes = {'good': 200, 'bad': 401}

    class C:
        def __init__(self, *a, headers=None, **k):
            self.h = headers or {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def get(self, url):
            code = codes['bad' if self.h.get('Authorization', '').endswith('X' * 40) else 'good']
            return httpx.Response(code, json={'resources': {'core': {'limit': 5000, 'remaining': 4999}}})
    monkeypatch.setattr(httpx, 'AsyncClient', C)

    async def go():
        client = TestClient(TestServer(build_web(store, runner, '42:TEST', {ADMIN}, access=Access(store, {ADMIN}), data_dir=tmp_path)))
        await client.start_server()
        try:
            hd = {'X-Init-Data': init_data(ADMIN)}
            bad = await client.post('/api/settings', json={'values': {'github_token': 'github_pat_' + 'X' * 40}}, headers=hd)
            good = await client.post('/api/settings', json={'values': {'github_token': 'github_pat_' + 'G' * 40}}, headers=hd)
            chk = await (await client.post('/api/check/github', headers=hd)).json()
            return bad.status, good.status, chk
        finally:
            await client.close()
    bad, good, chk = asyncio.run(go())
    assert (bad, good) == (400, 200) and store.get('github_token') == 'github_pat_' + 'G' * 40
    assert chk['ok'] and '5000' in chk['message'] and ghapi.TOKEN['v'].startswith('github_pat_G')
    ghapi.TOKEN['v'] = ''
