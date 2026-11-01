import re

from bot import selfupdate as su
from bot.store import Store


def test_changelog_is_ordered_and_well_formed():
    log = su.changelog()
    assert log, 'bot/changelog.json пуст или не читается'
    vs = [su.vkey(r['version']) for r in log]
    assert vs == sorted(vs, reverse=True) and len(set(vs)) == len(vs), 'версии — по убыванию, без повторов'
    for r in log:
        assert re.fullmatch(r'\d+\.\d+', r['version'])
        assert re.fullmatch(r'\d{4}-\d{2}-\d{2}', r['date'])
        assert r['title'] and r['items']
        assert all(i['kind'] in ('new', 'better', 'fix') and i['text'] for i in r['items'])
    dates = [r['date'] for r in log]
    assert dates == sorted(dates, reverse=True)


def test_whats_new_once_and_only_newer(tmp_path):
    store = Store(tmp_path / 'db.sqlite')
    first = su.whats_new(store)                       # первый запуск с версиями — только текущая
    assert su.current() in first and first.count('\n<b>') == 1
    assert su.whats_new(store) is None                # второй раз — молчим
    log = su.changelog()
    if len(log) > 2:
        store.set('seen_version', log[2]['version'])
        note = su.whats_new(store)
        assert log[0]['version'] in note and log[1]['version'] in note and f'<b>{log[2]["version"]} —' not in note


def test_version_reads_build(tmp_path):
    (tmp_path / 'version.txt').write_text('0123456789\t01.11.2026 12:12\tMerge\n', encoding='utf-8')
    v = su.version(str(tmp_path))
    assert v['number'] == su.current() and v['sha'] == '0123456'
