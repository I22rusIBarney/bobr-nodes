import asyncio
import json

import pytest

from bot import feedback as fbm
from bot.runner import job_text
from bot.store import Store


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, uid, text, reply_markup=None, **kw):
        self.sent.append((uid, text, reply_markup))


class Access:
    admins = {1}

    def recipients(self, prof):
        return [1]


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / 'db.sqlite')
    store.save_user({'tg_id': 7, 'username': 'vasya', 'first_name': 'Вася'})
    pid = store.save_profile({'name': 'Клиент Вася', 'panel_url': 'https://p', 'panel_token': 't', 'created_by': 7})
    (tmp_path / 'logs').mkdir()
    bot = Bot()
    return store, pid, fbm.Feedback(store, bot, Access(), tmp_path / 'logs'), bot, tmp_path


def job(store, pid, uid=7, status='failed', batch='b1', **data):
    jid = store.add_job(batch, pid, uid, {'domain': f'de-{batch}.ex.com', 'loc_name': 'Германия', **data})
    store.update_job(jid, status=status, stage='ssh', error='SSH: нет ответа при входе' if status == 'failed' else '')
    return jid


def run(c):
    return asyncio.run(c)


def test_failed_job_reports_once_and_tells_user(env):
    store, pid, fb, bot, tmp = env
    jid = job(store, pid)
    (tmp / 'logs' / f'job-{jid}.log').write_text('line1\nline2\n')
    rid = run(fb.job_failed(jid))
    r = store.report(rid)
    assert r['kind'] == 'auto' and r['uid'] == 7 and 'line2' in r['data']['log'] and r['data']['error'].startswith('SSH')
    admin_msgs = [t for u, t, _ in bot.sent if u == 1]
    assert len(admin_msgs) == 1 and '@vasya' in admin_msgs[0] and f'#{rid}' in admin_msgs[0]
    j = store.job(jid)
    assert j['data']['report'] == rid and j['data']['report_to_admin'] is True
    assert 'уже у администратора' in job_text(j)                     # пользователь видит, что писать не нужно
    assert run(fb.job_failed(jid)) == rid                              # повтор — тот же отчёт, без нового сообщения
    assert store.report(rid)['data']['count'] == 2 and len([u for u, _, _ in bot.sent if u == 1]) == 1


def test_admin_own_failure_is_recorded_silently(env):
    store, pid, fb, bot, tmp = env
    jid = job(store, pid, uid=1)
    rid = run(fb.job_failed(jid))
    assert store.report(rid) and bot.sent == []
    assert 'уже у администратора' not in job_text(store.job(jid))


def test_survey_batches_and_answers(env):
    store, pid, fb, bot, tmp = env
    a, b = job(store, pid, status='done', batch='x'), job(store, pid, status='done', batch='x')
    svc = job(store, pid, status='done', batch='y', kind='node')       # дополнения на ноде — без опроса
    rep = job(store, pid, status='done', batch='z', monitor=True)      # починка — без опроса
    for j in (a, b, svc, rep):
        fb.job_done(j, now=1000)
    assert run(fb.surveys(now=1000 + 60)) == 0                         # рано
    assert run(fb.surveys(now=1000 + fbm.SURVEY_DELAY + 1)) == 1      # одна анкета на партию
    uid, text, kb = bot.sent[-1]
    assert uid == 7 and 'Всё работает' in text and kb.inline_keyboard[0][0].callback_data == f'fb:y:{a}'
    msg, rid = run(fb.answer(7, a, False))
    assert rid and 'писать ему отдельно не нужно' in msg
    assert store.job(a)['data']['survey'] == 'bad' and store.job(b)['data']['survey'] == 'bad'
    assert any(u == 1 and 'не работает' in t for u, t, _ in bot.sent)
    assert run(fb.answer(7, a, True))[0].startswith('Ответ уже записан')
    assert 'Добавлено' in run(fb.comment(7, rid, 'не подключается с iPhone'))
    assert 'iPhone' in store.report(rid)['text']


def test_manual_and_resolve_reply(env):
    store, pid, fb, bot, tmp = env
    with pytest.raises(ValueError):
        run(fb.manual(7, 'ой'))
    rid = run(fb.manual(7, 'Не вижу свою группу', pid))
    r = run(fb.resolve(rid, 'Поправил, обновите страницу'))
    assert r['status'] == 'done' and r['data']['replies'][0]['text'].startswith('Поправил')
    to_user = [t for u, t, _ in bot.sent if u == 7]
    assert to_user and 'решена' in to_user[-1] and 'Поправил' in to_user[-1]


def test_stats(env):
    store, pid, fb, bot, tmp = env
    job(store, pid, status='done', batch='a')
    job(store, pid, status='failed', batch='b')
    j = job(store, pid, status='done', batch='c', kind='upgrade')
    store.patch_job_data(j, survey='ok')
    store.add_report(7, 'survey', pid, j, rating=1, status='done')
    s = fb.stats()
    assert s['done'] == 2 and s['failed'] == 1 and s['good'] == 1 and s['surveys'] == 1
    up = next(k for k in s['kinds'] if k['kind'] == 'upgrade')
    assert up['good'] == 1
