"""Кеш «серверы, которые бот купил или поставил» (bot_jobs_by_ip) и занятых доменов: результат тот же, что без кеша,
повторный вызов не ходит в базу, любое изменение задач сбрасывает кеш."""
from bot import services as svc
from bot.store import Store


def _done(store, pid, **data):
    jid = store.add_job('b', pid, 1, data)
    store.update_job(jid, status='done')
    return jid


def test_bot_jobs_by_ip_cached_and_invalidated(tmp_path):
    store = Store(tmp_path / 'db.sqlite')
    pid, other = store.save_profile({'name': 'a'}), store.save_profile({'name': 'b'})
    j1 = _done(store, pid, ip='10.0.0.1', domain='a.ex.com', server_id=1)
    _done(store, other, ip='10.0.0.9', domain='z.ex.com')
    failed = store.add_job('b', pid, 1, {'ip': '10.0.0.2', 'domain': 'f.ex.com'})
    store.update_job(failed, status='failed')
    _done(store, pid, ip='10.0.0.3', kind='upgrade')        # служебная задача — не покупка
    bj = svc.bot_jobs_by_ip(store, pid)
    assert set(bj) == {'10.0.0.1', 'a.ex.com'} and bj['a.ex.com']['_job'] == j1

    calls = []
    real = store._q
    store._q = lambda *a: calls.append(a) or real(*a)
    assert svc.bot_jobs_by_ip(store, pid) == bj and not calls   # повтор — без запроса к базе
    store._q = real

    j2 = _done(store, pid, ip='10.0.0.4', domain='b.ex.com')     # новая покупка — видна сразу
    assert 'b.ex.com' in svc.bot_jobs_by_ip(store, pid)
    store.patch_job_data(j2, removed=True)                       # сервер удалили — больше не «наш»
    assert 'b.ex.com' not in svc.bot_jobs_by_ip(store, pid)
    assert set(svc.bot_jobs_by_ip(store, other)) == {'10.0.0.9', 'z.ex.com'}   # группы не смешиваются


def test_used_domains_cache_follows_changes(tmp_path):
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'a'})
    jid = _done(store, pid, ip='10.0.0.1', domain='a.ex.com')
    assert 'a.ex.com' in store.used_domains()
    store.patch_job_data(jid, removed=True)
    assert 'a.ex.com' not in store.used_domains()
    store.add_job('b', pid, 1, {'domain': 'n.ex.com'})
    assert 'n.ex.com' in store.used_domains()


def test_events_search_by_node_name(tmp_path):
    """История ноды: события, где упомянуты её имя или домен; % и _ в запросе — обычные символы; чужие получатели не видны."""
    store = Store(tmp_path / 'db.sqlite')
    store.add_event('node.ru_down', 1, '🔴 de5 · Основная: не проходит из России', [1])
    store.add_event('node.ru_up', 1, '🟢 de5 · Основная: снова доступна', [1])
    store.add_event('node.ru_down', 1, '🔴 de55 · Основная', [2])
    store.add_event('node.ru_down', 1, '🔴 nl1 · 50% нагрузки', [1])
    assert [e['text'][:4] for e in store.events_for(1, q='de5')] == ['🟢 de', '🔴 de']
    assert len(store.events_for(1, q='50%')) == 1 and store.events_for(1, q='_') == []
