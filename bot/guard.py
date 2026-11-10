"""Страж подписок: ищет в панели Remnawave тех, кто раздаёт или перепродаёт подписку.

Раз в час по каждой группе, где страж включён, бот скачивает из панели пользователей и их HWID-устройства и
смотрит на признаки:
- устройство — сервер или скрипт (3x-ui, xray, curl, python…), а не приложение на телефоне или компьютере;
- устройство без платформы и модели;
- подписку забирали с IP хостинга (свои ноды не в счёт: подписка могла обновиться через наш же VPN);
- много устройств за сутки, устройства в разных сетях;
- трафик за вчера выше порога группы или в разы выше обычного для этого пользователя;
- то же устройство или IP, что у отключённого в панели пользователя (обход бана), то же устройство у другого;
- подключения на нодах (Remnawave 2.7+). Число IP само по себе ничего не значит: телефон меняет IP от вышки к вышке,
  оператор выпускает одно устройство с нескольких адресов, дома — Wi-Fi. Считаем СЕТИ (операторов), из которых
  подписчик подключён ОДНОВРЕМЕННО: одно устройство не бывает в двух сетях сразу. Сетей одновременно больше лимита
  устройств и так в нескольких проверках за сутки — подпиской пользуются сверх лимита; один и тот же IP хостинга
  в нескольких проверках — сервер-посредник;
- история запросов подписки: забирали запрещённым клиентом (правило ответа заблокировало) или сервером;
  ссылку за сутки забирали из очень многих разных сетей — ссылкой делятся (слабый признак);
- смена устройств: за неделю разных HWID намного больше лимита — старые удаляют, новые добавляют (перепродажа);
- трафик идёт почти круглые сутки — сервер-посредник или раздача;
- ферма пробных периодов: на одном устройстве или компьютере несколько аккаунтов, и большинство уже истекли;
- торренты (срабатывания Torrent Blocker);
- устройство или компьютер отключённого подписчика из другой группы того же владельца.
Делиться подпиской в пределах лимита устройств можно: разные города и сети сами по себе не признак.

Каждый признак даёт баллы. От WARN — сообщение тем, кто ведёт группу, с кнопками «перевыпустить подписку»,
«отключить», «всё в порядке». Автодействие (по настройке группы) — только для критичных признаков.
Тот же набор признаков второй раз не присылаем; «всё в порядке» — белый список, но критичное новое всё равно придёт.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import html
import json
import logging
import re
import secrets
import time

log = logging.getLogger(__name__)

EVERY = 3600              # как часто проверять группу
WARN, CRIT = 50, 100      # баллы: от WARN — сообщить, от CRIT — критично
TOKEN_TTL = 14 * 86400    # сколько живут кнопки в сообщениях
REMIND = 7 * 86400        # тот же набор признаков — не чаще раза в неделю
HISTORY_DAYS = 9          # сколько дней храним отметки трафика
IP_LOOKUPS = 150          # новых IP на разбор сети за один проход (RIPEstat не любит частых запросов)
IP_BUDGET = 60            # секунд на разбор новых IP за проход: не успели — узнаем в следующий
ALERTS_EACH = 5           # столько самых серьёзных — отдельными сообщениями, остальных — одной сводкой
PAGE = 500
MAX_USERS = 50_000
CONN_ACTIVE = 3600        # IP подключения считается живым, если был виден не раньше часа назад
CONN_NOW = 180            # «одновременно»: IP видели не раньше чем за 3 минуты до самого свежего в этой проверке
CONC_CHECKS = 3           # в стольких проверках за сутки должна повториться аномалия, чтобы считать её устойчивой
CONN_WAIT = 45            # сколько ждать ответа ноды о подключениях
SUBS_KEEP = 15            # запросов подписки на пользователя — для карточки
BLOCKED = {'BLOCK', 'STATUS_CODE_404', 'STATUS_CODE_451', 'SOCKET_DROP'}
HW_DAYS = 30              # сколько помним устройства подписчика (смена устройств)
SUBIP_KEEP = 48 * 3600    # сколько помним IP, с которых забирали подписку
HOURLY_KEEP = 27 * 3600   # почасовой трафик: последние сутки с запасом
ACTIVE_HOUR = 50 * 2**20  # час «с трафиком» — от 50 МБ
SUBS_PAGES = 20           # страниц истории запросов подписки за проход (по 500)
AI_DAY = 30               # разборов нейросетью в уведомлениях на группу в сутки (по кнопке — без ограничения)
NIGHT_MAX = 30            # подписчиков в ночном разборе группы (самые подозрительные за сутки)
NIGHT_KEEP = 14           # сколько ночных итогов помним для раздела
AI_TTL = 24 * 3600        # разбор с тем же набором признаков — из кэша

SERVER_UA = re.compile(r'3x-?ui|x-ui|xray|v2ray-?core|sing-?box-?server|marzban|remnawave|curl|wget|python|aiohttp|httpx|'
                       r'go-http|okhttp|node-fetch|axios|java/|postman|insomnia|libwww|scrapy|headless', re.I)
CLIENT_UA = re.compile(r'happ|incy|v2raytun|v2rayng|hiddify|streisand|shadowrocket|stash|clash|mihomo|flclash|karing|'
                       r'nekobox|nekoray|singbox|sfa|sfi|sfm|foxray|v2box|throne|koala|prizrak|ssclash', re.I)
# модели, которые есть у тысяч людей: по ним «то же устройство» не определить
GENERIC_MODEL = re.compile(r'^(iphone|ipad|ipod|sm-|samsung|galaxy|pixel|redmi|xiaomi|mi |poco|huawei|honor|oneplus|oppo|vivo|realme|'
                           r'motorola|moto |nokia|sony|xperia|tecno|infinix|itel|zte|nothing|asus|lenovo|google|apple|'
                           r'macbook|imac|mac ?mini|mac ?pro|mac ?studio|system product name|to be filled|o\.e\.m|virtual|vmware|'
                           r'kvm|standard pc)', re.I)
GENERIC_WORD = re.compile(r'(mac|pc|computer|desktop|laptop|windows|linux|ubuntu|android|unknown|default|user|admin|home)'
                          r'(-pc)?', re.I)  # имя целиком из общего слова
ARCH = re.compile(r'[_ ](x86_64|amd64|x64|x86|i[3-6]86|arm64|aarch64|armv\d\w*)$', re.I)
OS_NAME = re.compile(r'(windows|win\d|mac ?os|macos|os ?x|darwin|linux|ubuntu|debian|fedora|arch|android|ios|ipados)(\b|\d|_|-)', re.I)
SHARED_MAX = 5            # HWID или имя ПК больше чем у стольких аккаунтов — не уникальные (клиент шлёт одинаковое)
DESKTOP = re.compile(r'windows|win32|win64|mac ?os|macos|darwin|linux', re.I)


def pc_name(d: dict) -> str:
    """Имя компьютера, если оно похоже на уникальное (DESKTOP-7F3K2LQ, VASYA-PC); у телефонов и типовых моделей — ''."""
    m = ARCH.sub('', (d.get('model') or '').strip())  # Happ на Windows: DESKTOP-FR15BBH_x86_64 — архитектура не часть имени
    # в имени компьютера не бывает пробелов (Windows их запрещает), а «Windows 10 amd64», «Mac OS X aarch64» —
    # это система, которую присылают некоторые клиенты вместо имени
    if (len(m) < 5 or ' ' in m or OS_NAME.match(m) or GENERIC_MODEL.match(m) or GENERIC_WORD.fullmatch(m)
            or re.fullmatch(r'[\w ]+\d+,\d+', m)):  # MacBookPro18,1 — модель, а не имя
        return ''
    if DESKTOP.search(d.get('platform') or '') or re.match(r'(desktop|laptop|win)-[a-z0-9]{5,}$', m, re.I):
        return m.lower()
    return ''


HOSTING = re.compile(r'hetzner|\bovh|digitalocean|amazon|\baws\b|google|microsoft|azure|oracle|linode|akamai|vultr|choopa|'
                     r'contabo|leaseweb|m247|aeza|timeweb|selectel|beget|vdsina|firstbyte|stark|hostinger|hostkey|serverius|'
                     r'scaleway|ionos|netcup|kamatera|g-?core|datacamp|cdn77|psychz|hivelocity|quadranet|'
                     r'colocrossing|buyvm|frantech|pq hosting|alibaba|tencent|huawei cloud|hosting|datacent|data cent|'
                     r'\bcloud|\bvps|server|dedicated|colo', re.I)

# сети WARP и iCloud Private Relay: оттуда ходят обычные люди со «скрытием IP» — не хостинг клиента
RELAY_NET = re.compile(r'cloudflare|akamai|fastly', re.I)
NAMES_DEF = r'^(bobr_\d+|\d+_\d+)'   # подписки бота продаж; test, bridge_…, statistics и прочие системные — не проверяем


def hosting(h: str) -> bool:
    return bool(h) and bool(HOSTING.search(h)) and not RELAY_NET.search(h)


LEVELS = {'warn': '🟡', 'crit': '🔴'}
ACTIONS = {'off': 'только сообщать', 'revoke': 'перевыпускать подписку', 'disable': 'отключать пользователя'}


# ───────── настройки группы ─────────
def cfg(prof: dict) -> dict:
    g = prof.get('guard') or {}
    return {'on': bool(g.get('on')),
            # автодействие при критичных признаках: off | revoke | disable
            'auto': g.get('auto') if g.get('auto') in ACTIONS else 'off',
            'day_gb': int(g.get('day_gb', 50)),       # трафик за сутки выше — подозрительно (0 — не смотреть)
            'spike': int(g.get('spike', 5)),          # во сколько раз выше обычного (0 — не смотреть)
            'burst': int(g.get('burst', 4)),          # столько новых устройств за сутки (0 — не смотреть)
            'ai': bool(g.get('ai')),
            'night': bool(g.get('night')),            # итог дня от нейросети в полночь (ключ Gemini администратора)
            # какие подписки проверять ('' — все); прежний шаблон по умолчанию (с $) — на новый
            'names': NAMES_DEF if g.get('names') in (None, NAMES_DEF + '$') or not isinstance(g.get('names'), str) else g['names'],                  # разбор нейросетью в сообщениях (ключ Gemini администратора)
            'tz': int(g.get('tz', (prof.get('auto') or {}).get('tz', 0)))}


def clean(raw: dict, prof: dict) -> dict:
    g = cfg(prof)
    if 'on' in raw:
        g['on'] = bool(raw['on'])
    if 'ai' in raw:
        g['ai'] = bool(raw['ai'])
    if 'night' in raw:
        g['night'] = bool(raw['night'])
    if 'names' in raw:
        v = str(raw['names'] or '').strip()
        if len(v) > 200:
            raise ValueError('Шаблон имён: не длиннее 200 символов')
        try:
            re.compile(v, re.I)
        except re.error as e:
            raise ValueError(f'Шаблон имён — не регулярное выражение: {e}') from None
        g['names'] = v
    if 'auto' in raw:
        if raw['auto'] not in ACTIONS:
            raise ValueError('Автодействие: только сообщать, перевыпускать или отключать')
        g['auto'] = raw['auto']
    for k, lo, hi, what in (('day_gb', 0, 10_000, 'Трафик за сутки: от 0 до 10 000 ГБ'), ('spike', 0, 100, 'Всплеск: от 0 до 100 раз'),
                            ('burst', 0, 100, 'Новых устройств за сутки: от 0 до 100')):
        if k in raw:
            v = raw[k]
            if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                raise ValueError(what)
            g[k] = v
    if 'tz' in raw:
        v = raw['tz']
        if not isinstance(v, int) or isinstance(v, bool) or not -14 * 60 <= v <= 14 * 60:
            raise ValueError('Часовой пояс: смещение в минутах')
        g['tz'] = v
    return g


# ───────── данные панели ─────────
def _first(d: dict, *keys, default=None):
    for k in keys:
        v = d.get(k)
        if v not in (None, ''):
            return v
    return default


def _ts(v) -> float | None:
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def handle_of(desc) -> str:
    """@имя в Telegram из описания пользователя (бот продаж пишет «@alchern1999 | Алексей Чернышков»)."""
    m = re.search(r'@([A-Za-z][A-Za-z0-9_]{3,31})\b', str(desc or ''))
    return '@' + m.group(1) if m else ''


def norm_user(u: dict) -> dict | None:
    key = str(_first(u, 'uuid', 'id', default='') or '')
    if not key:
        return None
    tr = u.get('userTraffic') if isinstance(u.get('userTraffic'), dict) else {}
    used = _first(tr, 'lifetimeUsedTrafficBytes', 'usedTrafficBytes', default=None)
    if used is None:
        used = _first(u, 'lifetimeUsedTrafficBytes', 'usedTrafficBytes', default=0)
    ids = {str(x) for x in (u.get('uuid'), u.get('id')) if x not in (None, '')}
    return {'key': key, 'ids': ids, 'name': str(u.get('username') or key)[:64], 'status': str(u.get('status') or '').upper(),
            'tg': u.get('telegramId'), 'limit': u.get('hwidDeviceLimit'), 'used': int(float(used or 0)),
            'expire': _ts(u.get('expireAt')), 'created': _ts(u.get('createdAt')),
            'handle': handle_of(u.get('description'))}


def norm_device(d: dict) -> dict | None:
    hwid = str(d.get('hwid') or '')
    owner = str(_first(d, 'userUuid', 'userId', default='') or '')
    if not hwid or not owner:
        return None
    return {'hwid': hwid[:80], 'owner': owner, 'platform': str(d.get('platform') or '')[:40],
            'os': str(d.get('osVersion') or '')[:40], 'model': str(d.get('deviceModel') or '')[:60],
            'ua': str(d.get('userAgent') or '')[:120], 'ip': str(_first(d, 'ip', 'requestIp', 'lastIp', 'ipAddress', default='') or ''),
            'created': _ts(d.get('createdAt'))}


def pages(call, path: str, field: str, limit: int = MAX_USERS) -> list[dict]:
    """Весь список по страницам: GET path?start=…&size=…; ответ {field: [...], total} или просто [...]."""
    out, start = [], 0
    sep = '&' if '?' in path else '?'
    while start < limit:
        page = call('GET', f'{path}{sep}start={start}&size={PAGE}')
        items = page.get(field) if isinstance(page, dict) else page
        if not items:
            break
        out += items
        total = page.get('total') if isinstance(page, dict) else None
        start += len(items)
        if len(items) < PAGE or (isinstance(total, int) and start >= total):
            break
    return out


def norm_sub(r: dict) -> dict | None:
    """Запрос подписки из истории панели: кто, когда, с какого IP, каким клиентом и что ответило правило."""
    owner = str(_first(r, 'userId', 'userUuid', default='') or '')
    if not owner:
        return None
    return {'owner': owner, 'at': _ts(_first(r, 'requestAt', 'createdAt')) or 0, 'ip': str(r.get('requestIp') or ''),
            'ua': str(r.get('userAgent') or '')[:120], 'type': str(r.get('srrResponseType') or ''),
            'rule': str(r.get('srrRuleName') or '')[:60]}


def sub_history(call, since: float) -> list[dict]:
    """Запросы подписки не старше since: по страницам, свежие сначала (панель может отдавать и старые сначала)."""
    first = call('GET', '/api/subscription-request-history?start=0&size=500')
    rows = first.get('records') if isinstance(first, dict) else first
    total = first.get('total') if isinstance(first, dict) else None
    recs = [x for x in (norm_sub(r) for r in rows or [] if isinstance(r, dict)) if x]
    if len(recs) < 2 or not isinstance(total, int) or total <= 500:
        return [r for r in recs if r['at'] >= since]
    asc = recs[0]['at'] < recs[-1]['at']
    out, step = [r for r in recs if r['at'] >= since], 500
    starts = range(total - step, -1, -step) if asc else range(step, total, step)
    if asc:
        out = []  # первая страница — самые старые: идём с конца
    for n, start in enumerate(starts):
        if n >= SUBS_PAGES:
            break
        page = call('GET', f'/api/subscription-request-history?start={max(start, 0)}&size={step}')
        rows = page.get('records') if isinstance(page, dict) else page
        part = [x for x in (norm_sub(r) for r in rows or [] if isinstance(r, dict)) if x]
        if not part:
            break
        out += [r for r in part if r['at'] >= since]
        if min(r['at'] for r in part) < since:
            break
    return out


def torrent_reports(call) -> list[dict]:
    """Срабатывания Torrent Blocker за последнее время: [{owner, at}] (owner — id, uuid или «name:имя»)."""
    page = call('GET', '/api/node-plugins/torrent-blocker?start=0&size=500') or {}
    out = []
    for x in page.get('records') or []:
        u = x.get('user') if isinstance(x.get('user'), dict) else {}
        owner = str(_first(u, 'id', 'uuid', default='') or _first(x, 'userId', 'userUuid', default='') or '')
        if not owner and u.get('username'):
            owner = 'name:' + str(u['username'])
        if owner:
            out.append({'owner': owner, 'at': _ts(x.get('createdAt')) or 0})
    return out


def _job(panel, start: str, result: str, wait: float = CONN_WAIT, sleep=time.sleep):
    """Фоновая задача панели: POST start → jobId, потом GET result/{jobId}, пока не isCompleted. None — не дождались."""
    job = panel.call('POST', start)
    jid = (job or {}).get('jobId') if isinstance(job, dict) else None
    if not jid:
        return None
    t0 = time.monotonic()
    while True:
        r = panel.call('GET', f'{result}/{jid}')
        if isinstance(r, dict) and (r.get('isCompleted') or r.get('isFailed')):
            return None if r.get('isFailed') else r.get('result')
        if time.monotonic() - t0 > wait:
            return None
        sleep(2)


# пути «IP подключений на ноде»: панель 3.x и 2.7+
CONN_API = [('/api/connections/by-node/{uuid}', '/api/connections/by-node'),
            ('/api/ip-control/fetch-users-ips/{uuid}', '/api/ip-control/fetch-users-ips/result')]


def node_connections(panel, node: dict, api: list | None = None, sleep=time.sleep) -> list[dict]:
    """IP пользователей на ноде: [{owner, ip, last, node}]. Без поддержки в панели — StepError наружу."""
    from . import engine
    err = None
    for start, result in (api or CONN_API):
        try:
            res = _job(panel, start.format(uuid=node['uuid']), result, sleep=sleep)
        except engine.StepError as e:
            err = e
            if 'HTTP 404' in str(e) or 'HTTP 400' in str(e):
                continue  # другой версии путь — пробуем следующий
            raise
        out = []
        for u in (res or {}).get('users') or []:
            owner = str(_first(u, 'userId', 'userUuid', 'uuid', default='') or '')
            for x in u.get('ips') or []:
                ip, last = (x.get('ip'), _ts(x.get('lastSeen'))) if isinstance(x, dict) else (x, None)
                if owner and ip:
                    out.append({'owner': owner, 'ip': str(ip), 'last': last, 'node': str(node.get('name') or '')[:40]})
        return out
    raise err or ValueError('панель не отдаёт IP подключений')


def collect(panel, extra_own: set | None = None, conns: bool = True, stage=lambda s: None) -> dict:
    """Пользователи, устройства, IP подключений, история запросов подписки и адреса своих нод из панели (в потоке)."""
    from concurrent.futures import ThreadPoolExecutor
    stage('Скачиваю подписчиков и устройства из панели')
    users = [x for x in (norm_user(u) for u in pages(panel.call, '/api/users', 'users')) if x]
    devices, dev_err = [], ''
    try:
        devices = [x for x in (norm_device(d) for d in pages(panel.call, '/api/hwid/devices', 'devices')) if x]
    except Exception as e:  # noqa: BLE001 — старая панель без списка устройств: смотрим только трафик
        dev_err = str(e)[:200]
    subs, subs_err = [], ''
    try:
        subs = sub_history(panel.call, time.time() - 26 * 3600)
    except Exception as e:  # noqa: BLE001
        subs_err = str(e)[:200]
    torrents = []
    try:
        torrents = torrent_reports(panel.call)
    except Exception:  # noqa: BLE001 — плагина нет или он выключен
        pass
    own, nodes = set(extra_own or ()), []
    try:
        from . import engine
        nodes = [n for n in panel.call('GET', '/api/nodes') if isinstance(n, dict)]
        addrs = {str(n.get('address') or '') for n in nodes}
        own |= {a for a in addrs if re.fullmatch(r'[\d.]+', a)}
        for ips in engine.resolve_many([a for a in addrs if a and not re.fullmatch(r'[\d.]+', a)]).values():
            own |= set(ips)
    except Exception:  # noqa: BLE001 — без адресов нод просто не отличим «обновил через наш VPN»
        pass
    conn, conn_err = [], ''
    stage('Спрашиваю ноды, кто с каких IP подключён')
    live = [n for n in nodes if n.get('uuid') and n.get('isConnected') and not n.get('isDisabled')]
    if conns and live:
        errs = []

        def one(n):
            try:
                return node_connections(panel, n)
            except Exception as e:  # noqa: BLE001
                errs.append(str(e)[:160])
                return []
        with ThreadPoolExecutor(max_workers=min(6, len(live))) as ex:
            for part in ex.map(one, live):
                conn += part
        if errs and not conn:
            conn_err = errs[0]
    return {'users': users, 'devices': devices, 'dev_err': dev_err, 'own': own, 'conn': conn, 'conn_err': conn_err,
            'subs': subs, 'subs_err': subs_err, 'torrents': torrents}


# ───────── трафик ─────────
def day_of(now: float, tz: int) -> str:
    return (dt.datetime.fromtimestamp(now, dt.timezone.utc) + dt.timedelta(minutes=tz)).strftime('%Y-%m-%d')


def remember_traffic(hist: dict, users: list[dict], now: float, tz: int) -> dict:
    """Отметка «сколько всего потрачено» на начало каждого дня: {ключ: {дата: байт}} за HISTORY_DAYS дней."""
    today = day_of(now, tz)
    keep = {day_of(now - i * 86400, tz) for i in range(HISTORY_DAYS)}
    out = {}
    for u in users:
        h = {d: v for d, v in (hist.get(u['key']) or {}).items() if d in keep}
        h.setdefault(today, u['used'])
        out[u['key']] = h
    return out


def traffic(h: dict, now: float, tz: int) -> tuple[int | None, float | None]:
    """(трафик за вчера, обычный трафик в сутки за неделю до этого) по отметкам начала дней; None — данных мало."""
    d = [day_of(now - i * 86400, tz) for i in range(HISTORY_DAYS)]
    if d[0] not in h or d[1] not in h:
        return None, None
    y = h[d[0]] - h[d[1]]
    if y < 0:  # счётчик сбросили
        return None, None
    old = [i for i in range(2, HISTORY_DAYS) if d[i] in h]
    if not old:
        return y, None
    span = max(old)
    base = (h[d[1]] - h[d[span]]) / (span - 1) if span > 1 else None
    return y, (base if base is not None and base >= 0 else None)


def gb(b: float) -> str:
    return f'{b / 2**30:.1f} ГБ'


# ───────── разбор ─────────
def index(data: dict, names: str | None = None) -> dict:
    """Кто есть кто: пользователи, их устройства, IP подключений и запросы подписки — по ключу пользователя."""
    rx = re.compile(names, re.I) if names else None
    users = {u['key']: u for u in data['users']  # служебные (мосты, test, statistics…) — не проверяем и не связываем
             if not u['name'].startswith('bridge_') and (rx is None or rx.search(u['name']))}
    alias = {i: u['key'] for u in users.values() for i in u['ids']}
    alias.update({'name:' + u['name']: u['key'] for u in users.values()})
    own = data.get('own') or set()
    ix = {'users': users, 'dev': {}, 'conn': {}, 'subs': {}, 'hwid': {}, 'pc': {}, 'ip': {}, 'own': own, 'torrent': {},
          'banned': {k for k, u in users.items() if u['status'] == 'DISABLED'}}
    for d in data.get('devices') or []:
        k = alias.get(d['owner'])
        if k:
            ix['dev'].setdefault(k, []).append(d)
            ix['hwid'].setdefault(d['hwid'], set()).add(k)
            if pc_name(d):
                ix['pc'].setdefault(pc_name(d), {}).setdefault(k, set()).add(d['hwid'])
            if d['ip'] and d['ip'] not in own:
                ix['ip'].setdefault(d['ip'], set()).add(k)
    for c in data.get('conn') or []:
        k = alias.get(c['owner'])
        if k and c['ip'] not in own:
            cur = ix['conn'].setdefault(k, {}).setdefault(c['ip'], {'ip': c['ip'], 'last': c['last'], 'nodes': []})
            if c['last'] and (cur['last'] or 0) < c['last']:
                cur['last'] = c['last']
            if c['node'] and c['node'] not in cur['nodes']:
                cur['nodes'].append(c['node'])
            ix['ip'].setdefault(c['ip'], set()).add(k)
    seen_at = [c['last'] for c in data.get('conn') or [] if c.get('last')]
    ix['conn_at'] = max(seen_at) if seen_at else None
    ix['common_hw'] = {h for h, ks in ix['hwid'].items() if len(ks) > SHARED_MAX}
    ix['common_pc'] = {n for n, ks in ix['pc'].items() if len(ks) > SHARED_MAX}
    for t in data.get('torrents') or []:
        k = alias.get(t['owner'])
        if k:
            ix['torrent'].setdefault(k, []).append(t['at'])
    for r in sorted(data.get('subs') or [], key=lambda r: -r['at']):
        k = alias.get(r['owner'])
        if k:
            ix['subs'].setdefault(k, []).append(r)
    return ix


def holder(ipinfo: dict, ip: str) -> str:
    return str((ipinfo.get(ip) or {}).get('holder') or '')


def net_of(ipinfo: dict, ip: str) -> str:
    """Сеть IP — оператор (ASN). Неизвестные — в одну кучу «?»: лучше недосчитать сеть, чем обвинить зря."""
    i = ipinfo.get(ip) or {}
    return str(i.get('asn') or i.get('holder') or '?')


def concurrent(ix: dict, k: str) -> list[dict]:
    """IP, с которых подписчик подключён прямо сейчас: видны в пределах CONN_NOW от самого свежего IP этой проверки."""
    ref = ix.get('conn_at')
    return [c for c in (ix['conn'].get(k) or {}).values() if not c['last'] or not ref or ref - c['last'] <= CONN_NOW]


def remember_conc(mem: dict, ix: dict, ipinfo: dict, now: float) -> dict:
    """По проверкам за сутки: {ключ: [[когда, сетей одновременно, IP одновременно, [IP хостингов]], …]} — только где есть что помнить."""
    out = {}
    for k in ix['users']:
        cur = [e for e in (mem.get(k) or []) if e[0] >= now - 25 * 3600 and e[0] < now]
        conc = concurrent(ix, k)
        nets = len({net_of(ipinfo, c['ip']) for c in conc})
        hosts = sorted(c['ip'] for c in conc if hosting(holder(ipinfo, c['ip'])))[:3]
        if nets >= 2 or hosts:
            cur.append([now, nets, len(conc), hosts])
        if cur:
            out[k] = cur[-30:]
    return out


def live_ips(ix: dict, k: str, now: float) -> list[dict]:
    """IP подключений пользователя, видимые за последний час (без времени — считаем живыми)."""
    return [c for c in (ix['conn'].get(k) or {}).values() if not c['last'] or now - c['last'] < CONN_ACTIVE]


def judge(ix: dict, k: str, conf: dict, hist: dict, ipinfo: dict, now: float, ex: dict | None = None) -> dict:
    """Признаки одного пользователя: {key, name, score, level, crit, reasons, devices, ips, …}; level '' — чисто."""
    u, banned, own = ix['users'][k], ix['banned'], ix['own']
    ex = ex or {}
    reasons: list[dict] = []

    def add(code, w, text, crit=False, dev=None, ip=None):
        r = {'code': code, 'w': w, 'text': text, 'crit': crit}
        if dev:
            r['dev'] = dev_brief(dev, ipinfo)
        if ip:
            r['ip'] = ip
            r['net'] = holder(ipinfo, ip)
        reasons.append(r)

    devs = ix['dev'].get(k, [])
    for d in devs:
        ident = ' '.join(x for x in (d['ua'], d['hwid'], d['platform'], d['model']) if x)
        if SERVER_UA.search(ident) and not CLIENT_UA.search(d['ua'] or ''):
            add('server:' + d['hwid'], CRIT, f'устройство похоже на сервер или скрипт: {d["ua"] or d["hwid"][:40]}', True, d)
        elif not (d['platform'] or d['model'] or d['os']) and not CLIENT_UA.search(d['ua']):
            add('bare:' + d['hwid'], 20, f'устройство без платформы и модели ({d["ua"] or "без User-Agent"})', dev=d)
    hosted = sorted({d['ip'] for d in devs if d['ip'] and d['ip'] not in own and hosting(holder(ipinfo, d['ip']))})
    for ip in hosted[:3]:
        d = next((d for d in devs if d['ip'] == ip), None)
        app = bool(d and CLIENT_UA.search(d['ua']) and (d['platform'] or d['model']))  # обычное приложение на устройстве
        add('host:' + ip, 15 if app else 60, f'подписку забирали с IP хостинга {ip} ({holder(ipinfo, ip)[:40]})'
            + (' — обычное приложение; скорее всего, был включён другой VPN' if app else ''), ip=ip, dev=d)
    if conf['burst']:
        fresh_d = [d for d in devs if d['created'] and now - d['created'] < 86400]
        if len(fresh_d) >= conf['burst']:
            add('burst', 30, f'{len(fresh_d)} новых устройств за сутки')
    # то же устройство: совпадает HWID — его выдаёт приложение (Happ, Incy…) на конкретном устройстве, по модели
    # («iPhone 15») не сравниваем. Общий HWID — подписки двух аккаунтов в одном приложении на одном устройстве:
    # один аккаунт рядом — бывает (две подписки у одного человека), два и больше — повод посмотреть
    twins, twin_dev = set(), None
    for d in devs:
        if d['hwid'] in ix['common_hw']:
            continue  # такой HWID у многих — клиент присылает не уникальный: связью не считаем
        for o in ix['hwid'].get(d['hwid'], set()) - {k}:
            if o in banned:
                add('ban-hwid:' + o, CRIT, f'то же устройство (HWID), что у отключённого {ix["users"][o]["name"]}', True, d)
            else:
                twins.add(o)
                twin_dev = twin_dev or d
    if twins:
        names = ', '.join(ix['users'][o]['name'] for o in sorted(twins)[:3]) + (f' и ещё {len(twins) - 3}' if len(twins) > 3 else '')
        add('twin:' + ','.join(sorted(twins)[:5]), 30 if len(twins) == 1 else 50,
            f'то же устройство (HWID) у других аккаунтов: {names}', dev=twin_dev)
    # ферма пробных периодов: соседи по устройству или компьютеру, и большинство уже истекли
    pcs = {o for d in devs if pc_name(d) and pc_name(d) not in ix['common_pc'] for o in (ix['pc'].get(pc_name(d)) or {})} - {k} - banned
    near = twins | pcs
    gone = [o for o in near if ix['users'][o]['status'] in ('EXPIRED', 'LIMITED') or (ix['users'][o].get('expire') or now) < now]
    if len(near) >= 2 and len(gone) >= 2:
        add('farm', 40, f'на этом устройстве или компьютере ещё {len(near)} аккаунтов, из них истекли {len(gone)} — '
                        f'похоже на сбор пробных периодов')
    # устройство или компьютер отключённого в другой группе того же владельца
    xb = ex.get('ban') or {}
    for d in devs:
        if d['hwid'] in (xb.get('hw') or {}):
            add('xban-hw', CRIT, 'устройство (HWID) было у отключённого подписчика в другой вашей группе', True, d)
        if pc_name(d) and pc_name(d) in (xb.get('pc') or {}):
            add('xban-pc', 50, f'компьютер «{d["model"]}» был у отключённого подписчика в другой вашей группе', dev=d)
    # тот же компьютер под другим HWID (другое приложение или переустановка): уникальное имя ПК вроде DESKTOP-7F3K2LQ
    for d in devs:
        name = pc_name(d)
        if not name or name in ix['common_pc']:
            continue
        same = [o for o, hw in sorted((ix['pc'].get(name) or {}).items()) if o != k and d['hwid'] not in hw]
        for o in [o for o in same if o in banned][:2]:
            add('ban-pc:' + o, 50, f'компьютер «{d["model"]}» — с тем же именем, что у отключённого {ix["users"][o]["name"]}', dev=d)
        rest = [o for o in same if o not in banned]
        if rest:  # одной строкой и одними баллами, сколько бы их ни было
            names = ', '.join(ix['users'][o]['name'] for o in rest[:3]) + (f' и ещё {len(rest) - 3}' if len(rest) > 3 else '')
            add('pc:' + name, 20, f'компьютер «{d["model"]}» — с тем же именем есть у {names}', dev=d)
    mine_ips = {d['ip'] for d in devs if d['ip']} | set(ix['conn'].get(k) or {})
    for ip in sorted(mine_ips - own):
        for o in sorted((ix['ip'].get(ip, set()) & banned) - {k})[:2]:
            add('ban-ip:' + o, 20, f'тот же IP {ip}, что у отключённого {ix["users"][o]["name"]}', ip=ip,
                dev=next((d for d in devs if d['ip'] == ip), None))
    # подключения на нодах. Число IP — не признак (вышки, CGNAT, Wi-Fi). Признак — из скольких СЕТЕЙ подключён
    # одновременно: одно устройство не бывает в двух сетях сразу (в трёх — тем более), и повторяется ли это
    lim = int(u.get('limit') or 0) or max(len(devs), 3)
    ips = concurrent(ix, k)
    nets = len({net_of(ipinfo, c['ip']) for c in ips})
    past = [e for e in ((ex.get('conc') or {}).get(k) or []) if now - 86400 <= e[0] <= now]
    if nets > lim:
        times = max(sum(1 for e in past if e[1] > lim), 1)
        if times >= CONC_CHECKS:
            add('nets-limit!', 80, f'одновременно подключён из {nets} разных сетей при лимите {lim} устройств — так уже в '
                                   f'{times} проверках за сутки: подпиской пользуются сверх лимита')
        else:
            add('nets-limit', 30, f'одновременно подключён из {nets} разных сетей при лимите {lim} устройств '
                                  f'(пока в {times} из {CONC_CHECKS} проверок — наблюдаю)')
    elif devs and nets > len(devs) + 1 and sum(1 for e in past if e[1] > len(devs) + 1) >= CONC_CHECKS:
        add('nets-dev', 20, f'одновременно подключён из {nets} сетей при {len(devs)} устройствах: ключи добавлены мимо '
                            f'подписки, но в пределах лимита {lim}')
    if len(ips) > 3 * max(lim, len(devs)) + 2:  # даже в одной сети столько адресов сразу одно-два устройства не дадут
        add('ips-many', 40, f'одновременно {len(ips)} IP подключений при лимите {lim} устройств')
    for c in sorted((c for c in ips if hosting(holder(ipinfo, c['ip']))), key=lambda c: c['ip'])[:3]:
        times = max(sum(1 for e in past if c['ip'] in (e[3] if len(e) > 3 else [])), 1)
        tail = f', ноды: {", ".join(c["nodes"])}' if c['nodes'] else ''
        if times >= CONC_CHECKS:
            add('conn-host!:' + c['ip'], 70, f'подключение к ноде с IP хостинга {c["ip"]} ({holder(ipinfo, c["ip"])[:40]}) держится: '
                                             f'{times} проверок за сутки — сервер-посредник' + tail, ip=c['ip'])
        else:
            add('conn-host:' + c['ip'], 15, f'подключение к ноде с IP хостинга {c["ip"]} ({holder(ipinfo, c["ip"])[:40]}) — '
                                            f'разово так бывает при втором VPN; наблюдаю' + tail, ip=c['ip'])
    # запросы подписки: запрещённый клиент или сервер
    for r in (ix['subs'].get(k) or [])[:50]:
        if now - r['at'] > 3 * 86400:
            break
        if SERVER_UA.search(r['ua']) and not CLIENT_UA.search(r['ua']):
            add('sub-server:' + r['ua'][:30], CRIT, f'подписку запрашивал сервер или скрипт: {r["ua"]}', True)
        elif r['type'] in BLOCKED:
            add('sub-block:' + r['ua'][:30], 40, f'подписку пытались забрать запрещённым клиентом: {r["ua"] or "без User-Agent"}'
                + (f' (правило «{r["rule"]}»)' if r['rule'] else ''))
    week = [h for h, (_f, last) in ((ex.get('hw') or {}).get(k) or {}).items() if last >= now - 7 * 86400]
    if len(week) >= lim * 2 and len(week) >= lim + 4:
        add('churn', 50, f'за 7 дней {len(week)} разных устройств при лимите {lim}: старые удаляют, новые добавляют — '
                         f'похоже на перепродажу')
    elif len(week) >= lim + 3:
        add('churn', 25, f'за 7 дней {len(week)} разных устройств при лимите {lim}')
    subips = [ip for ip, t in ((ex.get('subip') or {}).get(k) or {}).items() if t >= now - 86400]
    subnets = len({net_of(ipinfo, ip) for ip in subips})  # не IP, а сети: у телефона IP меняется сам
    if subnets > 3 * max(lim, len(devs)) + 2:
        add('subnets', 25, f'за сутки ссылку подписки забирали из {subnets} разных сетей при лимите {lim} устройств — '
                           f'похоже, ссылкой делятся')
    hours = active_hours((ex.get('hourly') or {}).get(k), now)
    if hours is not None and hours['active'] >= 22 and hours['total'] >= 5 * 2**30:
        add('flat', 30, f'трафик идёт почти круглые сутки: {hours["active"]} часов из 24, {gb(hours["total"])} — '
                        f'так качает сервер или раздача, а не люди')
    tor = [t for t in ix['torrent'].get(k, []) if t >= now - 7 * 86400]
    if len(tor) >= 3:
        add('torrent', 20, f'торренты: {len(tor)} срабатываний Torrent Blocker за неделю')
    y, base = traffic(hist.get(k) or {}, now, conf['tz'])
    if y is not None:
        if conf['day_gb'] and y > conf['day_gb'] * 2**30:
            add('day', 40, f'за вчера {gb(y)} — больше порога {conf["day_gb"]} ГБ')
        if conf['spike'] and base and base > 2**30 and y > 5 * 2**30 and y > conf['spike'] * base:
            add('spike', 30, f'за вчера {gb(y)} — в {y / base:.0f} раз больше обычного ({gb(base)} в сутки)')
    uniq = {}
    for r in reasons:  # одна и та же причина — один раз
        uniq.setdefault(r['code'], r)
    reasons = sorted(uniq.values(), key=lambda r: -r['w'])
    score = sum(r['w'] for r in reasons)
    crit = any(r['crit'] for r in reasons)
    level = 'crit' if crit or score >= CRIT else 'warn' if score >= WARN else ''
    return {'key': k, 'name': u['name'], 'tg': u['tg'], 'handle': u.get('handle', ''), 'status': u['status'], 'score': score,
            'crit': crit, 'level': level, 'dev_list': [dev_brief(d, ipinfo) for d in devs][:12],
            'reasons': reasons, 'devices': len(devs), 'ips': len(ips), 'nets': nets, 'sub_nets': subnets,
            'nets_checks': sum(1 for e in past if e[1] > lim), 'limit': u['limit'], 'yesterday': y, 'usual': base,
            'week_devices': len(week), 'sub_ips': len(subips), 'active_hours': hours['active'] if hours else None,
            'torrents': len(tor)}


def analyze(data: dict, conf: dict, hist: dict, ipinfo: dict, now: float, ix: dict | None = None, ex: dict | None = None) -> list[dict]:
    """Подозрительные пользователи (от WARN или с критичным признаком), по убыванию баллов. Отключённые — не в счёт.
    ipinfo — {ip: {'holder': …}} (что успели узнать); hist — отметки трафика (remember_traffic)."""
    ix = ix or index(data, conf.get('names'))
    out = [f for f in (judge(ix, k, conf, hist, ipinfo, now, ex) for k in ix['users'] if k not in ix['banned']) if f['level']]
    return sorted(out, key=lambda x: -x['score'])


def signature(f: dict) -> str:
    return hashlib.sha1('|'.join(sorted(r['code'] for r in f['reasons'])).encode()).hexdigest()[:12]


def fresh(found: list[dict], seen: dict, ok: set[str], now: float) -> list[dict]:
    """О ком писать: новый набор признаков, или прошла неделя; из «всё в порядке» — только с новым критичным признаком."""
    out = []
    for f in found:
        s = seen.get(f['key']) or {}
        sig = signature(f)
        if f['key'] in ok:
            known = set(s.get('codes') or [])
            if not any(r['crit'] and r['code'] not in known for r in f['reasons']):
                continue
        elif s.get('sig') == sig and now - s.get('ts', 0) < REMIND:
            continue
        out.append(f)
    return out


def dev_brief(d: dict, ipinfo: dict | None = None) -> dict:
    """Устройство для сообщения: что это, каким клиентом, откуда."""
    sysname = ' '.join(x for x in (d['platform'], d['os']) if x)
    return {'hwid': d['hwid'], 'what': ' · '.join(x for x in (d['model'], sysname) if x) or 'платформа неизвестна',
            'platform': d['platform'], 'ua': d['ua'], 'ip': d['ip'], 'net': holder(ipinfo or {}, d['ip']) if d['ip'] else '',
            'server': bool(SERVER_UA.search(' '.join((d['ua'], d['hwid']))) and not CLIENT_UA.search(d['ua'])),
            'created': d.get('created')}


_COPY = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:bobr_\d+|\d+_\d+)\w*', re.I)


def copyable(text_: str) -> str:
    """HTML-текст, где IP и имена подписок — <code> (в Telegram копируются по нажатию)."""
    return _COPY.sub(lambda m: f'<code>{m.group(0)}</code>', html.escape(text_))


def dev_line(b: dict) -> str:
    e = html.escape
    icon = '🖥' if DESKTOP.search(b['platform'] or '') else '🤖' if b['server'] else '📱'
    out = f'{icon} <b>{e(b["what"])}</b>' + (f' · {e(b["ua"][:60])}' if b['ua'] else ' · без User-Agent')
    if b['ip']:
        out += f'\n     IP <code>{e(b["ip"])}</code>' + (f' ({e(b["net"][:40])})' if b['net'] else '')
    out += f'\n     HWID <code>{e(b["hwid"])}</code>'
    return out


def text(prof: dict, f: dict, done: str = '', ai: dict | None = None) -> str:
    """Сообщение о подозрительном: имя и @ копируются нажатием, у признаков — какое устройство и откуда, ниже — все устройства."""
    e = html.escape
    who = f'<code>{e(f["name"])}</code>' + (f' · <code>{e(f["handle"])}</code>' if f.get('handle') else '')
    lines = [f'{LEVELS[f["level"]]} <b>Подозрительная подписка</b> · {e(prof["name"])}', who,
             f'Устройств: {f["devices"]}' + (f' из {f["limit"]}' if f.get('limit') else '')
             + (f' · сейчас подключён из {f["nets"]} сет{"и" if f["nets"] == 1 else "ей"} ({f["ips"]} IP)' if f.get('ips') else ''), '']
    shown = set()
    for r in f['reasons'][:8]:
        lines.append(('🔴 ' if r.get('crit') else '• ') + copyable(r['text']))
        if r.get('dev') and r['dev']['hwid'] in shown:
            lines.append(f'   ↳ то же устройство: {html.escape(r["dev"]["what"])}')
        elif r.get('dev'):
            lines.append('   ' + dev_line(r['dev']).replace('\n', '\n   '))
            shown.add(r['dev']['hwid'])
    rest = [b for b in f.get('dev_list') or [] if b['hwid'] not in shown]
    if rest:
        lines += ['', '<b>Устройства:</b>'] + [dev_line(b) for b in rest[:6]]
        if len(rest) > 6:
            lines.append(f'… и ещё {len(rest) - 6}')
    if ai:
        lines += ['', ai_line(ai)]
    if done:
        lines += ['', done]
    out = '\n'.join(lines)
    if len(out) > 3900:  # Telegram: не больше 4096 — срезаем по строкам, чтобы не порвать разметку
        keep, n = [], 0
        for line in lines:
            if n + len(line) > 3800:
                keep.append('…')
                break
            keep.append(line)
            n += len(line) + 1
        out = '\n'.join(keep)
    return out


# ───────── итог дня ─────────
NIGHT_SYSTEM = """Ты — аналитик VPN-сервиса (панель Remnawave). Раз в сутки тебе дают обезличенные сводки по подписчикам,
которых бот-страж за день счёл подозрительными (у каждого id вида S1, S2…). Твоя задача — разобрать каждого и сказать
владельцу, с кем действительно надо что-то делать, а где ложная тревога.

Правило сервиса: делиться подпиской в пределах лимита устройств разрешено — близкие и друзья в разных городах и сетях
со своими устройствами это нормально. Нарушение — выйти за лимит обходными путями (ключи мимо подписки, смена устройств,
раздача ссылки), перепродажа, сервер-посредник, обход бана, сбор пробных периодов.

Число IP само по себе не признак: телефон меняет IP от вышки к вышке, оператор выпускает одно устройство с нескольких
адресов, дома Wi-Fi. Значимо, из скольких разных сетей (операторов) подписчик подключён одновременно — одно устройство
не бывает в двух сетях сразу — и повторяется ли это от проверки к проверке
(checks_last_24h_with_more_networks_than_limit). Обычное приложение, забравшее подписку с IP хостинга, — чаще всего
человек с включённым вторым VPN. Устройство-сервер (3x-ui, xray, curl) или устройство отключённого — сильные факты.
Не выдумывай фактов, которых нет в сводке.

Ответ — строго JSON на русском:
{"summary": "3–5 предложений: что за день видно в целом, на кого обратить внимание в первую очередь",
 "users": [{"id": "S1",
            "verdict": "honest"|"family"|"sharing"|"reselling"|"relay"|"evasion"|"unclear",
            "confidence": 0-100,
            "action": "none"|"watch"|"message"|"revoke"|"disable",
            "why": "1–2 предложения: главные факты за вывод и, если есть, против"}]}
Разбери всех из списка, по одному элементу на каждый id."""

NIGHT_ACT = {'revoke', 'disable', 'message'}


def night_clean(raw: dict, ids: dict) -> dict:
    """Ответ нейросети → {summary, users: [{key, verdict, confidence, action, why}]}; чужие id отбрасываем."""
    raw = raw if isinstance(raw, dict) else {}
    users, seen = [], set()
    for x in raw.get('users') or []:
        if not isinstance(x, dict) or x.get('id') not in ids or x['id'] in seen:
            continue
        seen.add(x['id'])
        a = ai_clean({**x, 'summary': x.get('why')})
        users.append({'key': ids[x['id']], 'verdict': a['verdict'], 'confidence': a['confidence'], 'action': a['action'],
                      'why': a['summary'][:400]})
    return {'summary': str(raw.get('summary') or '')[:1500], 'users': users}


def night_text(prof: dict, rep_: dict) -> str:
    """Итог дня для Telegram: кого проверить и что сделать, кого наблюдать, где ложная тревога."""
    e = html.escape
    day = '.'.join(reversed(rep_['day'].split('-')[1:]))
    who = lambda u: f'<code>{e(u["name"])}</code>' + (f' · <code>{e(u["handle"])}</code>' if u.get('handle') else '')  # noqa: E731
    act = [u for u in rep_['users'] if u['action'] in NIGHT_ACT]
    watch = [u for u in rep_['users'] if u['action'] == 'watch']
    calm = [u for u in rep_['users'] if u['action'] == 'none']
    lines = [f'🌙 <b>Итог дня стража</b> · {e(prof["name"])} · {day}', '', e(rep_['summary']) if rep_['summary'] else '']
    if act:
        lines += ['', f'<b>Действовать ({len(act)}):</b>']
        for u in act[:12]:
            lines.append(f'{"🔴" if u["action"] == "disable" else "🟠"} {who(u)} — {e(VERDICTS[u["verdict"]])} ({u["confidence"]}%), '
                         f'совет: {e(AI_ACTIONS[u["action"]])}\n   <i>{e(u["why"])}</i>')
    if watch:
        lines += ['', f'<b>Понаблюдать ({len(watch)}):</b>']
        lines += [f'🟡 {who(u)} — {e(VERDICTS[u["verdict"]])}: <i>{e(u["why"][:160])}</i>' for u in watch[:10]]
    if calm:
        lines += ['', f'<b>Похоже, ложная тревога ({len(calm)}):</b> ' + ', '.join(f'<code>{e(u["name"])}</code>' for u in calm[:15])
                  + (f' и ещё {len(calm) - 15}' if len(calm) > 15 else '')]
    if not rep_['users']:
        lines += ['', 'За сутки подозрительных не было.']
    lines += ['', 'Карточки и кнопки — в разделе «Страж» панели управления. Нейросеть может ошибаться: решение за вами.']
    out, n = [], 0
    for line in lines:
        if n + len(line) > 3800:
            out.append('…')
            break
        out.append(line)
        n += len(line) + 1
    return '\n'.join(out)


# ───────── память между проверками ─────────
def remember_devices(hw: dict, ix: dict, now: float) -> dict:
    """Какие HWID были у подписчика за HW_DAYS: {ключ: {hwid: [впервые, последний раз]}}."""
    out = {}
    for k in ix['users']:
        cur = {h: list(v) for h, v in (hw.get(k) or {}).items() if v[1] >= now - HW_DAYS * 86400}
        for d in ix['dev'].get(k, []):
            first = (cur.get(d['hwid']) or [d['created'] or now])[0]
            cur[d['hwid']] = [first, now]
        if cur:
            out[k] = cur
    return out


def remember_subips(mem: dict, ix: dict, now: float) -> dict:
    """С каких IP забирали подписку: {ключ: {ip: когда последний раз}} за SUBIP_KEEP."""
    out = {}
    for k in ix['users']:
        cur = {ip: t for ip, t in (mem.get(k) or {}).items() if t >= now - SUBIP_KEEP}
        for r in ix['subs'].get(k) or []:
            if r['ip'] and r['ip'] not in ix['own'] and r['at'] >= now - SUBIP_KEEP:
                cur[r['ip']] = max(cur.get(r['ip'], 0), r['at'])
        if cur:
            out[k] = cur
    return out


def remember_hourly(mem: dict, users: list[dict], now: float) -> dict:
    """Счётчик трафика каждый проход: {ключ: [[время, байт], …]} за сутки. Без изменений — только последняя отметка."""
    out = {}
    for u in users:
        pts = [p for p in (mem.get(u['key']) or []) if p[0] >= now - HOURLY_KEEP and p[1] <= u['used']] + [[now, u['used']]]
        out[u['key']] = pts[-30:] if len({p[1] for p in pts}) > 1 else pts[-1:]
    return out


def active_hours(pts: list | None, now: float) -> dict | None:
    """Сколько из последних 24 часов шёл трафик (от ACTIVE_HOUR) и сколько всего; None — отметок за сутки мало."""
    pts = sorted(p for p in (pts or []) if p[0] >= now - 25 * 3600)
    pairs = [(a, b) for a, b in zip(pts, pts[1:]) if 0 < b[0] - a[0] <= 2 * 3600 and b[1] >= a[1]]
    if len(pairs) < 20:
        return None
    by_hour: dict[int, int] = {}
    for a, b in pairs:
        by_hour[int(b[0] // 3600)] = by_hour.get(int(b[0] // 3600), 0) + b[1] - a[1]
    last = int(now // 3600)
    hours = [by_hour.get(h, 0) for h in range(last - 23, last + 1)]
    return {'active': sum(1 for x in hours if x >= ACTIVE_HOUR), 'total': sum(hours), 'hours': hours}


def tenant(prof: dict, admins=()) -> str:
    """Чьи это группы: общий чёрный список — только между группами одного владельца."""
    made = str(prof.get('created_by') or '')
    owner = str(prof.get('owner_tg_id') or prof.get('owner') or '')
    if owner:
        return 'owner:' + owner.lower()
    if made and made not in {str(a) for a in admins}:
        return 'user:' + made
    return 'admin'


def remember_bans(ban: dict, ix: dict, pid: int, now: float) -> dict:
    """Чёрный список владельца: HWID и имена ПК отключённых подписчиков из всех его групп ({hw|pc: {что: [группа, когда]}})."""
    out = {kind: {x: v for x, v in (ban.get(kind) or {}).items() if v[0] != pid} for kind in ('hw', 'pc')}
    for k in ix['banned']:
        for d in ix['dev'].get(k, []):
            if d['hwid'] not in ix.get('common_hw', ()):
                out['hw'][d['hwid']] = [pid, now]
            if pc_name(d) and pc_name(d) not in ix.get('common_pc', ()):
                out['pc'][pc_name(d)] = [pid, now]
    for kind in ('hw', 'pc'):  # не больше 20 000 записей: старые уходят
        if len(out[kind]) > 20000:
            out[kind] = dict(sorted(out[kind].items(), key=lambda x: -x[1][1])[:20000])
    return out


def others(ban: dict, pid: int) -> dict:
    """Записи чёрного списка из других групп."""
    return {kind: {x: v for x, v in (ban.get(kind) or {}).items() if v[0] != pid} for kind in ('hw', 'pc')}


# ───────── разбор нейросетью ─────────
AI_SYSTEM = """Ты помогаешь владельцу VPN-сервиса (панель Remnawave) понять, честный ли подписчик или он раздаёт либо
перепродаёт подписку. Тебе дают обезличенную сводку по одному подписчику: признаки, которые нашёл бот (с баллами),
устройства (HWID: платформа, модель, клиент, сеть, хостинг ли), IP подключений к нодам за последний час (сеть, хостинг ли,
к каким нодам), последние запросы подписки (клиент, ответ правила), трафик по дням и связи с другими аккаунтами.

Правило сервиса: делиться подпиской в пределах лимита устройств разрешено — близкие и друзья в разных городах
и сетях с собственными устройствами это нормально, не нарушение. Нарушение — выйти за лимит обходными путями
(ключи мимо подписки, смена устройств, раздача ссылки), перепродажа, посредник, обход бана, сбор пробных периодов.

Число IP само по себе не признак: телефон меняет IP от вышки к вышке, оператор выпускает одно устройство с нескольких
адресов, дома Wi-Fi. Значимо другое: из скольких разных СЕТЕЙ (операторов) подписчик подключён одновременно — одно
устройство не бывает в двух сетях сразу — и повторяется ли это от проверки к проверке. Обычное приложение, забравшее
подписку с IP хостинга, — чаще всего человек с включённым вторым VPN.

Как рассуждать:
- семья или компания друзей — несколько телефонов и ПК, IP одного-двух домашних провайдеров или мобильного оператора
  одного региона; 1–2 лишних IP у телефона в дороге — норма;
- раздача — IP подключений заметно больше устройств, много разных провайдеров и городов, ключи вставлены мимо подписки;
- перепродажа или посредник — устройство-сервер (3x-ui, xray, curl), подключения или запросы подписки с IP хостингов,
  ровный большой трафик;
- обход бана — общее устройство или IP с отключённым аккаунтом.
Не выдумывай фактов, которых нет в сводке. Мало данных — так и скажи (verdict "unclear").

Ответ — строго JSON на русском:
{"verdict": "honest"|"family"|"sharing"|"reselling"|"relay"|"evasion"|"unclear",
 "confidence": 0-100,
 "summary": "2–3 предложения простыми словами: что происходит",
 "evidence": ["до 5 фактов из сводки, на которых основан вывод"],
 "doubts": ["до 3 фактов, которые говорят в пользу клиента или против вывода"],
 "action": "none"|"watch"|"message"|"revoke"|"disable",
 "check": ["до 3 вещей, которые админу стоит проверить руками"]}"""

VERDICTS = {'honest': 'честный клиент', 'family': 'семья или друзья', 'sharing': 'раздаёт подписку',
            'reselling': 'перепродаёт подписку', 'relay': 'сервер-посредник', 'evasion': 'обход бана', 'unclear': 'неясно'}
AI_ACTIONS = {'none': 'ничего не делать', 'watch': 'понаблюдать', 'message': 'написать клиенту',
              'revoke': 'перевыпустить подписку', 'disable': 'отключить'}


def _mask(ip: str) -> str:
    """IP без хвоста: сети нейросети хватает, сам адрес ей не нужен."""
    parts = ip.split('.')
    return '.'.join(parts[:2] + ['x', 'x']) if len(parts) == 4 else ip[:9] + '…'


_IPV4 = re.compile(r'\b(\d{1,3})\.(\d{1,3})\.\d{1,3}\.\d{1,3}\b')


def _scrub(text: str, secrets_: list[str]) -> str:
    """Текст признака без имён, HWID и полных IP."""
    for x in sorted({x for x in secrets_ if x and len(x) >= 3}, key=len, reverse=True):
        text = text.replace(x, '«скрыто»')
    return _IPV4.sub(lambda m: f'{m.group(1)}.{m.group(2)}.x.x', text)


def ai_input(card: dict, now: float) -> str:
    """Сводка карточки без имени, TG ID, HWID и полных IP."""
    hide = [card['name'], str(card.get('tg') or ''), card.get('handle') or '', (card.get('handle') or '').lstrip('@')] + [x['name'] for x in card['links']] + [d['hwid'] for d in card['device_list']]
    hide += [d['hwid'][:40] for d in card['device_list']] + [d['model'] for d in card['device_list'] if pc_name(d)]
    days = lambda t: None if not t else round((now - t) / 86400, 1)  # noqa: E731
    net = lambda x: {k: x[k] for k in ('holder', 'hosting', 'own') if k in x} if x else None  # noqa: E731
    out = {
        'score': card['score'], 'level': card['level'] or 'clean', 'status': card['status'],
        'device_limit': card['limit'], 'devices': card['devices'], 'ips_right_now': card['ips'], 'networks_right_now': card.get('nets'),
        'checks_last_24h_with_more_networks_than_limit': card.get('nets_checks'),
        'reasons': [{'text': _scrub(r['text'], hide), 'points': r['w'], 'critical': r['crit']} for r in card['reasons']],
        'device_list': [{'platform': d['platform'], 'os': d['os'], 'model': 'уникальное имя ПК' if pc_name(d) else d['model'],
                         'server_like': d['server'], 'client': _scrub(d['ua'], hide), 'added_days_ago': days(d['created']), 'ip': _mask(d['ip']) if d['ip'] else None,
                         'net': net(d.get('net'))} for d in card['device_list'][:20]],
        'connections': [{'ip': _mask(c['ip']), 'net': net(c.get('net')), 'nodes': c['nodes'], 'live': c['live'],
                         'seen_hours_ago': None if not c['last'] else round((now - c['last']) / 3600, 1)} for c in card['conn_list'][:30]],
        'subscription_requests': [{'client': _scrub(r['ua'], hide), 'answer': r['type'], 'rule': r['rule'], 'blocked': r['blocked'],
                                   'hours_ago': round((now - r['at']) / 3600, 1) if r['at'] else None, 'net': net(r.get('net'))}
                                  for r in card['subs'][:10]],
        'traffic_gb_by_day': {t['day']: round(t['bytes'] / 2**30, 2) for t in card['traffic']},
        'distinct_devices_last_7_days': card.get('week_devices'),
        'subscription_fetch_ips_last_24h': len(card.get('sub_ips') or []),
        'hours_with_traffic_last_24h': card.get('active_hours'),
        'torrent_blocks_last_7_days': card.get('torrents'),
        'policy': 'делиться подпиской в пределах лимита устройств разрешено; разные города и сети — не нарушение',
        'linked_accounts': [{'status': x['status'], 'shared': ['device' if v == 'устройство' else 'pc_name' if v.startswith('имя ПК') else 'ip'
                                                               for v in x['via']]}
                            for x in card['links'][:10]],
    }
    if card.get('conn_err'):
        out['note'] = 'ноды не отдали IP подключений'
    return json.dumps(out, ensure_ascii=False)


def ai_clean(raw: dict) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    lst = lambda k, n: [str(x)[:300] for x in (raw.get(k) or []) if x][:n]  # noqa: E731
    try:
        conf = max(0, min(100, int(raw.get('confidence') or 0)))
    except (TypeError, ValueError):
        conf = 0
    return {'verdict': raw.get('verdict') if raw.get('verdict') in VERDICTS else 'unclear', 'confidence': conf,
            'summary': str(raw.get('summary') or '')[:1000], 'evidence': lst('evidence', 5), 'doubts': lst('doubts', 3),
            'action': raw.get('action') if raw.get('action') in AI_ACTIONS else 'watch', 'check': lst('check', 3)}


def ai_line(a: dict) -> str:
    """Одна-две строки разбора для сообщения в Telegram (HTML)."""
    e = html.escape
    return (f'🤖 <b>Нейросеть:</b> {e(VERDICTS[a["verdict"]])} ({a["confidence"]}%) — совет: {e(AI_ACTIONS[a["action"]])}'
            + (f'\n<i>{e(a["summary"][:400])}</i>' if a.get('summary') else ''))


def snapshot(data: dict, now: float) -> dict:
    """Последний срез панели для карточек: только то, что нужно, и без множеств (JSON)."""
    subs = {}
    for r in sorted(data.get('subs') or [], key=lambda r: -r['at']):
        lst = subs.setdefault(r['owner'], [])
        if len(lst) < SUBS_KEEP:
            lst.append(r)
    return {'at': now, 'users': [{**{k: v for k, v in u.items() if k != 'ids'}, 'ids': sorted(u['ids'])} for u in data['users']],
            'devices': data.get('devices') or [], 'conn': data.get('conn') or [], 'subs': [r for v in subs.values() for r in v],
            'own': sorted(data.get('own') or ()), 'conn_err': data.get('conn_err', ''), 'dev_err': data.get('dev_err', ''),
            'torrents': [t for t in data.get('torrents') or [] if t['at'] >= now - 7 * 86400]}


def unsnap(snap: dict) -> dict:
    return {**snap, 'users': [{**u, 'ids': set(u.get('ids') or [u['key']])} for u in snap.get('users') or []],
            'own': set(snap.get('own') or ())}


# ───────── страж ─────────
class Guard:
    def __init__(self, store, notify, ipinfo=None, gemini=None):
        self.store, self.notify = store, notify
        self._ipinfo = ipinfo  # для тестов: функция ip → {'holder': …}
        self._gemini = gemini  # для тестов: свой Gemini
        self._at: dict[int, float] = {}
        self._lock = asyncio.Lock()
        self.running: dict[int, dict] = {}   # идущие проверки: {pid: {started, stage}}
        self.nightly: set[int] = set()       # идущие итоги дня по кнопке
        self._tasks: set = set()
        self.panel_button = None             # кнопка «Панель управления» (задаёт автопилот)

    def get(self, key: str, default):
        try:
            v = json.loads(self.store.get(key) or 'null')
        except ValueError:
            v = None
        return v if isinstance(v, type(default)) else default

    def put(self, key: str, v) -> None:
        self.store.set(key, json.dumps(v, ensure_ascii=False))

    @staticmethod
    def panel(prof: dict):
        from . import engine
        return engine.Panel(prof['panel_url'], prof['panel_token'], prof.get('panel_cookie', ''), prof.get('panel_headers'))

    def lookup(self, ips: list[str], budget: float = IP_BUDGET) -> dict:
        """Сеть IP (держатель ASN): из кэша, новые — параллельно, не больше IP_LOOKUPS и budget секунд за проход
        (что не успели — останется в кэше к следующей проверке)."""
        if self._ipinfo is not None:
            return {ip: self._ipinfo(ip) or {} for ip in ips}
        from concurrent.futures import ThreadPoolExecutor, wait
        from . import banlab
        out, new = {}, []
        for ip in ips:
            hit = banlab.cached_info(self.store, ip, fetch=False)
            if hit:
                out[ip] = hit
            elif len(new) < IP_LOOKUPS:
                new.append(ip)
        if new:
            ex = ThreadPoolExecutor(max_workers=8, thread_name_prefix='guard-ip')
            futs = {ex.submit(banlab.cached_info, self.store, ip): ip for ip in new}
            done, _ = wait(futs, timeout=budget)
            ex.shutdown(wait=False, cancel_futures=True)
            for f in done:
                try:
                    hit = f.result()
                except Exception:  # noqa: BLE001
                    hit = None
                if hit:
                    out[futs[f]] = hit
        return out

    async def tick(self, profiles: list[dict], now: float | None = None) -> None:
        now = now if now is not None else time.time()
        if self._lock.locked():
            return
        async with self._lock:
            for prof in profiles:
                if not cfg(prof)['on'] or now - self._at.get(prof['id'], 0) < EVERY - 60 or prof['id'] in self.running:
                    continue
                self._at[prof['id']] = now
                await self.run(prof, now)
            for prof in profiles:
                await self.night_due(prof, now)

    async def night_due(self, prof: dict, now: float) -> None:
        """Итог дня — один раз после полуночи по времени группы; включили днём — первый итог в ближайшую полночь."""
        conf = cfg(prof)
        if not (conf['on'] and conf['night']):
            return
        key, today = f'guard:night_day:{prof["id"]}', day_of(now, conf['tz'])
        done = self.store.get(key)
        if done == today:
            return
        self.store.set(key, today)
        if not done:
            return  # только что включили: ночь ещё не наступила
        try:
            await self.night(prof, now)
        except Exception as e:  # noqa: BLE001
            log.info('guard night %s: %s', prof.get('id'), e)
            await self.notify.emit('security.guard', f'🌙 Итог дня стража · {html.escape(prof["name"])}: '
                                                                     f'нейросеть не ответила — {html.escape(str(e)[:200])}', prof)

    async def run(self, prof: dict, now: float | None = None) -> None:
        """Проверка группы с отметкой «идёт» (её видно в разделе) и записью ошибки в итог."""
        pid, now = prof['id'], now if now is not None else time.time()
        self.running[pid] = {'started': now, 'stage': 'Начинаю'}
        try:
            await self.check(prof, now)
        except Exception as e:  # noqa: BLE001 — одна группа не должна мешать остальным
            log.info('guard %s: %s', pid, e)
            last = self.get(f'guard:last:{pid}', {})
            self.put(f'guard:last:{pid}', {**last, 'at': now, 'error': str(e)[:300]})
        finally:
            self.running.pop(pid, None)

    def start(self, prof: dict) -> dict:
        """«Проверить сейчас»: в фоне, сразу возвращает состояние; уже идёт — вторую не запускаем."""
        pid = prof['id']
        if pid not in self.running:
            self.running[pid] = {'started': time.time(), 'stage': 'Начинаю'}
            t = asyncio.create_task(self.run(prof))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        return dict(self.running.get(pid) or {})

    def start_night(self, prof: dict) -> None:
        """«Итог дня сейчас»: в фоне; ошибка — сообщением в Telegram."""
        pid = prof['id']
        if pid in self.nightly:
            return
        self.nightly.add(pid)

        async def go():
            try:
                await self.night(prof)
            except Exception as e:  # noqa: BLE001
                await self.notify.emit('security.guard', f'🌙 Итог дня стража · {html.escape(prof["name"])}: '
                                                         f'не получилось — {html.escape(str(e)[:200])}', prof)
            finally:
                self.nightly.discard(pid)
        t = asyncio.create_task(go())
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def stage(self, pid: int, text_: str) -> None:
        if pid in self.running:
            self.running[pid]['stage'] = text_

    def own_ips(self, prof: dict) -> set[str]:
        """Свои серверы группы, через которые идут пользователи: входы мостов и проброса (их IP — не «хостинг клиента»)."""
        try:
            return {str(r.get('ip')) for r in self.store.relays() if r.get('ip')}
        except Exception:  # noqa: BLE001
            return set()

    def ban_key(self, prof: dict) -> str:
        return 'guard:ban:' + tenant(prof, getattr(self.notify, 'admins', ()) or ())

    def memory(self, prof: dict, ix: dict, data: dict, now: float, info: dict | None = None) -> dict:
        """Обновить память между проверками (устройства, IP подписки, почасовой трафик, чёрный список) и отдать её разбору."""
        pid = prof['id']
        hw = remember_devices(self.get(f'guard:hw:{pid}', {}), ix, now)
        subip = remember_subips(self.get(f'guard:subip:{pid}', {}), ix, now)
        hourly = remember_hourly(self.get(f'guard:hourly:{pid}', {}), data['users'], now)
        ban = remember_bans(self.get(self.ban_key(prof), {}), ix, pid, now)
        conc = remember_conc(self.get(f'guard:conc:{pid}', {}), ix, info or {}, now)
        for key, val in ((f'guard:hw:{pid}', hw), (f'guard:subip:{pid}', subip), (f'guard:hourly:{pid}', hourly), (self.ban_key(prof), ban),
                         (f'guard:conc:{pid}', conc)):
            self.put(key, val)
        return {'hw': hw, 'subip': subip, 'hourly': hourly, 'ban': others(ban, pid), 'conc': conc}

    def recall(self, prof: dict) -> dict:
        """Память для карточки — только чтение."""
        pid = prof['id']
        return {'hw': self.get(f'guard:hw:{pid}', {}), 'subip': self.get(f'guard:subip:{pid}', {}),
                'hourly': self.get(f'guard:hourly:{pid}', {}), 'ban': others(self.get(self.ban_key(prof), {}), pid),
                'conc': self.get(f'guard:conc:{pid}', {})}

    def crunch(self, prof: dict, data: dict, now: float) -> tuple[list[dict], dict, set]:
        """Тяжёлая часть проверки (в потоке, чтобы не задерживать бота): трафик, сети IP, разбор, срез для карточек."""
        conf, pid = cfg(prof), prof['id']
        hist = remember_traffic(self.get(f'guard:traf:{pid}', {}), data['users'], now, conf['tz'])
        self.put(f'guard:traf:{pid}', hist)
        ix = index(data, conf['names'])
        conn_ips = {c['ip'] for k in ix['conn'] for c in live_ips(ix, k, now)}
        dev_ips = {d['ip'] for d in data['devices'] if d['ip'] and d['ip'] not in data['own']}
        self.stage(pid, 'Узнаю, чьи сети у IP')
        sub_ips = {r['ip'] for rs in ix['subs'].values() for r in rs if r['ip'] and r['ip'] not in data['own'] and r['at'] >= now - 86400}
        info = self.lookup(sorted(conn_ips | dev_ips) + sorted(sub_ips - conn_ips - dev_ips))  # сначала важные: запас запросов не бесконечен
        self.stage(pid, 'Разбираю признаки')
        ex = self.memory(prof, ix, data, now, info)
        found = analyze(data, conf, hist, info, now, ix, ex)
        self.put(f'guard:snap:{pid}', snapshot(data, now))  # до сообщений: разбор нейросетью берёт карточку из среза
        return found, ix, conn_ips

    async def check(self, prof: dict, now: float, data: dict | None = None) -> list[dict]:
        conf, pid = cfg(prof), prof['id']
        if data is None:
            data = await asyncio.to_thread(collect, self.panel(prof), self.own_ips(prof), True, lambda t: self.stage(pid, t))
        found, ix, conn_ips = await asyncio.to_thread(self.crunch, prof, data, now)
        seen = self.get(f'guard:seen:{pid}', {})
        ok = set(self.get(f'guard:ok:{pid}', []))
        acted = {'revoke': 0, 'disable': 0}
        todo = fresh(found, seen, ok, now)
        if todo:
            self.stage(pid, f'Отправляю сообщения: {len(todo)}')
        rest = []
        for i, f in enumerate(todo):
            seen[f['key']] = {'sig': signature(f), 'ts': now, 'codes': [r['code'] for r in f['reasons']]}
            done = ''
            if f['crit'] and conf['auto'] != 'off' and f['key'] not in ok:
                done = await self.act(prof, f['key'], conf['auto'])
                acted[conf['auto']] += not done.startswith('❌')
            if i < ALERTS_EACH or done:
                await self.alert(prof, f, done, await self.ai_auto(prof, f['key'], now) if conf['ai'] else None)
            else:
                rest.append(f)
        if rest:  # первая проверка большой панели — не заваливать сообщениями
            await self.digest(prof, rest)
        live = {f['key'] for f in found}
        seen = {k: v for k, v in seen.items() if k in live or now - v.get('ts', 0) < REMIND}
        self.put(f'guard:seen:{pid}', seen)
        self.count(pid, now, conf['tz'], warn=sum(f['level'] == 'warn' for f in found), crit=sum(f['level'] == 'crit' for f in found),
                   users=len(ix['users']), **acted)
        self.put(f'guard:last:{pid}', {'at': now, 'users': len(ix['users']), 'skipped': len(data['users']) - len(ix['users']),
                                       'devices': len(data['devices']),
                                       'ips': len(conn_ips), 'online': sum(1 for k in ix['conn'] if live_ips(ix, k, now)),
                                       'dev_err': data.get('dev_err', ''), 'conn_err': data.get('conn_err', ''),
                                       'subs_err': data.get('subs_err', ''), 'error': '',
                                       'found': [{k: f[k] for k in ('key', 'name', 'tg', 'handle', 'score', 'level', 'devices', 'ips', 'limit')}
                                                 | {'reasons': [r['text'] for r in f['reasons']], 'ok': f['key'] in ok}
                                                 for f in found[:100]]})
        return found

    async def digest(self, prof: dict, rest: list[dict]) -> None:
        """Одно сообщение вместо пачки: остальные подозрительные списком, подробности — в разделе."""
        e = html.escape
        crit = sum(f['level'] == 'crit' for f in rest)
        lines = [f'🛡 <b>Страж: ещё {len(rest)} подозрительных</b> · {e(prof["name"])}' + (f' (критично: {crit})' if crit else ''), '']
        lines += [f'{LEVELS[f["level"]]} {e(f["name"])} — {e(f["reasons"][0]["text"][:120])}' for f in rest[:15]]
        if len(rest) > 15:
            lines.append(f'… и ещё {len(rest) - 15}')
        lines += ['', 'Подробности и кнопки — в разделе «Страж» панели управления.']
        mk = self.panel_button() if callable(self.panel_button) else None
        await self.notify.emit('security.guard', '\n'.join(lines)[:3900], prof, mk)

    def count(self, pid: int, now: float, tz: int, **vals) -> None:
        """Сводка по дням за 30 дней: сколько подозрительных (максимум за день) и сколько действий."""
        daily = self.get(f'guard:daily:{pid}', {})
        d = daily.setdefault(day_of(now, tz), {})
        for k, v in vals.items():
            d[k] = max(d.get(k, 0), v) if k in ('warn', 'crit', 'users') else d.get(k, 0) + v
        keep = sorted(daily)[-30:]
        self.put(f'guard:daily:{pid}', {k: daily[k] for k in keep})

    def detail(self, prof: dict, key: str, now: float | None = None) -> dict | None:
        """Карточка подписчика по последней проверке: признаки, устройства, IP подключений, запросы подписки,
        трафик по дням и связанные аккаунты (общие устройства и IP)."""
        now = now if now is not None else time.time()
        pid, conf = prof['id'], cfg(prof)
        snap = self.get(f'guard:snap:{pid}', {})
        if not snap:
            return None
        data = unsnap(snap)
        ix = index(data, conf['names'])
        if key not in ix['users']:
            return None
        ips = sorted({d['ip'] for d in ix['dev'].get(key, []) if d['ip']} | set(ix['conn'].get(key) or {}))
        info = self.cached(ips)
        hist = self.get(f'guard:traf:{pid}', {})
        ex = self.recall(prof)
        at = snap.get('at') or now
        subips = sorted(((ip, t) for ip, t in (ex['subip'].get(key) or {}).items() if t >= at - 86400), key=lambda x: -x[1])
        info.update(self.cached([ip for ip, _ in subips if ip not in info]))
        f = judge(ix, key, conf, hist, info, at, ex)
        now_ips = {c['ip'] for c in concurrent(ix, key)}
        cur = {d['hwid'] for d in ix['dev'].get(key, [])}
        hours = active_hours(ex['hourly'].get(key), at)
        net = lambda ip: {'holder': holder(info, ip), 'hosting': hosting(holder(info, ip)), 'own': ip in ix['own'],  # noqa: E731
                          'relay': bool(RELAY_NET.search(holder(info, ip)))}
        links = {}
        for d in ix['dev'].get(key, []):
            for o in (ix['hwid'].get(d['hwid'], set()) - {key}) if d['hwid'] not in ix['common_hw'] else ():
                links.setdefault(o, set()).add('устройство')
            for o in (ix['pc'].get(pc_name(d)) or {}) if pc_name(d) and pc_name(d) not in ix['common_pc'] else ():
                if o != key:
                    links.setdefault(o, set()).add(f'имя ПК {d["model"]}')
        for ip in ips:
            for o in ix['ip'].get(ip, set()) - {key}:
                links.setdefault(o, set()).add(f'IP {ip}')
        h = hist.get(key) or {}
        days = sorted(h)
        traffic_days = [{'day': days[i], 'bytes': h[days[i + 1]] - h[days[i]]} for i in range(len(days) - 1) if h[days[i + 1]] >= h[days[i]]]
        u = ix['users'][key]
        return {**{k: f[k] for k in ('key', 'name', 'tg', 'handle', 'status', 'score', 'level', 'devices', 'ips', 'nets', 'sub_nets', 'nets_checks',
                                      'limit', 'yesterday', 'usual')},
                'conc_history': [{'at': e[0], 'nets': e[1], 'ips': e[2]} for e in (ex['conc'].get(key) or []) if e[0] >= at - 86400],
                'reasons': [{'text': r['text'], 'w': r['w'], 'crit': r['crit']} for r in f['reasons']],
                'used': u['used'], 'at': snap.get('at'), 'ok': key in set(self.get(f'guard:ok:{pid}', [])),
                'device_list': [{**{k: d[k] for k in ('hwid', 'platform', 'os', 'model', 'ua', 'ip', 'created')},
                                 'server': bool(SERVER_UA.search(' '.join((d['ua'], d['hwid']))) and not CLIENT_UA.search(d['ua'])),
                                 **({'net': net(d['ip'])} if d['ip'] else {})} for d in ix['dev'].get(key, [])],
                'conn_list': sorted(({**c, 'live': c['ip'] in now_ips, 'net': net(c['ip'])}
                                     for c in (ix['conn'].get(key) or {}).values()), key=lambda c: -(c['last'] or 0)),
                'subs': [{**r, 'blocked': r['type'] in BLOCKED, **({'net': net(r['ip'])} if r['ip'] else {})}
                         for r in (ix['subs'].get(key) or [])[:SUBS_KEEP]],
                'traffic': traffic_days[-8:],
                'links': [{'key': o, 'name': ix['users'][o]['name'], 'status': ix['users'][o]['status'], 'via': sorted(v)}
                          for o, v in sorted(links.items()) if o in ix['users']][:20],
                'conn_err': snap.get('conn_err', ''), 'dev_err': snap.get('dev_err', ''),
                'week_devices': f['week_devices'], 'active_hours': f['active_hours'], 'torrents': f['torrents'],
                'device_history': [{'hwid': h, 'first': v[0], 'last': v[1], 'current': h in cur}
                                   for h, v in sorted((ex['hw'].get(key) or {}).items(), key=lambda x: -x[1][1])][:40],
                'sub_ips': [{'ip': ip, 'at': t, 'net': net(ip)} for ip, t in subips[:40]],
                'hourly': hours['hours'] if hours else None,
                'torrent_list': sorted((t for t in ix['torrent'].get(key, [])), reverse=True)[:20],
                'ai': self.ai_cached(pid, key)}

    def search(self, pid: int, q: str, limit: int = 20) -> list[dict]:
        snap = self.get(f'guard:snap:{pid}', {})
        q = q.strip().lower()
        names = self.store.profile(pid) if hasattr(self.store, 'profile') else None
        rx = re.compile(cfg(names or {})['names'], re.I) if cfg(names or {})['names'] else None
        out = [{'key': u['key'], 'name': u['name'], 'status': u['status'], 'tg': u['tg'], 'handle': u.get('handle', ''),
                'checked': rx is None or bool(rx.search(u['name']))} for u in snap.get('users') or []
               if q and (q in u['name'].lower() or q == str(u.get('tg') or '') or q == u['key'].lower())]
        return out[:limit]

    def cached(self, ips: list[str]) -> dict:
        """Сети IP только из кэша — для карточки, без походов в RIPEstat."""
        if self._ipinfo is not None:
            return {ip: self._ipinfo(ip) or {} for ip in ips}
        from . import banlab
        return {ip: hit for ip in ips if (hit := banlab.cached_info(self.store, ip, fetch=False))}

    # ── нейросеть ──
    def gemini(self):
        """Gemini администратора; None — ключа нет."""
        if self._gemini is not None:
            return self._gemini
        if not self.store.get('gemini_key'):
            return None
        from . import scripts as sc
        return sc.Gemini(self.store.get('gemini_key'), self.store.get('gemini_model'), self.store.get('gemini_proxy'),
                         base=self.store.get('gemini_base') or None)

    def ai_cached(self, pid: int, key: str) -> dict | None:
        hit = self.get(f'guard:ai:{pid}:{key}', {})
        return hit or None

    async def ai(self, prof: dict, key: str, now: float | None = None, force: bool = False) -> dict:
        """Разбор подписчика нейросетью по карточке последней проверки. Тот же набор признаков за сутки — из кэша.
        Нейросеть не видит имени, TG ID, HWID и полных IP. Ошибки — ValueError с понятным текстом."""
        now = now if now is not None else time.time()
        g = self.gemini()
        if g is None:
            raise ValueError('Нейросеть не подключена: задайте ключ Gemini в «Подключениях»')
        card = await asyncio.to_thread(self.detail, prof, key, now)
        if card is None:
            raise ValueError('Подписчик не найден в последней проверке — нажмите «Проверить сейчас»')
        sig = hashlib.sha1(json.dumps([sorted(r['text'] for r in card['reasons']), card['devices'], card['ips'], card['at']],
                                      ensure_ascii=False).encode()).hexdigest()[:12]
        hit = self.ai_cached(prof['id'], key)
        if hit and not force and hit.get('sig') == sig and now - hit.get('at', 0) < AI_TTL:
            return hit
        from . import scripts as sc
        try:
            raw = await g.ask(AI_SYSTEM, [{'role': 'user', 'text': ai_input(card, card.get('at') or now)}])
        except sc.ScriptError as e:
            raise ValueError(str(e)) from None
        out = {**ai_clean(raw), 'sig': sig, 'at': now}
        self.put(f'guard:ai:{prof["id"]}:{key}', out)
        return out

    def night_candidates(self, prof: dict, now: float) -> list[dict]:
        """Кого разбирать ночью: подозрительные последней проверки и все, о ком страж писал за сутки, — по баллам."""
        pid = prof['id']
        keys = {f['key'] for f in self.get(f'guard:last:{pid}', {}).get('found') or []}
        keys |= {k for k, v in self.get(f'guard:seen:{pid}', {}).items() if now - v.get('ts', 0) < 86400}
        cards = [c for c in (self.detail(prof, k, now) for k in keys) if c]
        return sorted(cards, key=lambda c: -c['score'])[:NIGHT_MAX]

    async def night(self, prof: dict, now: float | None = None) -> dict:
        """Итог дня: одна просьба к нейросети по всем подозрительным группы; ответ — в Telegram и в раздел «Страж».
        Нейросеть не видит имён, TG ID, HWID и полных IP. Ошибки — ValueError."""
        now = now if now is not None else time.time()
        pid, conf = prof['id'], cfg(prof)
        g = self.gemini()
        if g is None:
            raise ValueError('Нейросеть не подключена: задайте ключ Gemini в «Подключениях»')
        cards = await asyncio.to_thread(self.night_candidates, prof, now)
        day = day_of(now - 3600, conf['tz'])  # в полночь подводим итог прошедших суток
        report = {'day': day, 'at': now, 'summary': '', 'users': []}
        if cards:
            ids = {f'S{i + 1}': c['key'] for i, c in enumerate(cards)}
            body = [{'id': f'S{i + 1}', **json.loads(ai_input(c, c.get('at') or now))} for i, c in enumerate(cards)]
            from . import scripts as sc
            try:
                raw = await g.ask(NIGHT_SYSTEM, [{'role': 'user', 'text': json.dumps(body, ensure_ascii=False)}])
            except sc.ScriptError as e:
                raise ValueError(str(e)) from None
            report.update(night_clean(raw, ids))
            by_key = {c['key']: c for c in cards}
            for u in report['users']:
                c = by_key[u['key']]
                u.update({'name': c['name'], 'handle': c.get('handle', ''), 'score': c['score'], 'level': c['level']})
                if not self.ai_cached(pid, u['key']) or self.ai_cached(pid, u['key']).get('at', 0) < now - AI_TTL:
                    self.put(f'guard:ai:{pid}:{u["key"]}', {'verdict': u['verdict'], 'confidence': u['confidence'], 'summary': u['why'],
                                                           'evidence': [], 'doubts': [], 'action': u['action'], 'check': [],
                                                           'sig': '', 'at': now, 'night': True})
            order = {'disable': 0, 'revoke': 1, 'message': 2, 'watch': 3, 'none': 4}
            report['users'].sort(key=lambda u: (order.get(u['action'], 5), -u['confidence']))
        hist = [r for r in self.get(f'guard:nights:{pid}', []) if r.get('day') != day]
        self.put(f'guard:nights:{pid}', ([report] + hist)[:NIGHT_KEEP])
        mk = self.panel_button() if callable(self.panel_button) else None
        await self.notify.emit('security.guard_night', night_text(prof, report), prof, mk)
        return report

    async def ai_auto(self, prof: dict, key: str, now: float) -> dict | None:
        """Разбор для сообщения: не больше AI_DAY в сутки на группу; не вышло — сообщение уйдёт без него."""
        key_used, today = f'guard:ai_used:{prof["id"]}', day_of(now, 0)
        used = self.get(key_used, {}).get(today, 0)
        if used >= AI_DAY or self.gemini() is None:
            return None
        self.put(key_used, {today: used + 1})
        try:
            return await self.ai(prof, key, now)
        except Exception as e:  # noqa: BLE001
            log.info('guard ai %s %s: %s', prof.get('id'), key, e)
            return None

    # ── кнопки и действия ──
    def token(self, pid: int, f: dict, now: float) -> str:
        toks = {t: v for t, v in self.get(f'guard:tok:{pid}', {}).items() if now - v.get('ts', 0) < TOKEN_TTL}
        t = secrets.token_hex(4)
        toks[t] = {'key': f['key'], 'name': f['name'], 'ts': now}
        self.put(f'guard:tok:{pid}', toks)
        return t

    def find(self, pid: int, tok: str) -> dict | None:
        return self.get(f'guard:tok:{pid}', {}).get(tok)

    def markup(self, pid: int, tok: str):
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        b = lambda label, a: InlineKeyboardButton(text=label, callback_data=f'gd:{a}:{pid}:{tok}')  # noqa: E731
        return InlineKeyboardMarkup(inline_keyboard=[[b('🔄 Перевыпустить', 'r'), b('⛔ Отключить', 'd')],
                                                     [b('✅ Всё в порядке', 'o')]])

    async def alert(self, prof: dict, f: dict, done: str = '', ai: dict | None = None) -> None:
        tok = self.token(prof['id'], f, time.time())
        try:
            mk = self.markup(prof['id'], tok)
        except ImportError:  # тесты без aiogram
            mk = None
        await self.notify.emit('security.guard', text(prof, f, done, ai), prof, mk)

    async def act(self, prof: dict, key: str, what: str) -> str:
        """revoke | disable | ok. Возвращает строку для пользователя."""
        pid = prof['id']
        if what == 'ok':
            ok = self.get(f'guard:ok:{pid}', [])
            if key not in ok:
                self.put(f'guard:ok:{pid}', (ok + [key])[-2000:])
            return '✅ Отмечено: всё в порядке. Об этом пользователе бот напишет, только если появится что-то критичное'
        path = {'revoke': 'revoke', 'disable': 'disable'}.get(what)
        if not path:
            raise ValueError('Неизвестное действие')
        p = self.panel(prof)
        try:
            await asyncio.to_thread(p.call, 'POST', f'/api/users/{key}/actions/{path}', {} if path == 'revoke' else None)
        except Exception as e:  # noqa: BLE001
            return f'❌ Панель не выполнила: {str(e)[:200]}'
        return ('🔄 Подписка перевыпущена: старая ссылка и ключи больше не работают' if what == 'revoke'
                else '⛔ Пользователь отключён в панели')

    async def button(self, pid: int, tok: str, what: str) -> str:
        prof = self.store.profile(pid)  # из базы: настройки группы могли поменяться
        hit = self.find(pid, tok)
        if not prof or not hit:
            raise ValueError('Кнопка устарела — откройте группу в панели управления')
        return await self.act(prof, hit['key'], {'r': 'revoke', 'd': 'disable', 'o': 'ok'}[what])
