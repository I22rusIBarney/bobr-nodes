"""Hysteria2 как дополнение ноды: inbound в профиле (как в рабочем профиле Remnawave), хосты, включение на ноде."""
from bot import engine

BASE = {'inbounds': [{'tag': 'Vision-NV', 'port': 443, 'protocol': 'vless', 'settings': {'clients': []},
                      'streamSettings': {'network': 'raw', 'security': 'tls', 'tlsSettings': {
                          'certificates': [{'certificateFile': '/etc/letsencrypt/nv-node/fullchain.pem', 'keyFile': '/etc/letsencrypt/nv-node/privkey.pem'}]}}}],
        'outbounds': [{'tag': 'DIRECT', 'protocol': 'freedom'}]}


def test_add_hy2_matches_working_profile():
    cfg = engine.apply_extras(BASE, {'hy2': {'port': 443}, 'xhttp': {'port': 2083}})
    hy = next(i for i in cfg['inbounds'] if i['protocol'] == 'hysteria')
    assert hy['tag'] == 'HY2-Vision-NV' and hy['port'] == 443 and hy['settings'] == {'clients': [], 'version': 2}
    st = hy['streamSettings']
    assert (st['network'], st['security'], st['hysteriaSettings'], st['tlsSettings']['alpn']) == ('hysteria', 'tls', {'version': 2}, ['h3'])
    assert st['tlsSettings']['certificates'] == BASE['inbounds'][0]['streamSettings']['tlsSettings']['certificates']
    assert st['finalmask']['quicParams']['congestion'] == 'bbr'
    assert engine.features_of(cfg) == {'hy2', 'xhttp'} and engine.hy2_of(cfg)['tag'] == 'HY2-Vision-NV'
    assert engine._main_tls(cfg)['tag'] == 'Vision-NV'                       # основной — по-прежнему VLESS
    assert engine.add_hy2(cfg)['inbounds'].count(hy) == 1                      # повтор не дублирует
    name, tags = engine.variant('NV Vision', ['Vision-NV'], {'hy2'})
    assert name == 'NV Vision Hysteria2' and tags == {'Vision-NV': 'Vision-NV-HY'}
    assert engine.extras_problem(BASE, {'hy2': {}}, 'P').startswith('в профиле «P» нет inbound HY2-')


class FakePanel(engine.Panel):
    def __init__(self, hosts=None):
        self.hosts, self.calls = hosts or [], []

    def call(self, m, path, body=None):
        self.calls.append((m, path, body))
        if m == 'GET' and path == '/api/hosts':
            return self.hosts
        if m == 'GET' and path == '/api/internal-squads':
            return {'internalSquads': [{'uuid': 's', 'name': 'S', 'inbounds': [{'uuid': 'i1'}]}]}
        return {}


def test_hy2_hosts_and_activation():
    cfg = engine.apply_extras(engine.retag(BASE, 'Vision-NV', 'Vision-NV-HY'), {'hy2': {'port': 443}})
    prof = {'uuid': 'p2', 'name': 'NV Vision Hysteria2', 'config': cfg,
            'inbounds': [{'uuid': 'i2', 'tag': 'Vision-NV-HY'}, {'uuid': 'h2', 'tag': 'HY2-Vision-NV-HY'}]}
    pn = FakePanel()
    pn.hy2_publish(prof, 'a.example.com', 'Germany', 31000, lambda t: None)
    body = next(b for m, _, b in pn.calls if m == 'POST')
    assert (body['address'], body['port'], body['alpn'], body['sni'], body['remark']) == ('a.example.com', 31000, 'h3', 'a.example.com', 'Germany HY2')
    assert body['inbound']['configProfileInboundUuid'] == 'h2'
    pn2 = FakePanel([{'uuid': 'x', 'address': 'a.example.com', 'inbound': {'configProfileInboundUuid': 'h2'}}])
    pn2.hy2_unpublish(prof, 'a.example.com', '1.2.3.4', lambda t: None)
    assert ('DELETE', '/api/hosts/x', None) in pn2.calls
    # перевод ноды в профиль с Hysteria2: её inbound — на ноде и в скваде вместе с основным
    pnode = {'uuid': 'n', 'name': 'A', 'configProfile': {'activeConfigProfileUuid': 'p1', 'activeInbounds': ['i1']}}
    cur = {'uuid': 'p1', 'name': 'NV Vision', 'inbounds': [{'uuid': 'i1', 'tag': 'Vision-NV'}]}
    pn3 = FakePanel()
    pn3.move_node(pnode, cur, prof, {'Vision-NV': 'Vision-NV-HY'}, [], lambda t: None)
    assert pnode['configProfile']['activeInbounds'] == ['i2', 'h2']
    sq = next(b for m, p, b in pn3.calls if m == 'PATCH' and p == '/api/internal-squads')
    assert sq['inbounds'] == ['i1', 'i2', 'h2']
