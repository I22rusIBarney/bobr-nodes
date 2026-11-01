import asyncio
import calendar
import time

import pytest

from bot import notify
from bot.store import Store


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, uid, text, **kw):
        self.sent.append((uid, text))


class Access:
    admins = {1}

    def recipients(self, prof):
        return [1, 7]


def at(h):
    return calendar.timegm(time.strptime(f'2026-11-02 {h:02d}:30', '%Y-%m-%d %H:%M'))


@pytest.fixture
def nt(tmp_path):
    store = Store(tmp_path / 'db.sqlite')
    bot = Bot()
    return notify.Notifier(store, bot, Access()), store, bot


PROF = {'id': 3, 'name': 'g'}


def test_emit_feeds_and_sends_by_default(nt):
    n, store, bot = nt
    sent = asyncio.run(n.emit('node.down', '🔴 <b>a</b> &amp; b', PROF))
    assert sent == [1, 7] and len(bot.sent) == 2
    f = n.feed(7)
    assert f['unread'] == 1 and f['items'][0]['text'] == '🔴 a & b' and f['items'][0]['cat'] == 'nodes'
    n.mark_read(7)
    assert n.unread(7) == 0 and n.unread(1) == 1


def test_modes_bell_and_off(nt):
    n, store, bot = nt
    n.save_prefs(7, {'types': {'node.load': 'bell'}})
    n.save_prefs(1, {'categories': {'nodes': 'off'}})
    asyncio.run(n.emit('node.load', 'жарко', PROF))
    assert bot.sent == []                          # у 7 — только колокольчик, у 1 — выключено
    assert n.feed(7)['items'] and not n.feed(1)['items']


def test_quiet_hours_only_critical(nt):
    n, store, bot = nt
    n.save_prefs(7, {'tz': 0})                     # 23–08 по умолчанию
    asyncio.run(n.emit('node.up', 'вернулась', PROF, to=[7], now=at(2)))
    assert bot.sent == [] and n.feed(7)['items']   # ночью — только в ленту
    asyncio.run(n.emit('node.down', 'упала', PROF, to=[7], now=at(2)))
    assert len(bot.sent) == 1                      # важное — и ночью
    asyncio.run(n.emit('node.up', 'вернулась', PROF, to=[7], now=at(12)))
    assert len(bot.sent) == 2


def test_quiet_hours_need_known_timezone(nt):
    n, store, bot = nt
    assert n.prefs(5)['tz'] is None
    asyncio.run(n.emit('node.up', 'x', PROF, to=[5], now=at(2)))
    assert len(bot.sent) == 1                      # пояс неизвестен — ночь не угадываем


def test_admin_only_types_and_muted_monitor(nt):
    n, store, bot = nt
    asyncio.run(n.emit('security.login', 'вход', PROF))
    assert [u for u, _ in bot.sent] == [1]         # безопасность — только администраторам
    asyncio.run(n.emit('node.ru_down', 'нет из РФ', PROF, tg=False))
    assert len(bot.sent) == 1 and n.feed(7)['items'][0]['type'] == 'node.ru_down'


def test_save_prefs_validates_and_stores_only_diffs(nt):
    n, store, bot = nt
    with pytest.raises(ValueError):
        n.save_prefs(1, {'types': {'node.down': 'sms'}})
    with pytest.raises(ValueError):
        n.save_prefs(1, {'quiet': {'from': 25}})
    p = n.save_prefs(1, {'types': {'node.down': 'tg', 'node.version': 'tg'}, 'quiet': {'on': False}})
    assert p['types']['node.version'] == 'tg' and p['quiet']['on'] is False
    import json
    assert json.loads(store.get('notif:1'))['types'] == {'node.version': 'tg'}


def test_feed_filters_by_category_and_pages(nt):
    n, store, bot = nt
    for i in range(3):
        asyncio.run(n.emit('panel.down', f'p{i}', PROF, tg=False))
    asyncio.run(n.emit('node.down', 'n', PROF, tg=False))
    assert [x['text'] for x in n.feed(1, 'panels')['items']] == ['p2', 'p1', 'p0']
    first = n.feed(1)['items']
    assert [x['text'] for x in n.feed(1, before=first[1]['id'])['items']] == ['p1', 'p0']
