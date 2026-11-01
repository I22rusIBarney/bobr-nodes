import asyncio
import json
import sqlite3
import tarfile

import pytest

from bot import migrate as mg
from bot.store import Store


def test_parse_and_assess():
    out = 'READY\nOS=ubuntu\nVER=24.04\nARCH=x86_64\nMEM=1970\nDISK=20\nBUSY=80\nIP=203.0.113.9\nCHECK_OK\n'
    f = mg.parse_kv(out)
    assert f['OS'] == 'ubuntu' and f['BUSY'] == ['80'] and f['MEM'] == '1970'
    bad, warn = mg.assess(f)
    assert any('80' in x for x in bad)
    bad, warn = mg.assess({'OS': 'centos', 'MEM': '512', 'DISK': '3', 'ARCH': 'x86_64'})
    assert len(bad) == 2 and warn and 'памяти' in warn[0]
    assert mg.assess({'OS': 'debian', 'MEM': '2000', 'DISK': '30', 'ARCH': 'aarch64'}) == ([], [])


def test_env_text_and_bcloud():
    env = {'BOT_TOKEN': '1:a', 'ADMIN_IDS': '1,2', 'PUBLIC_URL': 'https://nodes.ex.com', 'BCLOUD_URL': 'http://bot:8080',
           'BCLOUD_TOKEN': 't', 'BCLOUD_TG_ID': '1', 'PATH': '/usr/bin'}
    t = mg.env_text(env, 'https://cloud.ex.com')
    assert "DOMAIN='nodes.ex.com'" in t and "BCLOUD_URL='https://cloud.ex.com'" in t and "COMPOSE_PROFILES='caddy'" in t
    assert 'PATH' not in t
    with pytest.raises(mg.MigrateError):
        mg.env_text({**env, 'BOT_TOKEN': "x'y"})
    assert mg.internal_bcloud('http://bot:8080') and mg.internal_bcloud('http://127.0.0.1:9000')
    assert not mg.internal_bcloud('https://cloud.ex.com') and not mg.internal_bcloud('')


def test_repo_info(tmp_path):
    sha = 'a' * 40
    (tmp_path / 'version.txt').write_text(f'{sha}\t01.11.2026\tx\n')
    (tmp_path / 'origin.txt').write_text('https://github.com/Me/Bobr-Nodes.git\n')
    url, s, img = mg.repo_info(tmp_path)
    assert s == sha and img == f'ghcr.io/me/bobr-nodes:sha-{sha}' and url.endswith('Bobr-Nodes.git')
    assert mg.repo_info(tmp_path / 'none')[0] == mg.REPO_DEFAULT


def test_backup_and_pack(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    store = Store(data / 'bot.db')
    store.set('bcloud_url', 'http://bot:8080')
    store.set('migrate', '{"status": "running"}')
    (data / 'ssh').mkdir()
    (data / 'ssh' / 'id_ed25519').write_text('KEY')
    (data / 'version.txt').write_text('x')
    work = data / 'migrate'
    work.mkdir()
    mg.backup_db(data / 'bot.db', work / 'bot.db', 'https://cloud.ex.com')
    db = sqlite3.connect(str(work / 'bot.db'))
    rows = dict(db.execute('SELECT key, value FROM settings').fetchall())
    assert rows['bcloud_url'] == 'https://cloud.ex.com' and 'migrate' not in rows
    mg.pack(data, work / 'bot.db', work / 'data.tgz', {'to': '203.0.113.9'})
    names = tarfile.open(work / 'data.tgz').getnames()
    assert 'bot.db' in names and 'ssh/id_ed25519' in names and mg.ARRIVED in names
    assert not any(n == 'migrate' or n.startswith('migrate/') or n == 'version.txt' for n in names)


class FakeRemote:
    def __init__(self, log, fail_on=None):
        self.log, self.fail_on = log, fail_on

    def run(self, cmd, timeout=120):
        self.log.append(cmd)
        if self.fail_on and self.fail_on in cmd:
            return 1, 'boom'
        if 'CHECK_OK' in cmd:
            return 0, 'OS=ubuntu\nVER=24.04\nARCH=x86_64\nMEM=2000\nDISK=40\nIP=203.0.113.9\nCHECK_OK'
        if 'install.sh restore' in cmd:
            return 0, 'Бот запущен\nRESTORE_OK'
        return 0, ' '.join(m for m in ('KEY_OK', 'DOCKER_OK', 'CODE_OK', 'PULL_OK', 'EXTRACT_OK') if m in cmd)


class Runner:
    keydir = None

    def bot_key(self):
        return '/tmp/k', 'ssh-ed25519 AAAA bobr'


def make(tmp_path, monkeypatch, fail_on=None):
    data = tmp_path / 'data'
    data.mkdir()
    store = Store(data / 'bot.db')
    store.set('cf_token', 'cf')
    m = mg.Migration(store, Runner(), data, env={'BOT_TOKEN': '1:a', 'ADMIN_IDS': '1', 'PUBLIC_URL': 'https://nodes.ex.com'})
    cmds, events = [], []
    monkeypatch.setattr(m, 'remote', lambda t, with_password=True: FakeRemote(cmds, fail_on))
    monkeypatch.setattr(m, 'push', lambda t, local, remote: events.append(('push', remote)))
    monkeypatch.setattr(m, 'switch_dns', lambda ip: events.append(('dns', ip)) or {'domain': 'nodes.ex.com', 'ip': '198.51.100.1', 'proxied': False})
    monkeypatch.setattr(m, 'restore_dns', lambda old: events.append(('dns-back', old['ip'])))

    async def stop():
        events.append(('stop',))
    mg.HOOKS.clear()
    mg.HOOKS.update(stop=stop, exit=lambda code: events.append(('exit', code)))
    return store, m, cmds, events, data


def run_migration(m, dns=True):
    async def go():
        m.start({'ip': '203.0.113.9', 'port': 22, 'password': 'pw'}, dns, None)
        await m.task
        await asyncio.sleep(0)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(go())
    finally:
        loop.close()


def test_full_migration(tmp_path, monkeypatch):
    store, m, cmds, events, data = make(tmp_path, monkeypatch)
    run_migration(m)
    st = m.state()
    assert st['status'] == 'done' and st['stage'] == 'done', st
    assert ('stop',) in events and ('dns', '203.0.113.9') in events
    assert [e for e in events if e[0] == 'push'] == [('push', '/root/bn-data.tgz'), ('push', '/root/bn-env')]
    assert events.index(('stop',)) < events.index(('push', '/root/bn-data.tgz'))   # база — после остановки
    assert json.loads((data / mg.MOVED).read_text())['to'] == '203.0.113.9'
    assert mg.moved(data)['to'] == '203.0.113.9'
    assert 'pw' not in store.get('migrate')                                          # пароль не в базе
    assert any('install.sh restore' in c for c in cmds)


def test_failed_start_rolls_back(tmp_path, monkeypatch):
    store, m, cmds, events, data = make(tmp_path, monkeypatch, fail_on='install.sh restore')
    run_migration(m)
    st = m.state()
    assert st['status'] == 'failed' and 'запуск' in st['error']
    assert ('dns-back', '198.51.100.1') in events                                     # DNS вернули
    assert any('docker compose down' in c for c in cmds)                              # новый остановили
    assert not (data / mg.MOVED).exists()


def test_refuses_busy_server(tmp_path, monkeypatch):
    store, m, cmds, events, data = make(tmp_path, monkeypatch)
    store.add_job('b', 1, 1, {'domain': 'x'})                                         # идёт задача
    run_migration(m)
    assert m.state()['status'] == 'failed' and ('stop',) not in events


def test_arrived_note_once(tmp_path):
    (tmp_path / mg.ARRIVED).write_text(json.dumps({'to': '203.0.113.9'}))
    assert '203.0.113.9' in mg.arrived_note(tmp_path)
    assert mg.arrived_note(tmp_path) is None
