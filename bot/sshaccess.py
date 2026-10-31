"""Доступ по SSH к серверу (нода панели, чекер): пользователь, порт, пароль, ключ и пароль sudo.

Бывает: root по паролю; root по ключу бота; свой ключ; ключ И пароль сразу (Сбер Клауд: AuthenticationMethods
publickey,password); не-root пользователь — тогда команды идут через sudo (пароль sudo — свой или тот же, что SSH).
Свой закрытый ключ хранится файлом 600 в data/ssh/keys, в базе — только путь к нему."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

KEY_MODES = ('', 'bot', 'own')


def legacy(acc: dict | None) -> dict:
    """Старый вид {mode: key|password, ...} → новый {key: bot|'', ...}."""
    acc = dict(acc or {})
    if 'key' not in acc and acc.get('mode'):
        acc['key'] = 'bot' if acc['mode'] == 'key' else ''
        if acc['mode'] == 'key':
            acc['password'] = ''
    if acc.get('auth') and 'key' not in acc:  # чекеры: auth key|password
        acc['key'] = 'bot' if acc['auth'] == 'key' else ''
    return acc


def save_key(text: str, keydir: str | Path) -> str:
    """Закрытый ключ из админки → файл 600. Проверяем ssh-keygen: ключ целый и без парольной фразы."""
    t = (text or '').replace('\r\n', '\n').replace('\r', '\n').strip()
    if 'PuTTY-User-Key-File' in t:
        raise ValueError('это ключ PuTTY (.ppk) — откройте его в PuTTYgen и сохраните как OpenSSH key')
    if re.match(r'(ssh-|ecdsa-)', t):
        raise ValueError('это публичный ключ (.pub) — нужен закрытый')
    m = re.fullmatch(r'(-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----)\s*(.+?)\s*(-----END [A-Z0-9 ]*PRIVATE KEY-----)', t, re.S)
    if not m:
        raise ValueError('нужен закрытый ключ целиком: от «-----BEGIN … PRIVATE KEY-----» до «-----END … PRIVATE KEY-----»')
    head, body, tail = m.groups()
    lines = [x for x in re.split(r'\s+', body) if x] if ':' not in body else body.split('\n')  # вставка с телефона склеивает строки
    t = head + '\n' + '\n'.join(lines) + '\n' + tail + '\n'
    d = Path(keydir) / 'keys'
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    path = d / (hashlib.sha256(t.encode()).hexdigest()[:16] + '.key')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(t)
    try:
        r = subprocess.run(['ssh-keygen', '-y', '-P', '', '-f', str(path)], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return str(path)  # проверить нечем — проверит первый вход
    if r.returncode != 0:
        path.unlink(missing_ok=True)
        if 'passphrase' in (r.stderr or '').lower():
            raise ValueError('ключ защищён парольной фразой — снимите её: ssh-keygen -p -f ключ')
        raise ValueError('ключ не читается: ' + (r.stderr or '').strip()[-120:])
    return str(path)


def clean(raw: dict, cur: dict | None, keydir: str | Path) -> dict:
    """Доступ из формы. Пустой пароль, ключ или пароль sudo — оставить сохранённый."""
    cur = legacy(cur)
    user = str(raw.get('user') or cur.get('user') or 'root').strip()
    if not re.fullmatch(r'[a-z_][a-z0-9_.-]{0,31}', user):
        raise ValueError('пользователь: латиница, например root или user')
    try:
        port = int(raw.get('port') or cur.get('port') or 22)
        assert 1 <= port <= 65535
    except (TypeError, ValueError, AssertionError):
        raise ValueError('SSH-порт: число от 1 до 65535') from None
    raw = legacy(raw) if 'key' not in raw and raw.get('mode') else raw
    key = raw.get('key', cur.get('key', '')) or ''
    if key not in KEY_MODES:
        raise ValueError('ключ: нет, свой или ключ бота')
    key_file = cur.get('key_file', '') if key == 'own' else ''
    if key == 'own' and str(raw.get('key_text') or '').strip():
        key_file = save_key(str(raw['key_text']), keydir)
    if key == 'own' and not key_file:
        raise ValueError('вставьте закрытый ключ')
    pw = str(raw.get('password') or '') or cur.get('password', '')
    if raw.get('password') == '-':
        pw = ''
    sudo_pw = str(raw.get('sudo_password') or '') or cur.get('sudo_password', '')
    if raw.get('sudo_password') == '-' or user == 'root':
        sudo_pw = ''
    if not pw and not key:
        raise ValueError('нужен пароль, ключ или оба')
    return {'user': user, 'port': port, 'password': pw, 'key': key, 'key_file': key_file, 'sudo_password': sudo_pw}


def to_node(acc: dict | None, bot_keypath: str = '') -> dict:
    """Поля для engine.Remote: ssh_user, ssh_port, ssh_password, ssh_key, sudo_password."""
    acc = legacy(acc)
    out = {'ssh_user': acc.get('user') or 'root', 'ssh_port': int(acc.get('port') or 22)}
    if acc.get('password'):
        out['ssh_password'] = acc['password']
    if acc.get('key') == 'bot' and bot_keypath:
        out['ssh_key'] = bot_keypath
    elif acc.get('key') == 'own' and acc.get('key_file'):
        out['ssh_key'] = acc['key_file']
    if acc.get('sudo_password'):
        out['sudo_password'] = acc['sudo_password']
    return out


def view(acc: dict | None) -> dict:
    """Для админки — без секретов."""
    acc = legacy(acc)
    return {'user': acc.get('user') or 'root', 'port': int(acc.get('port') or 22), 'key': acc.get('key') or '',
            'has_password': bool(acc.get('password')), 'has_key': bool(acc.get('key_file')),
            'has_sudo_password': bool(acc.get('sudo_password'))}


def describe(acc: dict | None) -> str:
    acc = legacy(acc)
    how = ' + '.join(x for x in ('пароль' if acc.get('password') else '',
                                 {'bot': 'ключ бота', 'own': 'свой ключ'}.get(acc.get('key') or '', '')) if x) or 'не задан'
    user = acc.get('user') or 'root'
    return f'{user}, {how}' + ('' if user == 'root' else ', через sudo')
