"""Журнал действий: кто, когда и что поменял.

Действия в панели управления записываются сами (запрос, который что-то меняет и прошёл успешно, — см. ROUTES),
действия из Telegram и самого бота (автопродление, докупка, пауза подписки) — через `log()`.
Пароли, токены и ключи в подробности не попадают.
"""
from __future__ import annotations

import json
import logging
import re

log_ = logging.getLogger(__name__)

BOT = 0  # uid «сам бот»

# (метод, путь) → (код действия, как назвать). Не записываем: проверки, прочтение уведомлений, обучение и т.п.
ROUTES: list[tuple[str, str, str, str]] = [
    ('POST', r'/api/settings', 'settings', 'Изменил настройки бота'),
    ('POST', r'/api/theme', 'theme', 'Изменил оформление'),
    ('PUT', r'/api/theme/background', 'theme', 'Загрузил фон'),
    ('DELETE', r'/api/theme/background', 'theme', 'Убрал фон'),
    ('POST', r'/api/profiles', 'group.create', 'Создал группу'),
    ('PUT', r'/api/profiles/(?P<pid>\d+)', 'group.edit', 'Изменил группу'),
    ('DELETE', r'/api/profiles/(?P<pid>\d+)', 'group.delete', 'Удалил группу'),
    ('POST', r'/api/profiles/(?P<pid>\d+)/nodes/upgrade', 'node.upgrade', 'Запустил обновление нод'),
    ('DELETE', r'/api/profiles/(?P<pid>\d+)/nodes/(?P<uuid>[^/]+)', 'node.delete', 'Удалил ноду'),
    ('POST', r'/api/profiles/(?P<pid>\d+)/nodes/(?P<uuid>[^/]+)/relays', 'relay.create', 'Добавил вход (мост) к ноде'),
    ('POST', r'/api/relays/(?P<rid>\d+)/apply', 'relay.apply', 'Переустановил вход (мост)'),
    ('DELETE', r'/api/relays/(?P<rid>\d+)', 'relay.delete', 'Убрал вход (мост)'),
    ('POST', r'/api/profiles/(?P<pid>\d+)/nodes/(?P<uuid>[^/]+)/ssh', 'node.ssh', 'Изменил SSH-доступ к ноде'),
    ('POST', r'/api/profiles/(?P<pid>\d+)/nodes/(?P<uuid>[^/]+)/ip', 'node.ip', 'Сменил IP ноды'),
    ('POST', r'/api/profiles/(?P<pid>\d+)/nodes/(?P<uuid>[^/]+)/address-domain', 'node.address', 'Сменил адрес ноды'),
    ('POST', r'/api/profiles/(?P<pid>\d+)/nodes/(?P<uuid>[^/]+)/rollback', 'node.rollback', 'Откатил версию ноды'),
    ('POST', r'/api/profiles/(?P<pid>\d+)/nodes/(?P<uuid>[^/]+)/apply', 'node.apply', 'Применил дополнения к ноде'),
    ('POST', r'/api/profiles/(?P<pid>\d+)/nodes/apply-many', 'node.apply', 'Применил дополнения к нодам'),
    ('POST', r'/api/hosts/import', 'hosts.import', 'Перенёс хосты в панель'),
    ('POST', r'/api/deploy', 'deploy', 'Запустил покупку и установку нод'),
    ('POST', r'/api/scripts', 'script.create', 'Добавил скрипт установки'),
    ('PUT', r'/api/scripts/(?P<sid>\d+)/recipe', 'script.edit', 'Изменил скрипт установки'),
    ('POST', r'/api/scripts/(?P<sid>\d+)/approve', 'script.approve', 'Одобрил скрипт установки'),
    ('DELETE', r'/api/scripts/(?P<sid>\d+)', 'script.delete', 'Удалил скрипт установки'),
    ('POST', r'/api/jobs/(?P<jid>\d+)/retry', 'job.retry', 'Повторил задачу'),
    ('POST', r'/api/checkers', 'checker.create', 'Добавил чекер'),
    ('POST', r'/api/checkers/(?P<cid>\d+)', 'checker.edit', 'Изменил чекер'),
    ('POST', r'/api/checkers/(?P<cid>\d+)/install', 'checker.install', 'Установил чекер'),
    ('DELETE', r'/api/checkers/(?P<cid>\d+)', 'checker.delete', 'Удалил чекер'),
    ('POST', r'/api/profiles/(?P<pid>\d+)/monitor', 'monitor', 'Изменил мониторинг'),
    ('POST', r'/api/profiles/(?P<pid>\d+)/monitor/notify', 'monitor', 'Изменил уведомления мониторинга'),
    ('POST', r'/api/monitor/action', 'monitor.action', 'Действие с нодой из мониторинга'),
    ('POST', r'/api/profiles/(?P<pid>\d+)/auto', 'auto', 'Изменил автоматизацию'),
    ('POST', r'/api/profiles/(?P<pid>\d+)/plugins', 'plugins', 'Изменил плагин нод'),
    ('POST', r'/api/profiles/(?P<pid>\d+)/plugins/preset', 'plugins', 'Применил пресет плагина'),
    ('POST', r'/api/profiles/(?P<pid>\d+)/plugins/unblock', 'plugins.unblock', 'Разбанил IP в Torrent Blocker'),
    ('POST', r'/api/profiles/(?P<pid>\d+)/cleanup/servers/(?P<sid>\d+)', 'server.delete', 'Удалил сервер в Bobr Cloud'),
    ('POST', r'/api/selfupdate/now', 'update', 'Обновил бота'),
    ('POST', r'/api/selfupdate/night', 'update', 'Запланировал обновление бота на ночь'),
    ('POST', r'/api/selfupdate', 'update.cfg', 'Изменил автообновление бота'),
    ('POST', r'/api/migrate/start', 'migrate', 'Запустил переезд бота'),
    ('POST', r'/api/reports/(?P<rid>\d+)/resolve', 'report', 'Закрыл отчёт о проблеме'),
    ('POST', r'/api/billing/pay', 'billing.pay', 'Продлил подписку'),
    ('POST', r'/api/billing/autorenew', 'billing.autorenew', 'Изменил автопродление подписки'),
    ('POST', r'/api/billing/settings', 'billing.cfg', 'Изменил тариф'),
    ('POST', r'/api/users/(?P<tg>\d+)/access', 'user.access', 'Изменил доступ пользователя'),
    ('POST', r'/api/wallet/topup', 'wallet', 'Пополнение Bobr Cloud'),
    ('POST', r'/api/profiles/(?P<pid>\d+)/rent/(?P<sid>\d+)/renew', 'rent.renew', 'Продлил аренду сервера'),
    ('POST', r'/api/profiles/(?P<pid>\d+)/rent/(?P<sid>\d+)/autorenew', 'rent.autorenew', 'Изменил автопродление сервера'),
    ('POST', r'/api/backup', 'backup.cfg', 'Изменил резервные копии'),
    ('POST', r'/api/backup/now', 'backup', 'Сделал резервную копию'),
    ('POST', r'/api/backup/key', 'backup.key', 'Сменил ключ резервных копий'),
    ('POST', r'/api/invites', 'invite.create', 'Создал приглашение'),
    ('DELETE', r'/api/invites/(?P<iid>\d+)', 'invite.delete', 'Отозвал приглашение'),
    ('POST', r'/api/users/(?P<tg>\d+)/block', 'user.block', 'Заблокировал или разблокировал пользователя'),
    ('DELETE', r'/api/users/(?P<tg>\d+)', 'user.delete', 'Удалил пользователя'),
]
_ROUTES = [(m, re.compile(p + r'/?$'), a, lbl) for m, p, a, lbl in ROUTES]

SECRET = re.compile(r'pass|token|secret|key|cookie|headers|init|private|cred', re.I)
SHOW = {'name': 'название', 'domain': 'домен', 'domains': 'домены', 'ip': 'IP', 'address': 'адрес', 'country': 'страна',
        'location': 'локация', 'count': 'штук', 'version': 'версия', 'tag': 'версия', 'mode': 'режим', 'on': 'включено',
        'enabled': 'включено', 'days': 'дней', 'add_days': 'дней добавлено', 'access_days': 'дней доступа', 'discount': 'скидка %',
        'free': 'бесплатно', 'blocked': 'закрыт', 'price_rub': 'цена ₽', 'period_days': 'период, дней', 'scale': 'автомасштабирование',
        'scale_max': 'лимит нод', 'online_max': 'онлайн на ноду', 'update': 'автообновление', 'load': 'нагрузка', 'renew': 'автопродление',
        'autorenew': 'автопродление', 'action': 'действие', 'hour': 'час', 'host': 'хост', 'uuids': 'ноды', 'amount_rub': 'сумма ₽',
        'note': 'заметка', 'region': 'регион', 'hoster_url': 'хостер', 'source': 'откуда сервер', 'server_id': 'сервер', 'uses': 'входов', 'keep': 'хранить копий', 'period': 'срок'}


def match(method: str, path: str) -> tuple[str, str, dict] | None:
    for m, rx, action, label in _ROUTES:
        if m == method:
            g = rx.fullmatch(path)
            if g:
                return action, label, g.groupdict()
    return None


def _short(v) -> str:
    if isinstance(v, bool):
        return 'да' if v else 'нет'
    if isinstance(v, list):
        return ', '.join(_short(x) for x in v[:6]) + (f' и ещё {len(v) - 6}' if len(v) > 6 else '')
    if isinstance(v, dict):
        return '…'
    return str(v)[:80]


def describe(body) -> str:
    """Короткие подробности из тела запроса: только понятные поля, без секретов."""
    if not isinstance(body, dict):
        return ''
    parts = []
    for k, v in body.items():
        if SECRET.search(k) or v in (None, '', [], {}) or k not in SHOW:
            continue
        parts.append(f'{SHOW[k]}: {_short(v)}')
    return '; '.join(parts)[:400]


def who_of(user) -> str:
    """Имя для журнала из пользователя Telegram (initData или aiogram)."""
    get = (lambda k: user.get(k)) if isinstance(user, dict) else (lambda k: getattr(user, k, None))
    name = ' '.join(x for x in (get('first_name'), get('last_name')) if x).strip()
    return name or (('@' + get('username')) if get('username') else '')


def log(store, uid: int, action: str, label: str, pid: int | None = None, detail: str = '', ts: float | None = None,
        who: str = '') -> None:
    """Записать действие. Журнал — не главное: ошибка записи не должна ломать само действие."""
    try:
        store.add_audit(uid, action, label, pid, detail, ts, who or ('Бот' if not uid else ''))
    except Exception:  # noqa: BLE001
        log_.exception('audit')


def from_request(store, uid: int, method: str, path: str, body, who: str = '') -> None:
    hit = match(method, path)
    if not hit:
        return
    action, label, g = hit
    pid = int(g['pid']) if g.get('pid') else None
    extra = []
    if g.get('uuid'):
        extra.append(f'нода {g["uuid"][:8]}')
    for k, what in (('sid', 'сервер'), ('jid', 'задача'), ('tg', 'пользователь'), ('rid', '№'), ('cid', 'чекер'), ('iid', 'приглашение')):
        if g.get(k):
            extra.append(f'{what} {g[k]}')
    d = describe(body)
    log(store, uid, action, label, pid, '; '.join(x for x in [', '.join(extra), d] if x), who=who)


def view(store, rows: list[dict]) -> list[dict]:
    """Строки журнала для панели: имя (сохранённое при записи или из списка пользователей), название группы."""
    groups = {p['id']: p.get('name', '') for p in store.profiles()}
    out = []
    for r in rows:
        who = r.get('who') or ''
        if not who and r['uid']:
            u = store.user(r['uid']) or {}
            who = u.get('first_name') or (u.get('username') and '@' + u['username']) or str(r['uid'])
        out.append({**r, 'who': who or 'Бот', 'group': groups.get(r['pid'], '') if r.get('pid') else ''})
    return out


def body_of(raw: bytes):
    try:
        return json.loads(raw or b'null')
    except (ValueError, UnicodeDecodeError):
        return None
