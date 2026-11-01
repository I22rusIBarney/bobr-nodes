import asyncio
import json
import sqlite3
import tarfile
import io

import pytest

from bot import backup as bk
from bot.notify import Notifier
from bot.store import Store


class Bot:
    def __init__(self):
        self.docs, self.sent = [], []

    async def send_message(self, uid, text, **kw):
        self.sent.append((uid, text, kw))

    async def send_document(self, uid, doc, caption='', **kw):
        self.docs.append((uid, doc, caption, kw))


@pytest.fixture
def env(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    store = Store(data / 'bot.db')
    store.save_profile({'name': 'g', 'panel_url': 'https://p', 'panel_token': 't'})
    (data / 'id_ed25519').write_text('KEY')
    (data / 'logs').mkdir()
    (data / 'logs' / 'job-1.log').write_text('big log')
    bot = Bot()
    nt = Notifier(store, bot, admins={1, 2})
    nt.file = lambda p: str(p)
    b = bk.Backup(store, nt, data, env={'BOT_TOKEN': '123:abc', 'ADMIN_IDS': '1,2'}, version=lambda: '3.1')
    return store, data, bot, b


def test_crypto_roundtrip_and_bad_key():
    key = bk.new_key()
    assert len(bk.norm(key)) == 24 and key.count('-') == 5
    blob = bk.encrypt(b'hello', key)
    assert blob.startswith(bk.MAGIC) and b'hello' not in blob
    assert bk.decrypt(blob, key.lower().replace('-', ' ')) == b'hello'   # пароль можно ввести как угодно
    with pytest.raises(bk.BadKey):
        bk.decrypt(blob, bk.new_key())
    with pytest.raises(bk.BackupError):
        bk.decrypt(b'not a backup', key)


def test_due():
    c = {'on': True, 'hour': 5, 'tz': 0, 'last': ''}
    import calendar
    import time
    at = lambda h: calendar.timegm(time.strptime(f'2026-11-02 {h:02d}:10', '%Y-%m-%d %H:%M'))  # noqa: E731
    assert bk.due(c, at(4)) is None
    assert bk.due(c, at(5)) == '2026-11-02' and bk.due(c, at(20)) == '2026-11-02'
    assert bk.due({**c, 'last': '2026-11-02'}, at(6)) is None
    assert bk.due({**c, 'on': False}, at(6)) is None


def test_backup_sends_file_and_restores(env, tmp_path):
    store, data, bot, b = env
    res = asyncio.run(b.run())
    assert res['ok'] and res['sent'] == 2
    assert {d[0] for d in bot.docs} == {1, 2}
    uid, path, caption, kw = bot.docs[0]
    assert kw['disable_notification'] is True and 'Групп 1' in caption and 'не сохранён' in caption
    blob = open(path, 'rb').read()
    plain = bk.decrypt(blob, bk.key_of(store))
    names = tarfile.open(fileobj=io.BytesIO(plain), mode='r:gz').getnames()
    assert 'bot.db' in names and 'id_ed25519' in names and 'env.txt' in names
    assert not any(n.startswith('logs') or n.startswith('backups') for n in names)
    man = bk.inspect(plain)
    assert man['groups'] == 1 and man['version'] == '3.1'
    assert b.view()['local'][0]['name'] == path.split('/')[-1]

    # новый сервер: свои данные, прислали копию
    new = tmp_path / 'new'
    new.mkdir()
    s2 = Store(new / 'bot.db')
    s2.save_profile({'name': 'other', 'panel_url': 'https://x', 'panel_token': 'y'})
    s2.save_profile({'name': 'other2', 'panel_url': 'https://x', 'panel_token': 'y'})
    s2.db.close()
    bk.stage(plain, new)
    m = bk.apply_pending(new)
    assert m['groups'] == 1
    assert [p['name'] for p in Store(new / 'bot.db').profiles()] == ['g']
    assert (new / 'id_ed25519').read_text() == 'KEY' and "BOT_TOKEN='123:abc'" in (new / 'restored-env.txt').read_text()
    olds = [p for p in new.iterdir() if p.name.startswith('before-restore-')]
    assert len(olds) == 1 and (olds[0] / 'bot.db').exists()     # прежние данные не удалены
    note = bk.restored_note(new)
    assert 'Групп 1' in note and bk.restored_note(new) is None
    assert bk.apply_pending(new) is None


def test_backup_keeps_last_n(env):
    store, data, bot, b = env
    bk.save_cfg(store, {'keep': 2})
    for i in range(4):
        b.make(now=1_700_000_000 + i * 86400)
    assert len(b.local()) == 2


def test_bell_mode_sends_no_file(env):
    store, data, bot, b = env
    b.notify.save_prefs(1, {'types': {'system.backup': 'off'}})
    res = asyncio.run(b.run())
    assert {d[0] for d in bot.docs} == {2} and res['sent'] == 1


def test_inspect_rejects_bad_archives():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        ti = tarfile.TarInfo('../evil')
        ti.size = 1
        tar.addfile(ti, io.BytesIO(b'x'))
    with pytest.raises(bk.BackupError):
        bk.inspect(buf.getvalue())
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        ti = tarfile.TarInfo('note.txt')
        ti.size = 1
        tar.addfile(ti, io.BytesIO(b'x'))
    with pytest.raises(bk.BackupError, match='нет базы'):
        bk.inspect(buf.getvalue())
