import random
import unittest

from bot import engine


class Stealth(unittest.TestCase):
    def test_values_in_range(self):
        ports = set()
        for seed in range(500):
            s = engine.stealth(random.Random(seed))
            p, xp = s['port'], s['xhttp_port']
            ports.add(p)
            self.assertTrue(p is None or p in engine.STEALTH_PORTS or 20000 <= p <= 60000)
            self.assertNotEqual(p, 443)          # 443 — стандартный, без переадресации
            self.assertNotIn(xp, (443, p))       # XHTTP не на порту основного входа
            self.assertIn(s['fingerprint'], engine.FINGERPRINTS)
            lo, hi = map(int, s['xhttp_extra']['xPaddingBytes'].split('-'))
            self.assertTrue(100 <= lo <= hi <= 1000)
            frag = s['final_mask']['tcp'][0]['settings']
            self.assertEqual(frag['packets'], 'tlshello')
            a, b = map(int, frag['length'].split('-'))
            self.assertLessEqual(a, b)
        self.assertIn(None, ports)               # смесь: часть нод остаётся на 443
        self.assertTrue(any(p and p >= 20000 for p in ports))

    def test_host_body_uses_fingerprint(self):
        prof, ib = {'uuid': 'p'}, {'uuid': 'i'}
        node = {'domain': 'a.example.com', 'host': {'fingerprint': 'safari', 'port': 30000, 'final_mask': {'tcp': []}}}
        body = engine.Panel.host_body(prof, ib, node, True)
        self.assertEqual((body['fingerprint'], body['port']), ('safari', 30000))
        self.assertEqual(engine.Panel.host_body(prof, ib, {'domain': 'a.example.com'}, True)['fingerprint'], 'firefox')


if __name__ == '__main__':
    unittest.main()


class XhttpHosts(unittest.TestCase):
    def test_direct_host_gets_own_port_and_extra(self):
        cfg = engine.add_xhttp({'inbounds': [{'tag': 'Vision-NV', 'port': 443, 'protocol': 'vless', 'streamSettings': {
            'network': 'raw', 'security': 'tls', 'tlsSettings': {'certificates': [{'certificateFile': 'x'}]}}}]})
        prof = {'uuid': 'p', 'name': 'NV Vision XHTTP', 'config': cfg, 'inbounds': [{'uuid': 'xi', 'tag': 'XHTTP-Vision-NV'}]}
        calls = []
        panel = engine.Panel.__new__(engine.Panel)
        panel.call = lambda m, path, body=None: calls.append((m, path, body)) or ([] if m == 'GET' else {})
        s = engine.stealth(random.Random(1))
        panel.xhttp_publish(prof, 'a.example.com', '1.2.3.4', 'A', None, lambda _: None, 'direct', s)
        body = next(b for m, _, b in calls if m == 'POST')
        self.assertEqual(body['port'], s['xhttp_port'])
        self.assertEqual(body['xhttpExtraParams'], s['xhttp_extra'])
        self.assertEqual(body['fingerprint'], s['xhttp_fingerprint'])
