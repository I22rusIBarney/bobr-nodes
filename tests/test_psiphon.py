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
