"""Центр уведомлений: все события бота проходят здесь.

Каждое событие записывается в ленту (колокольчик в панели управления) для своих получателей, а в Telegram
уходит по настройкам каждого получателя: у события три режима — «в Telegram», «только в колокольчике», «выключено».
Тихие часы (по умолчанию 23:00–08:00): в Telegram доходит только критичное, остальное — в колокольчик.
Кому: события группы — тем, кто ведёт группу (Access.recipients), системные и безопасность — администраторам.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
import re
import time

log = logging.getLogger(__name__)

TG, BELL, OFF = 'tg', 'bell', 'off'

CATEGORIES = {
    'nodes': 'Ноды', 'panels': 'Панели', 'tasks': 'Задачи', 'money': 'Деньги', 'security': 'Безопасность', 'system': 'Бот',
}

# тип: (категория, название, режим по умолчанию, критичное — доходит и в тихие часы, только для администраторов)
TYPES: dict[str, tuple[str, str, str, bool, bool]] = {
    'node.down':        ('nodes', 'Нода потеряла связь с панелью', TG, True, False),
    'node.up':          ('nodes', 'Нода снова на связи с панелью', TG, False, False),
    'node.ru_down':     ('nodes', 'Нода недоступна из России', TG, True, False),
    'node.ru_up':       ('nodes', 'Нода снова доступна из России', TG, False, False),
    'node.repair':      ('nodes', 'Починка: шаги и итог', TG, False, False),
    'node.load':        ('nodes', 'Нагрузка на ноде', TG, False, False),
    'node.version':     ('nodes', 'Новая версия ноды или Xray', BELL, False, False),
    'panel.down':       ('panels', 'Панель не отвечает', TG, True, False),
    'panel.up':         ('panels', 'Панель снова отвечает', TG, False, False),
    'panel.restart':    ('panels', 'Панель перезапустилась', BELL, False, False),
    'panel.version':    ('panels', 'Панель обновилась', TG, False, False),
    'task.autoupdate':  ('tasks', 'Итог автообновления нод', TG, False, False),
    'task.removed':     ('tasks', 'Сервер удалён в Bobr Cloud', TG, False, False),
    'money.rent':       ('money', 'Скоро конец аренды', TG, True, False),
    'money.cleanup':    ('money', 'Можно убрать лишнее', TG, False, False),
    'money.balance':    ('money', 'Прогноз: деньги скоро кончатся', TG, False, False),
    'money.scale':      ('money', 'Автомасштабирование: докупка нод', TG, False, False),
    'billing.notice':   ('money', 'Подписка BobrNet: напоминания и оплата', TG, True, False),
    'security.login':   ('security', 'Вход по SSH на сервер бота', TG, True, True),
    'security.brute':   ('security', 'Подбор паролей к серверу бота', TG, False, True),
    'system.host':      ('system', 'Серверу бота не хватает ресурсов', TG, False, True),
    'system.update':    ('system', 'Бот обновился', TG, False, True),
    'system.update_available': ('system', 'Вышла новая версия бота', TG, False, True),
    'report.new':       ('system', 'Ошибки и отзывы пользователей', TG, False, True),
    'report.reply':     ('tasks', 'Ответ на ваш отчёт о проблеме', TG, False, False),
    'survey.ask':       ('tasks', 'Опрос: всё ли работает после установки', TG, False, False),
}
MODES = (TG, BELL, OFF)
QUIET_DEF = {'on': True, 'from': 23, 'to': 8}


def plain(text: str) -> str:
    """Текст для ленты: без HTML-разметки Telegram."""
    return html.unescape(re.sub(r'<[^>]+>', '', text)).strip()


class Notifier:
    def __init__(self, store, bot, access=None, admins=None):
        self.store, self.bot, self.access = store, bot, access
        self.admins = set(admins if admins is not None else (access.admins if access is not None else ()))

    # ───────── настройки получателя ─────────
    def prefs(self, uid: int) -> dict:
        """Настройки пользователя с умолчаниями: режим каждого типа, тихие часы, часовой пояс."""
        try:
            raw = json.loads(self.store.get(f'notif:{uid}') or '{}')
        except ValueError:
            raw = {}
        modes = {t: (raw.get('types') or {}).get(t, d[2]) for t, d in TYPES.items()}
        modes = {t: m if m in MODES else TYPES[t][2] for t, m in modes.items()}
        q = {**QUIET_DEF, **(raw.get('quiet') or {})}
        tz = raw.get('tz')
        if tz is None:  # пояс ещё не сохранён — из утренней сводки, если она настроена; иначе тихих часов нет
            try:
                tz = json.loads(self.store.get(f'summary:{uid}') or '{}').get('tz')
            except ValueError:
                tz = None
        return {'types': modes, 'quiet': {'on': bool(q['on']), 'from': int(q['from']) % 24, 'to': int(q['to']) % 24},
                'tz': None if tz is None else int(tz)}

    def save_prefs(self, uid: int, patch: dict) -> dict:
        cur = self.prefs(uid)
        types = dict(cur['types'])
        for t, m in (patch.get('types') or {}).items():
            if t not in TYPES or m not in MODES:
                raise ValueError('Неизвестный тип или режим уведомления')
            types[t] = m
        for cat, m in (patch.get('categories') or {}).items():  # вся категория разом
            if cat not in CATEGORIES or m not in MODES:
                raise ValueError('Неизвестная категория или режим')
            types.update({t: m for t, d in TYPES.items() if d[0] == cat})
        q = {**cur['quiet'], **(patch.get('quiet') or {})}
        for k in ('from', 'to'):
            if not 0 <= int(q[k]) <= 23:
                raise ValueError('Час: от 0 до 23')
        tz = patch.get('tz', cur['tz'])
        tz = None if tz is None else int(tz)
        out = {'types': {t: m for t, m in types.items() if m != TYPES[t][2]},  # храним только отличия от умолчаний
               'quiet': {'on': bool(q['on']), 'from': int(q['from']), 'to': int(q['to'])}}
        if tz is not None:
            out['tz'] = tz
        self.store.set(f'notif:{uid}', json.dumps(out))
        return self.prefs(uid)

    @staticmethod
    def quiet_now(p: dict, now: float | None = None) -> bool:
        q = p['quiet']
        if not q['on'] or q['from'] == q['to'] or p['tz'] is None:  # пояс неизвестен — не угадываем ночь
            return False
        h = (dt.datetime.fromtimestamp(now if now is not None else time.time(), dt.timezone.utc)
             + dt.timedelta(minutes=p['tz'])).hour
        return q['from'] <= h < q['to'] if q['from'] < q['to'] else h >= q['from'] or h < q['to']

    # ───────── отправка ─────────
    def recipients(self, prof: dict | None, type_: str) -> list[int]:
        if TYPES[type_][4] or not prof:
            return sorted(self.admins)
        if self.access is not None:
            return self.access.recipients(prof)
        return sorted(self.admins)

    async def emit(self, type_: str, text: str, prof: dict | None = None, markup=None, to: list[int] | None = None,
                   tg: bool = True, now: float | None = None) -> list[int]:
        """Записать событие в ленту и отправить в Telegram тем, у кого это включено. tg=False — только лента
        (например, нода заглушена в мониторинге). Возвращает, кому ушло сообщение."""
        if type_ not in TYPES:
            raise KeyError(type_)
        rcpt = list(dict.fromkeys(to if to is not None else self.recipients(prof, type_)))
        if not rcpt:
            return []
        prefs = {uid: self.prefs(uid) for uid in rcpt}
        feed = [uid for uid in rcpt if prefs[uid]['types'][type_] != OFF]
        if feed:
            self.store.add_event(type_, prof['id'] if prof else None, plain(text), feed, now)
        sent = []
        if not tg:
            return sent
        crit = TYPES[type_][3]
        for uid in feed:
            p = prefs[uid]
            if p['types'][type_] != TG or (not crit and self.quiet_now(p, now)):
                continue
            try:
                await self.bot.send_message(uid, text, parse_mode='HTML', reply_markup=markup, disable_web_page_preview=True)
                sent.append(uid)
            except Exception as e:  # noqa: BLE001 — получатель мог не запускать бота или заблокировать его
                log.info('notify %s %s: %s', type_, uid, e)
        return sent

    # ───────── лента ─────────
    def feed(self, uid: int, cat: str = '', before: int | None = None, limit: int = 50) -> dict:
        types = [t for t, d in TYPES.items() if d[0] == cat] if cat in CATEGORIES else None
        rows = self.store.events_for(uid, types, before, limit)
        read = int(self.store.get(f'notif_read:{uid}') or 0)
        return {'items': [{**r, 'cat': TYPES.get(r['type'], ('system',))[0], 'title': TYPES.get(r['type'], ('', ''))[1],
                           'crit': TYPES.get(r['type'], ('', '', '', False))[3], 'unread': r['id'] > read} for r in rows],
                'unread': self.store.unread_events(uid, read)}

    def mark_read(self, uid: int) -> None:
        last = self.store.events_for(uid, limit=1)
        if last:
            self.store.set(f'notif_read:{uid}', str(last[0]['id']))

    def unread(self, uid: int) -> int:
        return self.store.unread_events(uid, int(self.store.get(f'notif_read:{uid}') or 0))
