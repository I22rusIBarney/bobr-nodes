"""Движок установки одной ноды: Cloudflare DNS → SSH → Nuvrion Vision Installer → панель Remnawave → проверка.

Синхронный (pexpect + urllib), бот запускает его в отдельном потоке. Общается с внешним миром через два колбэка:
  log(text)   — строка в журнал задачи;
  stage(name) — начался этап: dns | ssh | install | panel | check.

Оригинальный установщик не изменяется: он скачивается по закреплённому коммиту, сверяется по SHA-256,
а ответы на его вопросы подставляются автоматически. Неизвестный вопрос — остановка, а не угадывание.
"""
from __future__ import annotations

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
INBOUND_TAG = 'Vision-TLS'
YN = r'[^\n]*\[Д/Н; Enter — Н\]'

# Регистрация в панели идёт по очереди: сквады обновляются целым списком, параллельные записи потеряли бы inbound.
PANEL_LOCK = threading.Lock()


class StepError(Exception):
    """Ошибка этапа с понятным текстом."""


def strip_ansi(s: str | None) -> str:
    return re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', s or '')


# ─────────────────────────────── HTTP ────────────────────────────────

def http(method, url, headers=None, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {'raw': raw[:500].decode('utf-8', 'replace')}
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
        if code >= 300:
            hint = ''
            if code in (403, 404, 444) or 'raw' in data:
                hint = ' (проверьте cookie-защиту и адрес панели)'
            if code == 401:
                hint = ' (неверный API-токен)'
            raise StepError(f'панель {method} {path}: HTTP {code}{hint}: {str(data)[:200]}')
        return data.get('response', data)

    def secret_key(self) -> str:
        return self.call('GET', '/api/keygen')['secretKey']

    def check(self) -> str:
        """Проверка доступа: ключ + список профилей. Возвращает короткое описание."""
        self.secret_key()
        total = self.call('GET', '/api/config-profiles')['total']
        nodes = len(self.call('GET', '/api/nodes'))
        return f'профилей: {total}, нод: {nodes}'

    def register(self, node: dict, profile_config: dict, squads: list[str], node_port: int, log):
        pname = re.sub(r'[^A-Za-z0-9_ -]', '-', f'NV {node["name"]}')[:30]
        profiles = self.call('GET', '/api/config-profiles')['configProfiles']
        prof = next((p for p in profiles if p['name'] == pname), None)
        if prof:
            prof = self.call('PATCH', '/api/config-profiles', {'uuid': prof['uuid'], 'config': profile_config})
            log(f'панель: профиль «{pname}» обновлён')
        else:
            prof = self.call('POST', '/api/config-profiles', {'name': pname, 'config': profile_config})
            log(f'панель: профиль «{pname}» создан')
        inbound = next((i for i in prof['inbounds'] if i['tag'] == INBOUND_TAG), None)
        if not inbound:
            raise StepError(f'в профиле нет inbound {INBOUND_TAG}')
        cp = {'activeConfigProfileUuid': prof['uuid'], 'activeInbounds': [inbound['uuid']]}

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

        hosts = self.call('GET', '/api/hosts')
        host = next((h for h in hosts if h['address'] == node['domain']
                     and h['inbound']['configProfileInboundUuid'] == inbound['uuid']), None)
        if host:
            log('панель: хост уже есть')
        else:
            self.call('POST', '/api/hosts', {
                'inbound': {'configProfileUuid': prof['uuid'], 'configProfileInboundUuid': inbound['uuid']},
                'remark': (node.get('remark') or node['name'])[:100],
                'address': node['domain'], 'port': 443, 'sni': node['domain'],
                'alpn': 'h2,http/1.1', 'fingerprint': 'firefox', 'securityLayer': 'TLS'})
            log('панель: хост создан')

        for sq in squads:
            cur = self.call('GET', f'/api/internal-squads/{sq}')
            ids = [i['uuid'] for i in cur.get('inbounds', [])]
            if inbound['uuid'] not in ids:
                self.call('PATCH', '/api/internal-squads', {'uuid': sq, 'inbounds': ids + [inbound['uuid']]})
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

    if panel is None:
        return 'установлено, панель пропущена'
    stage('panel')
    rc, raw = r.run('cat /opt/remnanode/vision-config-profile.json')
    if rc != 0 or '{' not in raw:
        raise StepError('не удалось прочитать vision-config-profile.json')
    profile = json.loads(raw[raw.index('{'):])
    squads = [x for x in re.split(r'[\s,]+', prof.get('squads') or '') if x] \
        if isinstance(prof.get('squads'), str) else list(prof.get('squads') or [])
    with PANEL_LOCK:
        panel.register(node, profile, squads, int(prof.get('node_port') or 2222), log)

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
