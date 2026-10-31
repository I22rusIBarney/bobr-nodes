"""Движок установки одной ноды: Cloudflare DNS → SSH → Nuvrion Vision Installer → панель Remnawave → проверка.

Синхронный (pexpect + urllib), бот запускает его в отдельном потоке. Общается с внешним миром через два колбэка:
  log(text)   — строка в журнал задачи;
  stage(name) — начался этап: dns | ssh | install | panel | check.

Оригинальный установщик не изменяется: он скачивается по закреплённому коммиту, сверяется по SHA-256,
а ответы на его вопросы подставляются автоматически. Неизвестный вопрос — остановка, а не угадывание.
"""
from __future__ import annotations

import copy
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

import pexpect

DEFAULT_REF = '93297a689100a666e5d49d20750c5ede3713dad2'
DEFAULT_SHA256 = '0daa9b3887ff9d9157a7348b7025980028f153cd6fc0743d005059d455eca123'
RAW = 'https://raw.githubusercontent.com/nuvrion-kvn/Nuvrion-Vision-Installer'
# vps-psiphon — выход через Psiphon для части трафика (по умолчанию Google), закреплённый коммит
PSI_RAW = 'https://raw.githubusercontent.com/Chara-Freedom/vps-psiphon'
PSI_REF = 'c28177d18a5337419616137400ed0462cf43eb51'
PSI_SHA256 = 'e41b9aeed9f5f11a2a6f1fe2103a74e075f1703a01bc4d1e5cf734e8cf30f4f3'
PSI_TAG = 'psiphon-out'
PSI_DOMAINS = ['domain:google.com', 'domain:googleapis.com', 'domain:gstatic.com', 'domain:googleusercontent.com']
# vps-warp — WireGuard-туннель WARP на ноде; Xray пускает в него трафик меткой sockopt.mark (одинаково на всех нодах)
WARP_RAW = 'https://raw.githubusercontent.com/Chara-Freedom/vps-warp'
WARP_REF = '79f8606acf593813a4d1df3ec3ce89e197360629'
WARP_SHA256 = '26df0adb90e5a22b26399751b993e4be7142e77d93de2bb1fc7b771731eff502'
WARP_TAG = 'warp-out'
WARP_MARK = 255
WARP_DOMAINS = ['geosite:tiktok', 'domain:cloudflare.com']
# Final Mask хоста: фрагментация TLS ClientHello на клиенте (часть операторов режет его на 443/TCP)
FINAL_MASK = {'tcp': [{'type': 'fragment', 'settings': {'length': '10-20', 'packets': 'tlshello', 'interval': '10-20'}}]}
# дополнения ноды: ключ, название в имени профиля, код в теге inbound
EXTRAS = [('psiphon', 'Psiphon', 'PS'), ('warp', 'WARP', 'WP'), ('xhttp', 'XHTTP', 'XH')]
# запасной вход XHTTP: свой inbound на порту, который проксирует Cloudflare, с сертификатом ноды;
# напрямую — домен ноды:порт, через CDN — cdn-<домен ноды> (Cloudflare на 443 → нода на порт по Origin Rule)
XHTTP_PORT = 2083
XHTTP_PREFIX = 'XHTTP-'      # тег inbound: XHTTP-<тег основного inbound>
CDN_PREFIX = 'cdn-'
CF_RULE = 'Bobr Nodes XHTTP'
XHTTP_MODES = {'both': 'напрямую и через CDN', 'direct': 'только напрямую', 'cdn': 'только через CDN'}


def xhttp_mode(v) -> str:
    """Режим запасного XHTTP: both (по умолчанию, как раньше), direct или cdn."""
    v = str(v or 'both').strip().lower()
    if v not in XHTTP_MODES:
        raise StepError('режим XHTTP: both, direct или cdn')
    return v  # описание правил Cloudflare (Origin Rule и Config Rule), которыми бот управляет сам
INBOUND_TAG = 'Vision-TLS'
YN = r'[^\n]*\[Д/Н; Enter — Н\]'

# Регистрация в панели идёт по очереди: сквады обновляются целым списком, параллельные записи потеряли бы inbound.
PANEL_LOCK = threading.Lock()


class StepError(Exception):
    """Ошибка этапа с понятным текстом."""


def strip_ansi(s: str | None) -> str:
    return re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', s or '')


# ─────────────────────────────── HTTP ────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Перенаправление не выполняем: для API это почти всегда заглушка прокси (не прошла cookie-защита)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _not_json(raw: bytes, headers=None) -> dict:
    text = raw[:4000].decode('utf-8', 'replace')
    m = re.search(r'<title[^>]*>(.*?)</title>', text, re.I | re.S)
    out = {'raw': text[:500], 'not_json': True, 'title': (m.group(1).strip()[:80] if m else '')}
    if headers is not None and headers.get('Location'):
        out['location'] = headers.get('Location')
    return out


def http(method, url, headers=None, body=None, timeout=30):
    """(код, JSON). Если ответ не JSON (HTML-заглушка, редирект) — словарь с not_json, raw, title, location."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, (json.loads(raw) if raw else {})
            except ValueError:
                return r.status, _not_json(raw, r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                payload = {'value': payload}
        except ValueError:
            payload = _not_json(raw, e.headers)
        return e.code, payload
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise StepError(f'{url.split("/")[2]} недоступен: {getattr(e, "reason", e)}') from e


# ─────────────────────────────── Cloudflare ──────────────────────────

class Cloudflare:
    API = 'https://api.cloudflare.com/client/v4'

    def __init__(self, token: str):
        if not token:
            raise StepError('не задан токен Cloudflare')
        self.h = {'Authorization': f'Bearer {token}'}
        self.zones: dict[str, str] = {}

    def call(self, method, path, body=None):
        code, data = http(method, self.API + path, self.h, body)
        if code >= 300 or not data.get('success', False):
            errs = data.get('errors') or data
            raise StepError(f'Cloudflare {method} {path.split("?")[0]}: {code} {errs}')
        return data['result']

    def verify(self) -> str:
        return self.call('GET', '/user/tokens/verify').get('status', '?')

    def zone_for(self, domain: str) -> str:
        labels = domain.split('.')
        for i in range(len(labels) - 1):
            cand = '.'.join(labels[i:])
            if cand in self.zones:
                return self.zones[cand]
        for i in range(len(labels) - 1):
            cand = '.'.join(labels[i:])
            res = self.call('GET', '/zones?' + urllib.parse.urlencode({'name': cand}))
            if res:
                self.zones[cand] = res[0]['id']
                return res[0]['id']
        raise StepError(f'зона для {domain} не найдена в аккаунте Cloudflare')

    def zone_names(self) -> list[str]:
        """Домены (зоны) аккаунта — нужен токен с правом Zone → Read."""
        out, page = [], 1
        while page <= 10:
            res = self.call('GET', f'/zones?per_page=50&page={page}')
            out += [z['name'] for z in res]
            for z in res:
                self.zones[z['name']] = z['id']
            if len(res) < 50:
                break
            page += 1
        return sorted(out)

    def records(self, domain: str) -> list[dict]:
        zid = self.zone_for(domain)
        return self.call('GET', f'/zones/{zid}/dns_records?' + urllib.parse.urlencode({'name': domain}))

    def delete_a(self, domain: str, ip: str, log) -> int:
        """Удаляет A-записи домена, которые указывают на ip (на чужие адреса не трогает). Возвращает, сколько удалено."""
        zid = self.zone_for(domain)
        n = 0
        for r in self.records(domain):
            if r['type'] == 'A' and r['content'] == ip:
                self.call('DELETE', f'/zones/{zid}/dns_records/{r["id"]}')
                n += 1
        if n:
            log(f'Cloudflare: A-запись {domain} → {ip} удалена')
        return n

    def upsert_a(self, domain: str, ip: str, log, proxied: bool = False):
        zid = self.zone_for(domain)
        records = self.records(domain)
        body = {'type': 'A', 'name': domain, 'content': ip, 'ttl': 1 if proxied else 60, 'proxied': proxied, 'comment': 'nv-deploy'}
        a = [r for r in records if r['type'] == 'A']
        for r in records:
            if r['type'] == 'CNAME':
                raise StepError(f'у {domain} есть CNAME — удалите его вручную')
            if r['type'] == 'AAAA':
                log(f'[!] есть AAAA {r["content"]} — установщик примет его, только если это IPv6 этого сервера')
        for r in a[1:]:
            self.call('DELETE', f'/zones/{zid}/dns_records/{r["id"]}')
            log(f'удалена лишняя A-запись {r["content"]}')
        if a:
            r = a[0]
            if r['content'] == ip and bool(r.get('proxied')) == proxied:
                log(f'DNS: A {domain} → {ip} уже есть' + (' (через Cloudflare)' if proxied else ''))
                return
            self.call('PUT', f'/zones/{zid}/dns_records/{r["id"]}', body)
            log(f'DNS: A {domain} обновлена → {ip} (было {r["content"]}, proxied={r.get("proxied")})')
        else:
            self.call('POST', f'/zones/{zid}/dns_records', body)
            log(f'DNS: A {domain} → {ip} создана' + (' (через Cloudflare)' if proxied else ''))

    RULE_KEYS = ('id', 'ref', 'description', 'expression', 'action', 'action_parameters', 'enabled', 'logging')

    def _host_rule(self, zid: str, phase: str, host: str, add: bool, action: str, params: dict) -> None:
        """Своё правило в фазе Rulesets зоны: список хостов в выражении; чужие правила фазы не трогаем."""
        code, data = http('GET', f'{self.API}/zones/{zid}/rulesets/phases/{phase}/entrypoint', self.h)
        if code == 404:
            rules = []
        elif code >= 300 or not data.get('success'):
            raise StepError(f'{code} {data.get("errors") or data}')
        else:
            rules = data['result'].get('rules') or []
        mine = next((r for r in rules if r.get('description') == CF_RULE), None)
        hosts = re.findall(r'"([^"]+)"', (mine or {}).get('expression', ''))
        hosts = sorted((set(hosts) | {host}) if add else (set(hosts) - {host}))
        keep = [{k: r[k] for k in self.RULE_KEYS if k in r} for r in rules if r is not mine]
        if hosts:
            rule = {k: mine[k] for k in ('id', 'ref') if mine and k in mine}
            rule.update({'description': CF_RULE, 'expression': '(http.host in {' + ' '.join(f'"{x}"' for x in hosts) + '})',
                         'action': action, 'action_parameters': params, 'enabled': True})
            keep.append(rule)
        if mine is None and not add:
            return
        code, data = http('PUT', f'{self.API}/zones/{zid}/rulesets/phases/{phase}/entrypoint', self.h, {'rules': keep})
        if code >= 300 or not data.get('success'):
            raise StepError(f'{code} {data.get("errors") or data}')

    def xhttp_rules(self, host: str, port: int, log, add: bool = True) -> bool:
        """Origin Rule (клиент на 443 → нода на port) и Config Rule (SSL Full: сертификат ноды выпущен на её домен,
        а не на cdn-имя) — только для этого имени. True — Origin Rule есть и клиенты ходят на 443, False — на сам port."""
        zid = self.zone_for(host)
        ok = True
        for phase, action, params, what, perm in (
                ('http_request_origin', 'route', {'origin': {'port': port}}, f'Origin Rule 443 → {port}', 'Origin Rules: Edit'),
                ('http_config_settings', 'set_config', {'ssl': 'full'}, 'Config Rule «SSL: Full»', 'Config Rules: Edit')):
            try:
                self._host_rule(zid, phase, host, add, action, params)
                log(f'Cloudflare: {what} для {host} ' + ('поставлено' if add else 'убрано'))
            except StepError as e:
                ok = ok and phase != 'http_request_origin'  # без Origin Rule клиенты идут через CDN прямо на port
                log(f'⚠️ Cloudflare: {what} не {"поставлено" if add else "убрано"} ({e}). '
                    f'Добавьте токену право «Zone → {perm}»' + (f' — пока клиенты ходят через CDN на порт {port}' if add and phase == 'http_request_origin' else ''))
        return ok


def doh_a(domain: str) -> list[list[str]]:
    out = []
    for url in ('https://cloudflare-dns.com/dns-query', 'https://dns.google/resolve'):
        _, data = http('GET', url + '?' + urllib.parse.urlencode({'name': domain, 'type': 'A'}),
                       {'Accept': 'application/dns-json'}, timeout=10)
        out.append(sorted({a['data'] for a in data.get('Answer', []) if a.get('type') == 1}))
    return out


def wait_dns(domain: str, ip: str, log, timeout=600, every=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if all(r == [ip] for r in doh_a(domain)):
                log(f'DNS отвечает {ip}')
                return
        except Exception:
            pass
        time.sleep(every)
    raise StepError(f'DNS {domain} за {timeout // 60} мин так и не стал указывать только на {ip}')


# ─────────────────────────────── Remnawave ───────────────────────────

TAG_RE = re.compile(r'^[A-Z0-9_]{1,30}$')


SHARED_PROFILE = 'NV Vision'
SHARED_TAG = 'Vision-NV'          # свой тег: Vision-TLS обычно уже занят нодами, поставленными вручную
CERT_LINK = '/etc/letsencrypt/nv-node'  # на каждой ноде — ссылка на live/<её домен>; путь одинаковый у всех


def shared_profile(config: dict, domain: str) -> dict:
    """Профиль ноды → общий для всех нод: свой тег inbound и путь к сертификату через ссылку CERT_LINK."""
    cfg = retag(config, INBOUND_TAG, SHARED_TAG)
    raw = json.dumps(cfg, ensure_ascii=False).replace(f'/etc/letsencrypt/live/{domain}/', CERT_LINK + '/')
    return json.loads(raw)


def retag(config, old: str, new: str):
    """Копия конфига, где тег inbound old заменён на new (в inbounds и в правилах маршрутизации)."""
    if isinstance(config, dict):
        return {k: (new if k in ('tag',) and v == old else retag(v, old, new)) for k, v in config.items()}
    if isinstance(config, list):
        return [new if x == old else retag(x, old, new) for x in config]
    return config


def domain_rules(raw, default: list[str], what: str) -> list[str]:
    """Список доменов для правила: «google.com» → «domain:google.com»; geosite:/full:/keyword: — как есть."""
    items = re.split(r'[\s,]+', raw) if isinstance(raw, str) else list(raw or [])
    out = []
    for x in items:
        x = str(x).strip().lower()
        if not x:
            continue
        if not re.match(r'(domain|full|keyword|geosite):', x):
            x = 'domain:' + x
        if not re.fullmatch(r'(domain|full|keyword|geosite):[a-z0-9][a-z0-9._@!-]{0,200}', x):
            raise StepError(f'{what}: непонятный домен «{x}»')
        if x not in out:
            out.append(x)
    return out or list(default)


def psiphon_rules(raw) -> list[str]:
    return domain_rules(raw, PSI_DOMAINS, 'Psiphon')


def warp_rules(raw) -> list[str]:
    return domain_rules(raw, WARP_DOMAINS, 'WARP')


def final_mask(raw):
    """Final Mask хоста: dict или JSON-строка с объектом. Пусто — маска по умолчанию (фрагментация ClientHello)."""
    if raw in (None, '', True):
        return json.loads(json.dumps(FINAL_MASK))
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as e:
            raise StepError(f'Final Mask: это не JSON ({e})') from None
    if not isinstance(raw, dict) or not raw or len(json.dumps(raw)) > 8000:
        raise StepError('Final Mask: нужен JSON-объект, например {"tcp": [{"type": "fragment", …}]}')
    return raw


def public_port(raw, node_port: int = 2222) -> int | None:
    """Порт для клиентов: пусто или 443 — стандартный (None), иначе 1024–65535, не порт API ноды."""
    if raw in (None, '', 443, '443'):
        return None
    try:
        p = int(raw)
    except (TypeError, ValueError):
        raise StepError('Порт: число от 1024 до 65535') from None
    if not 1024 <= p <= 65535 or p == int(node_port or 2222):
        raise StepError(f'Порт: от 1024 до 65535 и не {node_port or 2222} (порт API ноды)')
    return p


def psiphon_regions(raw) -> str:
    rs = [x.upper() for x in re.split(r'[\s,]+', raw or '') if x]
    if not all(re.fullmatch(r'[A-Z]{2}', x) for x in rs):
        raise StepError('Psiphon: страна выхода — двухбуквенный код (DE, NL…), можно несколько через запятую')
    return ','.join(rs)


def add_psiphon(config: dict, out: dict, domains: list[str]) -> dict:
    """Копия профиля Xray с выходом psiphon-out (локальный SOCKS5 Psiphon) и правилом для доменов — в конце,
    после блокировок (реклама и приватные адреса так и остаются заблокированными). Psiphon не умеет UDP — только tcp."""
    cfg = json.loads(json.dumps(config))
    cfg['outbounds'] = [o for o in cfg.get('outbounds', []) if o.get('tag') != PSI_TAG] + [
        {'tag': PSI_TAG, 'protocol': 'socks', 'settings': {'servers': [{'address': out['address'], 'port': int(out['port'])}]}}]
    routing = cfg.setdefault('routing', {})
    routing['rules'] = [r for r in routing.get('rules', []) if r.get('outboundTag') != PSI_TAG] + [
        {'type': 'field', 'domain': list(domains), 'network': 'tcp', 'outboundTag': PSI_TAG}]
    return cfg


def psiphon_of(config) -> dict | None:
    """Куда профиль шлёт трафик Psiphon: {'address', 'port'} или None, если выхода psiphon-out нет."""
    for o in (config or {}).get('outbounds', []) if isinstance(config, dict) else []:
        if o.get('tag') == PSI_TAG:
            st = o.get('settings') or {}
            srv = (st.get('servers') or [st])[0]
            return {'address': srv.get('address'), 'port': int(srv.get('port') or 0)}
    return None


def add_warp(config: dict, domains: list[str]) -> dict:
    """Копия профиля с выходом warp-out (freedom с меткой 255 → таблица vps-warp) и правилом для доменов в конце."""
    cfg = json.loads(json.dumps(config))
    cfg['outbounds'] = [o for o in cfg.get('outbounds', []) if o.get('tag') != WARP_TAG] + [
        {'tag': WARP_TAG, 'protocol': 'freedom', 'settings': {'domainStrategy': 'ForceIPv4v6'},
         'streamSettings': {'sockopt': {'mark': WARP_MARK}}}]
    routing = cfg.setdefault('routing', {})
    routing['rules'] = [r for r in routing.get('rules', []) if r.get('outboundTag') != WARP_TAG] + [
        {'type': 'field', 'domain': list(domains), 'outboundTag': WARP_TAG}]
    return cfg


def warp_of(config) -> bool:
    return isinstance(config, dict) and any(o.get('tag') == WARP_TAG for o in config.get('outbounds', []))


def _main_tls(config) -> dict | None:
    """Основной inbound с TLS (его сертификат берёт XHTTP)."""
    for i in (config or {}).get('inbounds', []) if isinstance(config, dict) else []:
        st = i.get('streamSettings') or {}
        if st.get('security') == 'tls' and st.get('network') != 'xhttp' and not str(i.get('tag', '')).startswith(XHTTP_PREFIX):
            return i
    return None


def xhttp_of(config) -> dict | None:
    """Запасной XHTTP-inbound профиля: {'tag', 'port', 'path'} или None."""
    for i in (config or {}).get('inbounds', []) if isinstance(config, dict) else []:
        if str(i.get('tag', '')).startswith(XHTTP_PREFIX):
            st = i.get('streamSettings') or {}
            return {'tag': i['tag'], 'port': int(i.get('port') or XHTTP_PORT), 'path': (st.get('xhttpSettings') or {}).get('path') or '/'}
    return None


def add_xhttp(config: dict, port: int = XHTTP_PORT) -> dict:
    """Копия профиля с запасным VLESS XHTTP+TLS на port: сертификат и sniffing — как у основного inbound,
    путь — случайный (сохраняется, если XHTTP уже был: клиенты не теряют подписку)."""
    cfg = json.loads(json.dumps(config))
    main = _main_tls(cfg)
    if not main:
        raise StepError('XHTTP: в профиле нет inbound с TLS-сертификатом (у Reality своего сертификата нет)')
    if int(main.get('port') or 443) == port:
        raise StepError(f'XHTTP: порт {port} уже занят основным inbound')
    old = xhttp_of(cfg)
    tls = (main.get('streamSettings') or {}).get('tlsSettings') or {}
    ib = {'tag': XHTTP_PREFIX + main['tag'], 'port': port, 'listen': '0.0.0.0', 'protocol': 'vless',
          'settings': {'clients': [], 'decryption': 'none'},
          'sniffing': json.loads(json.dumps(main.get('sniffing') or {'enabled': True, 'routeOnly': True, 'destOverride': ['http', 'tls']})),
          'streamSettings': {'network': 'xhttp', 'security': 'tls',
                             'xhttpSettings': {'path': old['path'] if old else '/' + secrets.token_hex(8), 'mode': 'auto'},
                             'tlsSettings': {'alpn': ['h2', 'http/1.1'], 'minVersion': tls.get('minVersion', '1.2'),
                                             'certificates': json.loads(json.dumps(tls.get('certificates') or [])),
                                             'rejectUnknownSni': False}}}
    cfg['inbounds'] = [i for i in cfg['inbounds'] if not str(i.get('tag', '')).startswith(XHTTP_PREFIX)] + [ib]
    return cfg


def features_of(config) -> set[str]:
    """Какие дополнения уже прописаны в профиле."""
    return {k for k, f in (('psiphon', psiphon_of(config) is not None), ('warp', warp_of(config)),
                           ('xhttp', xhttp_of(config) is not None)) if f}


def apply_extras(config: dict, extras: dict) -> dict:
    if 'psiphon' in extras:
        config = add_psiphon(config, extras['psiphon']['out'], extras['psiphon']['domains'])
    if 'warp' in extras:
        config = add_warp(config, extras['warp']['domains'])
    if 'xhttp' in extras:
        config = add_xhttp(config, int(extras['xhttp'].get('port') or XHTTP_PORT))
    return config


def extras_problem(config, extras: dict, pname: str) -> str:
    """Почему готовый профиль не подходит для этих дополнений ('' — подходит)."""
    if 'psiphon' in extras:
        have, want = psiphon_of(config), extras['psiphon']['out']
        if have is None:
            return f'в профиле «{pname}» нет выхода {PSI_TAG} — добавьте его в панели или удалите профиль, бот создаст заново'
        if have != {'address': want['address'], 'port': int(want['port'])}:
            return (f'Psiphon на ноде слушает {want["address"]}:{want["port"]}, '
                    f'а профиль «{pname}» ведёт на {have["address"]}:{have["port"]}')
    if 'warp' in extras and not warp_of(config):
        return f'в профиле «{pname}» нет выхода {WARP_TAG} — добавьте его в панели или удалите профиль, бот создаст заново'
    if 'xhttp' in extras and not xhttp_of(config):
        return f'в профиле «{pname}» нет inbound {XHTTP_PREFIX}… — удалите профиль в панели, бот создаст заново'
    return ''


def _strip_extras(name: str, tags: list[str]) -> tuple[str, list[str]]:
    while True:
        cut = next((x for _, label, code in EXTRAS for x in (' ' + label, ' ' + code) if name.endswith(x)), None)
        if not cut:
            break
        name = name[:-len(cut)]
    out = []
    for t in tags:
        while any(t.endswith('-' + code) for _, _, code in EXTRAS):
            t = t.rsplit('-', 1)[0]
        out.append(t)
    return name, out


def variant(name: str, tags: list[str], feats: set[str]) -> tuple[str, dict[str, str]]:
    """Имя профиля и теги inbound для набора дополнений, в одном порядке: «NV Vision Psiphon WARP», Vision-NV-PS-WP.
    Суффиксы прежних дополнений снимаются, так что результат не зависит от того, что включали раньше."""
    base, btags = _strip_extras(name, tags)
    labels = [label for key, label, _ in EXTRAS if key in feats]
    codes = [code for key, _, code in EXTRAS if key in feats]
    full = ' '.join([base] + labels)
    if len(full) > 29:  # в панели имя профиля — меньше 30 символов
        full = (base[:29 - len(' '.join([''] + codes))].rstrip() + ' ' + ' '.join(codes)).strip()
    return full, {old: '-'.join([b] + codes) for old, b in zip(tags, btags)}


def balancer_tag_of(template_json) -> str | None:
    """Тег, хосты с которым подтягивает шаблон-балансировщик (remnawave.injectHosts tagRegex ^TAG$), или None."""
    try:
        for e in template_json['remnawave']['injectHosts']:
            sel = e.get('selector') or {}
            m = re.fullmatch(r'\^?([A-Za-z0-9_]+)\$?', sel.get('pattern', '')) if sel.get('type') == 'tagRegex' else None
            if m:
                return m.group(1).upper()
    except (KeyError, TypeError):
        pass
    return None


def make_balancer_template(base: dict, tag: str) -> dict:
    """Копия шаблона-балансировщика под другой тег: injectHosts ^TAG$ с префиксом tag, селекторы балансиров и наблюдателя."""
    tag = tag.upper()
    if not TAG_RE.match(tag):
        raise StepError('тег балансировщика: латиница, цифры, подчёркивание')
    t = copy.deepcopy(base)
    inject = ((t.get('remnawave') or {}).get('injectHosts')) or []
    if not inject:
        raise StepError('выбранный шаблон не похож на балансировщик: нет remnawave.injectHosts')
    old = [e.get('tagPrefix') for e in inject if e.get('tagPrefix')]
    new = tag.lower()
    for e in inject:
        e['selector'] = {'type': 'tagRegex', 'pattern': f'^{tag}$'}
        e['tagPrefix'] = new
        e.setdefault('selectFrom', 'ALL')

    def swap(lst):
        if not isinstance(lst, list):
            return [new]
        out = [new if x in old else x for x in lst]
        return out if new in out else [new]
    for b in ((t.get('routing') or {}).get('balancers') or []):
        b['selector'] = swap(b.get('selector'))
    for key in ('burstObservatory', 'observatory'):
        if isinstance(t.get(key), dict) and 'subjectSelector' in t[key]:
            t[key]['subjectSelector'] = swap(t[key]['subjectSelector'])
    return t


def active_inbounds(pnode: dict) -> list[str]:
    """UUID активных inbound ноды. В ответе панели это объекты {uuid, tag, port…}, в запросах — просто UUID."""
    return [x.get('uuid') if isinstance(x, dict) else x for x in ((pnode.get('configProfile') or {}).get('activeInbounds') or [])]


class Panel:
    def __init__(self, url: str, token: str, cookie: str = '', extra_headers: dict | None = None):
        self.url = url.rstrip('/')
        self.h = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
        if cookie:
            self.h['Cookie'] = cookie
        if self.url.startswith('http://'):
            # прямое обращение к бэкенду без reverse-proxy
            self.h['X-Forwarded-Proto'] = 'https'
            self.h['X-Forwarded-For'] = '127.0.0.1'
        self.h.update(extra_headers or {})

    def call(self, method, path, body=None):
        code, data = http(method, self.url + path, self.h, body)
        if data.get('not_json'):
            what = f'страницу «{data["title"]}»' if data.get('title') else 'не JSON'
            if data.get('location'):
                what = f'перенаправление на {data["location"][:80]}'
            hint = ('прокси не пропустил запрос: проверьте cookie-защиту (вставьте секретную ссылку входа целиком)'
                    if 'Cookie' in self.h else 'похоже, панель закрыта cookie-защитой — заполните поле «Cookie-защита»')
            raise StepError(f'панель вернула {what} (HTTP {code}) вместо API: {hint}. '
                            f'Также проверьте адрес — нужен тот, по которому открывается панель')
        if code >= 300:
            hint = ''
            if code in (403, 404, 444):
                hint = ' (проверьте cookie-защиту и адрес панели)'
            if code == 401:
                hint = ' (неверный API-токен: Панель → Настройки → API Tokens)'
            msg = data.get('message') or data.get('error') or str(data)
            raise StepError(f'панель {method} {path}: HTTP {code}{hint}: {str(msg)[:200]}')
        return data.get('response', data)

    def secret_key(self) -> str:
        return self.call('GET', '/api/keygen')['secretKey']

    def check(self) -> str:
        """Проверка доступа: ключ + список профилей. Возвращает короткое описание."""
        self.secret_key()
        total = self.call('GET', '/api/config-profiles')['total']
        nodes = len(self.call('GET', '/api/nodes'))
        return f'профилей: {total}, нод: {nodes}'

    def meta(self) -> dict:
        """Сквады и Xray-шаблоны панели (для выбора при установке)."""
        squads = [{'uuid': x['uuid'], 'name': x['name']} for x in self.call('GET', '/api/internal-squads')['internalSquads']]
        tpls = []
        for t in self.templates():
            if t.get('templateType') == 'XRAY_JSON':
                tpls.append({'uuid': t['uuid'], 'name': t['name'], 'balancer_tag': balancer_tag_of(t.get('templateJson'))})
        profiles = [{'uuid': p['uuid'], 'name': p['name'],
                     'inbounds': [{'uuid': i['uuid'], 'tag': i['tag'], 'port': i.get('port'), 'type': i.get('type'),
                                   'security': i.get('security')} for i in p.get('inbounds', [])],
                     'psiphon': psiphon_of(p.get('config')) is not None, 'warp': warp_of(p.get('config'))}
                    for p in self.call('GET', '/api/config-profiles')['configProfiles']]
        btpl = {t['uuid'] for t in tpls if t['balancer_tag']}
        tname = {t['uuid']: t['name'] for t in tpls}
        pname = {p['uuid']: p['name'] for p in profiles}
        hosts = [{'uuid': x['uuid'], 'remark': x.get('remark', ''), 'address': x.get('address', ''), 'port': x.get('port'),
                  'hidden': bool(x.get('isHidden')), 'disabled': bool(x.get('isDisabled')), 'tags': x.get('tags') or [],
                  'balancer': x.get('xrayJsonTemplateUuid') in btpl,
                  'profile': pname.get((x.get('inbound') or {}).get('configProfileUuid'), ''),
                  'template': tname.get(x.get('xrayJsonTemplateUuid'), '')}
                 for x in sorted(self.call('GET', '/api/hosts'), key=lambda x: x.get('viewPosition', 0))]
        return {'squads': squads, 'templates': tpls, 'profiles': profiles, 'hosts': hosts}

    def place_hosts(self, mine: list[str], place: dict, log):
        """Ставит только что созданные хосты (mine) после хоста place['after'] ('' — в самое начало).
        Хосты с адресами из place['before'] — новые хосты той же установки, которые по плану идут раньше:
        если они уже на месте, встаём за ними, поэтому порядок не зависит от того, какая нода готова первой."""
        hosts = sorted(self.call('GET', '/api/hosts'), key=lambda x: x.get('viewPosition', 0))
        ours = [x for x in hosts if x['uuid'] in mine]
        rest = [x for x in hosts if x['uuid'] not in mine]
        if not ours:
            return
        after = place.get('after')
        if after:
            idx = next((i for i, x in enumerate(rest) if x['uuid'] == after), None)
            if idx is None:
                log('панель: хоста, после которого ставить новые, больше нет — оставил в конце')
                return
        else:
            idx = -1
        before = set(place.get('before') or [])
        while idx + 1 < len(rest) and rest[idx + 1].get('address') in before:
            idx += 1
        order = rest[:idx + 1] + ours + rest[idx + 1:]
        positions = sorted(x.get('viewPosition', i) for i, x in enumerate(hosts))
        if len(set(positions)) != len(positions):
            positions = list(range(1, len(hosts) + 1))
        changes = [{'uuid': x['uuid'], 'viewPosition': pos} for x, pos in zip(order, positions)
                   if x.get('viewPosition') != pos]
        if changes:
            self.call('POST', '/api/hosts/actions/reorder', {'hosts': changes})
        prev = rest[idx]['remark'] if idx >= 0 else None
        log('панель: хосты поставлены ' + (f'после «{prev}»' if prev else 'в начало списка'))

    def templates(self) -> list[dict]:
        """Шаблоны подписки с содержимым. Список панели отдаёт их без templateJson — Xray JSON дочитываем по одному."""
        out = []
        for t in self.call('GET', '/api/subscription-templates')['templates']:
            if t.get('templateType') == 'XRAY_JSON' and t.get('templateJson') is None:
                t = {**t, **(self.call('GET', f'/api/subscription-templates/{t["uuid"]}') or {})}
            out.append(t)
        return out

    def balancer_templates(self) -> set[str]:
        """UUID Xray-шаблонов с балансировкой (injectHosts по тегу). Просто шаблон у хоста — ещё не балансировщик."""
        return {t['uuid'] for t in self.templates()
                if t.get('templateType') == 'XRAY_JSON' and balancer_tag_of(t.get('templateJson'))}

    def ensure_balancer(self, tag: str, base_uuid: str | None, remark: str, prof: dict, inbound: dict,
                        node: dict, log, tls: bool = True) -> str | None:
        """Шаблон Balancer_<tag> (копия базового с подстановкой тега) и видимый хост-балансировщик с ним.
        Возвращает UUID хоста-балансировщика, если он создан сейчас.
        Если они уже есть — ничего не меняем: новые скрытые хосты с тегом подхватятся сами."""
        tag = tag.upper()
        tpls = self.templates()
        xray = [t for t in tpls if t.get('templateType') == 'XRAY_JSON']
        tpl = next((t for t in xray if balancer_tag_of(t.get('templateJson')) == tag), None)
        if tpl:
            log(f'панель: шаблон балансировщика для {tag} уже есть — «{tpl["name"]}»')
        else:
            base = next((t for t in xray if t['uuid'] == base_uuid), None) if base_uuid else None
            base = base or next((t for t in xray if balancer_tag_of(t.get('templateJson'))), None)
            if not base:
                raise StepError('в панели нет Xray-шаблона с балансировщиком (remnawave.injectHosts), чтобы скопировать его')
            body = make_balancer_template(base['templateJson'], tag)
            name = f'Balancer_{tag.lower()}'
            tpl = self.call('POST', '/api/subscription-templates', {'name': name, 'templateType': 'XRAY_JSON'})
            tpl = self.call('PATCH', '/api/subscription-templates', {'uuid': tpl['uuid'], 'templateJson': body})
            log(f'панель: шаблон «{name}» создан из «{base["name"]}»')
        hosts = self.call('GET', '/api/hosts')
        bh = next((h for h in hosts if h.get('xrayJsonTemplateUuid') == tpl['uuid']), None)
        if bh:
            log(f'панель: хост-балансировщик «{bh["remark"]}» уже есть')
            new = None
        else:
            new = self.call('POST', '/api/hosts', {**self.host_body(prof, inbound, node, tls), 'remark': remark[:100],
                                                   'xrayJsonTemplateUuid': tpl['uuid']})['uuid']
            log(f'панель: хост-балансировщик «{remark}» создан')
        return new  # UUID нового хоста-балансировщика или None, если он уже был

    def register(self, node: dict, profile_config: dict, squads: list[str], node_port: int, log,
                 extras: dict | None = None):
        # Один общий профиль на все ноды: тег Vision-NV (уникален в панели), сертификат — через ссылку CERT_LINK.
        # Ноды с дополнениями (Psiphon, WARP) — в своём общем профиле: там выходы, которых на остальных нодах нет.
        extras = extras or {}
        tag, pname = SHARED_TAG, SHARED_PROFILE
        profile_config = shared_profile(profile_config, node['domain'])
        if extras:
            pname, tmap = variant(SHARED_PROFILE, [SHARED_TAG], set(extras))
            tag = tmap[SHARED_TAG]
            profile_config = apply_extras(retag(profile_config, SHARED_TAG, tag), extras)
        profiles = self.call('GET', '/api/config-profiles')['configProfiles']
        prof = next((p for p in profiles if p['name'] == pname), None)
        if prof:
            # не перезаписываем: изменение профиля перезапускает Xray на всех нодах сразу
            log(f'панель: общий профиль «{pname}» уже есть')
            problem = extras_problem(prof.get('config'), extras, pname)
            if problem:
                raise StepError(problem)
        else:
            prof = self.call('POST', '/api/config-profiles', {'name': pname, 'config': profile_config})
            log(f'панель: общий профиль «{pname}» создан' + ''.join(
                f', {label} для: {", ".join(extras[key]["domains"])}' for key, label, _ in EXTRAS if key in extras))
        inbound = next((i for i in prof['inbounds'] if i['tag'] == tag), None)
        if not inbound:
            raise StepError(f'в профиле нет inbound {tag}')
        xin = [i for i in prof['inbounds'] if i['tag'] == XHTTP_PREFIX + tag]
        self.attach(node, prof, [inbound], squads, node_port, log, tls=True, also=xin)
        return prof

    def node_hosts(self, pnode: dict, domains: set[str], ips: set[str] = frozenset()) -> list[dict]:
        """Хосты этой ноды: привязаны к ней в панели или их адрес — её IP / известный боту домен / домен с её IP."""
        active = set(active_inbounds(pnode))
        names = {pnode['address']} | set(domains)
        names |= {CDN_PREFIX + d for d in names if d and not is_ip(d)}  # хост запасного XHTTP через Cloudflare
        out = []
        for h in self.call('GET', '/api/hosts'):
            if (h.get('inbound') or {}).get('configProfileInboundUuid') not in active:
                continue
            mine = pnode['uuid'] in (h.get('nodes') or []) or h.get('address') in names
            if not mine and h.get('address'):
                try:
                    mine = bool(({pnode['address']} | set(ips)) & {a[4][0] for a in socket.getaddrinfo(h['address'], None, socket.AF_INET)})
                except (OSError, UnicodeError):
                    mine = False
            if mine:
                out.append(h)
        return out

    def upgrade_node(self, pnode: dict, extras: dict, hosts: list[dict], log) -> dict:
        """Переводит ноду в вариант её профиля с дополнениями: профиль-копия с выходами и правилами,
        нода, её хосты и сквады — на inbound'ы копии. Остальные ноды старого профиля не задеты."""
        cp = pnode.get('configProfile') or {}
        profiles = self.call('GET', '/api/config-profiles')['configProfiles']
        cur = next((p for p in profiles if p['uuid'] == cp.get('activeConfigProfileUuid')), None)
        if not cur:
            raise StepError('у ноды в панели нет профиля')
        feats = features_of(cur.get('config')) | set(extras)
        if feats == features_of(cur.get('config')) and not extras_problem(cur.get('config'), extras, cur['name']):
            log(f'панель: в профиле «{cur["name"]}» всё уже есть')
            return cur
        vname, tmap = variant(cur['name'], [i['tag'] for i in cur['inbounds']], feats)
        prof = next((p for p in profiles if p['name'] == vname), None)
        if prof:
            log(f'панель: профиль «{vname}» уже есть')
            problem = extras_problem(prof.get('config'), extras, vname)
            if problem:
                raise StepError(problem)
        else:
            cfg = cur.get('config') or {}
            for old, new in tmap.items():
                cfg = retag(cfg, old, new)
            prof = self.call('POST', '/api/config-profiles', {'name': vname, 'config': apply_extras(cfg, extras)})
            log(f'панель: профиль «{vname}» создан из «{cur["name"]}»')
        self.move_node(pnode, cur, prof, tmap, hosts, log)
        return prof

    def move_node(self, pnode: dict, cur: dict, prof: dict, tmap: dict[str, str], hosts: list[dict], log,
                  squads: bool = True) -> None:
        """Нода, её хосты и (если squads) сквады — с inbound'ов cur на inbound'ы prof по карте тегов tmap."""
        vname = prof['name']
        by_tag = {i['tag']: i['uuid'] for i in prof['inbounds']}
        old_tag = {i['uuid']: i['tag'] for i in cur['inbounds']}
        umap = {u: by_tag[tmap[t]] for u, t in old_tag.items() if tmap.get(t) in by_tag}
        active = [umap[u] for u in active_inbounds(pnode) if u in umap]
        if not active:
            raise StepError(f'в профиле «{vname}» нет inbound для этой ноды')
        xh = next((i['uuid'] for i in prof['inbounds'] if i['tag'].startswith(XHTTP_PREFIX)), None)
        if xh and xh not in active:  # запасной XHTTP — тоже на ноде
            active.append(xh)
        # Сначала сквады: панель собирает пользователей ноды из сквадов в момент смены профиля.
        # Если inbound в сквады попадёт позже, нода поднимется с новым профилем без пользователей —
        # и подключиться к ней нельзя, пока её не перезапустят.
        if squads:
            targets = set(umap.values())
            for sq in self.call('GET', '/api/internal-squads')['internalSquads']:
                ids = [i['uuid'] for i in sq.get('inbounds', [])]
                add = [umap[u] for u in ids if u in umap and umap[u] not in ids]
                if xh and xh not in ids and xh not in add and (add or any(u in umap or u in targets for u in ids)):
                    add.append(xh)
                if add:
                    self.call('PATCH', '/api/internal-squads', {'uuid': sq['uuid'], 'inbounds': ids + add})
                    log(f'панель: inbound «{vname}» добавлен в сквад «{sq["name"]}»')
        self.call('PATCH', '/api/nodes', {'uuid': pnode['uuid'],
                                          'configProfile': {'activeConfigProfileUuid': prof['uuid'], 'activeInbounds': active}})
        pnode['configProfile'] = {'activeConfigProfileUuid': prof['uuid'], 'activeInbounds': active}
        log(f'панель: нода «{pnode["name"]}» переведена в «{vname}»')
        for h in hosts:
            u = h['inbound']['configProfileInboundUuid']
            if u in umap:
                self.call('PATCH', '/api/hosts', {'uuid': h['uuid'], 'inbound': {
                    'configProfileUuid': prof['uuid'], 'configProfileInboundUuid': umap[u]}})
                h['inbound'] = {'configProfileUuid': prof['uuid'], 'configProfileInboundUuid': umap[u]}
        log(f'панель: хосты ноды ({len(hosts)}) — на inbound «{vname}»')
        self.restart_node(pnode, log)

    def xhttp_publish(self, prof: dict, domain: str, ip: str, remark: str, cf: 'Cloudflare | None', log,
                      mode: str = 'both') -> None:
        """Хосты запасного XHTTP по режиму: direct — напрямую (домен ноды:порт), cdn — через Cloudflare CDN
        (cdn-домен, оранжевое облако), both — оба. Хосты другого режима, оставшиеся с прошлого раза, убираются."""
        mode = xhttp_mode(mode)
        x = xhttp_of(prof.get('config'))
        xin = next((i for i in prof['inbounds'] if x and i['tag'] == x['tag']), None)
        if not x or not xin:
            raise StepError(f'в профиле «{prof["name"]}» нет запасного XHTTP')
        cdn, cdn_port = CDN_PREFIX + domain, x['port']
        want_cdn = mode in ('cdn', 'both')
        if want_cdn and cf is None:
            if mode == 'cdn':
                raise StepError('XHTTP через CDN: нужен токен Cloudflare (общий в «Подключениях» или у группы)')
            log('⚠️ нет токена Cloudflare — хост через CDN не создан, только прямой')
            want_cdn = False
        if want_cdn:
            cf.upsert_a(cdn, ip, log, proxied=True)
            if cf.xhttp_rules(cdn, x['port'], log):
                cdn_port = 443
        targets = ([(domain, x['port'], 'XHTTP')] if mode in ('direct', 'both') else []) + \
            ([(cdn, cdn_port, 'CDN')] if want_cdn else [])
        hosts = self.call('GET', '/api/hosts')
        mine = [h for h in hosts if str(((h.get('inbound') or {}).get('configProfileInboundUuid')) or '') == xin['uuid']]
        keep = {a for a, _, _ in targets}
        for h in mine:  # смена режима: лишний хост этой ноды — убрать
            if h.get('address') in {domain, cdn} - keep:
                self.call('DELETE', f'/api/hosts/{h["uuid"]}')
                log(f'панель: хост «{h.get("remark", "")}» удалён (XHTTP: {XHTTP_MODES[mode]})')
        if not want_cdn and cf is not None and any(h.get('address') == cdn for h in mine):
            try:
                cf.delete_a(cdn, ip, log)
                cf.xhttp_rules(cdn, x['port'], log, add=False)
            except StepError as e:
                log(f'⚠️ Cloudflare: {e}')
        for addr, port, label in targets:
            body = {'inbound': {'configProfileUuid': prof['uuid'], 'configProfileInboundUuid': xin['uuid']},
                    'remark': f'{remark} {label}'[:100], 'address': addr, 'port': port, 'path': x['path'], 'sni': addr, 'host': addr,
                    'alpn': 'h2,http/1.1', 'fingerprint': 'chrome', 'securityLayer': 'TLS'}
            have = next((h for h in mine if h.get('address') == addr), None)
            if have:
                self.call('PATCH', '/api/hosts', {'uuid': have['uuid'], 'port': port, 'path': x['path']})
                log(f'панель: хост «{have.get("remark", addr)}» обновлён ({addr}:{port})')
            else:
                self.call('POST', '/api/hosts', body)
                log(f'панель: хост «{body["remark"]}» создан ({addr}:{port})')

    def xhttp_unpublish(self, cur: dict, domain: str, ip: str, cf: 'Cloudflare | None', log) -> None:
        """Убрать запасной XHTTP ноды: её хосты на XHTTP-inbound профиля cur, запись cdn-домена и правила Cloudflare."""
        x = xhttp_of(cur.get('config'))
        xin = next((i['uuid'] for i in cur.get('inbounds', []) if x and i['tag'] == x['tag']), None)
        cdn = CDN_PREFIX + domain if domain else ''
        if xin:
            for h in self.call('GET', '/api/hosts'):
                if ((h.get('inbound') or {}).get('configProfileInboundUuid') == xin and h.get('address') in {domain, cdn, ip} - {''}):
                    self.call('DELETE', f'/api/hosts/{h["uuid"]}')
                    log(f'панель: хост «{h.get("remark", "")}» удалён (запасной XHTTP)')
        if cf and cdn:
            try:
                cf.delete_a(cdn, ip, log)
                cf.xhttp_rules(cdn, (x or {}).get('port', XHTTP_PORT), log, add=False)
            except StepError as e:
                log(f'⚠️ Cloudflare: {e}')

    def restart_node(self, pnode: dict, log) -> None:
        """Перезапуск ноды из панели: Xray получает актуальный профиль и список пользователей."""
        try:
            self.call('POST', f'/api/nodes/{pnode["uuid"]}/actions/restart', {})
            log(f'панель: нода «{pnode["name"]}» перезапущена — пользователи профиля подтянуты')
        except StepError as e:
            log(f'⚠️ панель: не удалось перезапустить ноду ({e}) — если к ней не подключиться, перезапустите её в панели')

    def downgrade_node(self, pnode: dict, hosts: list[dict], log) -> dict:
        """Обратно: нода и её хосты — в исходный профиль (без Psiphon/WARP). Профиль-копия остаётся для других нод."""
        profiles = self.call('GET', '/api/config-profiles')['configProfiles']
        cur = next((p for p in profiles if p['uuid'] == (pnode.get('configProfile') or {}).get('activeConfigProfileUuid')), None)
        if not cur:
            raise StepError('у ноды в панели нет профиля')
        tags = [i['tag'] for i in cur['inbounds']]
        base_name, base_tags = _strip_extras(cur['name'], tags)
        if base_name == cur['name'] and base_tags == tags:
            raise StepError(f'«{cur["name"]}» — не профиль с дополнениями, возвращать некуда')
        base = next((p for p in profiles if p['name'] == base_name), None)
        if not base:
            raise StepError(f'исходного профиля «{base_name}» в панели нет')
        self.move_node(pnode, cur, base, dict(zip(tags, base_tags)), hosts, log, squads=False)
        return base

    def existing_profile(self, uuid: str, inbound_uuids: list[str]) -> tuple[dict, list[dict]]:
        """Профиль, уже настроенный в панели (для скриптов, которые сами профиль не пишут)."""
        profiles = self.call('GET', '/api/config-profiles')['configProfiles']
        prof = next((p for p in profiles if p['uuid'] == uuid), None)
        if not prof:
            raise StepError('выбранного в группе профиля в панели больше нет — выберите другой в настройках группы')
        inbounds = [i for i in prof['inbounds'] if i['uuid'] in inbound_uuids] or prof['inbounds'][:1]
        if not inbounds:
            raise StepError(f'в профиле «{prof["name"]}» нет inbound')
        return prof, inbounds

    def node_state(self, ip: str, domain: str = '', uuid: str = '') -> dict | None:
        """Нода по UUID, либо по адресу — IP или домену (новые ноды регистрируются по домену)."""
        nodes = self.call('GET', '/api/nodes')
        if uuid:
            return next((n for n in nodes if n['uuid'] == uuid), None)
        return next((n for n in nodes if n['address'] in {ip, domain} - {''}), None)

    @staticmethod
    def host_body(prof: dict, inbound: dict, node: dict, tls: bool) -> dict:
        body = {'inbound': {'configProfileUuid': prof['uuid'], 'configProfileInboundUuid': inbound['uuid']},
                'address': node['domain']}
        if tls:  # свой TLS-профиль бота: SNI = домен ноды
            body.update({'port': 443, 'sni': node['domain'], 'alpn': 'h2,http/1.1', 'fingerprint': 'firefox',
                         'securityLayer': 'TLS'})
        else:    # чужой профиль (например Reality): параметры безопасности — из inbound
            body.update({'port': int(inbound.get('port') or 443), 'securityLayer': 'DEFAULT'})
        opts = node.get('host') or {}
        if opts.get('port'):        # нестандартный порт: на ноде он переадресован на порт inbound
            body['port'] = int(opts['port'])
        if opts.get('final_mask'):
            body['finalMask'] = opts['final_mask']
        return body

    def attach(self, node: dict, prof: dict, inbounds: list[dict], squads: list[str], node_port: int, log,
               tls: bool = True, also: list[dict] = ()):
        """Нода с профилем и inbound'ами, хосты (обычные/скрытые с тегами/балансировщик), сквады.
        also — ещё inbound'ы ноды без обычных хостов (запасной XHTTP: его хосты ставит xhttp_publish)."""
        cp = {'activeConfigProfileUuid': prof['uuid'], 'activeInbounds': [i['uuid'] for i in [*inbounds, *also]]}
        # сквады — до ноды: пользователей нода получает из сквадов при запуске с профилем;
        # inbound, добавленный в сквад позже, до перезапуска ноды остаётся без пользователей
        for sq in squads:
            cur = self.call('GET', f'/api/internal-squads/{sq}')
            ids = [i['uuid'] for i in cur.get('inbounds', [])]
            new = [i['uuid'] for i in [*inbounds, *also] if i['uuid'] not in ids]
            if new:
                self.call('PATCH', '/api/internal-squads', {'uuid': sq, 'inbounds': ids + new})
                log(f'панель: inbound добавлен в сквад «{cur.get("name", sq)}»')

        nodes = self.call('GET', '/api/nodes')
        existing = next((n for n in nodes if n['address'] in (node['ip'], node['domain'])), None)
        if existing:
            self.call('PATCH', '/api/nodes', {'uuid': existing['uuid'], 'configProfile': cp})
            log(f'панель: нода «{existing["name"]}» уже была, профиль назначен')
        else:
            cc = (node.get('country') or '').upper()
            # адрес ноды — домен: при смене IP (бан адреса) достаточно поменять A-запись
            body = {'name': node['name'][:30], 'address': node.get('domain') or node['ip'], 'port': node_port,
                    'countryCode': cc if re.fullmatch(r'[A-Z]{2}', cc) else 'XX', 'configProfile': cp}
            self.call('POST', '/api/nodes', body)
            log(f'панель: нода «{node["name"]}» создана')

        opts = node.get('host') or {}
        balancer = opts.get('mode') == 'balancer'
        hidden = bool(opts.get('hidden', balancer))
        tags = [t for t in (opts.get('tags') or []) if re.fullmatch(r'[A-Z0-9_:]{1,36}', t)]
        hosts = self.call('GET', '/api/hosts')
        btpl = self.balancer_templates()
        mine = []
        for n, inbound in enumerate(inbounds, 1):
            host = next((h for h in hosts if h['address'] == node['domain']
                         and h['inbound']['configProfileInboundUuid'] == inbound['uuid']
                         and h.get('xrayJsonTemplateUuid') not in btpl), None)
            if host:
                log(f'панель: хост {inbound.get("tag", "")} уже есть')
                continue
            body = self.host_body(prof, inbound, node, tls)
            remark = (node.get('remark') or node['name'])
            body['remark'] = (remark if len(inbounds) == 1 else f'{remark} {inbound.get("tag", n)}')[:100]
            if hidden:
                body['isHidden'] = True
            if tags:
                body['tags'] = tags
            mine.append(self.call('POST', '/api/hosts', body)['uuid'])
            extra = ', '.join(([] if not hidden else ['скрытый']) + ([f'теги {", ".join(tags)}'] if tags else []))
            log(f'панель: хост {inbound.get("tag", "")} создан' + (f' ({extra})' if extra else ''))
        if balancer:
            b = opts.get('balancer') or {}
            bh = self.ensure_balancer(b.get('tag') or (tags[0] if tags else 'PL'), b.get('template'),
                                      b.get('remark') or node.get('remark') or node['name'], prof, inbounds[0], node, log, tls)
            if bh:
                mine.insert(0, bh)  # новый хост-балансировщик — над своими нодами
        if mine and node.get('place') is not None:
            try:
                self.place_hosts(mine, node['place'], log)
            except StepError as e:  # порядок — не повод ронять готовую ноду
                log(f'панель: не удалось переставить хосты: {e}')



# ─────────────────────────────── SSH ─────────────────────────────────

HOSTKEY_CHANGED = ('ключ SSH сервера изменился с прошлого входа — возможна подмена сервера, пароль не отправлен. '
                   'Если сервер переустанавливали, нажмите «Забыть ключ SSH» на странице ноды и повторите')


def forget_host(known_hosts: str, host: str) -> None:
    """Забыть ключ сервера (новый сервер на старом IP или переустановка)."""
    try:
        subprocess.run(['ssh-keygen', '-R', host, '-f', known_hosts], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass


class Remote:
    def __init__(self, node: dict, logfile):
        self.n = node
        self.logfile = logfile

    def argv(self, cmd, tty):
        n = self.n
        kh = n.get('known_hosts')
        # ключ сервера запоминается при первом входе (как в обычном ssh); если он сменился — останавливаемся
        hk = ['-o', 'StrictHostKeyChecking=accept-new', '-o', f'UserKnownHostsFile={kh}'] if kh else \
            ['-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null']
        a = ['-tt' if tty else '-T', *hk, '-o', 'LogLevel=ERROR',
             '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=10', '-o', 'ConnectTimeout=20',
             '-p', str(n.get('ssh_port') or 22)]
        if n.get('ssh_key'):
            a += ['-i', os.path.expanduser(n['ssh_key']), '-o', 'IdentitiesOnly=yes']
        elif n.get('ssh_password'):
            a += ['-o', 'PubkeyAuthentication=no', '-o', 'PreferredAuthentications=password,keyboard-interactive']
        return a + [f'{n.get("ssh_user") or "root"}@{n["ip"]}', cmd]

    def spawn(self, cmd, tty, timeout):
        child = pexpect.spawn('ssh', self.argv(cmd, tty), encoding='utf-8', codec_errors='replace',
                              timeout=timeout, dimensions=(50, 120))
        child.logfile_read = self.logfile
        return child

    def run(self, cmd, timeout=120):
        child = self.spawn(cmd, False, timeout)
        out = []
        sent_pw = False
        while True:
            i = child.expect([r'(?i)password:', pexpect.EOF, pexpect.TIMEOUT])
            out.append(child.before or '')
            if i == 0:
                if not self.n.get('ssh_password') or sent_pw:
                    child.close(force=True)
                    raise StepError('SSH не принял пароль' if sent_pw else 'SSH спрашивает пароль, а он не задан')
                child.sendline(self.n['ssh_password'])
                sent_pw = True
                out.clear()
                continue
            if i == 2:
                child.close(force=True)
                raise StepError(f'таймаут команды: {cmd[:60]}')
            break
        child.close()
        text = ''.join(out)
        if 'REMOTE HOST IDENTIFICATION HAS CHANGED' in text or 'Host key verification failed' in text:
            raise StepError(HOSTKEY_CHANGED)
        return child.exitstatus, text


def wait_ssh(r: Remote, log, timeout=600):
    end = time.time() + timeout
    last = ''
    while time.time() < end:
        try:
            rc, out = r.run('id -u', 30)
            if rc == 0 and out.strip().splitlines()[-1:] == ['0']:
                log('SSH доступен, вход под root')
                return
            last = out.strip()[-120:] or f'код {rc}'
            if 'не принял пароль' in last:
                break
        except StepError as e:
            last = str(e)
            if 'не принял пароль' in last or last == HOSTKEY_CHANGED:
                raise StepError(last if last == HOSTKEY_CHANGED else f'не удалось войти по SSH под root: {last}') from None
        time.sleep(15)
    raise StepError(f'не удалось войти по SSH под root: {last}')


def remote_install_cmd(ref, sha, resume, long_key):
    icanon = 'stty -icanon min 1 time 0; ' if long_key else ''
    if resume:
        return f'{icanon}exec bash /opt/remnanode/installer.sh --resume'
    return (
        'set -e; d=$(mktemp -d /root/nv-deploy.XXXXXX); cd "$d"; '
        'if ! command -v curl >/dev/null; then apt-get update -qq && '
        'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates >/dev/null; fi; '
        f'curl -fsSL -o i.sh {RAW}/{ref}/nuvrion-vision-install.sh; '
        f'echo "{sha}  i.sh" | sha256sum -c --quiet; '
        f'{icanon}exec bash ./i.sh --install'
    )


def drive_installer(child, a: dict, log) -> int:
    """Отвечает на вопросы установщика. Неизвестный вопрос — остановка, а не угадывание."""
    yn = lambda v: 'Д' if v else 'Н'  # noqa: E731
    rules = [
        (r'(?i)password:', None),
        (r'Установить на машину Nuvrion Vision\?' + YN, 'Д'),
        (r'Разрешить для текущей установки обновление APT' + YN, 'Д'),
        (r'Удалить перечисленные ненужные зависимости\?' + YN, yn(a['autoremove'])),
        (r'Начать установку с этими настройками\?' + YN, 'Д'),
        (r'Установить и включить Nuvrion Traffic Control\?' + YN, yn(a['traffic_control'])),
        (r'Продолжить установку\?' + YN, 'Д'),
        (r'Верно\?' + YN, 'Д'),
        (r'Это именно исходящие IP панели\?' + YN, 'Д'),
        (r'Домен ноды \(без https://\)[^\n]*: ', a['domain']),
        (r'Порт API ноды[^\n]*: ', str(a['node_port'])),
        (r'IP исходящих подключений панели[^\n]*: ', a['panel_ips']),
        (r"Email для сертификата[^\n]*: ", a['email']),
        (r'Секретный ключ ноды из панели[^\n]*: ', a['secret']),
        (r'Повторите только это поле|Повторите ключ|Введите Д/Y', 'FAIL_INPUT'),
        # общий шаблон начинается со скобки, поэтому конкретные вопросы (они левее) всегда выигрывают
        (r'\[Д/Н; Enter — Н\]', 'FAIL_UNKNOWN'),
        (r'введите своё значение\)?: ', 'FAIL_UNKNOWN'),
    ]
    patterns = [r for r, _ in rules] + [pexpect.EOF, pexpect.TIMEOUT]
    started = False  # пароль SSH принимаем только до первого вопроса установщика
    while True:
        i = child.expect(patterns)
        if i == len(rules):
            child.close()
            return child.exitstatus if child.exitstatus is not None else 128 + (child.signalstatus or 0)
        if i == len(rules) + 1:
            child.close(force=True)
            raise StepError('установщик слишком долго ничего не выводил (таймаут)')
        pat, ans = rules[i]
        if ans is None:
            if started:
                continue
            if not a.get('ssh_password'):
                child.close(force=True)
                raise StepError('SSH спрашивает пароль, а он не задан')
            child.sendline(a['ssh_password'])
            continue
        started = True
        if ans.startswith('FAIL_'):
            tail = strip_ansi((child.before or '') + (child.after or '')).strip().splitlines()
            text = tail[-1] if tail else ''
            child.sendcontrol('c')
            child.close(force=True)
            what = 'установщик отклонил значение' if ans == 'FAIL_INPUT' else 'неизвестный вопрос установщика'
            raise StepError(f'{what}: «{text[-120:]}»')
        if 'Секретный' in pat:
            log('ответ: SECRET_KEY передан (в журнал не пишется)')
        else:
            q = strip_ansi(child.after).strip()
            log(f'ответ: {q[:70]} → {ans if len(ans) < 40 else ans[:37] + "..."}')
        child.sendline(ans)


# ─────────────────────────────── Нода целиком ────────────────────────

def install_psiphon(r: 'Remote', opts: dict, log) -> dict:
    """Ставит vps-psiphon с закреплённого коммита (или берёт уже работающий). Возвращает {'address', 'port'} SOCKS5."""
    region = psiphon_regions(opts.get('region') or '')
    env = 'test -r /etc/default/vps-psiphon && . /etc/default/vps-psiphon && echo "PSI_ADDR=$BIND PSI_PORT=$SOCKS_PORT"'
    _, out = r.run(f'systemctl is-active --quiet vps-psiphon && {env}; true')
    m = re.search(r'PSI_ADDR=(\S+) PSI_PORT=(\d+)', out)
    if m and opts.get('reuse', True):
        log(f'Psiphon уже работает на ноде: {m.group(1)}:{m.group(2)}')
        return {'address': m.group(1), 'port': int(m.group(2))}
    args = ' --socks-port 1080 --no-http' + (f' --region {region}' if region else '')
    log(f'ставлю vps-psiphon @ {PSI_REF[:12]}' + (f', страна выхода {region}' if region else ', страна выхода — авто'))
    rc, out = r.run(
        'd=$(mktemp -d /root/psiphon.XXXXXX) && cd "$d" && '
        f'curl -fsSL -o p.sh {PSI_RAW}/{PSI_REF}/psiphon_install.sh && '
        f'echo "{PSI_SHA256}  p.sh" | sha256sum -c --quiet && bash ./p.sh{args}; echo "PSI_RC=$?"; {env}; true', 900)
    text = strip_ansi(out)
    for line in [x.strip() for x in text.splitlines() if x.strip()][-12:]:
        if not line.startswith('PSI_'):
            log('psiphon: ' + line[:200])
    rcm, m = re.search(r'PSI_RC=(\d+)', text), re.search(r'PSI_ADDR=(\S+) PSI_PORT=(\d+)', text)
    if not rcm or rcm.group(1) != '0' or not m:
        tail = ' / '.join([x.strip() for x in text.splitlines() if x.strip() and not x.startswith('PSI_')][-3:])
        raise StepError('установка Psiphon не удалась' + (f': {tail[-300:]}' if tail else ''))
    log(f'Psiphon работает: SOCKS5 {m.group(1)}:{m.group(2)}')
    return {'address': m.group(1), 'port': int(m.group(2))}


def install_warp(r: 'Remote', opts: dict, log) -> None:
    """Ставит vps-warp с закреплённого коммита (или оставляет уже работающий). Вопросы установщика (язык, ключ WARP+)
    без терминала получают ответ по умолчанию: английский и без WARP+."""
    up = 'ip rule 2>/dev/null | grep -q "fwmark 0xff" && systemctl is-active --quiet wg-quick@warp && echo WARP_UP'
    _, out = r.run(f'{up}; true')
    if 'WARP_UP' in out:
        log('WARP уже работает на ноде')
        return
    log(f'ставлю vps-warp @ {WARP_REF[:12]}')
    rc, out = r.run(
        'd=$(mktemp -d /root/warp.XXXXXX) && cd "$d" && '
        f'curl -fsSL -o w.sh {WARP_RAW}/{WARP_REF}/warp_install.sh && '
        f'echo "{WARP_SHA256}  w.sh" | sha256sum -c --quiet && TERM=dumb setsid -w bash ./w.sh </dev/null; '
        f'echo "WARP_RC=$?"; sleep 2; {up}; true', 900)
    text = strip_ansi(out)
    lines = [x.strip() for x in text.splitlines() if x.strip() and not x.strip().startswith('WARP_')]
    for line in lines[-10:]:
        log('warp: ' + line[:200])
    m = re.search(r'WARP_RC=(\d+)', text)
    if not m or m.group(1) != '0' or 'WARP_UP' not in text:
        tail = ' / '.join(lines[-3:])
        raise StepError('установка WARP не удалась' + (f': {tail[-300:]}' if tail else ''))
    log(f'WARP работает: трафик с меткой {WARP_MARK} уходит в туннель')


def setup_port(r: 'Remote', public: int, target: int, log) -> None:
    """Клиенты подключаются на public, ядро переадресует его на target (порт Xray). Фаервол и Traffic Control видят
    уже target, поэтому открывать ничего не нужно; target тоже продолжает работать. Правило переживает перезагрузку."""
    if public == target:
        return
    unit = f'bnodes-port-{public}'
    rule = f'PREROUTING -p tcp --dport {public} -j REDIRECT --to-ports {target}'
    body = (f'[Unit]\nDescription=Bobr Nodes: port {public} -> {target}\n'
            'After=network-online.target docker.service ufw.service nftables.service\n'
            f'[Service]\nType=oneshot\nRemainAfterExit=yes\n'
            f"ExecStart=/bin/sh -c 'iptables -t nat -C {rule} 2>/dev/null || iptables -t nat -I {rule}; "
            f"ip6tables -t nat -C {rule} 2>/dev/null || ip6tables -t nat -I {rule} 2>/dev/null; true'\n"
            f"ExecStop=/bin/sh -c 'iptables -t nat -D {rule}; ip6tables -t nat -D {rule}; true'\n"
            '[Install]\nWantedBy=multi-user.target\n')
    rc, out = r.run(
        f'if ss -ltnH "sport = :{public}" 2>/dev/null | grep -q .; then echo PORT_BUSY; exit 0; fi; '
        'command -v iptables >/dev/null || { apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iptables >/dev/null; }; '
        f'printf "{body}" > /etc/systemd/system/{unit}.service && systemctl daemon-reload && '
        f'systemctl enable -q {unit} && systemctl restart {unit} && iptables -t nat -C {rule} && echo PORT_OK; true', 300)
    if 'PORT_BUSY' in out:
        raise StepError(f'порт {public} на ноде уже занят другой программой — выберите другой')
    if 'PORT_OK' not in out:
        raise StepError(f'не удалось переадресовать порт {public} → {target}: {strip_ansi(out).strip()[-200:]}')
    log(f'порт {public} переадресован на {target} (служба {unit})')


def add_keys(r: 'Remote', keys: str, log) -> None:
    for key in [k.strip().replace("'", '') for k in (keys or '').splitlines() if k.strip()]:
        r.run("umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; "
              f"grep -qxF '{key}' ~/.ssh/authorized_keys || echo '{key}' >> ~/.ssh/authorized_keys")
    if keys and keys.strip():
        log('SSH-ключи добавлены в authorized_keys')


def install_extras(r: 'Remote', node: dict, log) -> dict:
    """Psiphon и WARP на ноде → данные для профиля."""
    extras = {}
    if node.get('psiphon'):
        extras['psiphon'] = {'out': install_psiphon(r, node['psiphon'], log),
                             'domains': psiphon_rules(node['psiphon'].get('domains'))}
    if node.get('warp'):
        install_warp(r, node['warp'], log)
        extras['warp'] = {'domains': warp_rules(node['warp'].get('domains'))}
    if node.get('xhttp'):
        extras['xhttp'] = {'port': open_xhttp_port(r, XHTTP_PORT, log), 'mode': xhttp_mode((node['xhttp'] or {}).get('mode')
                                                                                         if isinstance(node['xhttp'], dict) else None)}
    return extras


def open_xhttp_port(r: 'Remote', port: int, log) -> int:
    """Порт запасного XHTTP на ноде: свободен (или его уже слушает Xray) и открыт в ufw, если тот включён."""
    rc, out = r.run(f'L=$(ss -ltnpH "sport = :{port}" 2>/dev/null); '
                    f'if [ -n "$L" ] && ! echo "$L" | grep -q xray; then echo XH_BUSY; exit 0; fi; '
                    f'if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then ufw allow {port}/tcp >/dev/null && echo XH_UFW; fi; '
                    'echo XH_OK')
    if 'XH_BUSY' in out:
        raise StepError(f'порт {port} на ноде занят другой программой — запасной XHTTP не поставить')
    if 'XH_OK' not in out:
        raise StepError(f'не удалось проверить порт {port}: {strip_ansi(out).strip()[-200:]}')
    log(f'порт {port} для XHTTP свободен' + (', открыт в ufw' if 'XH_UFW' in out else ''))
    return port


def deploy(node: dict, prof: dict, logfile, log: Callable[[str], None], stage: Callable[[str], None],
           check_tries: int = 12, check_every: int = 20) -> str:
    """Разворачивает одну ноду. node: name, ip, domain, country, remark, ssh_user/ssh_port/ssh_password/ssh_key,
    traffic_control. prof: panel_url, panel_token, panel_cookie, outgoing_ips, node_port, squads, cf_token, email,
    autoremove, installer_ref, installer_sha256, silence_timeout, ssh_pubkey, skip_dns, skip_panel.
    Возвращает итог одной строкой; при ошибке — StepError."""
    panel = None
    if not prof.get('skip_panel'):
        panel = Panel(prof['panel_url'], prof['panel_token'], prof.get('panel_cookie', ''), prof.get('panel_headers'))
    secret = prof.get('secret_key') or (panel.secret_key() if panel else '')
    if not secret:
        raise StepError('нет SECRET_KEY')

    if not prof.get('skip_dns'):
        stage('dns')
        cf = Cloudflare(prof['cf_token'])
        cf.upsert_a(node['domain'], node['ip'], log)
        wait_dns(node['domain'], node['ip'], log)

    stage('ssh')
    r = Remote(node, logfile)
    wait_ssh(r, log)
    add_keys(r, prof.get('ssh_pubkey') or '', log)
    _, out = r.run('test -f /opt/remnanode/.installation-complete && echo NV_DONE; '
                   'test -e /opt/remnanode && echo NV_EXISTS; '
                   'test -f /opt/remnanode/.nuvrion-managed && echo NV_MANAGED; true')
    done, exists, managed = 'NV_DONE' in out, 'NV_EXISTS' in out, 'NV_MANAGED' in out

    stage('install')
    if done:
        log('нода уже установлена — установку пропускаю')
    else:
        if exists and not managed:
            raise StepError('/opt/remnanode уже есть, но это не управляемая установка — нужен ручной разбор')
        resume = exists and managed
        log('продолжаю прерванную установку (--resume)' if resume else 'запускаю установщик')
        a = dict(domain=node['domain'], node_port=prof.get('node_port') or 2222, panel_ips=prof['outgoing_ips'],
                 email=prof['email'], secret=secret, autoremove=prof.get('autoremove', True),
                 traffic_control=node.get('traffic_control', prof.get('traffic_control', True)),
                 ssh_password=node.get('ssh_password'))
        child = r.spawn(remote_install_cmd(prof.get('installer_ref') or DEFAULT_REF,
                                           prof.get('installer_sha256') or DEFAULT_SHA256,
                                           resume, len(secret) > 3500),
                        True, int(prof.get('silence_timeout') or 2400))
        rc = drive_installer(child, a, log)
        if rc not in (0, 2):
            raise StepError(f'установщик завершился с кодом {rc}')
        log(f'установщик завершён, код {rc}' + (' (ждёт профиль из панели)' if rc == 2 else ''))

    port = (node.get('host') or {}).get('port')
    extras = {}
    if node.get('psiphon') or node.get('warp') or node.get('xhttp') or port:
        stage('extras')
        extras = install_extras(r, node, log)
        if port:
            setup_port(r, int(port), 443, log)
    if panel is None:
        return 'установлено, панель пропущена'
    stage('panel')
    # одинаковый путь к сертификату на всех нодах — для общего профиля
    rc, out = r.run(f'ln -sfn live/{node["domain"]} {CERT_LINK} && test -s {CERT_LINK}/fullchain.pem && echo LINK_OK')
    if 'LINK_OK' not in out:
        raise StepError(f'не нашёл сертификат /etc/letsencrypt/live/{node["domain"]} — установка выпустила его?')
    log(f'ссылка {CERT_LINK} → live/{node["domain"]}')
    rc, raw = r.run('cat /opt/remnanode/vision-config-profile.json')
    if rc != 0 or '{' not in raw:
        raise StepError('не удалось прочитать vision-config-profile.json')
    profile = json.loads(raw[raw.index('{'):])
    if node.get('squads') is not None:
        squads = list(node['squads'])
    else:
        squads = [x for x in re.split(r'[\s,]+', prof.get('squads') or '') if x] \
            if isinstance(prof.get('squads'), str) else list(prof.get('squads') or [])
    with PANEL_LOCK:
        pprof = panel.register(node, profile, squads, int(prof.get('node_port') or 2222), log, extras)
        if 'xhttp' in extras:
            panel.xhttp_publish(pprof, node['domain'], node['ip'], node.get('remark') or node['name'],
                                Cloudflare(prof['cf_token']) if prof.get('cf_token') else None, log, extras['xhttp']['mode'])

    stage('check')
    for attempt in range(check_tries):
        time.sleep(check_every)
        _, out = r.run('bash /opt/remnanode/installer.sh --check >/dev/null 2>&1; echo rc=$?', 300)
        m = re.search(r'rc=(\d+)', out)
        code = int(m.group(1)) if m else -1
        if code == 0:
            log('итоговая проверка пройдена ✓')
            return 'готово'
        log(f'проверка: код {code}, жду профиль из панели ({attempt + 1}/{check_tries})')
    raise StepError('панель настроена, но проверка на ноде так и не прошла — посмотрите ноду в панели')


# ─────────────────────────────── Установка по рецепту («Мои скрипты») ──

def drive_recipe(child, recipe: dict, values: dict, log, secret: str) -> int:
    """Отвечает на вопросы чужого скрипта строго по рецепту. Незнакомый вопрос — остановка.
    done_when — вывод, после которого установка считается законченной (скрипт прерывается Ctrl+C)."""
    from .scripts import DEFAULT_STOP, render
    rules = [(r'(?i)password:', None)] + [(p['match'], p) for p in recipe['prompts']]
    if recipe.get('done_when'):
        rules.append((recipe['done_when'], 'DONE'))
    stops = list(recipe.get('stop_on') or []) + DEFAULT_STOP
    rules += [(rx, 'STOP') for rx in stops]
    patterns = [rx for rx, _ in rules] + [pexpect.EOF, pexpect.TIMEOUT]
    started = False
    while True:
        i = child.expect(patterns)
        if i == len(rules):
            child.close()
            return child.exitstatus if child.exitstatus is not None else 128 + (child.signalstatus or 0)
        if i == len(rules) + 1:
            child.close(force=True)
            raise StepError('скрипт слишком долго ничего не выводил (таймаут)')
        _, rule = rules[i]
        if rule is None:  # пароль SSH — только до первого вопроса скрипта
            if not started and values.get('ssh_password'):
                child.sendline(values['ssh_password'])
            continue
        started = True
        if rule == 'DONE':
            log('скрипт сообщил о завершении — прерываю (done_when)')
            child.sendcontrol('c')
            try:
                child.expect(pexpect.EOF, timeout=30)
            except pexpect.TIMEOUT:
                pass
            child.close(force=True)
            return 0
        if rule == 'STOP':
            tail = strip_ansi((child.before or '') + (child.after or '')).strip().splitlines()
            text = tail[-1] if tail else ''
            child.sendcontrol('c')
            child.close(force=True)
            raise StepError(f'скрипт задал вопрос, которого нет в рецепте: «{text[-140:]}» — добавьте правило в «Мои скрипты»')
        answer = render(rule['answer'], values)
        shown = '‹SECRET_KEY›' if '{secret_key}' in rule['answer'] else answer
        q = strip_ansi(child.after).strip()
        log(f'ответ: {q[:70]} → {shown[:40]}')
        child.sendline(answer)
        if rule.get('multiline_end'):
            child.sendline('')


def deploy_recipe(node: dict, prof: dict, logfile, log: Callable[[str], None], stage: Callable[[str], None],
                  check_tries: int = 18, check_every: int = 10) -> str:
    """Установка ноды скриптом из «Моих скриптов». prof['script'] — утверждённый скрипт с рецептом."""
    from .scripts import command, render
    script = prof['script']
    rec = script['recipe']
    panel = Panel(prof['panel_url'], prof['panel_token'], prof.get('panel_cookie', ''), prof.get('panel_headers'))
    secret = panel.secret_key()
    ips = prof['outgoing_ips'].split()
    values = {'domain': node['domain'], 'ip': node['ip'], 'node_port': prof.get('node_port') or 2222,
              'panel_ips': ' '.join(ips), 'panel_ip': ips[0] if ips else '', 'email': prof.get('email', ''),
              'secret_key': secret, 'name': node['name'], 'yes': 'y', 'no': 'n',
              'ssh_password': node.get('ssh_password')}

    if not prof.get('skip_dns'):
        stage('dns')
        cf = Cloudflare(prof['cf_token'])
        cf.upsert_a(node['domain'], node['ip'], log)
        wait_dns(node['domain'], node['ip'], log)

    stage('ssh')
    r = Remote(node, logfile)
    wait_ssh(r, log)
    add_keys(r, prof.get('ssh_pubkey') or '', log)

    stage('install')
    existing = panel.node_state(node['ip'], node.get('domain', ''))
    if existing and existing.get('isConnected'):
        log('нода уже подключена к панели — установку пропускаю')
    else:
        log(f'запускаю {script["path"]} ({script["owner"]}/{script["repo"]} @ {script["sha"][:12]})')
        child = r.spawn(command(script, values), True, int(prof.get('silence_timeout') or 2400))
        rc = drive_recipe(child, rec, values, log, secret)
        if rc not in rec['success_codes']:
            raise StepError(f'скрипт завершился с кодом {rc}')
        log(f'скрипт завершён, код {rc}')
        for cmd in rec['post_commands']:
            rc, out = r.run(render(cmd, values), 300)
            log(f'после установки: {render(cmd, {**values, "secret_key": "‹SECRET_KEY›"})} → код {rc}')
            if rc != 0:
                raise StepError(f'команда после установки завершилась с кодом {rc}: {strip_ansi(out).strip()[-200:]}')

    extras = {}
    port = (node.get('host') or {}).get('port')
    if node.get('psiphon') or node.get('warp') or port:
        stage('extras')
        extras = install_extras(r, node, log)

    stage('panel')
    squads = list(node['squads']) if node.get('squads') is not None else \
        [x for x in re.split(r'[\s,]+', prof.get('squads') or '') if x]
    with PANEL_LOCK:
        if rec['profile']['mode'] == 'file':
            rc, raw = r.run(f"cat {rec['profile']['path']}")
            if rc != 0 or '{' not in raw:
                raise StepError(f"не удалось прочитать профиль {rec['profile']['path']} на ноде")
            cfg = json.loads(raw[raw.index('{'):])
            tag = rec['profile'].get('inbound_tag') or (cfg.get('inbounds') or [{}])[0].get('tag', '')
            pname = re.sub(r'[^A-Za-z0-9_ -]', '-', f'NV {node["name"]}')[:30]
            new_tag = f'{tag}-{node["name"]}'[:60]
            cfg = retag(cfg, tag, new_tag)  # теги inbound в панели уникальны
            cfg = apply_extras(cfg, extras)
            if port:
                setup_port(r, int(port), int(next((i.get('port') for i in cfg.get('inbounds', []) if i.get('tag') == new_tag), 443)), log)
            profiles = panel.call('GET', '/api/config-profiles')['configProfiles']
            pr = next((p for p in profiles if p['name'] == pname), None) or \
                panel.call('POST', '/api/config-profiles', {'name': pname, 'config': cfg})
            inbounds = [i for i in pr['inbounds'] if i['tag'] == new_tag] or pr['inbounds'][:1]
            panel.attach(node, pr, inbounds, squads, int(values['node_port']), log, tls=False)
        else:
            if not prof.get('panel_profile_uuid'):
                raise StepError('в группе не выбран профиль панели для этого скрипта (Настройки группы → Скрипт установки)')
            pr, inbounds = panel.existing_profile(prof['panel_profile_uuid'], prof.get('panel_inbounds') or [])
            log(f'панель: профиль «{pr["name"]}», inbound: {", ".join(i["tag"] for i in inbounds)}')
            problem = extras_problem(pr.get('config'), extras, pr['name'])
            if problem:  # чужой профиль бот не меняет — только предупреждает
                log(f'⚠️ {problem}. Дополнение поставлено на ноду, но трафик в него не идёт, пока выхода нет в профиле')
            if port:
                setup_port(r, int(port), int(inbounds[0].get('port') or 443), log)
            panel.attach(node, pr, inbounds, squads, int(values['node_port']), log, tls=False)

    stage('check')
    if rec.get('check_command'):
        rc, out = r.run(render(rec['check_command'], values), 120)
        if rc != 0:
            raise StepError(f'проверка из рецепта не прошла (код {rc}): {strip_ansi(out).strip()[-200:]}')
    for attempt in range(check_tries):
        st = panel.node_state(node['ip'], node.get('domain', '')) or {}
        if st.get('isConnected'):
            log('нода подключилась к панели ✓')
            return 'готово'
        log(f'жду подключения ноды к панели ({attempt + 1}/{check_tries})' +
            (f': {st["lastStatusMessage"]}' if st.get('lastStatusMessage') else ''))
        time.sleep(check_every)
    raise StepError('нода установлена, но к панели так и не подключилась — проверьте порт API и фаервол')


# ─────────────────────────────── Дополнения на уже работающей ноде ──

def xray_problem(r: 'Remote', port: int, log, tries: int = 12, every: float = 10) -> str:
    """После смены профиля панель перезапускает Xray на ноде. Ждём, пока он снова слушает порт inbound, и смотрим
    его ошибки в логах контейнера. '' — всё хорошо, иначе — что не так (строки ошибок уходят в журнал)."""
    errs: list[str] = []
    for attempt in range(max(tries, 2)):
        time.sleep(every if attempt else max(every, 1) * 2)  # сначала даём панели перезапустить Xray
        _, out = r.run(f'ss -ltnH "sport = :{port}" 2>/dev/null | head -3; echo "@@@"; '
                       '(docker logs --since 3m remnanode 2>&1 || true) | grep -iE "failed|error|panic|invalid|unknown" | tail -8', 60)
        listen, _, logs = strip_ansi(out).partition('@@@')
        errs = [x.strip()[:300] for x in logs.splitlines() if x.strip()]
        if listen.strip() and attempt:
            log(f'Xray слушает порт {port} ✓')
            return ''
        log(f'жду Xray на порту {port} ({attempt + 1}/{max(tries, 2)})')
    for e in errs:
        log('xray: ' + e)
    return f'Xray на ноде не слушает порт {port} после смены профиля'


def update_node(node: dict, prof: dict, logfile, log: Callable[[str], None], stage: Callable[[str], None],
                check_tries: int = 12, check_every: int = 10) -> str:
    """Нода из панели: ставит Psiphon/WARP по SSH и переводит ноду в вариант её профиля с этими выходами;
    по желанию — нестандартный порт (переадресация на ноде + порт хостов) и Final Mask на её хостах.
    node: node_uuid, ip, name, domains, ssh_*, psiphon, warp, port ('' — оставить, 443 — стандартный), final_mask
    (None — не трогать, False — убрать, dict — поставить)."""
    panel = Panel(prof['panel_url'], prof['panel_token'], prof.get('panel_cookie', ''), prof.get('panel_headers'))
    pnode = next((n for n in panel.call('GET', '/api/nodes') if n['uuid'] == node['node_uuid']), None)
    if not pnode:
        raise StepError('ноды больше нет в панели')
    node = {**node, 'ip': node.get('ip') or pnode['address']}
    domain = next(iter(node.get('domains') or []), '') or ('' if is_ip(pnode['address']) else pnode['address'])
    cf = Cloudflare(prof['cf_token']) if prof.get('cf_token') else None

    def ipv4() -> str:  # для A-записи cdn-домена нужен сам IP, а адрес ноды в панели — часто домен
        if is_ip(node['ip']):
            return node['ip']
        try:
            return socket.getaddrinfo(node['ip'], None, socket.AF_INET)[0][4][0]
        except (OSError, IndexError):
            raise StepError(f'не узнал IP ноды по {node["ip"]}') from None

    if node.get('rollback'):  # убрать дополнения: нода и хосты — обратно в исходный профиль
        stage('panel')
        with PANEL_LOCK:
            hosts = panel.node_hosts(pnode, set(node.get('domains') or []), {node['ip']} if is_ip(node['ip']) else set())
            was = next((p for p in panel.call('GET', '/api/config-profiles')['configProfiles']
                        if p['uuid'] == (pnode.get('configProfile') or {}).get('activeConfigProfileUuid')), None)
            base = panel.downgrade_node(pnode, hosts, log)
            if was and xhttp_of(was.get('config')):
                panel.xhttp_unpublish(was, domain, ipv4(), cf, log)
        stage('check')
        for attempt in range(check_tries):
            time.sleep(check_every)
            if (panel.node_state('', uuid=pnode['uuid']) or {}).get('isConnected'):
                log(f'нода снова в «{base["name"]}» и на связи ✓')
                return 'готово'
        raise StepError('после возврата профиля нода не вышла на связь — посмотрите её в панели')
    want_port = node.get('port')
    if node.get('xhttp') and not domain:
        raise StepError('для запасного XHTTP нужен домен ноды: сертификат на ноде выпущен на домен, а не на IP')
    if isinstance(node.get('xhttp'), dict) and node['xhttp'].get('mode') == 'cdn' and cf is None:
        raise StepError('XHTTP через CDN: нужен токен Cloudflare (общий в «Подключениях» или у группы)')
    need_ssh = bool(node.get('psiphon') or node.get('warp') or node.get('xhttp') or want_port)
    extras, r = {}, None
    if need_ssh:
        stage('ssh')
        r = Remote(node, logfile)
        wait_ssh(r, log)
        add_keys(r, prof.get('ssh_pubkey') or '', log)
        stage('extras')
        extras = install_extras(r, node, log)

    stage('panel')
    with PANEL_LOCK:
        hosts = panel.node_hosts(pnode, set(node.get('domains') or []), {node['ip']} if is_ip(node.get('ip') or '') else set())
        log(f'панель: хостов у ноды — {len(hosts)}' + (': ' + ', '.join(h['remark'] for h in hosts) if hosts else ''))
        profiles = panel.call('GET', '/api/config-profiles')['configProfiles']
        cur = next((p for p in profiles if p['uuid'] == (pnode.get('configProfile') or {}).get('activeConfigProfileUuid')), None)
        before = cur
        if extras:
            cur = panel.upgrade_node(pnode, extras, hosts, log)
        if 'xhttp' in extras:
            main_in = {i['uuid'] for i in (cur or {}).get('inbounds', []) if not i['tag'].startswith(XHTTP_PREFIX)}
            remark = next((h['remark'] for h in hosts if (h.get('inbound') or {}).get('configProfileInboundUuid') in main_in
                           and h.get('address') == domain), None) or pnode['name']
            panel.xhttp_publish(cur, domain, ipv4(), remark, cf, log, extras['xhttp']['mode'])
        if want_port is not None and want_port != '':
            active = set(active_inbounds(pnode))
            ib = next((i for i in (cur or {}).get('inbounds', []) if i['uuid'] in active and i.get('port')), None)
            target = int((ib or {}).get('port') or 443)
            public = public_port(want_port, int(prof.get('node_port') or 2222)) or target
            if public != target:
                setup_port(r, public, target, log)
            for h in hosts:
                if h.get('port') != public:
                    panel.call('PATCH', '/api/hosts', {'uuid': h['uuid'], 'port': public})
            log(f'панель: порт хостов — {public}' + ('' if public == target else f' (на ноде → {target})'))
        if node.get('final_mask') is not None:
            mask = node['final_mask'] or None
            for h in hosts:
                panel.call('PATCH', '/api/hosts', {'uuid': h['uuid'], 'finalMask': mask})
            log('панель: Final Mask ' + ('поставлен' if mask else 'убран') + f' на хостах ({len(hosts)})')

    if extras and r is not None:
        stage('check')
        port = next((int(i['port']) for i in (cur or {}).get('inbounds', []) if i['uuid'] in active_inbounds(pnode) and i.get('port')
                     and not i['tag'].startswith(XHTTP_PREFIX)), 443)
        problem = xray_problem(r, port, log, tries=check_tries, every=check_every)
        xh = xhttp_of((cur or {}).get('config'))
        if not problem and xh:
            problem = xray_problem(r, xh['port'], log, tries=max(3, check_tries // 2), every=check_every)
        if problem:  # Xray не поднялся с новым профилем — сразу возвращаем ноду, чтобы клиенты не остались без связи
            log(f'⚠️ {problem} — возвращаю ноду в профиль «{before["name"]}»')
            if before and cur and before['uuid'] != cur['uuid']:
                base = lambda t: _strip_extras('', [t])[1][0]  # noqa: E731
                back = {base(i['tag']): i['tag'] for i in before['inbounds']}
                with PANEL_LOCK:
                    panel.move_node(pnode, cur, before, {i['tag']: back[base(i['tag'])] for i in cur['inbounds']
                                                        if base(i['tag']) in back}, hosts, log, squads=False)
                    if xh and not xhttp_of(before.get('config')):
                        panel.xhttp_unpublish(cur, domain, ipv4(), cf, log)
            raise StepError(f'{problem}. Нода возвращена в профиль «{before["name"]}», клиенты снова подключаются. '
                            'Строки ошибок Xray — в журнале задачи')
    if extras:
        stage('check')
        for attempt in range(check_tries):
            time.sleep(check_every)
            st = panel.node_state('', uuid=node['node_uuid']) or {}
            if st.get('isConnected'):
                log('нода на связи с панелью ✓')
                return 'готово'
            log(f'жду, пока нода перезапустит Xray ({attempt + 1}/{check_tries})')
        raise StepError('после смены профиля нода не вышла на связь с панелью — посмотрите её в панели')
    return 'готово'


# ─────────────────────────────── Перепривязка IP (адрес в бане) ──

def is_ip(x: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_address(x)
        return True
    except ValueError:
        return False


def reip(node: dict, prof: dict, logfile, log: Callable[[str], None], stage: Callable[[str], None],
         check_tries: int = 30, check_every: int = 10, dns_timeout: int = 600) -> str:
    """Нода переехала на новый IP: A-записи её доменов в Cloudflare → новый IP, вход по SSH на новом IP,
    адрес ноды в панели — домен (или новый IP, если домена нет), хосты, где был старый IP, — на новый.
    node: node_uuid, old_ip, new_ip, domains, ssh_*. Старый IP в DNS меняем только там, где он и был."""
    old, new = node['old_ip'], node['new_ip']
    domains = [d for d in dict.fromkeys(node.get('domains') or []) if d and not is_ip(d)]
    panel = Panel(prof['panel_url'], prof['panel_token'], prof.get('panel_cookie', ''), prof.get('panel_headers'))
    pnode = panel.node_state('', uuid=node['node_uuid'])
    if not pnode:
        raise StepError('ноды больше нет в панели')
    with PANEL_LOCK:  # домены хостов ноды: кроме известных боту — те, что сейчас указывают на старый IP
        for h in panel.node_hosts(pnode, set(domains) | {old}, {old}):
            a = h.get('address') or ''
            if a and not is_ip(a) and a not in domains:
                domains.append(a)
    if not is_ip(pnode['address']) and pnode['address'] not in domains:
        domains.insert(0, pnode['address'])

    stage('dns')
    changed = []
    if domains:
        cf = Cloudflare(prof['cf_token'])
        for d in domains:
            try:
                recs = [r for r in cf.records(d) if r['type'] == 'A']
            except StepError as e:
                log(f'{d}: пропускаю — {e}')
                continue
            if recs and all(r['content'] == new for r in recs):
                log(f'{d}: уже {new}')
                changed.append(d)
                continue
            if recs and not any(r['content'] == old for r in recs) and d not in (node.get('domains') or []):
                log(f'{d}: указывает на {", ".join(r["content"] for r in recs)}, не на {old} — не трогаю')
                continue
            cf.upsert_a(d, new, log)
            changed.append(d)
        for d in changed:
            wait_dns(d, new, log, timeout=dns_timeout)
    if not changed:
        log('доменов этой ноды в Cloudflare не нашёл — адрес ноды в панели будет новым IP')

    stage('ssh')
    r = Remote({**node, 'ip': new}, logfile)
    wait_ssh(r, log, timeout=900)

    stage('panel')
    addr = pnode['address'] if (not is_ip(pnode['address']) and pnode['address'] in changed) else (changed[0] if changed else new)
    with PANEL_LOCK:
        if pnode['address'] != addr:
            panel.call('PATCH', '/api/nodes', {'uuid': pnode['uuid'], 'address': addr})
            log(f'панель: адрес ноды {pnode["address"]} → {addr}')
        else:
            log(f'панель: адрес ноды — {addr}, менять не нужно')
        for h in panel.call('GET', '/api/hosts'):
            if h.get('address') == old:
                panel.call('PATCH', '/api/hosts', {'uuid': h['uuid'], 'address': addr})
                log(f'панель: хост «{h.get("remark", "")}» {old} → {addr}')

    stage('check')
    for attempt in range(check_tries):
        st = panel.node_state('', uuid=pnode['uuid']) or {}
        if st.get('isConnected'):
            log('нода на связи с панелью ✓')
            return 'готово'
        log(f'жду подключения ноды ({attempt + 1}/{check_tries})')
        time.sleep(check_every)
    raise StepError('нода не вышла на связь с панелью на новом IP — проверьте её в панели')


# ─────────────────────────────── Сервер удалён в bcloud ──

def remove_node(prof: dict, ip: str, domain: str, log: Callable[[str], None]) -> dict:
    """Сервер удалён (в bcloud или истёк): нода и её хосты — из панели, A-запись домена — из Cloudflare.
    Профиль, шаблоны и общие хосты-балансировщики остаются: ими пользуются другие ноды."""
    panel = Panel(prof['panel_url'], prof['panel_token'], prof.get('panel_cookie', ''), prof.get('panel_headers'))
    names = {ip, domain} - {''}
    removed = {'nodes': 0, 'hosts': 0, 'dns': 0}
    with PANEL_LOCK:
        nodes = [n for n in panel.call('GET', '/api/nodes') if n['address'] in names]
        hosts = {}
        for n in nodes:
            for h in panel.node_hosts(n, {domain} - {''}, {ip} - {''}):
                hosts[h['uuid']] = h
        for h in panel.call('GET', '/api/hosts'):
            if h.get('address') in names:
                hosts[h['uuid']] = h
        btpl = panel.balancer_templates()
        for h in hosts.values():
            if h.get('xrayJsonTemplateUuid') in btpl:  # хост-балансировщик страны общий — оставляем
                log(f'панель: хост-балансировщик «{h.get("remark", "")}» оставлен (общий для страны)')
                continue
            panel.call('DELETE', f'/api/hosts/{h["uuid"]}')
            removed['hosts'] += 1
            log(f'панель: хост «{h.get("remark", "")}» удалён')
        for n in nodes:
            panel.call('DELETE', f'/api/nodes/{n["uuid"]}')
            removed['nodes'] += 1
            log(f'панель: нода «{n["name"]}» удалена')
    if domain and prof.get('cf_token'):
        try:
            removed['dns'] = Cloudflare(prof['cf_token']).delete_a(domain, ip, log)
        except StepError as e:
            log(f'Cloudflare: {e}')
    return removed


# ─────────────────────────────── Перенос хостов между панелями ──

BUNDLE_KIND = 'bobr-nodes/hosts'
HOST_FIELDS = ['remark', 'address', 'port', 'path', 'sni', 'host', 'alpn', 'fingerprint', 'isDisabled', 'securityLayer',
               'xhttpExtraParams', 'muxParams', 'sockoptParams', 'finalMask', 'serverDescription', 'tags', 'isHidden',
               'overrideSniFromAddress', 'keepSniBlank', 'pinnedPeerCertSha256', 'verifyPeerCertByName', 'vlessRouteId',
               'shuffleHost', 'mihomoX25519', 'mihomoIpVersion', 'excludeFromSubscriptionTypes', 'mapper']


def export_hosts(panel: 'Panel', uuids: list[str]) -> dict:
    """Набор для переноса: хосты со всеми полями + их профили (конфиг Xray) и Xray JSON-шаблоны.
    Ссылки внутри — по именам и тегам, а не UUID: в другой панели UUID свои."""
    hosts = {h['uuid']: h for h in panel.call('GET', '/api/hosts')}
    profiles = {p['uuid']: p for p in panel.call('GET', '/api/config-profiles')['configProfiles']}
    tpls = {t['uuid']: t for t in panel.templates()}
    out = {'kind': BUNDLE_KIND, 'version': 1, 'profiles': [], 'templates': [], 'hosts': []}
    seen_p, seen_t = set(), set()
    for u in uuids:
        h = hosts.get(u)
        if not h:
            raise StepError('хоста больше нет в панели — обновите список')
        ib = h.get('inbound') or {}
        prof = profiles.get(ib.get('configProfileUuid'))
        inbound = next((i for i in (prof or {}).get('inbounds', []) if i['uuid'] == ib.get('configProfileInboundUuid')), None)
        if not prof or not inbound:
            raise StepError(f'у хоста «{h.get("remark", "")}» нет профиля или inbound')
        if prof['uuid'] not in seen_p:
            seen_p.add(prof['uuid'])
            out['profiles'].append({'name': prof['name'], 'config': prof.get('config') or {}})
        tpl = tpls.get(h.get('xrayJsonTemplateUuid')) if h.get('xrayJsonTemplateUuid') else None
        if tpl and tpl['uuid'] not in seen_t:
            seen_t.add(tpl['uuid'])
            out['templates'].append({'name': tpl['name'], 'templateType': tpl.get('templateType') or 'XRAY_JSON',
                                     'templateJson': tpl.get('templateJson')})
        item = {k: h[k] for k in HOST_FIELDS if h.get(k) is not None}
        item.update({'profile': prof['name'], 'inbound': inbound['tag'], 'template': tpl['name'] if tpl else None})
        out['hosts'].append(item)
    return out


def _safe_name(name: str, limit: int) -> str:
    return (re.sub(r'[^A-Za-z0-9_\s-]', '-', str(name or '')).strip() or 'Imported')[:limit]


def check_bundle(b) -> dict:
    if isinstance(b, str):
        try:
            b = json.loads(b)
        except ValueError as e:
            raise StepError(f'это не JSON: {e}') from None
    if not isinstance(b, dict) or b.get('kind') != BUNDLE_KIND:
        raise StepError('это не набор хостов Bobr Nodes (нет "kind": "bobr-nodes/hosts")')
    hosts, profiles, tpls = b.get('hosts') or [], b.get('profiles') or [], b.get('templates') or []
    if not isinstance(hosts, list) or not 1 <= len(hosts) + len(tpls) <= 300:
        raise StepError('в наборе нет хостов и шаблонов (или их слишком много)')
    pnames = {p.get('name') for p in profiles if isinstance(p, dict)}
    tnames = {t.get('name') for t in tpls if isinstance(t, dict)}
    for t in tpls:  # всё проверяем до того, как что-то менять в панели
        if not isinstance(t, dict) or not t.get('name') or (t.get('templateType') or 'XRAY_JSON') != 'XRAY_JSON' \
                or not isinstance(t.get('templateJson'), dict):
            raise StepError(f'шаблон «{(t or {}).get("name", "?")}»: нужен Xray JSON-шаблон с содержимым')
    for p in profiles:
        if not isinstance(p, dict) or not p.get('name') or not isinstance(p.get('config'), dict):
            raise StepError('в наборе профиль без имени или конфига')
    for h in hosts:
        if not isinstance(h, dict) or h.get('profile') not in pnames or not h.get('inbound') or not h.get('remark'):
            raise StepError('в наборе хост без профиля, inbound или названия')
        prof = next(p for p in profiles if p['name'] == h['profile'])
        if h['inbound'] not in [i.get('tag') for i in prof['config'].get('inbounds', [])]:
            raise StepError(f'хост «{h["remark"]}»: в профиле «{prof["name"]}» нет inbound {h["inbound"]}')
        if h.get('template') and h['template'] not in tnames:
            raise StepError(f'шаблона «{h["template"]}» нет в наборе')
    return b


def import_bundle(panel: 'Panel', bundle, squads: list[str], template_mode: str, log) -> dict:
    """Создаёт в панели хосты из набора. Профили: одноимённый с тем же конфигом — берём его, иначе — копия с новыми
    тегами inbound (теги в панели уникальны). Шаблоны: одноимённый — обновить (template_mode='update') или копия.
    Хост с тем же названием, адресом и портом — обновляется, а не дублируется. inbound'ы — в выбранные сквады."""
    b = check_bundle(bundle)
    res = {'profiles': 0, 'templates': 0, 'created': 0, 'updated': 0}
    profiles = panel.call('GET', '/api/config-profiles')['configProfiles']
    taken = {i['tag'] for p in profiles for i in p['inbounds']}
    pmap: dict[str, tuple[dict, dict]] = {}   # имя в наборе → (профиль в панели, тег из набора → тег в панели)
    for p in b.get('profiles') or []:
        if not any(h['profile'] == p['name'] for h in b['hosts']):
            continue
        cfg = p.get('config') or {}
        tags = [i.get('tag') for i in cfg.get('inbounds', []) if i.get('tag')]
        same = next((x for x in profiles if x['name'] == p['name'] and (x.get('config') or {}) == cfg), None)
        if same:
            pmap[p['name']] = (same, {t: t for t in tags})
            log(f'профиль «{p["name"]}» уже есть такой же — использую его')
            continue
        tmap = {}
        for t in tags:
            new, n = t, 1
            while new in taken:
                n += 1
                new = f'{t}-{n}'
            tmap[t] = new
            taken.add(new)
        for old, new in tmap.items():
            if old != new:
                cfg = retag(cfg, old, new)
        names = {x['name'] for x in profiles}
        name, n = _safe_name(p['name'], 29), 1
        while name in names:
            n += 1
            name = f'{_safe_name(p["name"], 26)} {n}'
        created = panel.call('POST', '/api/config-profiles', {'name': name, 'config': cfg})
        profiles.append(created)
        pmap[p['name']] = (created, tmap)
        res['profiles'] += 1
        log(f'профиль «{name}» создан' + (f' (теги: {", ".join(f"{a}→{c}" for a, c in tmap.items() if a != c)})'
                                          if any(a != c for a, c in tmap.items()) else ''))
    tpls = panel.templates()
    tmap_t: dict[str, str] = {}
    for t in b.get('templates') or []:
        if (t.get('templateType') or 'XRAY_JSON') != 'XRAY_JSON' or not isinstance(t.get('templateJson'), dict):
            raise StepError(f'шаблон «{t.get("name")}»: переносятся только Xray JSON-шаблоны')
        cur = next((x for x in tpls if x['name'] == t['name'] and x.get('templateType') == 'XRAY_JSON'), None)
        if cur and (cur.get('templateJson') == t['templateJson'] or template_mode == 'update'):
            if cur.get('templateJson') != t['templateJson']:
                panel.call('PATCH', '/api/subscription-templates', {'uuid': cur['uuid'], 'templateJson': t['templateJson']})
                log(f'шаблон «{cur["name"]}» обновлён')
            else:
                log(f'шаблон «{cur["name"]}» уже такой же')
            tmap_t[t['name']] = cur['uuid']
            continue
        names = {x['name'] for x in tpls}
        name, n = _safe_name(t['name'], 250), 1
        while name in names:
            n += 1
            name = f'{_safe_name(t["name"], 240)} {n}'
        new = panel.call('POST', '/api/subscription-templates', {'name': name, 'templateType': 'XRAY_JSON'})
        new = panel.call('PATCH', '/api/subscription-templates', {'uuid': new['uuid'], 'templateJson': t['templateJson']})
        tpls.append(new)
        tmap_t[t['name']] = new['uuid']
        res['templates'] += 1
        log(f'шаблон «{name}» создан')
    existing = panel.call('GET', '/api/hosts')
    used_inbounds = set()
    for h in b['hosts']:
        prof, tagmap = pmap[h['profile']]
        tag = tagmap.get(h['inbound'], h['inbound'])
        ib = next((i for i in prof['inbounds'] if i['tag'] == tag), None)
        if not ib:
            raise StepError(f'в профиле «{prof["name"]}» нет inbound {tag}')
        used_inbounds.add(ib['uuid'])
        body = {k: h[k] for k in HOST_FIELDS if h.get(k) is not None}
        body['inbound'] = {'configProfileUuid': prof['uuid'], 'configProfileInboundUuid': ib['uuid']}
        body['xrayJsonTemplateUuid'] = tmap_t.get(h['template']) if h.get('template') else None
        same = next((x for x in existing if x.get('remark') == h['remark'] and x.get('address') == h.get('address')
                     and x.get('port') == h.get('port')), None)
        if same:
            panel.call('PATCH', '/api/hosts', {'uuid': same['uuid'], **body})
            res['updated'] += 1
            log(f'хост «{h["remark"]}» обновлён')
        else:
            panel.call('POST', '/api/hosts', body)
            res['created'] += 1
            log(f'хост «{h["remark"]}» создан')
    for sq in squads:
        cur = panel.call('GET', f'/api/internal-squads/{sq}')
        ids = [i['uuid'] for i in cur.get('inbounds', [])]
        add = [u for u in used_inbounds if u not in ids]
        if add:
            panel.call('PATCH', '/api/internal-squads', {'uuid': sq, 'inbounds': ids + add})
            log(f'inbound добавлен в сквад «{cur.get("name", sq)}»')
    return res


def template_bundle(name: str, raw) -> dict:
    """Просто Xray JSON (присланный роутинг) → набор из одного шаблона, без хостов."""
    try:
        tj = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError as e:
        raise StepError(f'это не JSON: {e}') from None
    if not isinstance(tj, dict):
        raise StepError('шаблон Xray JSON — это объект { … }')
    if not name or not name.strip():
        raise StepError('укажите имя шаблона')
    return {'kind': BUNDLE_KIND, 'version': 1, 'profiles': [], 'hosts': [],
            'templates': [{'name': name.strip(), 'templateType': 'XRAY_JSON', 'templateJson': tj}]}
