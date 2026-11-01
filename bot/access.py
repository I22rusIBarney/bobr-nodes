"""Кто чем пользуется: администраторы (ADMIN_IDS) — всем; приглашённые пользователи — своими группами.

Группа пользователя — та, что он создал сам (created_by), или та, что администратор завёл для него:
клиент Bobr Cloud группы — это он (по Telegram ID или @username). Пользователь не видит чужих групп, настроек бота,
«Моих скриптов», оформления и безопасности сервера; покупает только со своего баланса Bobr Cloud и только через свой
токен Cloudflare (у своих групп), чекеры добавляет общие для всех.
"""
from __future__ import annotations

import secrets
import threading
import time

from .store import Store

INVITE_PREFIX = 'inv_'
_redeem_lock = threading.Lock()


def uname(u) -> str:
    """@username из пользователя Telegram (dict из initData или объект aiogram) — в нижнем регистре, без @."""
    v = u.get('username') if isinstance(u, dict) else getattr(u, 'username', None)
    return (v or '').lower().lstrip('@')


class Access:
    def __init__(self, store: Store, admins: set[int]):
        self.store, self.admins = store, set(admins)

    # ── роли ──
    def is_admin(self, uid: int) -> bool:
        return int(uid or 0) in self.admins

    def member(self, uid: int) -> dict | None:
        u = self.store.user(int(uid or 0))
        return u if u and not u.get('blocked') else None

    def paused(self, uid: int) -> bool:
        """Подписка закончилась и не продлена: доступен только экран «Продлить»."""
        from . import billing
        return billing.paused(self.store, uid)

    def active(self, uid: int) -> dict | None:
        """Пользователь, который сейчас может работать с ботом (приглашён, не закрыт, не на паузе)."""
        u = self.member(uid)
        return u if u and not self.paused(uid) else None

    def role(self, uid: int) -> str | None:
        if self.is_admin(uid):
            return 'admin'
        if not self.member(uid):
            return None
        return 'paused' if self.paused(uid) else 'member'

    def touch(self, user) -> None:
        """Обновить имя и @username пользователя — по ним видно группы, которые для него завёл администратор."""
        uid = user.get('id') if isinstance(user, dict) else getattr(user, 'id', 0)
        u = self.store.user(int(uid or 0))
        if not u:
            return
        first = (user.get('first_name') if isinstance(user, dict) else getattr(user, 'first_name', '')) or ''
        new = {'username': uname(user), 'first_name': first[:64]}
        if any(u.get(k) != v for k, v in new.items()):
            self.store.save_user({**u, **new})

    # ── группы ──
    def owns(self, prof: dict, uid: int, username: str = '') -> bool:
        """Группа пользователя: создал сам или клиент Bobr Cloud группы — он."""
        uid = int(uid or 0)
        if not uid:
            return False
        if str(prof.get('created_by') or '') == str(uid) or str(prof.get('owner_tg_id') or '') == str(uid):
            return True
        owner = str(prof.get('owner') or '').lower()
        if owner == str(uid):
            return True
        username = (username or (self.store.user(uid) or {}).get('username') or '').lower().lstrip('@')
        return bool(username) and owner == '@' + username

    def can(self, prof: dict | None, uid: int, username: str = '') -> bool:
        if not prof:
            return False
        if self.is_admin(uid):
            return True
        return bool(self.active(uid)) and self.owns(prof, uid, username)

    def profiles_for(self, uid: int, username: str = '') -> list[dict]:
        ps = self.store.profiles()
        if self.is_admin(uid):
            return ps
        if not self.active(uid):
            return []
        return [p for p in ps if self.owns(p, uid, username)]

    def recipients(self, prof: dict) -> list[int]:
        """Кому писать о группе: свою группу пользователь ведёт сам; группу, заведённую администратором для
        пользователя, видят и администраторы, и он. Пользователя удалили — группа снова на администраторах."""
        out: list[int] = []
        made = prof.get('created_by')
        if made and self.member(int(made)):
            return [] if self.paused(int(made)) else [int(made)]  # на паузе — бот о его группе молчит
        out += sorted(self.admins)
        for cand in (prof.get('owner_tg_id'),):
            if cand and str(cand).isdigit() and self.active(int(cand)) and int(cand) not in out:
                out.append(int(cand))
        return out

    def main_chat(self, prof: dict) -> int:
        """Куда писать ход задач, которые бот запускает сам (починка в мониторинге)."""
        r = self.recipients(prof)
        return r[0] if r else (sorted(self.admins)[0] if self.admins else 0)

    # ── приглашения ──
    def create_invite(self, by: int, note: str = '', uses: int = 1, days: int = 7, access_days: int | None = None) -> dict:
        """days — сколько живёт ссылка; access_days — сколько дней бесплатного доступа у вошедших (None — бессрочно)."""
        if not 1 <= int(uses) <= 100:
            raise ValueError('Сколько раз можно использовать: от 1 до 100')
        if not 0 <= int(days) <= 365:
            raise ValueError('Срок: от 0 (бессрочно) до 365 дней')
        if access_days is not None and not 1 <= int(access_days) <= 3660:
            raise ValueError('Бесплатный доступ: от 1 дня или бессрочно')
        token = secrets.token_urlsafe(12).replace('-', 'x').replace('_', 'y')
        now = int(time.time())
        data = {'note': str(note or '').strip()[:60], 'max_uses': int(uses), 'used_by': [], 'created_by': int(by),
                'created_at': now, 'expires_at': now + int(days) * 86400 if int(days) else 0, 'revoked': False,
                'access_days': int(access_days) if access_days else None}
        iid = self.store.add_invite(token, data)
        return {**data, 'id': iid, 'token': token}

    @staticmethod
    def invite_state(inv: dict, now: float | None = None) -> str:
        now = now if now is not None else time.time()
        if inv.get('revoked'):
            return 'revoked'
        if inv.get('expires_at') and now > inv['expires_at']:
            return 'expired'
        if len(inv.get('used_by') or []) >= int(inv.get('max_uses') or 1):
            return 'used'
        return 'active'

    def redeem(self, token: str, user) -> tuple[bool, str]:
        """Пользователь открыл бота по ссылке-приглашению. (True, текст) — теперь у него есть доступ."""
        uid = int(user.get('id') if isinstance(user, dict) else getattr(user, 'id', 0))
        if self.is_admin(uid):
            return True, 'Вы администратор — приглашение не нужно.'
        with _redeem_lock:
            inv = self.store.invite(token=token)
            cur = self.store.user(uid)
            if cur and cur.get('blocked'):
                return False, 'Доступ к боту для вас закрыт администратором.'
            if cur:
                return True, 'У вас уже есть доступ.'
            st = self.invite_state(inv) if inv else 'missing'
            if st != 'active':
                return False, {'missing': 'Приглашение не найдено.', 'revoked': 'Приглашение отозвано.',
                               'expired': 'Срок приглашения истёк.', 'used': 'Приглашение уже использовано.'}[st] \
                    + ' Попросите у администратора новое.'
            inv['used_by'] = [*inv.get('used_by', []), uid]
            self.store.save_invite(inv)
            first = (user.get('first_name') if isinstance(user, dict) else getattr(user, 'first_name', '')) or ''
            now = int(time.time())
            days = inv.get('access_days')
            self.store.save_user({'tg_id': uid, 'username': uname(user), 'first_name': first[:64],
                                  'invited_by': inv.get('created_by'), 'invite_id': inv['id'], 'note': inv.get('note', ''),
                                  'created_at': now, 'blocked': False,
                                  'access_until': now + int(days) * 86400 if days else None})
        return True, 'Доступ открыт.'
