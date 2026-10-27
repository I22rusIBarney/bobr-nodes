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

    def upsert_a(self, domain: str, ip: str, log):
        zid = self.zone_for(domain)
        records = self.records(domain)
        body = {'type': 'A', 'name': domain, 'content': ip, 'ttl': 60, 'proxied': False, 'comment': 'nv-deploy'}
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
            if r['content'] == ip and not r.get('proxied'):
                log(f'DNS: A {domain} → {ip} уже есть')
                return
            self.call('PUT', f'/zones/{zid}/dns_records/{r["id"]}', body)
            log(f'DNS: A {domain} обновлена → {ip} (было {r["content"]}, proxied={r.get("proxied")})')
        else:
            self.call('POST', f'/zones/{zid}/dns_records', body)
            log(f'DNS: A {domain} → {ip} создана')


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


def psiphon_rules(raw) -> list[str]:
    """Список доменов для Psiphon: «google.com» → «domain:google.com»; geosite:/full:/keyword: — как есть."""
    items = re.split(r'[\s,]+', raw) if isinstance(raw, str) else list(raw or [])
    out = []
    for x in items:
        x = str(x).strip().lower()
        if not x:
            continue
        if not re.match(r'(domain|full|keyword|geosite):', x):
            x = 'domain:' + x
        if not re.fullmatch(r'(domain|full|keyword|geosite):[a-z0-9][a-z0-9._@!-]{0,200}', x):
            raise StepError(f'Psiphon: непонятный домен «{x}»')
        if x not in out:
            out.append(x)
    return out or list(PSI_DOMAINS)


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
        for t in self.call('GET', '/api/subscription-templates')['templates']:
            if t.get('templateType') == 'XRAY_JSON':
                tpls.append({'uuid': t['uuid'], 'name': t['name'], 'balancer_tag': balancer_tag_of(t.get('templateJson'))})
        profiles = [{'uuid': p['uuid'], 'name': p['name'],
                     'inbounds': [{'uuid': i['uuid'], 'tag': i['tag'], 'port': i.get('port'), 'type': i.get('type'),
                                   'security': i.get('security')} for i in p.get('inbounds', [])],
                     'psiphon': psiphon_of(p.get('config')) is not None}
                    for p in self.call('GET', '/api/config-profiles')['configProfiles']]
        hosts = [{'uuid': x['uuid'], 'remark': x.get('remark', ''), 'address': x.get('address', ''),
                  'hidden': bool(x.get('isHidden')), 'disabled': bool(x.get('isDisabled')), 'tags': x.get('tags') or [],
                  'balancer': bool(x.get('xrayJsonTemplateUuid'))}
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

    def ensure_balancer(self, tag: str, base_uuid: str | None, remark: str, prof: dict, inbound: dict,
                        node: dict, log, tls: bool = True) -> str | None:
        """Шаблон Balancer_<tag> (копия базового с подстановкой тега) и видимый хост-балансировщик с ним.
        Возвращает UUID хоста-балансировщика, если он создан сейчас.
        Если они уже есть — ничего не меняем: новые скрытые хосты с тегом подхватятся сами."""
        tag = tag.upper()
        tpls = self.call('GET', '/api/subscription-templates')['templates']
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
                 psiphon: dict | None = None):
        # Один общий профиль на все ноды: тег Vision-NV (уникален в панели), сертификат — через ссылку CERT_LINK.
        # Ноды с Psiphon — в своём общем профиле: в нём есть выход psiphon-out, которого на остальных нодах нет.
        tag, pname = SHARED_TAG, SHARED_PROFILE
        profile_config = shared_profile(profile_config, node['domain'])
        if psiphon:
            tag, pname = SHARED_TAG + '-PS', SHARED_PROFILE + ' Psiphon'
            profile_config = add_psiphon(retag(profile_config, SHARED_TAG, tag), psiphon['out'], psiphon['domains'])
        profiles = self.call('GET', '/api/config-profiles')['configProfiles']
        prof = next((p for p in profiles if p['name'] == pname), None)
        if prof:
            # не перезаписываем: изменение профиля перезапускает Xray на всех нодах сразу
            log(f'панель: общий профиль «{pname}» уже есть')
            if psiphon:
                have = psiphon_of(prof.get('config'))
                if have is None:
                    raise StepError(f'в профиле «{pname}» нет выхода {PSI_TAG} — добавьте его в панели или удалите профиль, бот создаст заново')
                if have != {'address': psiphon['out']['address'], 'port': int(psiphon['out']['port'])}:
                    raise StepError(f'Psiphon на ноде слушает {psiphon["out"]["address"]}:{psiphon["out"]["port"]}, '
                                    f'а профиль «{pname}» ведёт на {have["address"]}:{have["port"]}')
        else:
            prof = self.call('POST', '/api/config-profiles', {'name': pname, 'config': profile_config})
            log(f'панель: общий профиль «{pname}» создан' +
                (f', Psiphon для: {", ".join(psiphon["domains"])}' if psiphon else ''))
        inbound = next((i for i in prof['inbounds'] if i['tag'] == tag), None)
        if not inbound:
            raise StepError(f'в профиле нет inbound {tag}')
        self.attach(node, prof, [inbound], squads, node_port, log, tls=True)

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

    def node_state(self, ip: str) -> dict | None:
        return next((n for n in self.call('GET', '/api/nodes') if n['address'] == ip), None)

    @staticmethod
    def host_body(prof: dict, inbound: dict, node: dict, tls: bool) -> dict:
        body = {'inbound': {'configProfileUuid': prof['uuid'], 'configProfileInboundUuid': inbound['uuid']},
                'address': node['domain']}
        if tls:  # свой TLS-профиль бота: SNI = домен ноды
            body.update({'port': 443, 'sni': node['domain'], 'alpn': 'h2,http/1.1', 'fingerprint': 'firefox',
                         'securityLayer': 'TLS'})
        else:    # чужой профиль (например Reality): параметры безопасности — из inbound
            body.update({'port': int(inbound.get('port') or 443), 'securityLayer': 'DEFAULT'})
        return body

    def attach(self, node: dict, prof: dict, inbounds: list[dict], squads: list[str], node_port: int, log,
               tls: bool = True):
        """Нода с профилем и inbound'ами, хосты (обычные/скрытые с тегами/балансировщик), сквады."""
        cp = {'activeConfigProfileUuid': prof['uuid'], 'activeInbounds': [i['uuid'] for i in inbounds]}

        nodes = self.call('GET', '/api/nodes')
        existing = next((n for n in nodes if n['address'] in (node['ip'], node['domain'])), None)
        if existing:
            self.call('PATCH', '/api/nodes', {'uuid': existing['uuid'], 'configProfile': cp})
            log(f'панель: нода «{existing["name"]}» уже была, профиль назначен')
        else:
            cc = (node.get('country') or '').upper()
            body = {'name': node['name'][:30], 'address': node['ip'], 'port': node_port,
                    'countryCode': cc if re.fullmatch(r'[A-Z]{2}', cc) else 'XX', 'configProfile': cp}
            self.call('POST', '/api/nodes', body)
            log(f'панель: нода «{node["name"]}» создана')

        opts = node.get('host') or {}
        balancer = opts.get('mode') == 'balancer'
        hidden = bool(opts.get('hidden', balancer))
        tags = [t for t in (opts.get('tags') or []) if re.fullmatch(r'[A-Z0-9_:]{1,36}', t)]
        hosts = self.call('GET', '/api/hosts')
        mine = []
        for n, inbound in enumerate(inbounds, 1):
            host = next((h for h in hosts if h['address'] == node['domain']
                         and h['inbound']['configProfileInboundUuid'] == inbound['uuid']
                         and not h.get('xrayJsonTemplateUuid')), None)
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

        for sq in squads:
            cur = self.call('GET', f'/api/internal-squads/{sq}')
            ids = [i['uuid'] for i in cur.get('inbounds', [])]
            new = [i['uuid'] for i in inbounds if i['uuid'] not in ids]
            if new:
                self.call('PATCH', '/api/internal-squads', {'uuid': sq, 'inbounds': ids + new})
                log(f'панель: inbound добавлен в сквад «{cur.get("name", sq)}»')


# ─────────────────────────────── SSH ─────────────────────────────────

class Remote:
    def __init__(self, node: dict, logfile):
        self.n = node
        self.logfile = logfile

    def argv(self, cmd, tty):
        n = self.n
        a = ['-tt' if tty else '-T',
             '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null', '-o', 'LogLevel=ERROR',
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
        return child.exitstatus, ''.join(out)


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
            if 'не принял пароль' in last:
                break
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
    if prof.get('ssh_pubkey'):
        key = prof['ssh_pubkey'].strip().replace("'", '')
        r.run("umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; "
              f"grep -qxF '{key}' ~/.ssh/authorized_keys || echo '{key}' >> ~/.ssh/authorized_keys")
        log('SSH-ключ добавлен в authorized_keys')
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

    psi = None
    if node.get('psiphon'):
        stage('psiphon')
        psi = {'out': install_psiphon(r, node['psiphon'], log), 'domains': psiphon_rules(node['psiphon'].get('domains'))}
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
        panel.register(node, profile, squads, int(prof.get('node_port') or 2222), log, psi)

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
    if prof.get('ssh_pubkey'):
        key = prof['ssh_pubkey'].strip().replace("'", '')
        r.run("umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; "
              f"grep -qxF '{key}' ~/.ssh/authorized_keys || echo '{key}' >> ~/.ssh/authorized_keys")
        log('SSH-ключ добавлен в authorized_keys')

    stage('install')
    existing = panel.node_state(node['ip'])
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

    psi = None
    if node.get('psiphon'):
        stage('psiphon')
        psi = {'out': install_psiphon(r, node['psiphon'], log), 'domains': psiphon_rules(node['psiphon'].get('domains'))}

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
            if psi:
                cfg = add_psiphon(cfg, psi['out'], psi['domains'])
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
            if psi:  # чужой профиль бот не меняет — только предупреждает
                have = psiphon_of(pr.get('config'))
                if have is None:
                    log(f'⚠️ в профиле «{pr["name"]}» нет выхода {PSI_TAG}: Psiphon поставлен, но трафик в него не идёт — '
                        f'добавьте outbound socks {psi["out"]["address"]}:{psi["out"]["port"]} в профиль')
                elif have != psi['out']:
                    log(f'⚠️ профиль ведёт {PSI_TAG} на {have["address"]}:{have["port"]}, '
                        f'а Psiphon на ноде слушает {psi["out"]["address"]}:{psi["out"]["port"]}')
            panel.attach(node, pr, inbounds, squads, int(values['node_port']), log, tls=False)

    stage('check')
    if rec.get('check_command'):
        rc, out = r.run(render(rec['check_command'], values), 120)
        if rc != 0:
            raise StepError(f'проверка из рецепта не прошла (код {rc}): {strip_ansi(out).strip()[-200:]}')
    for attempt in range(check_tries):
        st = panel.node_state(node['ip']) or {}
        if st.get('isConnected'):
            log('нода подключилась к панели ✓')
            return 'готово'
        log(f'жду подключения ноды к панели ({attempt + 1}/{check_tries})' +
            (f': {st["lastStatusMessage"]}' if st.get('lastStatusMessage') else ''))
        time.sleep(check_every)
    raise StepError('нода установлена, но к панели так и не подключилась — проверьте порт API и фаервол')
