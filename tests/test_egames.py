"""Ноды и мосты «как в eGames»: команда установки, профиль, выход и вход в панели."""
from bot import egames as eg
from bot import xbridge as xb
from tests.test_xbridge import EXIT_HOSTS, EXIT_NODE, RELAY, FakePanel


def test_profile_like_egames():
    cfg = eg.profile_config('n1.example.com', 'STEAL_1_2_3_4', 'priv', 'abcd1234abcd1234')
    inb = cfg['inbounds'][0]
    rs = inb['streamSettings']['realitySettings']
    assert inb['port'] == 443 and inb['streamSettings']['security'] == 'reality'
    assert rs['dest'] == '/dev/shm/nginx.sock' and rs['xver'] == 1 and rs['serverNames'] == ['n1.example.com']
    assert {'protocol': ['bittorrent'], 'outboundTag': 'BLOCK', 'type': 'field'} in cfg['routing']['rules']
    assert eg.tag_for('1.2.3.4') == 'STEAL_1_2_3_4' and eg.tag_for('1.2.3.4', True) == 'BRIDGE_EG_1_2_3_4'


def test_install_cmd():
    c = eg.install_cmd('n1.example.com', 'a@b.c', 'SECRET', 2222, ['5.6.7.8'], [9999])
    assert 'certbot certonly --standalone' in c and 'EG_OK' in c and 'ufw allow 9999' in c
    assert '5.6.7.8' in c and 'SECRET' not in c   # секрет — внутри base64 compose
    assert 'unix:/dev/shm/nginx.sock ssl proxy_protocol' in eg.nginx_conf('n1.example.com')
    assert 'SECRET_KEY=SECRET' in eg.compose_text('SECRET', 2222)


def test_bridge_entry_egames_with_ss():
    pl, log = FakePanel(), []
    pnode = {**EXIT_NODE, 'configProfile': {'activeConfigProfileUuid': 'prof-exit', 'activeInbounds': [{'uuid': 'inb-exit'}]}}
    pl.nodes.append(pnode)
    orig = pl.call

    def call(method, path, body=None):
        if (method, path) == ('PATCH', '/api/config-profiles'):
            prof = next(p for p in pl.profiles if p['uuid'] == body['uuid'])
            prof['config'] = body['config']
            prof['inbounds'] = [{'uuid': 'inb-' + i['tag'], 'tag': i['tag'], 'port': i.get('port')} for i in body['config']['inbounds']]
            return prof
        if (method, path) == ('PATCH', '/api/nodes'):
            return {}
        if (method, path) == ('POST', '/api/internal-squads'):
            sq = {'uuid': 'sq-ss', 'name': body['name'], 'inbounds': [{'uuid': u} for u in body['inbounds']]}
            pl.squads.append(sq)
            return sq
        if (method, path) == ('POST', '/api/users'):
            return {**orig(method, path, body), 'ssPassword': 'pw'}
        return orig(method, path, body)
    pl.call = call
    x = {**RELAY, 'mode': 'egames', 'hop': 'ss', 'domain': 'de1.example.com', 'sni': ''}
    made = xb.setup_panel(pl, x, pnode, EXIT_HOSTS, log.append)
    bridge = next(p for p in pl.profiles if p['name'] == 'Bridge EG 203-0-113-50')['config']
    inb = bridge['inbounds'][0]
    assert inb['tag'] == 'BRIDGE_EG_203_0_113_50' and inb['streamSettings']['realitySettings']['serverNames'] == ['de1.example.com']
    assert bridge['outbounds'][0]['protocol'] == 'shadowsocks'
    rules = bridge['routing']['rules']
    assert [r.get('outboundTag') for r in rules] == ['BLOCK', 'BLOCK', 'DIRECT', 'DIRECT', xb.OUT_TAG]
    assert pl.nodes[-1]['address'] == 'de1.example.com' and pl.tls is False
    assert made['sni'] == 'de1.example.com' and made['reality']['private'] and len(made['reality']['sid']) == 16
    assert pl.attached_node['host']['final_mask']['tcp'][0]['type'] == 'fragment'   # Final Mask по умолчанию


class ExitPanel:
    """Панель с нодой-выходом на прежнем профиле (Vision), её хостом и сквадом."""
    def __init__(self):
        self.profiles = [{'uuid': 'p-old', 'name': 'NV', 'inbounds': [{'uuid': 'i-old', 'tag': 'Vision'}], 'config': {}}]
        self.squads = [{'uuid': 'sq1', 'name': 'Default-Squad', 'inbounds': [{'uuid': 'i-old'}]}]
        self.hosts = [{'uuid': 'h-old', 'remark': 'Sweden', 'address': 'swe.example.com',
                       'inbound': {'configProfileUuid': 'p-old', 'configProfileInboundUuid': 'i-old'}},
                      {'uuid': 'h-other', 'remark': 'Other node', 'address': 'nl.example.com',
                       'inbound': {'configProfileUuid': 'p-old', 'configProfileInboundUuid': 'i-old'}}]
        self.calls, self.attached = [], None

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if (method, path) == ('GET', '/api/internal-squads'):
            return {'internalSquads': self.squads}
        if (method, path) == ('GET', '/api/config-profiles'):
            return {'configProfiles': self.profiles}
        if (method, path) == ('POST', '/api/config-profiles'):
            p = {'uuid': 'p-eg', 'name': body['name'], 'config': body['config'],
                 'inbounds': [{'uuid': 'i-eg', 'tag': i['tag'], 'port': 443} for i in body['config']['inbounds']]}
            self.profiles.append(p)
            return p
        if (method, path) == ('GET', '/api/hosts'):
            return self.hosts
        if (method, path) == ('PATCH', '/api/hosts'):
            return {}
        raise AssertionError((method, path))

    def attach(self, node, prof, inbounds, squads, node_port, log, tls=True, also=()):
        self.attached = (node, prof['uuid'], [i['uuid'] for i in inbounds], squads, tls)
        self.hosts.append({'uuid': 'h-eg', 'address': node['domain'],
                           'inbound': {'configProfileUuid': prof['uuid'], 'configProfileInboundUuid': inbounds[0]['uuid']}})


def test_setup_exit_switches_node_and_disables_old_hosts():
    pl, log = ExitPanel(), []
    pnode = {'uuid': 'n-swe', 'name': 'Sweden', 'address': 'swe.example.com', 'countryCode': 'SE',
             'configProfile': {'activeConfigProfileUuid': 'p-old', 'activeInbounds': [{'uuid': 'i-old'}]}}
    made = eg.setup_exit(pl, pnode, '62.0.0.1', 'swe.example.com', 2222, None, log.append)
    node, prof, inbs, squads, tls = pl.attached
    assert prof == 'p-eg' and inbs == ['i-eg'] and squads == ['sq1'] and tls is False and node['domain'] == 'swe.example.com'
    assert ('PATCH', '/api/hosts', {'uuid': 'h-old', 'isDisabled': True}) in pl.calls
    assert ('PATCH', '/api/hosts', {'uuid': 'h-other', 'isDisabled': True}) not in pl.calls   # чужая нода того же профиля
    assert ('PATCH', '/api/hosts', {'uuid': 'h-eg', 'sni': 'swe.example.com', 'fingerprint': 'chrome'}) in pl.calls
    assert made['inbound'] == 'i-eg' and made['reality']['private']


def test_custom_port_and_final_mask():
    c = eg.install_cmd('n1.example.com', 'a@b.c', 'S', 2222, [], (), 8443)
    assert 'for p in 8443 80' in c and 'ufw allow 8443/tcp' in c
    assert eg.profile_config('n1.example.com', 'T', 'k', 's', 8443)['inbounds'][0]['port'] == 8443
    assert ':8443:127.0.0.1 https://n1.example.com:8443' in eg.check_cmd('n1.example.com', 8443)
