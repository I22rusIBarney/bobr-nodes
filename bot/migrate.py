"""Переезд бота на новый VPS со всеми настройками и базой.

Как это работает (всё делает сам бот, изнутри контейнера, по SSH):
1. Проверка нового сервера: вход root, Ubuntu/Debian, свободные порты 80/443, память и диск.
2. Подготовка, пока старый бот работает: ключ бота в authorized_keys, Docker, код той же версии, готовый образ.
3. Переключение: старый бот перестаёт принимать сообщения и останавливает фоновые проверки; база копируется
   в этот момент (SQLite backup — целостная копия), вместе с ключами, журналами и настройками; .env собирается
   из текущих значений. На новом сервере всё распаковывается, A-запись домена в Cloudflare переводится на
   новый IP, бот запускается (`install.sh restore`) и проверяется.
4. Получилось — старый сервер помечается «бот переехал» и больше не запускает бота (данные на нём остаются
   для отката). Не получилось — DNS возвращается, новый бот останавливается, старый запускается как был.

Пароль нового сервера живёт только в памяти этого процесса и в базу не пишется.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import sqlite3
import subprocess
import tarfile
import time
from pathlib import Path

from . import engine

log = logging.getLogger(__name__)

KEY = 'migrate'
DEST = '/opt/bobr-nodes'
REPO_DEFAULT = 'https://github.com/I22rusIBarney/bobr-nodes.git'
ENV_KEYS = ('BOT_TOKEN', 'ADMIN_IDS', 'DOMAIN', 'PUBLIC_URL', 'BCLOUD_URL', 'BCLOUD_TOKEN', 'BCLOUD_TG_ID', 'CF_TOKEN',
            'GEMINI_KEY', 'WEB_HOST', 'WEB_PORT')
MOVED = 'moved.json'        # на старом сервере: бот переехал — не запускаться
ARRIVED = 'migrated.json'   # на новом: что написать администраторам после первого запуска
SKIP = {'bot.db', 'bot.db-wal', 'bot.db-shm', 'migrate', MOVED, 'version.txt', 'origin.txt', '.hardened'}

HOOKS: dict = {}   # main.py: stop — остановить приём сообщений и фоновые проверки, exit — завершить процесс

STAGES = [('check', 'Проверка сервера'), ('prepare', 'Подготовка: Docker и код'), ('stop', 'Остановка старого бота'),
          ('backup', 'Копия базы и настроек'), ('upload', 'Перенос на новый сервер'), ('dns', 'DNS домена'),
          ('start', 'Запуск на новом сервере'), ('done', 'Готово')]

CHECK_CMD = r'''
. /etc/os-release 2>/dev/null; echo "OS=$ID"; echo "VER=$VERSION_ID"; echo "ARCH=$(uname -m)"
echo "MEM=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)"
echo "DISK=$(df -Pm / | awk 'NR==2{print int($4/1024)}')"
for p in 80 443; do ss -Hltn "( sport = :$p )" 2>/dev/null | grep -q . && echo "BUSY=$p"; done
command -v docker >/dev/null && echo DOCKER=1
[ -e /opt/bobr-nodes/.env ] && echo EXISTS=1
echo "IP=$(curl -fsS -4 -m 8 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
echo CHECK_OK
'''


class MigrateError(Exception):
    pass


def parse_kv(out: str) -> dict:
    d: dict = {}
    for line in engine.strip_ansi(out).splitlines():
        m = re.match(r'^([A-Z]+)=(.*)$', line.strip())
        if m:
            k, v = m.groups()
            d.setdefault(k, [])
            d[k].append(v.strip())
    return {k: v if k == 'BUSY' else v[-1] for k, v in d.items()}


def assess(facts: dict) -> tuple[list[str], list[str]]:
    """(что мешает, предупреждения) по фактам о сервере."""
    bad, warn = [], []
    if facts.get('OS') not in ('ubuntu', 'debian'):
        bad.append(f'нужен Ubuntu или Debian, а здесь {facts.get("OS") or "неизвестная система"}')
    if facts.get('BUSY'):
        bad.append('заняты порты ' + ', '.join(facts['BUSY']) + ' — нужен чистый сервер (там будет HTTPS бота)')
    if facts.get('EXISTS'):
        bad.append('в /opt/bobr-nodes уже есть установка бота')
    mem, disk = int(facts.get('MEM') or 0), int(facts.get('DISK') or 0)
    if mem and mem < 900:
        warn.append(f'памяти {mem} МБ — боту лучше от 1 ГБ')
    if disk and disk < 5:
        bad.append(f'свободно {disk} ГБ на диске — нужно хотя бы 5 ГБ')
    if facts.get('ARCH') not in ('x86_64', 'aarch64'):
        warn.append(f'архитектура {facts.get("ARCH")}: готового образа может не быть, бот соберётся на месте (дольше)')
    return bad, warn


def env_text(env: dict, bcloud_url: str | None = None) -> str:
    """.env нового сервера из текущих значений. Свой HTTPS (Caddy) — на новом сервере нет bcloud."""
    vals = {k: env.get(k, '') for k in ENV_KEYS if env.get(k) not in (None,)}
    if not vals.get('DOMAIN') and vals.get('PUBLIC_URL'):
        vals['DOMAIN'] = re.sub(r'^https?://', '', vals['PUBLIC_URL']).split('/')[0]
    if bcloud_url:
        vals['BCLOUD_URL'] = bcloud_url
    vals['COMPOSE_PROFILES'] = 'caddy'
    vals['BNODES_IMAGE'] = ''
    out = []
    for k, v in vals.items():
        if v == '' and k not in ('BCLOUD_URL', 'BCLOUD_TOKEN', 'BCLOUD_TG_ID', 'COMPOSE_PROFILES', 'BNODES_IMAGE'):
            continue
        if "'" in str(v) or '\n' in str(v):
            raise MigrateError(f'значение {k} содержит кавычку или перенос строки — перенесите .env вручную')
        out.append(f"{k}='{v}'")
    return '\n'.join(out) + '\n'


def internal_bcloud(url: str) -> bool:
    """bcloud по адресу внутри docker-сети (бот стоит на сервере bcloud) — с нового сервера туда не достучаться."""
    host = re.sub(r'^https?://', '', url or '').split('/')[0].split(':')[0]
    return bool(host) and ('.' not in host or host in ('localhost', '127.0.0.1'))


def repo_info(data_dir: Path) -> tuple[str, str, str]:
    """(URL репозитория, коммит, образ ghcr) текущей установки — их записывает bnodes при каждом запуске."""
    url = REPO_DEFAULT
    try:
        url = (data_dir / 'origin.txt').read_text().strip() or url
    except OSError:
        pass
    sha = ''
    try:
        sha = (data_dir / 'version.txt').read_text().split('\t', 1)[0].strip()
    except OSError:
        pass
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        sha = ''
    slug = re.sub(r'(\.git)?/*$', '', re.sub(r'^.*github\.com[:/]', '', url)).lower()
    return url, sha, (f'ghcr.io/{slug}:sha-{sha}' if sha and slug else '')


def backup_db(src: Path, dst: Path, bcloud_url: str | None = None) -> None:
    """Целостная копия SQLite (как sqlite3 .backup) и поправки для нового сервера."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    s, d = sqlite3.connect(str(src)), sqlite3.connect(str(dst))
    try:
        s.backup(d)
        if bcloud_url:
            d.execute("INSERT INTO settings(key, value) VALUES('bcloud_url', ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (bcloud_url,))
        d.execute("DELETE FROM settings WHERE key IN ('migrate')")
        d.commit()
    finally:
        s.close()
        d.close()


def pack(data_dir: Path, db_copy: Path, out: Path, note: dict) -> int:
    """Архив data/: всё, кроме живой базы (вместо неё — копия), служебных файлов переезда и версии."""
    with tarfile.open(out, 'w:gz') as tar:
        for p in sorted(data_dir.iterdir()):
            if p.name in SKIP:
                continue
            tar.add(str(p), arcname=p.name)
        tar.add(str(db_copy), arcname='bot.db')
        nf = out.parent / ARRIVED
        nf.write_text(json.dumps(note, ensure_ascii=False))
        tar.add(str(nf), arcname=ARRIVED)
    return out.stat().st_size


class Migration:
    """Один переезд за раз. Состояние — в store[KEY], чтобы экран переезда видел ход и после перезапуска."""

    def __init__(self, store, runner, data_dir: str | Path, env: dict | None = None):
        self.store, self.runner, self.data_dir = store, runner, Path(data_dir)
        self.env = env if env is not None else dict(os.environ)
        self.task: asyncio.Task | None = None
        self.target: dict | None = None   # с паролем — только в памяти

    # ───────── состояние ─────────
    def state(self) -> dict:
        try:
            st = json.loads(self.store.get(KEY) or '{}')
        except ValueError:
            st = {}
        return st if isinstance(st, dict) else {}

    def put(self, **kw) -> dict:
        st = {**self.state(), **kw}
        self.store.set(KEY, json.dumps(st, ensure_ascii=False))
        return st

    def say(self, line: str) -> None:
        st = self.state()
        lines = (st.get('log') or [])[-80:] + [f'{time.strftime("%H:%M:%S")} {line}']
        self.put(log=lines)
        log.info('migrate: %s', line)

    def stage(self, code: str) -> None:
        self.put(stage=code)
        self.say('— ' + dict(STAGES)[code])

    def view(self) -> dict:
        st = self.state()
        url, sha, img = repo_info(self.data_dir)
        busy = len(self.store.active_jobs())
        return {'status': st.get('status', 'idle'), 'stage': st.get('stage', ''), 'log': st.get('log') or [],
                'target': st.get('target') or {}, 'facts': st.get('facts') or {}, 'error': st.get('error', ''),
                'bad': st.get('bad') or [], 'warn': st.get('warn') or [], 'stages': [{'code': c, 'title': t} for c, t in STAGES],
                'domain': self.domain(), 'bcloud_internal': internal_bcloud(self.store.get('bcloud_url')),
                'cf': bool(self.store.get('cf_token')), 'busy_jobs': busy, 'version': sha[:7], 'running': self.running()}

    def running(self) -> bool:
        return bool(self.task and not self.task.done())

    def domain(self) -> str:
        return self.env.get('DOMAIN') or re.sub(r'^https?://', '', self.env.get('PUBLIC_URL', '')).split('/')[0]

    # ───────── SSH ─────────
    def node(self, t: dict, with_password: bool = True) -> dict:
        n = {'ip': t['ip'], 'ssh_user': 'root', 'ssh_port': int(t.get('port') or 22)}
        if with_password and t.get('password'):
            n['ssh_password'] = t['password']
        if t.get('key_file'):
            n['ssh_key'] = t['key_file']
        elif not with_password:
            n['ssh_key'] = self.runner.bot_key()[0]
        return n

    def remote(self, t: dict, with_password: bool = True) -> engine.Remote:
        return engine.Remote(self.node(t, with_password), None)

    def run(self, r: engine.Remote, cmd: str, ok: str, timeout: int = 300, what: str = '') -> str:
        rc, out = r.run(cmd, timeout)
        out = engine.strip_ansi(out)
        if ok not in out:
            tail = [x for x in out.strip().splitlines() if x.strip()][-3:]
            raise MigrateError(f'{what or "команда"} не выполнилась: ' + (' / '.join(tail)[-300:] if tail else f'код {rc}'))
        return out

    def push(self, t: dict, local: Path, remote_path: str) -> None:
        """Файл на новый сервер по ключу бота (ssh … 'cat > файл'), без паролей и без кусков base64."""
        r = self.remote(t, with_password=False)
        argv = ['ssh', '-o', 'BatchMode=yes', *r.argv(f'umask 077; cat > {shlex.quote(remote_path)}', False)]
        with open(local, 'rb') as f:
            p = subprocess.run(argv, stdin=f, capture_output=True, timeout=1800)
        if p.returncode != 0:
            raise MigrateError('файл не передался: ' + (p.stderr.decode('utf-8', 'replace').strip()[-200:] or f'код {p.returncode}'))

    # ───────── шаг 1: проверка ─────────
    def check(self, t: dict, fresh: bool = True) -> dict:
        """Войти и посмотреть сервер. Ничего не меняет. fresh — новая попытка с экрана (журнал с нуля)."""
        out = self.run(self.remote(t), CHECK_CMD, 'CHECK_OK', 60, 'проверка сервера')
        facts = parse_kv(out)
        bad, warn = assess(facts)
        if facts.get('IP') and facts['IP'] != t['ip']:
            warn.append(f'сервер видит себя как {facts["IP"]} — A-запись поставлю на {t["ip"]}')
        if self.store.active_jobs():
            bad.append('идут задачи бота — дождитесь, пока они закончатся')
        kw = {'log': [], 'stage': '', 'status': 'ready' if not bad else 'idle'} if fresh else {}
        return self.put(facts=facts, bad=bad, warn=warn, error='', target={'ip': t['ip'], 'port': int(t.get('port') or 22)}, **kw)

    # ───────── шаги 2–7 ─────────
    def start(self, t: dict, dns: bool, bcloud_url: str | None) -> None:
        if self.running():
            raise MigrateError('переезд уже идёт')
        self.target = t
        self.put(status='running', error='', log=[], started=time.time())
        self.task = asyncio.ensure_future(self._run(t, dns, bcloud_url))

    async def _run(self, t: dict, dns: bool, bcloud_url: str | None) -> None:
        stopped, dns_old, started = False, None, False
        try:
            self.stage('check')
            st = await asyncio.to_thread(self.check, t, False)
            if st['bad']:
                raise MigrateError('; '.join(st['bad']))
            self.put(status='running')
            url, sha, img = repo_info(self.data_dir)
            self.stage('prepare')
            await asyncio.to_thread(self.prepare, t, url, sha, img)

            self.stage('stop')
            if self.store.active_jobs():
                raise MigrateError('появились задачи бота — переезд отменён, попробуйте позже')
            if HOOKS.get('stop'):
                await HOOKS['stop']()
            stopped = True
            self.say('старый бот больше не принимает сообщения и не проверяет ноды')

            self.stage('backup')
            work = self.data_dir / 'migrate'
            work.mkdir(exist_ok=True)
            arc = work / 'data.tgz'
            note = {'from': self.env.get('PUBLIC_URL', ''), 'to': t['ip'], 'at': int(time.time())}
            size = await asyncio.to_thread(self.backup, work, arc, bcloud_url, note)
            env = env_text(self.env, bcloud_url)
            (work / 'env').write_text(env)
            self.say(f'архив данных: {size // 1024} КБ')

            self.stage('upload')
            await asyncio.to_thread(self.upload, t, work)

            if dns:
                self.stage('dns')
                dns_old = await asyncio.to_thread(self.switch_dns, t['ip'])

            self.stage('start')
            started = True
            await asyncio.to_thread(self.run, self.remote(t, False), f'cd {DEST} && ./install.sh restore 2>&1 | tail -n 20',
                                    'RESTORE_OK', 1500, 'запуск бота на новом сервере')
            (self.data_dir / MOVED).write_text(json.dumps({'to': t['ip'], 'at': int(time.time())}))
            self.stage('done')
            self.put(status='done', finished=time.time())
            self.say(f'бот работает на {t["ip"]}; этот сервер остановлен, данные на нём остались')
            if HOOKS.get('exit'):
                asyncio.get_running_loop().call_later(60, HOOKS['exit'], 0)  # дать экрану показать итог
        except Exception as e:  # noqa: BLE001 — любой сбой: откат и понятная причина
            msg = str(e) or type(e).__name__
            self.say('❌ ' + msg)
            if dns_old:
                try:
                    await asyncio.to_thread(self.restore_dns, dns_old)
                    self.say('DNS возвращён на старый сервер')
                except Exception as e2:  # noqa: BLE001
                    self.say(f'⚠️ DNS вернуть не удалось: {e2} — поправьте A-запись вручную')
            if started:
                try:
                    await asyncio.to_thread(self.remote(t, False).run, f'cd {DEST} && docker compose down', 120)
                    self.say('бот на новом сервере остановлен')
                except Exception:  # noqa: BLE001
                    pass
            self.put(status='failed', error=msg, finished=time.time())
            if stopped and HOOKS.get('exit'):
                self.say('старый бот перезапускается и продолжает работу как раньше')
                asyncio.get_running_loop().call_later(3, HOOKS['exit'], 1)
        finally:
            self.target = None

    def prepare(self, t: dict, url: str, sha: str, img: str) -> None:
        pub = self.runner.bot_key()[1]
        if not pub:
            raise MigrateError('у бота нет SSH-ключа (ssh-keygen недоступен)')
        self.run(self.remote(t), 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && '
                 f'(grep -qxF {shlex.quote(pub)} ~/.ssh/authorized_keys || echo {shlex.quote(pub)} >> ~/.ssh/authorized_keys) && '
                 'chmod 600 ~/.ssh/authorized_keys && echo KEY_OK', 'KEY_OK', 60, 'ключ бота')
        self.say('ключ бота добавлен — дальше без пароля')
        r = self.remote(t, False)
        self.run(r, 'export DEBIAN_FRONTEND=noninteractive; '
                 '(command -v git >/dev/null && command -v curl >/dev/null) || (apt-get update -qq && apt-get install -y -qq git curl ca-certificates >/dev/null); '
                 'command -v docker >/dev/null || (curl -fsSL https://get.docker.com | sh >/dev/null 2>&1); '
                 'docker compose version >/dev/null 2>&1 || apt-get install -y -qq docker-compose-plugin >/dev/null; '
                 'docker compose version >/dev/null && echo DOCKER_OK', 'DOCKER_OK', 900, 'установка Docker')
        self.say('Docker готов')
        ref = sha or 'origin/main'
        self.run(r, f'[ -d {DEST}/.git ] || git clone -q {shlex.quote(url)} {DEST}; cd {DEST} && git fetch -q origin && '
                 f'git checkout -q {shlex.quote(ref)} && echo CODE_OK', 'CODE_OK', 300, 'загрузка кода')
        self.say(f'код версии {sha[:7] or "main"} на месте')
        if img:
            rc, _ = r.run(f'docker pull -q {shlex.quote(img)} >/dev/null 2>&1 && echo PULL_OK', 900)
            self.say('готовый образ скачан' if rc == 0 else 'готового образа нет — соберётся при запуске (дольше)')

    def backup(self, work: Path, arc: Path, bcloud_url: str | None, note: dict) -> int:
        db = work / 'bot.db'
        backup_db(self.data_dir / 'bot.db', db, bcloud_url)
        return pack(self.data_dir, db, arc, note)

    def upload(self, t: dict, work: Path) -> None:
        self.push(t, work / 'data.tgz', '/root/bn-data.tgz')
        self.push(t, work / 'env', '/root/bn-env')
        dom = self.domain()
        caddy = f'{dom} {{\n\treverse_proxy nodes:8080\n}}\n'
        self.run(self.remote(t, False),
                 f'cd {DEST} && mkdir -p data && chmod 700 data && tar -xzf /root/bn-data.tgz -C data && '
                 'mv /root/bn-env .env && chmod 600 .env && rm -f /root/bn-data.tgz docker-compose.override.yml && '
                 f'printf %s {shlex.quote(caddy)} > Caddyfile && echo EXTRACT_OK', 'EXTRACT_OK', 300, 'распаковка')
        self.say('база, ключи и настройки на новом сервере')

    def switch_dns(self, ip: str) -> dict | None:
        dom, tok = self.domain(), self.store.get('cf_token')
        if not dom or not tok:
            self.say('Cloudflare не подключён — поменяйте A-запись домена на новый IP вручную')
            return None
        cf = engine.Cloudflare(tok)
        try:
            recs = [r for r in cf.records(dom) if r['type'] == 'A']
        except engine.StepError as e:
            self.say(f'домен {dom} не в вашем Cloudflare ({e}) — поменяйте A-запись вручную')
            return None
        old = {'domain': dom, 'ip': recs[0]['content'] if recs else '', 'proxied': bool(recs and recs[0].get('proxied'))}
        cf.upsert_a(dom, ip, self.say, proxied=old['proxied'])
        return old

    def restore_dns(self, old: dict) -> None:
        if old.get('ip'):
            engine.Cloudflare(self.store.get('cf_token')).upsert_a(old['domain'], old['ip'], self.say, proxied=old['proxied'])


def moved(data_dir: str | Path) -> dict | None:
    """Бот на этом сервере уже переехал — не запускать его здесь."""
    try:
        return json.loads((Path(data_dir) / MOVED).read_text())
    except (OSError, ValueError):
        return None


def arrived_note(data_dir: str | Path) -> str | None:
    """Первый запуск после переезда: сообщение администраторам (один раз)."""
    path = Path(data_dir) / ARRIVED
    try:
        n = json.loads(path.read_text())
        path.unlink()
    except (OSError, ValueError):
        return None
    import html
    return (f'🚚 <b>Бот переехал на новый сервер</b> <code>{html.escape(str(n.get("to", "")))}</code>\n\n'
            'База, настройки, ключи и журналы перенесены, задачи и проверки продолжаются. Старый сервер остановлен, '
            'данные на нём остались — удалите его, когда убедитесь, что всё работает.\n\n'
            'Защитите новый сервер: <code>bnodes harden</code>.')
