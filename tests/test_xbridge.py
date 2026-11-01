import pytest

from bot import engine
from bot import xbridge as xb

EXIT_INB = 'inb-exit'


def exit_profile(security='tls'):
    ss = {'network': 'tcp', 'security': security}
    if security == 'reality':
        priv, _ = xb.reality_keys()
        ss['realitySettings'] = {'privateKey': priv, 'shortIds': ['ab12'], 'serverNames': ['www.apple.com']}
    return {'uuid': 'prof-exit', 'name': 'Sweden_tls', 'inbounds': [{'uuid': EXIT_INB, 'tag': 'VLESS_TLS'}],
            'config': {'inbounds': [{'tag': 'VLESS_TLS', 'protocol': 'vless', 'streamSettings': ss}]}}


class FakePanel:
    def __init__(self, security='tls'):
        self.profiles = [exit_profile(security)]
        self.users, self.hosts, self.nodes, self.calls = {}, [], [], []
        self.squads = [{'uuid': 'sq1', 'name': 'Default', 'inbounds': [{'uuid': EXIT_INB}]}]
        self.plugin = None  # конфиг плагина нод; None — плагина нет

    def call(self, method, path, body=None):
        self.calls.append((method, path))
        if (method, path) == ('GET', '/api/config-profiles'):
            return {'configProfiles': self.profiles, 'total': len(self.profiles)}
        if (method, path) == ('POST', '/api/config-profiles'):
            p = {'uuid': 'prof-bridge', 'name': body['name'], 'config': body['config'],
                 'inbounds': [{'uuid': 'inb-bridge', 'tag': i['tag'], 'port': i['port']} for i in body['config']['inbounds']]}
            self.profiles.append(p)
            return p
        if (method, path) == ('GET', '/api/internal-squads'):
            return {'internalSquads': self.squads}
        if path.startswith('/api/users/by-username/') or path.startswith('/api/users/get-by/'):
            u = self.users.get(path.rsplit('/', 1)[1])
            if not u:
                raise engine.StepError('HTTP 404')
            return u
        if (method, path) == ('POST', '/api/users'):
            u = {'uuid': 'user-1', 'id': 77, 'vlessUuid': 'vless-1', 'username': body['username'],
                 'activeInternalSquads': [{'uuid': s} for s in body['activeInternalSquads']]}
            self.users[body['username']] = u
            return u
        if (method, path) == ('GET', '/api/nodes'):
            return self.nodes
        if (method, path) == ('GET', '/api/hosts'):
            return self.hosts
        if (method, path) == ('PATCH', '/api/hosts'):
            return {}
        if (method, path) == ('GET', '/api/node-plugins'):
            return {'nodePlugins': [{'uuid': 'pl1', 'name': 'Reverse Node Plugins'}] if self.plugin is not None else []}
        if (method, path) == ('GET', '/api/node-plugins/pl1'):
            return {'pluginConfig': self.plugin}
        if (method, path) == ('PATCH', '/api/node-plugins'):
            self.plugin = body['pluginConfig']
            return {}
        if method == 'POST' and path in ('/api/node-plugins/actions/sync', '/api/node-plugins/executor'):
            return {}
        if method == 'DELETE' or (method, path) == ('PATCH', '/api/internal-squads'):
            return {}
        raise AssertionError((method, path))

    def attach(self, node, prof, inbounds, squads, node_port, log, tls=True, also=()):
        assert squads == ['sq1'] and node_port == 2222
        self.tls = tls
        self.nodes.append({'uuid': 'node-entry', 'address': node['domain'], 'name': node['name']})
        self.hosts.append({'uuid': 'host-entry', 'address': node['domain'], 'port': inbounds[0]['port'],
                           'inbound': {'configProfileUuid': prof['uuid'], 'configProfileInboundUuid': inbounds[0]['uuid']}})


EXIT_NODE = {'uuid': 'exit-node-uuid-1234', 'address': 'swe.example.com', 'name': 'Sweden'}
EXIT_HOSTS = [{'uuid': 'h1', 'address': 'swe.example.com', 'port': 8443, 'sni': '',
               'inbound': {'configProfileUuid': 'prof-exit', 'configProfileInboundUuid': EXIT_INB}}]
RELAY = {'ip': '203.0.113.50', 'address': '203.0.113.50', 'listen': 443, 'target_port': 8443, 'sni': 'www.microsoft.com',
         'remark': 'Sweden ⇄', 'node_port': 2222, 'country': 'DE'}


def test_keys_and_sni():
    priv, pub = xb.reality_keys()
    assert xb.public_of(priv) == pub and '=' not in priv and len(pub) == 43
    assert xb.clean_sni(' WWW.Microsoft.com ') == 'www.microsoft.com'
    for bad in ('', 'localhost', 'http://x.com', 'a b.com'):
        with pytest.raises(ValueError):
            xb.clean_sni(bad)


def test_exit_outbound_tls_and_reality():
    o = xb.exit_outbound(FakePanel(), EXIT_NODE, EXIT_HOSTS, 8443, 'vless-1')
    v = o['settings']['vnext'][0]
    assert (v['address'], v['port'], v['users'][0]) == ('swe.example.com', 8443, {'id': 'vless-1', 'encryption': 'none',
                                                                                    'flow': 'xtls-rprx-vision'})
    assert o['streamSettings']['security'] == 'tls' and o['streamSettings']['tlsSettings']['serverName'] == 'swe.example.com'
    pl = FakePanel('reality')
    r = xb.exit_outbound(pl, EXIT_NODE, EXIT_HOSTS, 8443, 'vless-1')['streamSettings']['realitySettings']
    priv = pl.profiles[0]['config']['inbounds'][0]['streamSettings']['realitySettings']['privateKey']
    assert r['publicKey'] == xb.public_of(priv) and r['shortId'] == 'ab12' and r['serverName'] == 'www.apple.com'


def test_profile_routes_everything_to_exit():
    o = xb.exit_outbound(FakePanel(), EXIT_NODE, EXIT_HOSTS, 8443, 'vless-1')
    c = xb.profile_config(443, 'www.microsoft.com', 'PRIV', 'abcd', o)
    inb = c['inbounds'][0]
    assert inb['port'] == 443 and inb['streamSettings']['realitySettings']['serverNames'] == ['www.microsoft.com']
    assert inb['streamSettings']['realitySettings']['dest'] == 'www.microsoft.com:443'
    assert '_inbound' not in c['outbounds'][0] and c['outbounds'][0]['tag'] == xb.OUT_TAG
    assert {'type': 'field', 'inboundTag': [xb.TAG], 'outboundTag': xb.OUT_TAG} in c['routing']['rules']


def test_setup_and_teardown_panel():
    pl, log = FakePanel(), []
    made = xb.setup_panel(pl, RELAY, EXIT_NODE, EXIT_HOSTS, log.append)
    assert made['node'] == 'node-entry' and made['hosts'] == ['host-entry'] and made['squads'] == ['sq1']
    assert made['username'] == 'bridge_exitnodeuuid' and made['user'] == 'user-1' and made['reality']['private']
    bridge = next(p for p in pl.profiles if p['uuid'] == 'prof-bridge')
    vnext = bridge['config']['outbounds'][0]['settings']['vnext'][0]
    assert vnext['users'][0]['id'] == 'vless-1' and bridge['name'] == 'Bridge 203-0-113-50'
    assert xb.profile_name('179.254.115.154') == 'Bridge 179-254-115-154'
    # переустановка: тот же пользователь и те же ключи
    pl.profiles = [p for p in pl.profiles if p['uuid'] != 'prof-bridge']
    again = xb.setup_panel(pl, {**RELAY, 'reality': made['reality']}, EXIT_NODE, EXIT_HOSTS, log.append)
    assert again['reality'] == made['reality'] and sum(1 for c in pl.calls if c == ('POST', '/api/users')) == 1
    pl.calls.clear()
    xb.teardown_panel(pl, made, user_in_use=True, log=log.append)
    assert ('DELETE', '/api/nodes/node-entry') in pl.calls and ('DELETE', '/api/config-profiles/prof-bridge') in pl.calls
    assert ('DELETE', '/api/users/user-1') not in pl.calls     # им пользуются другие мосты к этому выходу


def test_install_command():
    c = xb.install_cmd('SECRET', 2222, 443, ['198.51.100.7', 'not-ip'])
    assert 'systemctl disable --now bobr-relay' in c and xb.MARK in c
    assert 'rw-core' in c and 'grep -qx remnanode' in c    # порт держит наша же нода — не «занят другой программой»
    assert 'ufw allow from "$ip" to any port 2222' in c and "198.51.100.7" in c and 'not-ip' not in c
    import base64
    import re
    comp = base64.b64decode(re.search(r'echo (\S+) \| base64 -d > /opt/remnanode/docker-compose.yml', c).group(1)).decode()
    assert 'SECRET_KEY=SECRET' in comp and 'NODE_PORT=2222' in comp and 'network_mode: host' in comp
    assert 'XB_STOPPED' in xb.stop_cmd() and 'rm -rf' not in xb.stop_cmd()


def test_user_found_when_lookup_path_differs():
    """Панель без /by-username: создание отвечает «already exists» — находим пользователя перебором списка."""
    class P(FakePanel):
        def call(self, method, path, body=None):
            if path.startswith('/api/users/by-username/') or path.startswith('/api/users/get-by/'):
                raise engine.StepError('HTTP 404')
            if (method, path) == ('POST', '/api/users'):
                raise engine.StepError('панель POST /api/users: HTTP 400: User username already exists')
            if path.startswith('/api/users?start='):
                return {'users': [{'uuid': 'u-old', 'username': 'bridge_exitnodeuuid', 'vlessUuid': 'v-old',
                                   'activeInternalSquads': [{'uuid': 'sq1'}]}], 'total': 1}
            return super().call(method, path, body)
    u = xb.ensure_user(P(), EXIT_NODE['uuid'], ['sq1'], lambda t: None)
    assert u['vlessUuid'] == 'v-old'

def test_exit_prefers_direct_tcp_host_over_cdn():
    pl = FakePanel()
    pl.profiles[0]['inbounds'].append({'uuid': 'inb-xh', 'tag': 'XHTTP_CDN'})
    pl.profiles[0]['config']['inbounds'].append({'tag': 'XHTTP_CDN', 'protocol': 'vless',
                                                 'streamSettings': {'network': 'xhttp', 'security': 'tls'}})
    cdn = {'uuid': 'h-cdn', 'address': 'cdn-swe.example.com', 'port': 8443, 'sni': 'cdn-swe.example.com',
           'inbound': {'configProfileUuid': 'prof-exit', 'configProfileInboundUuid': 'inb-xh'}}
    o = xb.exit_outbound(pl, EXIT_NODE, [cdn, *EXIT_HOSTS], 8443, 'vless-1')
    assert o['settings']['vnext'][0]['address'] == 'swe.example.com' and o['streamSettings']['network'] == 'tcp'


def test_reinstall_keeps_manual_sni():
    pl, log = FakePanel(), []
    made = xb.setup_panel(pl, RELAY, EXIT_NODE, EXIT_HOSTS, log.append)
    bridge = next(p for p in pl.profiles if p['uuid'] == 'prof-bridge')
    bridge['config']['inbounds'][0]['streamSettings']['realitySettings']['serverNames'] = ['my.own.site']
    patched = []
    orig = pl.call

    def call(method, path, body=None):
        if (method, path) == ('PATCH', '/api/config-profiles'):
            patched.append(body['config'])
            return {}
        return orig(method, path, body)
    pl.call = call
    again = xb.setup_panel(pl, {**RELAY, 'reality': made['reality']}, EXIT_NODE, EXIT_HOSTS, log.append)
    assert again['sni'] == 'my.own.site'
    assert patched[0]['inbounds'][0]['streamSettings']['realitySettings']['serverNames'] == ['my.own.site']


NV_BASE = {'inbounds': [{'tag': 'Vision-TLS', 'port': 443, 'protocol': 'vless', 'streamSettings': {
    'network': 'tcp', 'security': 'tls', 'tlsSettings': {'certificates': [{
        'certificateFile': '/etc/letsencrypt/live/de1.example.com/fullchain.pem'}]}}}],
    'outbounds': [{'tag': 'DIRECT', 'protocol': 'freedom'}, {'tag': 'BLOCK', 'protocol': 'blackhole'}],
    'routing': {'rules': [{'type': 'field', 'ip': ['geoip:private'], 'outboundTag': 'BLOCK'},
                          {'type': 'field', 'network': 'tcp,udp', 'outboundTag': 'DIRECT'}]}}


def test_vision_config_routes_to_exit_after_blocks():
    import json
    from bot import engine as en
    o = xb.exit_outbound(FakePanel(), EXIT_NODE, EXIT_HOSTS, 8443, 'vless-1')
    tag = xb.vision_tag('203.0.113.50')
    base = json.loads(json.dumps(NV_BASE).replace('Vision-TLS', en.INBOUND_TAG))
    c = xb.vision_config(base, 'de1.example.com', tag, o)
    assert c['inbounds'][0]['tag'] == tag and tag == 'BRIDGE_VISION_203_0_113_50'
    assert en.CERT_LINK in json.dumps(c) and '/live/de1.example.com/' not in json.dumps(c)
    assert c['outbounds'][0]['tag'] == xb.OUT_TAG
    rules = c['routing']['rules']
    assert rules[0]['outboundTag'] == 'BLOCK' and rules[1] == {'type': 'field', 'inboundTag': [tag], 'outboundTag': xb.OUT_TAG}
    assert rules[2]['outboundTag'] == 'DIRECT'      # общее «всё напрямую» — после нашего правила


def test_setup_panel_vision():
    import json
    from bot import engine as en
    pl, log = FakePanel(), []

    orig = pl.call

    def call(method, path, body=None):  # профиль моста Vision: тег — свой
        if (method, path) == ('POST', '/api/config-profiles'):
            p = {'uuid': 'prof-v', 'name': body['name'], 'config': body['config'],
                 'inbounds': [{'uuid': 'inb-v', 'tag': i['tag'], 'port': i['port']} for i in body['config']['inbounds']]}
            pl.profiles.append(p)
            return p
        return orig(method, path, body)
    pl.call = call
    base = json.loads(json.dumps(NV_BASE).replace('Vision-TLS', en.INBOUND_TAG))
    made = xb.setup_panel(pl, {**RELAY, 'mode': 'vision', 'domain': 'de1.example.com', 'base_cfg': base}, EXIT_NODE,
                          EXIT_HOSTS, log.append)
    assert pl.tls is True and made['hosts'] == ['host-entry'] and made['sni'] == 'de1.example.com'
    assert pl.nodes[0]['address'] == 'de1.example.com'
    assert any(p['name'] == 'Bridge V 203-0-113-50' for p in pl.profiles)
    assert not [c for c in pl.calls if c == ('PATCH', '/api/hosts')]   # SNI хоста — домен входа, его ставит attach


def test_bridge_ip_excluded_from_torrent_blocker():
    pl, log = FakePanel(), []
    pl.plugin = {'torrentBlocker': {'enabled': True, 'blockDuration': 3600, 'ignoreLists': {'ip': ['1.1.1.1'], 'userId': []}}}
    made = xb.setup_panel(pl, RELAY, EXIT_NODE, EXIT_HOSTS, log.append)
    ign = pl.plugin['torrentBlocker']['ignoreLists']
    assert ign['ip'] == ['1.1.1.1', '203.0.113.50'] and ign['userId'] == [77]
    assert ('POST', '/api/node-plugins/executor') in pl.calls          # бан с IP входа снят сразу
    n = len(pl.calls)
    pl.profiles = [p for p in pl.profiles if p['uuid'] != 'prof-bridge']
    xb.setup_panel(pl, {**RELAY, 'reality': made['reality']}, EXIT_NODE, EXIT_HOSTS, log.append)
    assert pl.plugin['torrentBlocker']['ignoreLists']['ip'].count('203.0.113.50') == 1
    assert ('PATCH', '/api/node-plugins') not in pl.calls[n:]          # уже в исключениях — не трогаем
    xb.teardown_panel(pl, made, user_in_use=False, log=log.append)
    assert pl.plugin['torrentBlocker']['ignoreLists'] == {'ip': ['1.1.1.1'], 'userId': []}
