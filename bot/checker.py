"""Чекеры доступности: xray-checker (kutovoys) на серверах в России, которыми управляет бот.

Бот сам ставит чекер по SSH и держит в нём подписки всех групп с мониторингом. Каждая подписка помечается
именем группы (`ссылка#bnodes-g<id>`) — xray-checker отдаёт его в поле subName, так видно, чьи это ноды.
API чекера открыт только на 127.0.0.1 сервера: бот читает его по SSH, наружу ничего не публикуется."""
from __future__ import annotations

import base64
import io
import json
import re
import secrets
import shlex

from . import engine
from .engine import StepError

DIR = '/opt/bnodes-checker'
IMAGE = 'kutovoys/xray-checker:latest'
MIRROR = 'mirror.gcr.io/kutovoys/xray-checker:latest'  # Docker Hub из России бывает недоступен
PORT = 2112
USER = 'bnodes'
INTERVAL = 300  # секунд между проверками — как у xray-checker по умолчанию
SUB_PREFIX = 'bnodes-g'


def sub_name(pid: int) -> str:
    return f'{SUB_PREFIX}{int(pid)}'


def pid_of(name: str) -> int | None:
    m = re.fullmatch(SUB_PREFIX + r'(\d+)', name or '')
    return int(m.group(1)) if m else None


def check_sub_url(v: str) -> str:
    v = (v or '').strip()
    if v in ('', '-'):
        return ''
    if not re.fullmatch(r'https?://[^\s,#"\'\\]+', v):
        raise ValueError('нужна ссылка подписки вида https://sub.example.com/AbC123 (без пробелов, запятых и #)')
    return v


def subs_mark(subs: list[tuple[int, str]]) -> str:
    """Отпечаток набора подписок: по нему видно, что на чекере записан не текущий набор."""
    import hashlib
    return hashlib.sha256(json.dumps(sorted(subs)).encode()).hexdigest()[:16]


def env_text(subs: list[tuple[int, str]], password: str, interval: int = INTERVAL) -> str:
    """.env xray-checker: подписки групп через запятую, у каждой — имя группы после #."""
    urls = ','.join(f'{url}#{sub_name(pid)}' for pid, url in subs)
    lines = {
        'SUBSCRIPTION_URL': urls,
        'SUBSCRIPTION_JSON_FORMAT': 'true',  # Remnawave отдаёт JSON: ноды за балансировщиками проверяются по отдельности
        'SUBSCRIPTION_UPDATE': 'true',
        'SUBSCRIPTION_UPDATE_INTERVAL': '300',
        'PROXY_CHECK_INTERVAL': str(interval),
        'PROXY_CHECK_METHOD': 'ip',
        'PROXY_TIMEOUT': '20',
        'METRICS_PROTECTED': 'true',
        'METRICS_USERNAME': USER,
        'METRICS_PASSWORD': password,
        'LOG_LEVEL': 'warn',
    }
    return ''.join(f'{k}="{v}"\n' for k, v in lines.items())


COMPOSE = f"""services:
  checker:
    image: ${{CHECKER_IMAGE:-{IMAGE}}}
    container_name: bnodes-checker
    restart: unless-stopped
    env_file: .env
    ports:
      - "127.0.0.1:{PORT}:{PORT}"
    logging:
      driver: json-file
      options: {{max-size: "10m", max-file: "3"}}
"""


def remote(ch: dict, keypath: str, known_hosts: str, logfile=None) -> engine.Remote:
    from . import sshaccess
    n = {'ip': ch['ip'], **sshaccess.to_node(ch, keypath), 'known_hosts': known_hosts}
    return engine.Remote(n, logfile if logfile is not None else io.StringIO())


def _put(r: engine.Remote, path: str, text: str, mode: str = '600') -> None:
    b64 = base64.b64encode(text.encode()).decode()
    rc, out = r.run(f'umask 077; echo {b64} | base64 -d > {path}.new && chmod {mode} {path}.new && mv {path}.new {path} && echo PUT_OK')
    if 'PUT_OK' not in out:
        raise StepError(f'не удалось записать {path}: {engine.strip_ansi(out).strip()[-200:]}')


def install(ch: dict, subs: list[tuple[int, str]], keypath: str, pubkey: str, known_hosts: str, log, logfile) -> dict:
    """Docker, чекер и подписки групп. Возвращает поля чекера для сохранения (пароль API, вход по ключу бота)."""
    r = remote(ch, keypath, known_hosts, logfile)
    engine.wait_ssh(r, log, timeout=120)
    if pubkey:
        engine.add_keys(r, pubkey, log)
    rc, out = r.run('. /etc/os-release 2>/dev/null; echo "OS=$ID"; uname -m')
    if not re.search(r'OS=(ubuntu|debian)', out):
        raise StepError('чекеру нужна Ubuntu или Debian')
    rc, out = r.run('command -v docker >/dev/null && docker compose version >/dev/null 2>&1 && echo HAVE_DOCKER')
    if 'HAVE_DOCKER' not in out:
        log('ставлю Docker (сначала из репозитория системы, затем get.docker.com)')
        rc, out = r.run('export DEBIAN_FRONTEND=noninteractive; apt-get update -qq >/dev/null 2>&1; '
                        '(apt-get install -y -qq docker.io docker-compose-v2 >/dev/null 2>&1 || '
                        ' apt-get install -y -qq docker.io docker-compose-plugin >/dev/null 2>&1 || '
                        ' curl -fsSL https://get.docker.com | sh >/dev/null 2>&1); '
                        'systemctl enable --now docker >/dev/null 2>&1; '
                        'docker compose version >/dev/null 2>&1 && echo DOCKER_OK', 900)
        if 'DOCKER_OK' not in out:
            raise StepError('не удалось поставить Docker с Compose — поставьте его на сервере вручную и повторите')
    log('Docker есть')
    password = ch.get('api_password') or secrets.token_urlsafe(24)
    r.run(f'mkdir -p {DIR} && chmod 700 {DIR}')
    _put(r, f'{DIR}/docker-compose.yml', COMPOSE, '644')
    image = IMAGE
    rc, out = r.run(f'cd {DIR} && docker pull -q {IMAGE} >/dev/null 2>&1 && echo PULL_OK', 600)
    if 'PULL_OK' not in out:
        log('Docker Hub недоступен — беру образ через зеркало mirror.gcr.io')
        rc, out = r.run(f'docker pull -q {MIRROR} >/dev/null 2>&1 && echo PULL_OK', 600)
        if 'PULL_OK' not in out:
            raise StepError('не удалось скачать образ xray-checker ни из Docker Hub, ни из mirror.gcr.io')
        image = MIRROR
    log(f'образ {image}')
    fields = {'api_password': password, 'image': image}
    apply_subs({**ch, **fields}, subs, keypath, known_hosts, log, logfile, r=r)
    # root по паролю — дальше по ключу бота, пароль root больше не храним. Свой ключ или не-root (sudo) — как задано
    if keypath and pubkey and (ch.get('user') or 'root') == 'root' and (ch.get('key') or '') == '':
        try:
            rk = remote({**ch, 'key': 'bot', 'password': ''}, keypath, known_hosts, logfile)
            rc, out = rk.run('id -u', 30)
            if rc == 0 and out.strip().splitlines()[-1:] == ['0']:
                fields.update(key='bot', password='')
                log('вход на чекер — по ключу бота, пароль root не сохраняется')
        except StepError as e:
            log(f'вход по ключу не прошёл ({e}) — остаётся пароль')
    return fields


def apply_subs(ch: dict, subs: list[tuple[int, str]], keypath: str, known_hosts: str, log, logfile=None,
               r: engine.Remote | None = None) -> str:
    """Записать подписки групп и перезапустить чекер. Без подписок чекер остановлен (ему нужна хотя бы одна)."""
    r = r or remote(ch, keypath, known_hosts, logfile)
    image = ch.get('image') or IMAGE
    if not subs:
        r.run(f'cd {DIR} 2>/dev/null && docker compose down >/dev/null 2>&1; true')
        log('подписок для проверки нет — чекер остановлен')
        return 'idle'
    _put(r, f'{DIR}/.env', env_text(subs, ch['api_password']))
    rc, out = r.run(f'cd {DIR} && CHECKER_IMAGE={shlex.quote(image)} docker compose up -d --force-recreate >/dev/null 2>&1 && echo UP_OK', 300)
    if 'UP_OK' not in out:
        raise StepError('чекер не запустился: ' + engine.strip_ansi(out).strip()[-200:])
    log(f'чекер запущен, подписок: {len(subs)}')
    return 'running'


def poll(ch: dict, keypath: str, known_hosts: str) -> list[dict]:
    """Последние результаты проверок: [{subName, server, port, name, online, latencyMs, lastCheck}]."""
    r = remote(ch, keypath, known_hosts)
    auth = shlex.quote(f'{USER}:{ch["api_password"]}')
    rc, out = r.run(f'curl -fsS -m 20 -u {auth} http://127.0.0.1:{PORT}/api/v1/proxies', 60)
    text = engine.strip_ansi(out)
    start = text.find('{')
    if start < 0:
        raise StepError('чекер не ответил' + (': ' + text.strip()[-160:] if text.strip() else ''))
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        raise StepError('чекер ответил не JSON') from None
    if not data.get('success'):
        raise StepError('чекер: ' + str(data.get('error') or 'ошибка'))
    keep = ('subName', 'server', 'port', 'name', 'online', 'latencyMs', 'lastCheck')
    return [{k: p.get(k) for k in keep} for p in data.get('data') or []]
