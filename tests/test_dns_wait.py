import struct

from bot import engine


class CF:
    def name_servers(self, domain):
        return ['ns1.example', 'ns2.example']


def fake_doh(answers):
    def http(method, url, headers=None, body=None, timeout=30):
        ip = answers['google' if 'google' in url else 'cloudflare']
        return 200, {'Answer': [{'type': 1, 'data': ip}] if ip else []}
    return http


def run(monkeypatch, auth, pub, clock_step=15):
    t = [1000.0]
    monkeypatch.setattr(engine.time, 'time', lambda: t[0])
    monkeypatch.setattr(engine.time, 'sleep', lambda s: t.__setitem__(0, t[0] + s))
    monkeypatch.setattr(engine.socket, 'getaddrinfo', lambda h, p, f: [(0, 0, 0, '', ('198.51.100.1', 53))])
    monkeypatch.setattr(engine, 'dns_query_a', lambda s, n, timeout=3: auth)
    monkeypatch.setattr(engine, 'http', fake_doh(pub))
    log = []
    engine.wait_dns('de1.example.com', '203.0.113.5', log.append, timeout=600, every=clock_step, cf=CF())
    return log, t[0] - 1000


def test_all_agree_immediately(monkeypatch):
    log, waited = run(monkeypatch, ['203.0.113.5'], {'google': '203.0.113.5', 'cloudflare': '203.0.113.5'})
    assert waited == 0 and log[-1] == 'DNS отвечает 203.0.113.5'


def test_stale_public_cache_does_not_block(monkeypatch):
    """Свои серверы имён зоны уже отвечают, один публичный DNS — тоже: не ждём, пока кэш второго протухнет."""
    log, waited = run(monkeypatch, ['203.0.113.5'], {'google': None, 'cloudflare': '203.0.113.5'})
    assert waited == 0 and 'Google DNS ещё держит старый ответ' in log[-1]


def test_after_two_minutes_authoritative_is_enough(monkeypatch):
    log, waited = run(monkeypatch, ['203.0.113.5'], {'google': '198.51.100.9', 'cloudflare': None})
    assert 120 <= waited < 150 and any('жду 203.0.113.5' in x and 'Google DNS → 198.51.100.9' in x for x in log)


def test_gives_up_when_zone_does_not_answer(monkeypatch):
    import pytest
    with pytest.raises(engine.StepError):
        run(monkeypatch, [], {'google': None, 'cloudflare': None})


def test_dns_query_a_parses_answer(monkeypatch):
    class Sock:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, t):
            pass

        def sendto(self, q, addr):
            self.q = q

        def recvfrom(self, n):
            qid = struct.unpack('>H', self.q[:2])[0]
            head = struct.pack('>HHHHHH', qid, 0x8400, 1, 1, 0, 0)
            question = self.q[12:]
            answer = b'\xc0\x0c' + struct.pack('>HHIH', 1, 1, 60, 4) + bytes([203, 0, 113, 5])
            return head + question + answer, ('198.51.100.1', 53)
    monkeypatch.setattr(engine.socket, 'socket', Sock)
    assert engine.dns_query_a('198.51.100.1', 'de1.example.com') == ['203.0.113.5']
