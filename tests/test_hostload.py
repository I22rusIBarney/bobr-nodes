"""Нагрузка сервера бота (bot/hostload.py)."""
import time

from bot.hostload import HostLoad


def test_sample_and_history(tmp_path):
    (tmp_path / 'bot.db').write_bytes(b'x' * 100)
    (tmp_path / 'logs').mkdir()
    (tmp_path / 'logs' / 'job-1.log').write_bytes(b'y' * 50)
    h = HostLoad(tmp_path)
    time.sleep(0.2)
    s = h.sample()
    assert s['cores'] >= 1 and 0 <= s['mem'] <= 100 and 0 <= s['disk'] <= 100 and s['bot_rss'] > 0
    assert s['db'] == 100 and s['logs'] == 50 and len(h.history) == 1
    h.sample()
    assert len(h.history) == 1  # история — по 5 минут


def test_verdict(tmp_path):
    h = HostLoad(tmp_path)
    now = time.time()
    h.now = {'disk': 50, 'swap': 0}
    h.history = [{'t': now - 600, 'cpu': 20, 'mem': 50}, {'t': now - 300, 'cpu': 30, 'mem': 55}]
    assert h.verdict() == ('ok', [])
    h.history = [{'t': now - 600, 'cpu': 90, 'mem': 50}, {'t': now - 300, 'cpu': 86, 'mem': 55}]
    level, why = h.verdict()
    assert level == 'warn' and 'процессор' in why[0]
    h.history = [{'t': now - 300, 'cpu': 97, 'mem': 96}]
    h.now = {'disk': 98, 'swap': 2 ** 30}
    level, why = h.verdict()
    assert level == 'bad' and len(why) == 4
    h.history = [{'t': now - 7200, 'cpu': 99, 'mem': 99}]  # старше часа — не в счёт
    h.now = {'disk': 10}
    assert h.verdict()[0] == 'ok'
