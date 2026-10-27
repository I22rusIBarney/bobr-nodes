from bot import services as s


def test_country_language():
    pl = {'code': 'pl', 'name': 'Польша', 'flag': '🇵🇱'}
    assert s.render_remark('{flag} {country} {n}', pl, 'pl3', 'pl3.example.com', 1) == '🇵🇱 Польша 3'
    assert s.render_remark('{flag} {country} {n}', pl, 'pl3', 'pl3.example.com', 1, 'en') == '🇵🇱 Poland 3'
    assert s.country_name({'code': 'de-fra1', 'name': 'Германия'}, 'en') == 'Germany'
    assert s.country_name({'code': 'xx', 'name': 'Своя локация'}, 'en') == 'Своя локация'  # не ISO — как в bcloud
