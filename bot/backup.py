"""Резервные копии: раз в сутки зашифрованный архив базы и настроек — администраторам в Telegram.

Что внутри: целостная копия базы (группы, пользователи, подписки, задачи, ключи нод), SSH-ключи и остальные
файлы data/ (кроме журналов задач), значения .env. Шифрование — AES-256-GCM, ключ из пароля восстановления
через scrypt. Пароль бот создаёт сам и показывает администратору в «Резервных копиях»: без него копию
не открыть, поэтому в Telegram он не отправляется.

Восстановление: поставить бота на новый сервер (install.sh), отправить ему файл копии и пароль — бот проверит
копию, покажет, что в ней, и после подтверждения перезапустится уже с ней. Прежние данные не удаляются:
они остаются в data/before-restore-<время>/.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import html
import io
import json
import logging
import os
import secrets
import shutil
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

KEY, SECRET = 'backup', 'backup_key'
MAGIC = b'BNB1'
EXT = '.bnb'
LOCAL = 'backups'            # data/backups — последние копии на самом сервере (на случай «случайно удалил»)
STAGE = 'restore'            # data/restore — распакованная копия ждёт перезапуска
READY = 'READY'
RESTORED = 'restored.json'   # после восстановления: что написать администраторам
TG_LIMIT = 48 * 1024 * 1024  # Telegram принимает от ботов файлы до 50 МБ
ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # без 0/O и 1/I — пароль переписывают руками
SKIP = {'bot.db', 'bot.db-wal', 'bot.db-shm', 'logs', LOCAL, STAGE, 'migrate', 'moved.json', 'version.txt', 'origin.txt',
        'update-now', 'update-note.txt', RESTORED, '.hardened', 'restore-upload.bnb'}
KEEP_DEF = 7


class BackupError(Exception):
    pass


class BadKey(BackupError):
    pass


# ───────── настройки ─────────
def cfg(store) -> dict:
    try:
        c = json.loads(store.get(KEY) or '{}')
    except ValueError:
        c = {}
    return {'on': c.get('on', True) is not False, 'hour': int(c.get('hour', 5)) % 24, 'tz': int(c.get('tz', 0)),
            'keep': int(c.get('keep', KEEP_DEF)), 'key_saved': bool(c.get('key_saved')), 'last': c.get('last', ''),
            'last_at': c.get('last_at'), 'last_size': c.get('last_size'), 'last_error': c.get('last_error', ''),
            'last_file': c.get('last_file', '')}


def save_cfg(store, patch: dict) -> dict:
    cur = cfg(store)
    for k, v in patch.items():
        if k == 'on' or k == 'key_saved':
            cur[k] = bool(v)
        elif k == 'hour':
            if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 23:
                raise ValueError('Час: от 0 до 23')
            cur[k] = v
        elif k == 'tz':
            if not isinstance(v, int) or isinstance(v, bool) or not -14 * 60 <= v <= 14 * 60:
                raise ValueError('Неверный часовой пояс')
            cur[k] = v
        elif k == 'keep':
            if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 30:
                raise ValueError('Хранить: от 1 до 30 копий')
            cur[k] = v
        elif k in ('last', 'last_at', 'last_size', 'last_error', 'last_file'):
            cur[k] = v
    store.set(KEY, json.dumps(cur, ensure_ascii=False))
    return cur


def due(c: dict, now: float | None = None) -> str | None:
    """Пора делать копию: включено, наступил выбранный час (или позже — бот мог быть выключен), сегодня ещё не делал."""
    if not c['on']:
        return None
    t = dt.datetime.fromtimestamp(now if now is not None else time.time(), dt.timezone.utc) + dt.timedelta(minutes=c['tz'])
    day = t.strftime('%Y-%m-%d')
    if t.hour < c['hour'] or c['last'] == day:
        return None
    return day


# ───────── пароль и шифрование ─────────
def new_key() -> str:
    raw = ''.join(secrets.choice(ALPHABET) for _ in range(24))  # 120 бит
    return '-'.join(raw[i:i + 4] for i in range(0, 24, 4))


def norm(key: str) -> str:
    return ''.join(ch for ch in (key or '').upper() if ch.isalnum())


def key_of(store, create: bool = True) -> str:
    k = store.get(SECRET) or ''
    if not k and create:
        k = new_key()
        store.set(SECRET, k)
    return k


def _derive(key: str, salt: bytes) -> bytes:
    return hashlib.scrypt(norm(key).encode(), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)


def encrypt(data: bytes, key: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = os.urandom(16), os.urandom(12)
    return MAGIC + salt + nonce + AESGCM(_derive(key, salt)).encrypt(nonce, data, MAGIC)


def decrypt(blob: bytes, key: str) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not blob.startswith(MAGIC) or len(blob) < 4 + 16 + 12 + 16:
        raise BackupError('Это не резервная копия BobrNet')
    salt, nonce, body = blob[4:20], blob[20:32], blob[32:]
    try:
        return AESGCM(_derive(key, salt)).decrypt(nonce, body, MAGIC)
    except InvalidTag:
        raise BadKey('Пароль не подходит к этой копии (или файл повреждён)') from None


# ───────── архив ─────────
def snapshot_db(src: Path, dst: Path) -> None:
    """Целостная копия SQLite, даже если бот сейчас пишет в базу."""
    s, d = sqlite3.connect(str(src)), sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        s.close()
        d.close()


def counts(db: Path) -> dict:
    c = sqlite3.connect(str(db))
    try:
        out = {}
        for t, k in (('profiles', 'groups'), ('users', 'users'), ('jobs', 'jobs'), ('scripts', 'scripts')):
            try:
                out[k] = c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
            except sqlite3.Error:
                out[k] = 0
        return out
    finally:
        c.close()


def env_lines(env: dict) -> str:
    from .migrate import ENV_KEYS
    return ''.join(f"{k}='{env[k]}'\n" for k in ENV_KEYS if env.get(k))


def build(data_dir: str | Path, env: dict, version: str = '', now: float | None = None) -> tuple[bytes, dict]:
    """Архив tar.gz (ещё не зашифрованный) и что в нём."""
    data_dir = Path(data_dir)
    now = now or time.time()
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / 'bot.db'
        snapshot_db(data_dir / 'bot.db', db)
        man = {'app': 'bobrnet', 'format': 1, 'at': int(now), 'version': version, **counts(db)}
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as tar:
            tar.add(str(db), arcname='bot.db')
            for p in sorted(data_dir.iterdir()):
                if p.name in SKIP or p.name.startswith('before-restore-'):
                    continue
                tar.add(str(p), arcname=p.name)
            for name, text in (('env.txt', env_lines(env)), ('backup.json', json.dumps(man, ensure_ascii=False))):
                b = text.encode()
                ti = tarfile.TarInfo(name)
                ti.size, ti.mtime, ti.mode = len(b), int(now), 0o600
                tar.addfile(ti, io.BytesIO(b))
    return buf.getvalue(), man


def _members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Только обычные файлы и папки внутри архива — без абсолютных путей, «..» и ссылок."""
    out = []
    for m in tar.getmembers():
        parts = Path(m.name).parts
        if m.name.startswith('/') or '..' in parts or not (m.isfile() or m.isdir()):
            raise BackupError(f'в копии подозрительный файл: {m.name[:80]}')
        out.append(m)
    return out


def inspect(plain: bytes) -> dict:
    """Проверить расшифрованный архив: есть база, она цела. Возвращает описание копии."""
    try:
        tar = tarfile.open(fileobj=io.BytesIO(plain), mode='r:gz')
    except tarfile.TarError:
        raise BackupError('Архив повреждён') from None
    with tar:
        members = {m.name: m for m in _members(tar)}
        if 'bot.db' not in members:
            raise BackupError('В копии нет базы бота')
        try:
            man = json.loads(tar.extractfile(members['backup.json']).read()) if 'backup.json' in members else {}
        except (ValueError, KeyError, AttributeError):
            man = {}
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'bot.db'
            db.write_bytes(tar.extractfile(members['bot.db']).read())
            c = sqlite3.connect(str(db))
            try:
                ok = c.execute('PRAGMA integrity_check').fetchone()[0]
            except sqlite3.Error as e:
                raise BackupError(f'База в копии не открывается: {e}') from None
            finally:
                c.close()
            if ok != 'ok':
                raise BackupError('База в копии повреждена')
            man = {**counts(db), **{k: v for k, v in man.items() if k not in ('groups', 'users', 'jobs', 'scripts')}}
        man['files'] = len([m for m in members.values() if m.isfile()])
    return man


def stage(plain: bytes, data_dir: str | Path) -> dict:
    """Распаковать проверенную копию в data/restore — её подставит следующий запуск бота (apply_pending)."""
    man = inspect(plain)
    data_dir = Path(data_dir)
    dst = data_dir / STAGE
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(plain), mode='r:gz') as tar:
        for m in _members(tar):
            target = dst / m.name
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(m).read())
            os.chmod(target, 0o600)
    (dst / READY).write_text(json.dumps(man, ensure_ascii=False))
    return man


def apply_pending(data_dir: str | Path, now: float | None = None) -> dict | None:
    """При запуске, до открытия базы: подставить распакованную копию. Текущие данные — в before-restore-<время>."""
    data_dir = Path(data_dir)
    src = data_dir / STAGE
    if not (src / READY).exists():
        return None
    try:
        man = json.loads((src / READY).read_text())
    except (OSError, ValueError):
        man = {}
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(now or time.time()))
    old = data_dir / f'before-restore-{stamp}'
    old.mkdir(mode=0o700)
    keep_here = {STAGE, LOCAL, 'logs', 'version.txt', 'origin.txt', old.name}
    for p in list(data_dir.iterdir()):
        if p.name in keep_here or p.name.startswith('before-restore-'):
            continue
        shutil.move(str(p), str(old / p.name))
    for p in list(src.iterdir()):
        if p.name in (READY, 'backup.json'):
            continue
        shutil.move(str(p), str(data_dir / ('restored-env.txt' if p.name == 'env.txt' else p.name)))
    shutil.rmtree(src, ignore_errors=True)
    man['old'] = old.name
    (data_dir / RESTORED).write_text(json.dumps(man, ensure_ascii=False))
    return man


def restored_note(data_dir: str | Path) -> str | None:
    """Первый запуск после восстановления: что написать администраторам. Читается один раз."""
    p = Path(data_dir) / RESTORED
    try:
        man = json.loads(p.read_text())
        p.unlink()
    except (OSError, ValueError):
        return None
    when = time.strftime('%d.%m.%Y %H:%M', time.localtime(man['at'])) if man.get('at') else 'неизвестно когда'
    return (f'♻️ <b>Бот восстановлен из копии от {when}</b>\n'
            f'Групп {man.get("groups", 0)}, пользователей {man.get("users", 0)}, задач {man.get("jobs", 0)}.\n\n'
            f'Прежние данные этого сервера не удалены — они в <code>data/{html.escape(man.get("old", ""))}</code>. '
            'Если домен бота поменялся, проверьте адрес в настройках и кнопку меню.')


def size_text(n: int) -> str:
    return f'{n / 1024 / 1024:.1f} МБ' if n >= 1024 * 1024 else f'{max(1, n // 1024)} КБ'


def describe(man: dict) -> str:
    when = time.strftime('%d.%m.%Y %H:%M', time.localtime(man['at'])) if man.get('at') else '?'
    return (f'копия от {when}' + (f', версия бота {man["version"]}' if man.get('version') else '') +
            f': групп {man.get("groups", 0)}, пользователей {man.get("users", 0)}, задач {man.get("jobs", 0)}')


# ───────── ежедневная отправка ─────────
class Backup:
    def __init__(self, store, notify, data_dir: str | Path, env: dict | None = None, version=lambda: ''):
        self.store, self.notify, self.data_dir = store, notify, Path(data_dir)
        self.env = env if env is not None else dict(os.environ)
        self.version = version
        self._lock = asyncio.Lock()

    def local(self) -> list[dict]:
        d = self.data_dir / LOCAL
        if not d.is_dir():
            return []
        files = sorted(d.glob('bobrnet-*' + EXT), key=lambda p: p.name, reverse=True)
        return [{'name': p.name, 'size': p.stat().st_size, 'at': int(p.stat().st_mtime)} for p in files]

    def make(self, now: float | None = None) -> tuple[Path, dict]:
        """Создать зашифрованную копию в data/backups и удалить старые сверх «хранить»."""
        now = now or time.time()
        plain, man = build(self.data_dir, self.env, self.version(), now)
        blob = encrypt(plain, key_of(self.store))
        d = self.data_dir / LOCAL
        d.mkdir(mode=0o700, exist_ok=True)
        path = d / f'bobrnet-{time.strftime("%Y%m%d-%H%M%S", time.localtime(now))}{EXT}'
        path.write_bytes(blob)
        os.chmod(path, 0o600)
        for old in sorted(d.glob('bobrnet-*' + EXT), key=lambda p: p.name, reverse=True)[cfg(self.store)['keep']:]:
            old.unlink(missing_ok=True)
        man['size'] = len(blob)
        return path, man

    async def tick(self, now: float | None = None) -> None:
        day = due(cfg(self.store), now)
        if day:
            save_cfg(self.store, {'last': day})  # даже если не вышло — не пытаться каждую минуту; ошибку покажем
            await self.run(now=now)

    async def run(self, to: list[int] | None = None, now: float | None = None) -> dict:
        """Сделать копию и отправить администраторам (или только to). Возвращает итог для панели."""
        async with self._lock:
            now = now or time.time()
            try:
                path, man = await asyncio.to_thread(self.make, now)
            except Exception as e:  # noqa: BLE001
                log.exception('backup')
                save_cfg(self.store, {'last_error': f'не получилось сделать копию: {e}'[:300], 'last_at': int(now)})
                await self.notify.emit('system.backup', f'⚠️ <b>Резервная копия не сделана</b>\n<code>{html.escape(str(e)[:300])}</code>', to=to)
                raise BackupError(f'Не получилось сделать копию: {e}') from None
            c = cfg(self.store)
            text = (f'🗄 <b>Резервная копия BobrNet</b>\n'
                    f'Групп {man["groups"]}, пользователей {man["users"]}, задач {man["jobs"]} · {size_text(man["size"])}\n\n'
                    'Зашифрована паролем восстановления. Если сервер бота пропадёт: установите бота на новом сервере '
                    'и отправьте ему этот файл.')
            if not c['key_saved']:
                text += '\n\n❗️ Пароль восстановления ещё не сохранён — без него копию не открыть. Панель → Настройки → «Резервные копии».'
            if man['size'] > TG_LIMIT:
                err = f'копия {size_text(man["size"])} — больше, чем Telegram принимает от бота; она сохранена только на сервере'
                save_cfg(self.store, {'last_error': err, 'last_at': int(now), 'last_size': man['size'], 'last_file': path.name})
                await self.notify.emit('system.backup', f'⚠️ <b>Резервная копия не отправлена</b>: {err}.', to=to)
                return {'ok': False, 'error': err, 'file': path.name, 'size': man['size']}
            sent = await self.notify.emit('system.backup', text, to=to, document=path, silent=True)
            err = '' if sent else 'никому не отправилась — проверьте, что уведомление «Резервная копия» не выключено'
            save_cfg(self.store, {'last_error': err, 'last_at': int(now), 'last_size': man['size'], 'last_file': path.name})
            return {'ok': bool(sent), 'error': err, 'file': path.name, 'size': man['size'], 'sent': len(sent)}

    def view(self) -> dict:
        c = cfg(self.store)
        return {**{k: c[k] for k in ('on', 'hour', 'tz', 'keep', 'key_saved', 'last_at', 'last_size', 'last_error')},
                'local': self.local()[:10]}
