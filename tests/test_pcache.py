import asyncio

from bot import pcache


class FakePanel:
    def __init__(self, calls):
        self.calls = calls

    def call(self, method, path):
        self.calls.append(path)
        return [{'n': len(self.calls)}]


def make(monkeypatch):
    calls = []
    c = pcache.PanelCache()
    monkeypatch.setattr(c, 'panel', lambda prof: FakePanel(calls))
    return c, calls


PROF = {'id': 1, 'panel_url': 'https://p.example', 'panel_token': 't'}


def test_fresh_hit_skips_panel(monkeypatch):
    c, calls = make(monkeypatch)

    async def go():
        a, st1 = await c.get(PROF, 'nodes')
        b, st2 = await c.get(PROF, 'nodes')
        return a, b, st1, st2
    a, b, st1, st2 = asyncio.run(go())
    assert a == b and calls == ['/api/nodes'] and not st1 and not st2


def test_stale_served_and_refreshed(monkeypatch):
    c, calls = make(monkeypatch)
    c.put(PROF, 'nodes', ['old'])
    k = c._key(PROF, 'nodes')
    c._data[k] = (c._data[k][0] - pcache.FRESH - 1, ['old'])

    async def go():
        d, stale = await c.get(PROF, 'nodes')
        fresh, st = await c.get(PROF, 'nodes', stale_ok=False)  # ждёт уже идущий фоновый запрос
        return d, stale, fresh, st
    d, stale, fresh, st = asyncio.run(go())
    assert d == ['old'] and stale and fresh == [{'n': 1}] and not st and calls == ['/api/nodes']


def test_concurrent_cold_requests_share_one_call(monkeypatch):
    c, calls = make(monkeypatch)

    async def go():
        return await asyncio.gather(*(c.get(PROF, 'hosts') for _ in range(5)))
    asyncio.run(go())
    assert calls == ['/api/hosts']


def test_drop_and_panel_change(monkeypatch):
    c, calls = make(monkeypatch)
    c.put(PROF, 'nodes', ['x'])
    assert c.age({**PROF, 'panel_url': 'https://other'}, 'nodes') is None  # другая панель — другой ключ
    c.drop(1)
    assert c.age(PROF, 'nodes') is None
