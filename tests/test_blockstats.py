"""История блокировок: открытие и закрытие в базе, сводка по локациям и конфигурациям, совет при покупке."""
from bot import blockstats as bs
from bot.store import Store

DAY = 86400


def test_store_open_close_no_duplicates(tmp_path):
    store = Store(tmp_path / 'db.sqlite')
    store.block_open(1, 'a', start=100, location='de', cause='')
    store.block_open(1, 'a', start=200, cause='ru_block')         # уже открыта — уточняем причину, не дублируем
    store.block_close(1, 'a', 'mask', end=300)
    store.block_close(1, 'a', 'reip', end=400)                    # закрытую не трогаем
    rows = store.blocks(0)
    assert len(rows) == 1 and rows[0]['cause'] == 'ru_block' and rows[0]['fix'] == 'mask' and rows[0]['end'] == 300
    assert store.blocks(0, pids=[2]) == []


def test_summary_and_advice():
    now = 100 * DAY
    nodes = [{'location': 'de', 'loc_name': 'Германия', 'flag': 'DE', 'config': 'встроенный', 'born': now - 60 * DAY} for _ in range(2)] + \
            [{'location': 'fi', 'loc_name': 'Финляндия', 'flag': 'FI', 'config': 'встроенный', 'born': now - 60 * DAY}]
    blocks = [{'location': 'de', 'config': 'встроенный', 'start': now - 10 * DAY - i * DAY, 'end': now - 10 * DAY - i * DAY + 7200,
               'born': now - 60 * DAY, 'cause': 'ru_block', 'fix': 'mask'} for i in range(6)] + \
             [{'location': 'fi', 'config': 'встроенный', 'start': now - 5 * DAY, 'end': None, 'born': None, 'cause': 'down', 'fix': ''}]
    s = bs.summarize(blocks, nodes, now, 90)
    de, fi = s['locations']
    assert de['label'] == 'Германия' and de['nodes'] == 2 and de['blocks'] == 6 and de['rate'] == 1.5 and de['hours'] == 2.0
    assert de['fixes'] == {'mask': 6} and fi['blocks'] == 0 and fi['outages'] == 1 and s['total'] == 6   # падение — не блокировка
    a = bs.advice(s, 'de')
    assert a and a['best']['key'] == 'fi'
    assert bs.advice(s, 'fi') is None and bs.advice(s, 'nl') is None


def test_costs_by_kind(tmp_path):
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'g'})
    add = lambda **d: store.add_job('b', pid, 1, d)
    add(server_id=1, price_kop=28000, domain='de1')                         # новая нода
    add(server_id=2, price_kop=28000, monitor=True, domain='de2')           # замена из мониторинга
    add(server_id=3, price_kop=56000, autoscale=True, domain='de3')         # докупка
    add(server_id=4, existing=True, price_kop=0, domain='own')              # свой сервер — не трата
    add(kind='reip', charged_kop=15000, domain='de1')                       # смена IP
    add(kind='reip', domain='de1')                                          # цена неизвестна — не выдумываем
    add(price_kop=28000, domain='x')                                        # до покупки не дошло
    c = bs.costs(store.jobs_since([pid], 0))
    assert c['total_kop'] == 127000
    assert {k['kind']: (k['kop'], k['n']) for k in c['kinds']} == {'new': (28000, 1), 'replace': (28000, 1), 'scale': (56000, 1), 'reip': (15000, 1)}
    assert store.jobs_since([pid + 1], 0) == [] and store.jobs_since([], 0) == []
