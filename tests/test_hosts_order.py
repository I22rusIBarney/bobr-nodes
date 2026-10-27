from bot import engine
from bot import services as s


def test_host_places():
    order = ['#nl', 'A', '#de', 'B', 'gone', '#pl']
    got = s.host_places(order, {'de': ['de1', 'de2'], 'nl': ['nl1'], 'pl': ['pl1'], 'fi': ['fi1']}, {'A', 'B'})
    assert got['nl1'] == {'after': '', 'before': []}
    assert got['de1'] == {'after': 'A', 'before': []}
    assert got['de2'] == {'after': 'A', 'before': ['de1']}
    assert got['pl1'] == {'after': 'B', 'before': []}  # неизвестный хост не сбивает якорь
    assert 'fi1' not in got  # не в списке — в конец, как раньше


class FakePanel(engine.Panel):
    def __init__(self, hosts):
        self.hosts, self.sent = hosts, []

    def call(self, method, path, body=None):
        if method == 'GET':
            return [dict(x) for x in self.hosts]
        self.sent.append(body)
        for x in body['hosts']:
            next(h for h in self.hosts if h['uuid'] == x['uuid'])['viewPosition'] = x['viewPosition']


def hosts(*names):
    return [{'uuid': n, 'remark': n, 'address': n, 'viewPosition': i * 10} for i, n in enumerate(names, 1)]


def order(p):
    return [h['uuid'] for h in sorted(p.hosts, key=lambda h: h['viewPosition'])]


def test_place_after_and_out_of_order_finish():
    p = FakePanel(hosts('A', 'B', 'C', 'de2'))
    p.place_hosts(['de2'], {'after': 'A', 'before': ['de1']}, print)  # de2 готова раньше de1
    assert order(p) == ['A', 'de2', 'B', 'C']
    p.hosts += hosts('x', 'x', 'x', 'x', 'de1')[4:]
    p.hosts[-1]['viewPosition'] = 99
    p.place_hosts(['de1'], {'after': 'A', 'before': []}, print)
    assert order(p) == ['A', 'de1', 'de2', 'B', 'C']
    assert sorted(h['viewPosition'] for h in p.hosts) == [10, 20, 30, 40, 99]  # прежние позиции переиспользуются


def test_place_top_and_missing_anchor():
    p = FakePanel(hosts('A', 'B', 'n1'))
    p.place_hosts(['n1'], {'after': '', 'before': []}, print)
    assert order(p) == ['n1', 'A', 'B']
    p.sent.clear()
    p.place_hosts(['n1'], {'after': 'gone', 'before': []}, print)
    assert not p.sent and order(p) == ['n1', 'A', 'B']
