import pytest

from bot import engine as e


def test_rules_and_regions():
    assert e.psiphon_rules('') == e.PSI_DOMAINS
    assert e.psiphon_rules('Google.com, geosite:youtube\nfull:gemini.google.com google.com') == [
        'domain:google.com', 'geosite:youtube', 'full:gemini.google.com']
    with pytest.raises(e.StepError):
        e.psiphon_rules('bad domain;rm')
    assert e.psiphon_regions(' de, nl ') == 'DE,NL'
    assert e.psiphon_regions('') == ''
    with pytest.raises(e.StepError):
        e.psiphon_regions('germany')


def test_add_psiphon_keeps_blocks_and_is_idempotent():
    cfg = {'outbounds': [{'tag': 'DIRECT', 'protocol': 'freedom'}, {'tag': 'BLOCK', 'protocol': 'blackhole'}],
           'routing': {'rules': [{'type': 'field', 'ip': ['geoip:private'], 'outboundTag': 'BLOCK'}]}}
    out = {'address': '172.17.0.1', 'port': 1080}
    a = e.add_psiphon(cfg, out, ['domain:google.com'])
    b = e.add_psiphon(a, out, ['domain:google.com'])
    assert a == b and 'psiphon-out' not in str(cfg)  # исходник не тронут, повтор не дублирует
    assert a['routing']['rules'][0]['outboundTag'] == 'BLOCK'
    assert a['routing']['rules'][-1] == {'type': 'field', 'domain': ['domain:google.com'], 'network': 'tcp',
                                         'outboundTag': 'psiphon-out'}
    assert e.psiphon_of(a) == out and e.psiphon_of(cfg) is None
    # выход в форме из README (address/port прямо в settings) тоже распознаётся
    assert e.psiphon_of({'outbounds': [{'tag': 'psiphon-out', 'settings': {'address': '127.0.0.1', 'port': 1081}}]}) == \
        {'address': '127.0.0.1', 'port': 1081}


def test_variant_names_are_canonical():
    assert e.variant('NV Vision', ['Vision-NV'], {'psiphon'}) == ('NV Vision Psiphon', {'Vision-NV': 'Vision-NV-PS'})
    assert e.variant('NV Vision', ['Vision-NV'], {'warp', 'psiphon'}) == ('NV Vision Psiphon WARP', {'Vision-NV': 'Vision-NV-PS-WP'})
    # с уже существующего варианта: суффиксы снимаются, порядок тот же
    assert e.variant('NV Vision WARP', ['Vision-NV-WP'], {'warp', 'psiphon'}) == \
        ('NV Vision Psiphon WARP', {'Vision-NV-WP': 'Vision-NV-PS-WP'})
    name, tags = e.variant('Очень длинное имя профиля', ['A', 'B'], {'psiphon', 'warp'})
    assert len(name) <= 29 and name.endswith(' PS WP') and tags == {'A': 'A-PS-WP', 'B': 'B-PS-WP'}


def test_warp_mask_port():
    cfg = e.add_warp({'outbounds': [], 'routing': {'rules': []}}, ['geosite:tiktok'])
    assert e.warp_of(cfg) and e.features_of(cfg) == {'warp'}
    assert cfg['outbounds'][0]['streamSettings'] == {'sockopt': {'mark': 255}}
    assert e.final_mask(None) == e.FINAL_MASK and e.final_mask('{"tcp": []}') == {'tcp': []}
    with pytest.raises(e.StepError):
        e.final_mask('[1]')
    assert e.public_port('') is None and e.public_port(443) is None and e.public_port('8443') == 8443
    for bad in ('22', '2222', 'x', '70000'):
        with pytest.raises(e.StepError):
            e.public_port(bad)


def _vision():
    return {'inbounds': [{'tag': 'Vision-NV', 'port': 443, 'protocol': 'vless', 'sniffing': {'enabled': True},
                          'streamSettings': {'network': 'tcp', 'security': 'tls', 'tlsSettings': {
                              'minVersion': '1.3', 'certificates': [{'certificateFile': '/c/fullchain.pem', 'keyFile': '/c/privkey.pem'}]}}}],
            'outbounds': [], 'routing': {'rules': []}}


def test_xhttp_inbound_and_variant():
    cfg = e.add_xhttp(_vision())
    x = e.xhttp_of(cfg)
    ib = cfg['inbounds'][-1]
    assert x['tag'] == 'XHTTP-Vision-NV' and x['port'] == e.XHTTP_PORT and len(x['path']) == 17
    assert ib['streamSettings']['network'] == 'xhttp' and ib['streamSettings']['tlsSettings']['certificates'][0]['keyFile'] == '/c/privkey.pem'
    assert e.features_of(cfg) == {'xhttp'}
    # повтор и вариант с ещё одним дополнением: путь тот же (подписки клиентов не ломаются), inbound не дублируется
    again = e.add_xhttp(cfg)
    assert e.xhttp_of(again)['path'] == x['path'] and len(again['inbounds']) == 2
    name, tmap = e.variant('NV Vision XHTTP', ['Vision-NV-XH', 'XHTTP-Vision-NV-XH'], {'xhttp', 'warp'})
    assert name == 'NV Vision WARP XHTTP' and tmap == {'Vision-NV-XH': 'Vision-NV-WP-XH', 'XHTTP-Vision-NV-XH': 'XHTTP-Vision-NV-WP-XH'}
    assert e._strip_extras('NV Vision WARP XHTTP', ['Vision-NV-WP-XH', 'XHTTP-Vision-NV-WP-XH']) == ('NV Vision', ['Vision-NV', 'XHTTP-Vision-NV'])
    with pytest.raises(e.StepError):  # у Reality нет своего сертификата
        e.add_xhttp({'inbounds': [{'tag': 'R', 'port': 443, 'streamSettings': {'security': 'reality'}}]})
    with pytest.raises(e.StepError):
        e.add_xhttp(_vision(), port=443)
