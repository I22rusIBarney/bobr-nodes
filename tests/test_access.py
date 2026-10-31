"""Приглашения и права пользователей (bot/access.py) — без сети."""
import time

from bot.access import Access
from bot.store import Store


def make(tmp_path):
    store = Store(tmp_path / 'bot.db')
    return store, Access(store, {1})


def tg(uid, username=None, first='Имя'):
    return {'id': uid, 'username': username, 'first_name': first}


def test_invite_once(tmp_path):
    store, acc = make(tmp_path)
    inv = acc.create_invite(1, 'Вася', uses=1, days=7)
    assert acc.role(77) is None
    assert acc.redeem(inv['token'], tg(77, 'Vasya')) == (True, 'Доступ открыт.')
    assert acc.role(77) == 'member' and store.user(77)['username'] == 'vasya'
    good, text = acc.redeem(inv['token'], tg(78))
    assert not good and 'уже использовано' in text and acc.role(78) is None
    assert acc.redeem(inv['token'], tg(77))[0]  # повторный вход тем же — не ошибка
    assert store.invite(inv['id'])['used_by'] == [77]


def test_invite_states(tmp_path):
    store, acc = make(tmp_path)
    many = acc.create_invite(1, uses=2, days=0)
    assert acc.redeem(many['token'], tg(10))[0] and acc.redeem(many['token'], tg(11))[0]
    assert not acc.redeem(many['token'], tg(12))[0]
    old = acc.create_invite(1, days=1)
    old['expires_at'] = int(time.time()) - 1
    store.save_invite(old)
    assert 'истёк' in acc.redeem(old['token'], tg(13))[1]
    rev = acc.create_invite(1)
    rev['revoked'] = True
    store.save_invite(rev)
    assert 'отозвано' in acc.redeem(rev['token'], tg(14))[1]
    assert 'не найдено' in acc.redeem('nope', tg(15))[1]
    for bad in ({'uses': 0}, {'uses': 101}, {'days': 400}):
        try:
            acc.create_invite(1, **bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_blocked_and_admin(tmp_path):
    store, acc = make(tmp_path)
    inv = acc.create_invite(1, uses=3)
    acc.redeem(inv['token'], tg(20))
    store.save_user({**store.user(20), 'blocked': True})
    assert acc.role(20) is None and not acc.redeem(inv['token'], tg(20))[0]
    assert acc.role(1) == 'admin' and acc.redeem(inv['token'], tg(1))[0]
    assert store.invite(inv['id'])['used_by'] == [20]  # админ приглашение не тратит


def test_groups_visibility(tmp_path):
    store, acc = make(tmp_path)
    acc.redeem(acc.create_invite(1)['token'], tg(77, 'Vasya_Client'))
    admin_own = {'name': 'Основная'}
    for_vasya_id = {'name': 'Вася ID', 'owner': '77', 'owner_tg_id': '77'}
    for_vasya_name = {'name': 'Вася ник', 'owner': '@vasya_client', 'owner_tg_id': ''}
    vasya_made = {'name': 'Своя', 'created_by': 77, 'owner_tg_id': '77'}
    other = {'name': 'Чужая', 'owner': '@petya', 'owner_tg_id': '88'}
    ids = {}
    for p in (admin_own, for_vasya_id, for_vasya_name, vasya_made, other):
        ids[p['name']] = store.save_profile(p)
    names = {p['name'] for p in acc.profiles_for(77, 'Vasya_Client')}
    assert names == {'Вася ID', 'Вася ник', 'Своя'}
    assert {p['name'] for p in acc.profiles_for(77)} == names  # @username — из сохранённого пользователя
    assert len(acc.profiles_for(1)) == 5 and acc.profiles_for(99) == []
    assert acc.can(store.profile(ids['Своя']), 77) and not acc.can(store.profile(ids['Чужая']), 77)
    # кому писать о группе
    assert acc.recipients(store.profile(ids['Своя'])) == [77]
    assert acc.recipients(store.profile(ids['Вася ID'])) == [1, 77]
    assert acc.recipients(store.profile(ids['Основная'])) == [1]
    store.save_user({**store.user(77), 'blocked': True})
    assert acc.recipients(store.profile(ids['Своя'])) == [1] and acc.profiles_for(77) == []


def test_touch_updates_username(tmp_path):
    store, acc = make(tmp_path)
    acc.redeem(acc.create_invite(1)['token'], tg(77, 'old_name'))
    acc.touch(tg(77, 'New_Name', 'Вася'))
    u = store.user(77)
    assert u['username'] == 'new_name' and u['first_name'] == 'Вася'
    acc.touch(tg(55, 'stranger'))  # не пользователь — ничего не создаём
    assert store.user(55) is None
