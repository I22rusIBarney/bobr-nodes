"""Страж подписок: ищет в панели Remnawave тех, кто раздаёт или перепродаёт подписку.

Раз в час по каждой группе, где страж включён, бот скачивает из панели пользователей и их HWID-устройства и
смотрит на признаки:
- устройство — сервер или скрипт (3x-ui, xray, curl, python…), а не приложение на телефоне или компьютере;
- устройство без платформы и модели;
- подписку забирали с IP хостинга (свои ноды не в счёт: подписка могла обновиться через наш же VPN);
- много устройств за сутки, устройства в разных сетях;
- трафик за вчера выше порога группы или в разы выше обычного для этого пользователя;
- то же устройство или IP, что у отключённого в панели пользователя (обход бана), то же устройство у другого.

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
PAGE = 500
MAX_USERS = 50_000

SERVER_UA = re.compile(r'3x-?ui|x-ui|xray|v2ray-?core|sing-?box-?server|marzban|remnawave|curl|wget|python|aiohttp|httpx|'
                       r'go-http|okhttp|node-fetch|axios|java/|postman|insomnia|libwww|scrapy|headless', re.I)
CLIENT_UA = re.compile(r'happ|incy|v2raytun|v2rayng|hiddify|streisand|shadowrocket|stash|clash|mihomo|flclash|karing|'
                       r'nekobox|nekoray|singbox|sfa|sfi|sfm|foxray|v2box|throne|koala|prizrak|ssclash', re.I)
HOSTING = re.compile(r'hetzner|\bovh|digitalocean|amazon|\baws\b|google|microsoft|azure|oracle|linode|akamai|vultr|choopa|'
                     r'contabo|leaseweb|m247|aeza|timeweb|selectel|beget|vdsina|firstbyte|stark|hostinger|hostkey|serverius|'
                     r'scaleway|ionos|netcup|kamatera|g-?core|cloudflare|datacamp|cdn77|psychz|hivelocity|quadranet|'
                     r'colocrossing|buyvm|frantech|pq hosting|alibaba|tencent|huawei cloud|hosting|datacent|data cent|'
                     r'\bcloud|\bvps|server|dedicated|colo', re.I)

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
            'tz': int(g.get('tz', (prof.get('auto') or {}).get('tz', 0)))}


def clean(raw: dict, prof: dict) -> dict:
    g = cfg(prof)
    if 'on' in raw:
        g['on'] = bool(raw['on'])
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
            'tg': u.get('telegramId'), 'limit': u.get('hwidDeviceLimit'), 'used': int(float(used or 0))}


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


def collect(panel) -> dict:
    """Пользователи, устройства и адреса своих нод из панели (в потоке)."""
    users = [x for x in (norm_user(u) for u in pages(panel.call, '/api/users', 'users')) if x]
    devices, dev_err = [], ''
    try:
        devices = [x for x in (norm_device(d) for d in pages(panel.call, '/api/hwid/devices', 'devices')) if x]
    except Exception as e:  # noqa: BLE001 — старая панель без списка устройств: смотрим только трафик
        dev_err = str(e)[:200]
    own = set()
    try:
        from . import engine
        addrs = {str(n.get('address') or '') for n in panel.call('GET', '/api/nodes') if isinstance(n, dict)}
        own = {a for a in addrs if re.fullmatch(r'[\d.]+', a)}
        for ips in engine.resolve_many([a for a in addrs if a and a not in own]).values():
            own |= set(ips)
    except Exception:  # noqa: BLE001 — без адресов нод просто не отличим «обновил через наш VPN»
        pass
    return {'users': users, 'devices': devices, 'dev_err': dev_err, 'own': own}


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
def analyze(data: dict, conf: dict, hist: dict, ipinfo: dict, now: float) -> list[dict]:
    """Подозрительные пользователи: [{key, name, score, level, reasons: [{code, w, text}], crit}], по убыванию баллов.
    ipinfo — {ip: {'holder': …}} (что успели узнать); hist — отметки трафика (remember_traffic)."""
    users = {u['key']: u for u in data['users'] if not u['name'].startswith('bridge_')}  # служебные пользователи мостов
    alias = {i: u['key'] for u in users.values() for i in u['ids']}
    by_user: dict[str, list[dict]] = {}
    hwid_users: dict[str, set[str]] = {}
    ip_users: dict[str, set[str]] = {}
    for d in data['devices']:
        k = alias.get(d['owner'])
        if not k:
            continue
        by_user.setdefault(k, []).append(d)
        hwid_users.setdefault(d['hwid'], set()).add(k)
        if d['ip'] and d['ip'] not in data['own']:
            ip_users.setdefault(d['ip'], set()).add(k)
    banned = {k for k, u in users.items() if u['status'] == 'DISABLED'}
    out = []
    for k, u in users.items():
        if k in banned:
            continue
        reasons: list[dict] = []

        def add(code, w, text, crit=False):
            reasons.append({'code': code, 'w': w, 'text': text, 'crit': crit})

        devs = by_user.get(k, [])
        for d in devs:
            ident = ' '.join(x for x in (d['ua'], d['hwid'], d['platform'], d['model']) if x)
            if SERVER_UA.search(ident) and not CLIENT_UA.search(d['ua'] or ''):
                add('server:' + d['hwid'], CRIT, f'устройство похоже на сервер или скрипт: {d["ua"] or d["hwid"][:40]}', True)
            elif not (d['platform'] or d['model'] or d['os']) and not CLIENT_UA.search(d['ua']):
                add('bare:' + d['hwid'], 20, f'устройство без платформы и модели ({d["ua"] or "без User-Agent"})')
        hosted = sorted({d['ip'] for d in devs if d['ip'] and d['ip'] not in data['own']
                         and HOSTING.search((ipinfo.get(d['ip']) or {}).get('holder') or '')})
        for ip in hosted[:3]:
            add('host:' + ip, 60, f'подписку забирали с IP хостинга {ip} ({ipinfo[ip]["holder"][:40]})')
        nets = {(ipinfo.get(d['ip']) or {}).get('holder') for d in devs if d['ip'] and d['ip'] not in data['own']} - {None, ''}
        if len(nets) >= 4:
            add('nets', 20, f'устройства в {len(nets)} разных сетях')
        if conf['burst']:
            fresh = [d for d in devs if d['created'] and now - d['created'] < 86400]
            if len(fresh) >= conf['burst']:
                add('burst', 30, f'{len(fresh)} новых устройств за сутки')
        for d in devs:
            other = hwid_users.get(d['hwid'], set()) - {k}
            for o in sorted(other)[:2]:
                if o in banned:
                    add('ban-hwid:' + o, CRIT, f'то же устройство, что у отключённого {users[o]["name"]}', True)
                else:
                    add('twin:' + o, 40, f'то же устройство, что у {users[o]["name"]}')
        for d in devs:
            if not d['ip'] or d['ip'] not in ip_users:
                continue
            for o in sorted((ip_users[d['ip']] & banned) - {k})[:2]:
                add('ban-ip:' + o, 50, f'тот же IP {d["ip"]}, что у отключённого {users[o]["name"]}')
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
        if score < WARN and not crit:
            continue
        out.append({'key': k, 'name': u['name'], 'tg': u['tg'], 'score': score, 'crit': crit,
                    'level': 'crit' if crit or score >= CRIT else 'warn', 'reasons': reasons, 'devices': len(devs),
                    'limit': u['limit']})
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


def text(prof: dict, f: dict, done: str = '') -> str:
    e = html.escape
    who = e(f['name']) + (f' · TG <code>{e(str(f["tg"]))}</code>' if f.get('tg') else '')
    lines = [f'{LEVELS[f["level"]]} <b>Подозрительная подписка</b> · {e(prof["name"])}', who,
             f'Устройств: {f["devices"]}' + (f' из {f["limit"]}' if f.get('limit') else ''), '']
    lines += ['• ' + e(r['text']) for r in f['reasons'][:8]]
    if done:
        lines += ['', done]
    return '\n'.join(lines)[:3900]


# ───────── страж ─────────
class Guard:
    def __init__(self, store, notify, ipinfo=None):
        self.store, self.notify = store, notify
        self._ipinfo = ipinfo  # для тестов: функция ip → {'holder': …}
        self._at: dict[int, float] = {}
        self._lock = asyncio.Lock()

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

    def lookup(self, ips: list[str]) -> dict:
        """Сеть IP (держатель ASN): из кэша, новые — понемногу за проход."""
        out, asked = {}, 0
        if self._ipinfo is not None:
            return {ip: self._ipinfo(ip) or {} for ip in ips}
        from . import banlab
        for ip in ips:
            hit = banlab.cached_info(self.store, ip, fetch=False)
            if not hit and asked < IP_LOOKUPS:
                asked += 1
                hit = banlab.cached_info(self.store, ip)
            if hit:
                out[ip] = hit
        return out

    async def tick(self, profiles: list[dict], now: float | None = None) -> None:
        now = now if now is not None else time.time()
        if self._lock.locked():
            return
        async with self._lock:
            for prof in profiles:
                if not cfg(prof)['on'] or now - self._at.get(prof['id'], 0) < EVERY - 60:
                    continue
                self._at[prof['id']] = now
                try:
                    await self.check(prof, now)
                except Exception as e:  # noqa: BLE001 — одна группа не должна мешать остальным
                    log.info('guard %s: %s', prof.get('id'), e)
                    last = self.get(f'guard:last:{prof["id"]}', {})
                    self.put(f'guard:last:{prof["id"]}', {**last, 'at': now, 'error': str(e)[:300]})

    async def check(self, prof: dict, now: float, data: dict | None = None) -> list[dict]:
        conf, pid = cfg(prof), prof['id']
        if data is None:
            data = await asyncio.to_thread(collect, self.panel(prof))
        hist = remember_traffic(self.get(f'guard:traf:{pid}', {}), data['users'], now, conf['tz'])
        self.put(f'guard:traf:{pid}', hist)
        ips = sorted({d['ip'] for d in data['devices'] if d['ip'] and d['ip'] not in data['own']})
        info = await asyncio.to_thread(self.lookup, ips)
        found = analyze(data, conf, hist, info, now)
        seen = self.get(f'guard:seen:{pid}', {})
        ok = set(self.get(f'guard:ok:{pid}', []))
        for f in fresh(found, seen, ok, now):
            seen[f['key']] = {'sig': signature(f), 'ts': now, 'codes': [r['code'] for r in f['reasons']]}
            done = ''
            if f['crit'] and conf['auto'] != 'off' and f['key'] not in ok:
                done = await self.act(prof, f['key'], conf['auto'])
            await self.alert(prof, f, done)
        live = {f['key'] for f in found}
        seen = {k: v for k, v in seen.items() if k in live or now - v.get('ts', 0) < REMIND}
        self.put(f'guard:seen:{pid}', seen)
        self.put(f'guard:last:{pid}', {'at': now, 'users': len(data['users']), 'devices': len(data['devices']),
                                       'dev_err': data.get('dev_err', ''), 'error': '',
                                       'found': [{k: f[k] for k in ('key', 'name', 'tg', 'score', 'level', 'devices', 'limit')}
                                                 | {'reasons': [r['text'] for r in f['reasons']], 'ok': f['key'] in ok}
                                                 for f in found[:40]]})
        return found

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

    async def alert(self, prof: dict, f: dict, done: str = '') -> None:
        tok = self.token(prof['id'], f, time.time())
        try:
            mk = self.markup(prof['id'], tok)
        except ImportError:  # тесты без aiogram
            mk = None
        await self.notify.emit('security.guard', text(prof, f, done), prof, mk)

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
