from bot import audit
from bot.store import Store


def test_match_and_describe():
    assert audit.match('DELETE', '/api/profiles/3/nodes/abcd-ef') == ('node.delete', 'Удалил ноду', {'pid': '3', 'uuid': 'abcd-ef'})
    assert audit.match('POST', '/api/profiles/3/check') is None          # проверки не пишем
    assert audit.match('GET', '/api/settings') is None
    d = audit.describe({'name': 'Германия', 'panel_token': 'SECRET', 'password': 'x', 'add_days': 30, 'free': True,
                        'values': {'cf_token': 'y'}})
    assert 'SECRET' not in d and 'password' not in d and 'cf_token' not in d
    assert 'название: Германия' in d and 'дней добавлено: 30' in d and 'бесплатно: да' in d


def test_from_request_and_list(tmp_path):
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'g1', 'panel_url': 'https://p', 'panel_token': 't'})
    audit.from_request(store, 7, 'DELETE', f'/api/profiles/{pid}/nodes/uuid-1', None, 'Иван')
    audit.from_request(store, 7, 'POST', '/api/notify/read', {}, 'Иван')       # не из списка — не пишется
    audit.log(store, 0, 'billing.auto', 'Автопродление подписки', detail='пользователь 9, 300 ₽')
    rows = audit.view(store, store.audit())
    assert [r['label'] for r in rows] == ['Автопродление подписки', 'Удалил ноду']
    assert rows[0]['who'] == 'Бот' and rows[1]['who'] == 'Иван' and rows[1]['group'] == 'g1'
    assert 'нода uuid-1' in rows[1]['detail']
    assert [r['uid'] for r in store.audit(uid=7)] == [7]
    assert len(store.audit(q='300 ₽')) == 1
    assert audit.who_of({'first_name': 'A', 'last_name': 'B'}) == 'A B' and audit.who_of({'username': 'x'}) == '@x'
