import asyncio
import types

import httpx

from bot import bcloud as B


class Resp:
    status_code = 200

    def json(self):
        return {'balance_kop': 500}


def client_factory(errors: list):
    """httpx.AsyncClient, который сначала бросает ошибки из списка, потом отвечает."""
    calls = []

    class C:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, **kw):
            calls.append(method)
            if errors:
                raise errors.pop(0)
            return Resp()
    return C, calls


def run(monkeypatch, errors, method='GET', retry_for=60.0):
    C, calls = client_factory(errors)
    monkeypatch.setattr(B, 'httpx', types.SimpleNamespace(**{**vars(httpx), 'AsyncClient': C}))
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(B.asyncio, 'sleep', fake_sleep)
    bc = B.Bcloud('http://bot:8080', 'tok', 1)
    bc.RETRY_FOR = retry_for
    try:
        asyncio.run(bc._call(method, '/balance'))
        return 'ok', calls, sleeps
    except B.BcloudError as e:
        return type(e).__name__, calls, sleeps


def test_restart_of_bcloud_is_survived(monkeypatch):
    dns = httpx.ConnectError('[Errno -3] Temporary failure in name resolution')
    res, calls, sleeps = run(monkeypatch, [dns, dns])
    assert res == 'ok' and len(calls) == 3 and sleeps == [2.0, 4.0]


def test_post_is_retried_only_if_not_sent(monkeypatch):
    res, calls, _ = run(monkeypatch, [httpx.ConnectError('refused')], 'POST')
    assert res == 'ok' and len(calls) == 2          # соединения не было — покупку можно повторить
    res, calls, _ = run(monkeypatch, [httpx.ReadTimeout('slow')], 'POST')
    assert res == 'BcloudUnavailable' and len(calls) == 1   # могла дойти — второй раз не покупаем


def test_gives_up_after_retry_window(monkeypatch):
    res, calls, _ = run(monkeypatch, [httpx.ConnectError('x')] * 50, retry_for=0)
    assert res == 'BcloudUnavailable' and len(calls) == 1
