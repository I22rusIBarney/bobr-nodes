import asyncio
import calendar
import json
import time

from bot import selfupdate as su
from bot.store import Store

SHA_OLD, SHA_NEW = 'a' * 40, 'b' * 40


class Resp:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data

    def raise_for_status(self):
        pass


class Client:
    def __init__(self, sha, runs, log):
        self.sha, self.runs, self.log, self.calls = sha, runs, log, []

    async def get(self, url, params=None):
        self.calls.append(url)
        if url.endswith('/commits/main'):
            return Resp({'sha': self.sha})
        if url.endswith('/bot/changelog.json'):
            return Resp(self.log)
        if url.endswith('/actions/runs'):
            return Resp({'workflow_runs': self.runs})
        raise AssertionError(url)


def data_dir(tmp_path):
    (tmp_path / 'version.txt').write_text(f'{SHA_OLD}\t01.11.2026\tx\n')
    (tmp_path / 'origin.txt').write_text('https://github.com/Me/bobr-nodes.git\n')
    return tmp_path


def check(tmp_path, client):
    su._REMOTE.clear()
    return asyncio.run(su.check_remote(data_dir(tmp_path), force=True, client=client))


LOG = [{'version': '9.1', 'date': '2026-11-02', 'title': 'Новое', 'items': [{'kind': 'new', 'text': 'x'}]},
       {'version': '9.0', 'date': '2026-11-01', 'title': 'Старое', 'items': [{'kind': 'fix', 'text': 'y'}]}]


def test_states(tmp_path, monkeypatch):
    monkeypatch.setattr(su, 'current', lambda: '9.0')
    ok = [{'event': 'push', 'status': 'completed', 'conclusion': 'success'}]
    r = check(tmp_path, Client(SHA_NEW, ok, LOG))
    assert r['state'] == 'ready' and r['version'] == '9.1' and [x['version'] for x in r['changes']] == ['9.1'] and r['sha'] == 'bbbbbbb'
    assert check(tmp_path, Client(SHA_OLD, ok, LOG))['state'] == 'latest'
    assert check(tmp_path, Client(SHA_NEW, [{'event': 'push', 'status': 'in_progress', 'conclusion': None}], LOG))['state'] == 'testing'
    assert check(tmp_path, Client(SHA_NEW, [], LOG))['state'] == 'testing'
    assert check(tmp_path, Client(SHA_NEW, [{'event': 'push', 'status': 'completed', 'conclusion': 'failure'}], LOG))['state'] == 'failed'

    class Down(Client):
        async def get(self, url, params=None):
            raise OSError('no route')
    assert check(tmp_path, Down(SHA_NEW, ok, LOG))['state'] == 'unknown'


def test_cache_and_slug(tmp_path, monkeypatch):
    c = Client(SHA_OLD, [], LOG)
    d = data_dir(tmp_path)
    su._REMOTE.clear()
    asyncio.run(su.check_remote(d, client=c))
    asyncio.run(su.check_remote(d, client=c))
    assert len(c.calls) == 1 and 'repos/Me/bobr-nodes/commits/main' in c.calls[0]


def test_now_and_night(tmp_path, monkeypatch):
    store = Store(tmp_path / 'bot.db')
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    su.request_now(store, tmp_path, 'bbbbbbb')
    assert (tmp_path / su.NOW_FILE).exists() and json.loads(store.get('self_update_req'))['mode'] == 'now'
    jid = store.add_job('b', 1, 1, {'domain': 'x'})              # идёт установка — подождать
    assert su.main(['x', 'check', '--now']) == 1 and store.get('self_update_pending') == '1'
    store.update_job(jid, status='done')
    assert su.main(['x', 'check']) == 0 and not store.get('self_update_pending')   # таймер подхватил позже
    c = su.request_night(store, 'bbbbbbb')
    assert c['once'] and not c['on']
    at4 = calendar.timegm(time.strptime('2026-11-02 04:30', '%Y-%m-%d %H:%M'))
    assert su.due(c, False, at4)[0] is True                        # «ночью» сработает, хоть автообновление выключено
    store.set(su.KEY, json.dumps({**c, 'last': ''}))
    assert su.main(['x', 'check']) in (0, 1)
