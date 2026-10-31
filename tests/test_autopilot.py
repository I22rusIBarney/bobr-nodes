"""Автопилот (bot/autopilot.py): расписание, замеры нагрузки, настройки, риски аренды."""
import datetime as dt

import pytest

from bot import autopilot as ap

OUT = ('CPU1 cpu 100 0 100 1000 0 0 0 0 0 0\nCPU2 cpu 190 0 105 1005 0 0 0 0 0 0\n'
       'MEM 1000 50\nDISK 40%\nNET lo 999 999\nNET eth0 2000000 3000000\nNET docker0 5 5\nNPROC 2\n')


def ts(y, m, d, h, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=dt.timezone.utc).timestamp()


def test_parse_load_and_rates():
    prev = {'at': 100.0, 'rx_total': 1000000, 'tx_total': 2000000}
    s = ap.parse_load(OUT, prev, 110.0)
    assert s['cpu'] == 95 and s['mem'] == 95 and s['disk'] == 40 and s['cores'] == 2
    assert s['rx'] == 0.8 and s['tx'] == 0.8  # 1 МБ за 10 с = 0.8 Мбит/с; lo и docker не считаем
    assert ap.hot(s) == ['cpu', 'mem'] and 'процессор 95%' in ap.load_text(s)
    assert 'rx' not in ap.parse_load(OUT, None, 110.0)


def test_due_local_hour_and_weekday():
    now = ts(2026, 10, 31, 2)  # 09:00 в UTC+7, суббота
    assert ap.due(9, 420, '', None, now) == '2026-10-31'
    assert ap.due(9, 420, '2026-10-31', None, now) is None  # сегодня уже было
    assert ap.due(8, 420, '', None, now) is None
    assert ap.due(9, 420, '', 5, now) == '2026-10-31' and ap.due(9, 420, '', 0, now) is None


def test_clean_auto():
    a = ap.clean_auto({'update': True, 'hour': 4, 'weekday': None, 'tz': 420, 'load': False, 'renew': True}, {})
    assert a == {'update': True, 'hour': 4, 'weekday': None, 'tz': 420, 'load': False, 'renew': True}
    for bad in ({'hour': 24}, {'weekday': 7}, {'tz': 9999}, {'hour': True}):
        with pytest.raises(ValueError):
            ap.clean_auto(bad, {})
    assert ap.auto_cfg({})['load'] is True and ap.auto_cfg({})['update'] is False


def test_rent_risks():
    r = {'balance_kop': 50000, 'servers': [
        {'id': 1, 'days_left': 2, 'price_kop': 30000, 'auto_renew': True},
        {'id': 2, 'days_left': 1, 'price_kop': 30000, 'auto_renew': True},
        {'id': 3, 'days_left': 2, 'price_kop': 30000, 'auto_renew': False},
        {'id': 4, 'days_left': 20, 'price_kop': 30000, 'auto_renew': False}]}
    risks = {x['id']: x['why'] for x in ap.Autopilot.rent_risks(r)}
    assert set(risks) == {1, 2, 3} and 'выключено' in risks[3] and 'нужно 600' in risks[1]
    r['balance_kop'] = 100000
    assert {x['id'] for x in ap.Autopilot.rent_risks(r)} == {3}


def test_load_hist_keeps_a_day():
    old = [{'t': 10, 'cpu': 1}, {'t': 90_000, 'cpu': 2}]
    h = ap.load_hist(old, {'cpu': 50, 'mem': 40, 'rx': 1.5, 'tx': 0.5, 'disk': 30}, 100_000.0)
    assert [x['t'] for x in h] == [90_000, 100_000]
    assert h[-1] == {'t': 100_000, 'cpu': 50, 'mem': 40, 'rx': 1.5, 'tx': 0.5}
    assert len(ap.load_hist([{'t': 99_000 + i} for i in range(300)], {}, 100_000.0)) == 200
    assert ap.load_hist(None, {'cpu': 1}, 5.0) == [{'t': 5, 'cpu': 1, 'mem': None, 'rx': None, 'tx': None}]
