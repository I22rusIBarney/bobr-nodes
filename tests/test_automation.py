import asyncio
import json
import time

import pytest

from bot import autopilot as ap
from bot import selfupdate as su
from bot.pcache import CACHE
from bot.store import Store


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, uid, text, **kw):
        self.sent.append((uid, text))


class Access:
    admins = {1}

    def recipients(self, prof):
        return [1]


class Panel:
    def __init__(self, nodes):
        self.nodes, self.bound, self.fail = nodes, [], None

    def call(self, method, path, body=None):
        if self.fail:
            from bot import engine
            raise engine.StepError(self.fail)
        if (method, path) == ('GET', '/api/nodes'):
            return [dict(n) for n in self.nodes]
        if (method, path) == ('POST', '/api/nodes/bulk-actions/update'):
            self.bound.append(body)
            return {}
        if (method, path) == ('GET', '/api/system/stats') and getattr(self, 'uptime', None) is not None:
            return {'uptime': self.uptime}
        if path in ('/api/system/metadata',) and getattr(self, 'version', None):
            return {'version': self.version}
        from bot import engine
        raise engine.StepError(f'HTTP 404 {path}')


def node(uuid, plugin=None, connected=True, address=None):
    return {'uuid': uuid, 'name': uuid, 'address': address or f'{uuid}.example', 'isDisabled': False,
            'isConnected': connected, 'activePluginUuid': plugin}


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'g', 'panel_url': f'https://p{tmp_path.name}', 'panel_token': 't'})
    bot = Bot()
    pilot = ap.Autopilot(store, None, bot, Access())
    pl = Panel([])
    pl.url = f'https://p{tmp_path.name}'
    monkeypatch.setattr(pilot, 'panel', lambda prof: pl)
    return store, store.profile(pid), pilot, pl, bot


def test_panel_watchdog_tells_once_and_on_recovery(env):
    store, prof, pilot, pl, bot = env
    pl.fail = 'HTTP 502'
    t = 1_000_000.0
    asyncio.run(pilot.panel_load(now=t))
    asyncio.run(pilot.panel_load(now=t + 180))
    assert bot.sent == []                       # меньше 5 минут — молчим
    asyncio.run(pilot.panel_load(now=t + 360))
    asyncio.run(pilot.panel_load(now=t + 540))
    assert len(bot.sent) == 1 and 'не отвечает' in bot.sent[0][1]
    pl.fail = None
    asyncio.run(pilot.panel_load(now=t + 720))
    assert len(bot.sent) == 2 and 'снова отвечает' in bot.sent[1][1]
    asyncio.run(pilot.panel_load(now=t + 900))
    assert len(bot.sent) == 2


def test_short_outage_is_silent(env):
    store, prof, pilot, pl, bot = env
    pl.fail = 'timeout'
    asyncio.run(pilot.panel_load(now=1000.0))
    pl.fail = None
    asyncio.run(pilot.panel_load(now=1180.0))
    assert bot.sent == []


def test_plugin_guard_binds_only_free_nodes(env):
    store, prof, pilot, pl, bot = env
    pl.nodes = [node('a', 'p1'), node('b', 'p1'), node('manual'), node('other', 'p2')]
    asyncio.run(pilot.panel_load(now=1000.0))
    assert pl.bound == [{'uuids': ['manual'], 'fields': {'activePluginUuid': 'p1'}}]


def test_plugin_guard_does_nothing_without_group_plugin(env):
    store, prof, pilot, pl, bot = env
    pl.nodes = [node('a'), node('b')]
    asyncio.run(pilot.panel_load(now=1000.0))
    assert pl.bound == []


def test_offline_tracking_and_cleanup(env, monkeypatch):
    store, prof, pilot, pl, bot = env
    now = time.time()
    pl.nodes = [node('dead', connected=False), node('ok'), node('mine', connected=False, address='mine.example')]
    asyncio.run(pilot.panel_load(now=now - 8 * 86400))
    asyncio.run(pilot.panel_load(now=now))
    # сервер бота без ноды в панели (установка не удалась) и сервер мёртвой ноды
    j1 = store.add_job('b', prof['id'], 1, {'server_id': 11, 'ip': '203.0.113.1', 'domain': 'lost.example'})
    store.update_job(j1, status='failed')
    j2 = store.add_job('b', prof['id'], 1, {'server_id': 12, 'ip': '203.0.113.2', 'domain': 'mine.example'})
    store.update_job(j2, status='done')
    for j in (j1, j2):  # покупки давние — не «установка ещё идёт»
        store._q('UPDATE jobs SET updated_at=? WHERE id=?', (now - 86400, j))

    class BC:
        RETRY_FOR = 0

        async def servers(self):
            return [{'id': 11, 'ip': '203.0.113.1', 'price_kop': 30000}, {'id': 12, 'ip': '203.0.113.2', 'price_kop': 30000},
                    {'id': 99, 'ip': '198.51.100.9', 'price_kop': 1}]
    monkeypatch.setattr(ap, 'bcloud_from', lambda store, tg: BC())
    monkeypatch.setattr(ap, 'buyer_of', lambda store, prof: 1)
    CACHE.put(prof, 'nodes', pl.nodes)
    c = asyncio.run(pilot.cleanup(prof, now))
    assert [x['id'] for x in c['servers']] == [11] and c['servers'][0]['why'] == 'установка не удалась'
    assert c['waste_kop'] == 30000
    assert sorted((x['uuid'], x['server_id']) for x in c['dead']) == [('dead', None), ('mine', 12)]
    asyncio.run(pilot.report_cleanup(now))
    asyncio.run(pilot.report_cleanup(now + 3600))
    clean = [t for _, t in bot.sent if 'Можно убрать' in t]
    assert len(clean) == 1   # тот же список — не чаще раза в неделю


def test_selfupdate_due():
    c = {'on': True, 'hour': 4, 'tz': 0, 'last': ''}
    import calendar
    at = lambda h: calendar.timegm(time.strptime(f'2026-11-02 {h:02d}:30', '%Y-%m-%d %H:%M'))  # noqa: E731
    assert su.due({**c, 'on': False}, False, at(4))[0] is False
    assert su.due(c, False, at(3))[0] is False
    ok, day = su.due(c, False, at(4))
    assert ok and day == '2026-11-02'
    assert su.due(c, False, at(6))[0] is True          # окно 3 часа
    assert su.due(c, False, at(7))[0] is False
    assert su.due({**c, 'last': '2026-11-02'}, False, at(5))[0] is False
    assert su.due(c, True, at(4)) == (False, 'идут задачи — попробую через час')
    assert su.due({**c, 'hour': 23}, False, at(1))[0] is True  # через полночь


def test_selfupdate_clean_and_note(tmp_path):
    assert su.clean({'on': True, 'hour': 5}, {'on': False, 'hour': 4, 'tz': 0}) == {'on': True, 'hour': 5, 'tz': 0}
    with pytest.raises(ValueError):
        su.clean({'hour': 24}, {})
    (tmp_path / 'update-note.txt').write_text('abc1234 def5678\nПлагины нод\n<b>x</b>\n', encoding='utf-8')
    note = su.take_note(str(tmp_path))
    assert 'abc1234 → def5678' in note and '• Плагины нод' in note and '&lt;b&gt;' in note
    assert su.take_note(str(tmp_path)) is None
    (tmp_path / 'version.txt').write_text('0123456789abcdef\t01.11.2026 07:00\tКэш\n', encoding='utf-8')
    assert su.version(str(tmp_path)) == {'sha': '0123456', 'date': '01.11.2026 07:00', 'subject': 'Кэш'}
    assert json.loads(json.dumps(su.cfg(type('S', (), {'get': lambda self, k: ''})()))) == {'on': False, 'hour': 4, 'tz': 0, 'last': ''}


def test_node_links_debounced_and_batched(env):
    store, prof, pilot, pl, bot = env
    t = 1_000_000.0
    pl.nodes = [node('a', connected=False), node('b', connected=False), node('c')]
    asyncio.run(pilot.panel_load(now=t))
    asyncio.run(pilot.panel_load(now=t + 180))
    assert bot.sent == []                                   # меньше 5 минут — ещё не пишем
    asyncio.run(pilot.panel_load(now=t + 360))
    assert len(bot.sent) == 1 and '2 нод потеряли связь' in bot.sent[0][1]
    asyncio.run(pilot.panel_load(now=t + 540))
    assert len(bot.sent) == 1                               # повторно не пишем
    pl.nodes = [node('a'), node('b', connected=False), node('c')]
    asyncio.run(pilot.panel_load(now=t + 720))
    assert len(bot.sent) == 2 and 'снова на связи' in bot.sent[1][1] and '<b>a</b>' in bot.sent[1][1]
    feed = pilot.notify.feed(1)['items']
    assert [x['type'] for x in feed] == ['node.up', 'node.down']


def test_versions_and_panel_restart(env):
    store, prof, pilot, pl, bot = env
    n = {**node('a'), 'versions': {'node': '2.2.0', 'xray': '25.1.1'}}
    pl.nodes, pl.uptime, pl.version = [n], 5000, '2.2.0'
    asyncio.run(pilot.panel_load(now=1000.0))
    assert bot.sent == []                                   # первое наблюдение — просто запоминаем
    pl.nodes = [{**n, 'versions': {'node': '2.2.1', 'xray': '25.1.1'}}]
    pl.uptime = 30                                          # аптайм стал меньше — перезапуск
    asyncio.run(pilot.panel_load(now=1200.0))
    feed = [x['type'] for x in pilot.notify.feed(1)['items']]
    assert 'panel.restart' in feed                          # «колокольчик» по умолчанию — в Telegram не уходит
    assert all('перезапустилась' not in t for _, t in bot.sent)
    assert 'node.version' in feed
