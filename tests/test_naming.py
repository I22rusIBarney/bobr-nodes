from bot import naming


def test_auto_names_skip_used_and_existing():
    names = naming.auto_names('{cc}{n}', 'example.com', 'de', 3, used={'de1.example.com'},
                              exists=lambda d: d == 'de3.example.com')
    assert names == ['de2.example.com', 'de4.example.com', 'de5.example.com']


def test_pattern_rules():
    assert naming.check_pattern('{cc}{n}', 'example.com') is None
    assert 'vpn' in naming.check_pattern('vpn-{n}', 'example.com')
    assert 'vpn' in naming.check_pattern('{cc}{n}', 'vpn.example.com')
    assert '{n}' in naming.check_pattern('{cc}', 'example.com')
    assert naming.check_pattern('{cc}_{n}', 'example.com')  # подчёркивание в DNS-имени нельзя


def test_node_name_and_os():
    assert naming.node_name('de1.example.com') == 'de1'
    assert len(naming.node_name('a.example.com')) >= 3
    assert naming.pick_os(['win2022', 'debian12', 'ubuntu2204']) == 'debian12'
    assert naming.pick_os(['ubuntu-24.04', 'debian12']) == 'ubuntu-24.04'
    assert naming.pick_os(['win2022']) is None


def test_cc_for_odd_location_codes():
    assert naming.cc_of('nl') == 'nl'
    assert naming.cc_of('de-fra1') == 'defra'
