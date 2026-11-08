"""Разбор блокировок: сеть по IP, проверка соседей по /24, пачки банов, снимок в истории и разрезы статистики."""
import asyncio
import json
import random
import sqlite3

from bot import banlab
from bot import blockstats as bs
from bot.store import Store

DAY = 86400


def test_ip_info_from_ripestat():
    calls = []

    def get(url, params):
        calls.append(url)
        if 'network-info' in url:
            return {'data': {'asns': ['24940'], 'prefix': '5.9.0.0/16'}}
        return {'data': {'holder': 'HETZNER-AS'}}
    assert banlab.ip_info('5.9.1.2', get) == {'asn': 'AS24940', 'holder': 'HETZNER-AS', 'prefix': '5.9.0.0/16'}
    assert banlab.ip_info('5.9.1.2', lambda u, p: {'data': {}}) == {'asn': '', 'holder': '', 'prefix': ''}


def test_cached_info_skips_private_and_uses_cache(tmp_path, monkeypatch):
    store = Store(tmp_path / 'db.sqlite')
    assert banlab.cached_info(store, '10.0.0.1') is None
    n = []
    monkeypatch.setattr(banlab, 'ip_info', lambda ip: n.append(ip) or {'asn': 'AS1', 'holder': 'X', 'prefix': ''})
    assert banlab.cached_info(store, '5.9.1.2', fetch=False) is None   # без похода в сеть — пусто
    assert banlab.cached_info(store, '5.9.1.2')['asn'] == 'AS1'
    assert banlab.cached_info(store, '5.9.1.2')['asn'] == 'AS1' and n == ['5.9.1.2']


def test_neighbors_same_net_without_self():
    ns = banlab.neighbors('5.9.1.2', 300, random.Random(1))
    assert '5.9.1.2' not in ns and '5.9.1.255' not in ns and all(x.startswith('5.9.1.') for x in ns) and len(ns) == 253
    assert banlab.net24('5.9.1.2') == '5.9.1.0/24' and banlab.net24('192.168.1.1') == ''


def test_alive_abroad_keeps_open_only():
    async def tcp(ip, port, timeout):
        return ip.endswith('.3') and port == 22 or ip.endswith('.4')
    res = asyncio.run(banlab.alive_abroad(['1.1.1.3', '1.1.1.4', '1.1.1.5'], tcp))
    assert res == {'1.1.1.3': [22], '1.1.1.4': [443, 22, 80]}


def test_probe_command_and_parse():
    cmd = banlab.ru_probe_cmd({'1.1.1.3': [22], 'bad;rm -rf /': [1]})
    assert '/dev/tcp/1.1.1.3/22' in cmd and 'rm' not in cmd and cmd.endswith('wait')
    out = 'OK 1.1.1.3 22\nNO 1.1.1.4 443\nNO 1.1.1.4 22\nOK 1.1.1.4 80\nNO 1.1.1.5 443\n'
    assert banlab.parse_probe(out) == {'1.1.1.3': True, '1.1.1.4': True, '1.1.1.5': False}


def test_verdict():
    ab = {f'1.1.1.{i}': [443] for i in range(5)}
    assert banlab.verdict(ab, {ip: False for ip in ab})['kind'] == 'subnet'
    assert banlab.verdict(ab, {ip: True for ip in ab})['kind'] == 'single'
    v = banlab.verdict(ab, {'1.1.1.0': True, '1.1.1.1': False, '1.1.1.2': True, '1.1.1.3': False})
    assert v['kind'] == 'mixed' and v['checked'] == 4 and v['blocked'] == 2
    assert banlab.verdict({'1.1.1.0': [443]}, {'1.1.1.0': False})['kind'] == 'unknown'


def test_clusters():
    b = [{'addr': 'a', 'name': 'A', 'net24': 'n1', 'start': 0}, {'addr': 'b', 'name': 'B', 'net24': 'n1', 'start': 3600},
         {'addr': 'c', 'name': 'C', 'net24': 'n1', 'start': 10 * DAY}, {'addr': 'd', 'name': 'D', 'net24': 'n2', 'start': 0},
         {'addr': 'c', 'name': 'C', 'net24': 'n1', 'start': 10 * DAY + 60}]   # та же нода дважды — не пачка
    assert banlab.clusters(b, 'net24') == [{'key': 'n1', 'n': 2, 'start': 0, 'nodes': ['A', 'B']}]


def test_store_snapshot_and_migration(tmp_path):
    path = tmp_path / 'db.sqlite'
    db = sqlite3.connect(path)  # база старой версии: таблица блокировок без снимка
    db.execute("CREATE TABLE blocks (id INTEGER PRIMARY KEY AUTOINCREMENT, pid INTEGER NOT NULL, addr TEXT NOT NULL, "
               "name TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '', loc_name TEXT NOT NULL DEFAULT '', "
               "flag TEXT NOT NULL DEFAULT '', config TEXT NOT NULL DEFAULT '', born REAL, start REAL NOT NULL, end REAL, "
               "cause TEXT NOT NULL DEFAULT '', fix TEXT NOT NULL DEFAULT '')")
    db.execute("INSERT INTO blocks(pid, addr, start) VALUES(1, 'old', 1)")
    db.commit()
    db.close()
    store = Store(path)
    store.block_open(1, 'a', start=100, port=30000, fp='safari', stealth=1, users=12, hoster='Aeza')
    store.block_update(1, 'a', ip='5.9.1.2', asn='AS1', net24='5.9.1.0/24', neigh='subnet', name='нельзя')
    r = [x for x in store.blocks(0) if x['addr'] == 'a'][0]
    assert (r['port'], r['fp'], r['stealth'], r['users'], r['hoster'], r['asn'], r['neigh'], r['name']) == \
        (30000, 'safari', 1, 12, 'Aeza', 'AS1', 'subnet', '')
    assert [x for x in store.blocks(0) if x['addr'] == 'old'][0]['asn'] == ''


def test_summary_by_hoster_and_settings():
    now = 100 * DAY
    node = {'location': 'de', 'config': 'x', 'born': now - 60 * DAY, 'asn': 'AS1', 'holder': 'BAD-HOST', 'net24': '1.1.1.0/24',
            'port': 443, 'fp': 'chrome', 'stealth': 0}
    nodes = [node, {**node, 'hoster': 'Свой', 'asn': 'AS2', 'holder': 'GOOD', 'stealth': 1}, {**node, 'asn': None, 'stealth': 0}]
    blocks = [{**node, 'addr': f'a{i}', 'start': now - i * DAY, 'end': None, 'cause': 'ru_block', 'neigh': 'subnet'} for i in range(3)]
    s = bs.summarize(blocks, nodes, now, 90)
    h = {r['label']: r for r in s['hosters']}
    assert h['BAD-HOST']['blocks'] == 3 and h['BAD-HOST']['nodes'] == 2 and h['Свой']['blocks'] == 0
    assert {r['label'] for r in s['asns']} == {'AS1 BAD-HOST', 'AS2 GOOD'}          # нода без ASN — не в разрезе
    assert {r['label'] for r in s['stealth']} == {'обычные настройки', 'уникальные настройки'}
    assert s['neighbors'] == {'subnet': 3} and s['clusters'][0]['n'] == 3


def test_ai_input_and_clean():
    s = bs.summarize([], [], 100 * DAY, 90)
    text = banlab.ai_input(s, [{'start': 1, 'name': 'A', 'ip': '1.2.3.4'}], '')
    data = json.loads(text.split('<<<DATA\n')[1].split('\nDATA>>>')[0])
    assert 'hosters' in data['tables'] and 'ip' not in data['recent'][0]   # IP нод нейросети не отдаём
    r = banlab.ai_clean({'summary': 'ок', 'findings': [{'factor': 'f', 'evidence': 'e', 'confidence': 'sure'}, 'мусор'], 'actions': ['a']})
    assert r['findings'] == [{'factor': 'f', 'evidence': 'e', 'confidence': 'low'}] and r['need_data'] == []
    assert 'Разбор блокировок' in banlab.ai_text(r)


def test_weekly_ai_watch(tmp_path):
    from bot.monitor import Monitor
    store = Store(tmp_path / 'db.sqlite')
    m = Monitor(store, None, None, set())
    ran = []

    async def fake():
        ran.append(1)
    m._ai_report = fake

    async def go():
        assert m.ai_watch(now=30 * DAY) is None                          # нет ключа Gemini
        store.set('gemini_key', 'k')
        for i in range(2):
            store.block_open(1, f'a{i}', start=DAY + i, cause='ru_block')
        assert m.ai_watch(now=30 * DAY) is None                          # мало новых блокировок
        store.block_open(1, 'a2', start=DAY + 2, cause='ru_block')
        t = m.ai_watch(now=30 * DAY)
        await t
        assert ran == [1] and m.ai_watch(now=30 * DAY + 3600) is None    # повтор не раньше чем через сутки
    asyncio.run(go())
