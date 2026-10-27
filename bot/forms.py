"""Поля настроек и профилей панелей с проверками — общие для чата и веб-админки.
Проверка получает строку и текущий профиль, возвращает нормализованное значение или бросает ValueError с понятным текстом."""
from __future__ import annotations

import ipaddress
import re
import urllib.parse
import uuid

from . import naming


def v_text(v, _=None):
    v = v.strip()
    if not 1 <= len(v) <= 40:
        raise ValueError('от 1 до 40 символов')
    return v


def v_url(v, _=None):
    v = v.strip().rstrip('/')
    if not re.match(r'^https?://[^\s/]+', v):
        raise ValueError('нужен адрес вида https://panel.example.com')
    return v


def v_nonempty(v, _=None):
    v = v.strip()
    if len(v) < 8 or ' ' in v:
        raise ValueError('похоже на неполный токен')
    return v


def v_cookie(v, _=None):
    v = v.strip()
    if v == '-':
        return ''
    if v.startswith('http'):
        q = urllib.parse.parse_qsl(urllib.parse.urlparse(v).query)
        if not q:
            raise ValueError('в ссылке нет параметра ?ИМЯ=ЗНАЧЕНИЕ')
        v = f'{q[0][0]}={q[0][1]}'
    if not re.fullmatch(r'[^=;\s]+=[^;\s]+', v):
        raise ValueError('нужно ИМЯ=ЗНАЧЕНИЕ, например secretkey=AbC123')
    return v


def v_ips(v, _=None):
    ips = [x for x in re.split(r'[\s,]+', v.strip()) if x]
    if not ips:
        raise ValueError('укажите хотя бы один IP')
    try:
        return ' '.join(str(ipaddress.ip_address(x)) for x in ips)
    except ValueError:
        raise ValueError('только IP-адреса, без доменов и подсетей') from None


def v_email(v, _=None):
    v = v.strip()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', v):
        raise ValueError('нужен рабочий email')
    return v


def v_base(v, prof=None):
    v = naming.norm_domain(v)
    if v in ('', '-'):
        return ''
    if not naming.valid_domain(v):
        raise ValueError('нужен домен вида example.com или nodes.example.com')
    err = naming.check_pattern((prof or {}).get('name_pattern') or naming.DEFAULT_PATTERN, v)
    if err:
        raise ValueError(err)
    return v


def v_pattern(v, prof=None):
    v = v.strip().lower()
    err = naming.check_pattern(v, (prof or {}).get('base_domain') or 'example.com')
    if err:
        raise ValueError(err)
    return v


def v_squads(v, _=None):
    if v.strip() == '-':
        return ''
    out = []
    for x in re.split(r'[\s,]+', v.strip()):
        if x:
            try:
                uuid.UUID(x)
            except ValueError:
                raise ValueError(f'«{x}» — не UUID сквада') from None
            out.append(x)
    return ', '.join(out)


def v_port(v, _=None):
    if not v.strip().isdigit() or not 1024 <= int(v) <= 65535:
        raise ValueError('число от 1024 до 65535')
    return int(v)


def v_opt_token(v, _=None):
    return '' if v.strip() == '-' else v_nonempty(v)


def v_int(v, _=None):
    if not v.strip().isdigit():
        raise ValueError('только цифры')
    return v.strip()


def v_pubkey(v, _=None):
    v = ' '.join(v.split())
    if v == '-':
        return ''
    if not re.match(r'^(ssh-(ed25519|rsa)|ecdsa-sha2-nistp\d+) [A-Za-z0-9+/=]+( \S+)?$', v):
        raise ValueError('нужен публичный ключ: ssh-ed25519 AAAA… (содержимое .pub)')
    return v


def v_owner(v, _=None):
    v = v.strip()
    if v in ('', '-'):
        return ''
    if re.fullmatch(r'\d{3,15}', v):
        return v
    if re.fullmatch(r'@?[A-Za-z][A-Za-z0-9_]{3,31}', v):
        return '@' + v.lstrip('@')
    raise ValueError('нужен Telegram ID (цифры) или @username')


def v_parallel(v, _=None):
    if not v.strip().isdigit() or not 1 <= int(v) <= 10:
        raise ValueError('число от 1 до 10')
    return v.strip()


# key: (название, подсказка, секрет, проверка)
SETTINGS = {
    'bcloud_url': ('Адрес bcloud', 'Адрес бота bcloud, как PUBLIC_URL, например https://bobr-cloud.online', False, v_url),
    'bcloud_token': ('Токен bcloud', 'На сервере bcloud выполните: <code>bcloud api-token</code>', True, v_nonempty),
    'bcloud_tg_id': ('Ваш аккаунт в bcloud (Telegram ID)', 'С его баланса покупаются серверы групп без своего клиента.', False, v_int),
    'cf_token': ('Токен Cloudflare', 'Права: Zone → DNS → Edit и Zone → Zone → Read', True, v_nonempty),
    'ssh_pubkey': ('Мой SSH-ключ', 'Публичный ключ (содержимое .pub) — добавим на каждый сервер. «-» — не добавлять', False, v_pubkey),
    'parallel': ('Параллельных установок', 'Сколько нод ставить одновременно, 1–10', False, v_parallel),
}
SECRET_SETTINGS = {k for k, (_, _, secret, _) in SETTINGS.items() if secret}

PFIELDS = {
    'name': ('Название группы', 'Например «Мои ноды» или имя клиента', False, v_text),
    'owner': ('Клиент bcloud', 'Telegram ID или @username владельца в bcloud: серверы покупаются от его имени, с его баланса и видны ему в bcloud. Пусто — с вашего аккаунта', False, v_owner),
    'panel_url': ('Адрес панели', 'Как открываете панель в браузере: https://panel.example.com', False, v_url),
    'panel_token': ('API-токен панели', 'Панель → Настройки → API Tokens → создать', True, v_nonempty),
    'panel_cookie': ('Cookie-защита', 'Пришлите секретную ссылку входа целиком или пару ИМЯ=ЗНАЧЕНИЕ. «-» — защиты нет', True, v_cookie),
    'outgoing_ips': ('Исходящие IP панели', 'Только им будет открыт API ноды. Через пробел', False, v_ips),
    'email': ("Email для Let's Encrypt", 'На него приходят уведомления о сертификатах', False, v_email),
    'base_domain': ('Домен для автоимён', 'Например example.com → ноды de1.example.com, nl2.example.com. «-» — домены только вручную', False, v_base),
    'name_pattern': ('Шаблон автоимён', '{cc} — код локации, {n} — номер. По умолчанию {cc}{n}. Без «vpn»', False, v_pattern),
    'squads': ('Сквады', 'UUID внутренних сквадов через запятую — туда добавим inbound новых нод. «-» — не добавлять', False, v_squads),
    'node_port': ('Порт API ноды', 'По умолчанию 2222', False, v_port),
    'cf_token': ('Токен Cloudflare группы', 'Если домены группы в другом аккаунте Cloudflare. Пусто — общий из «Подключений»', True, v_opt_token),
}
SECRET_FIELDS = {k for k, (_, _, secret, _) in PFIELDS.items() if secret}
REQUIRED = ['name', 'panel_url', 'panel_token', 'outgoing_ips', 'email']
WIZARD = ['name', 'panel_url', 'panel_token', 'panel_cookie', 'outgoing_ips', 'email', 'base_domain']
PROFILE_DEFAULTS = {'owner': '', 'owner_tg_id': '', 'owner_label': '', 'name_pattern': naming.DEFAULT_PATTERN, 'node_port': 2222, 'traffic_control': True, 'squads': '',
                    'cf_token': '', 'panel_cookie': '', 'base_domain': '', 'autoremove': True}


def clean_profile(data: dict, current: dict | None = None) -> dict:
    """Профиль из формы: проверяет каждое поле. Пустой секрет — оставить текущий. Ошибки — ValueError «Поле: текст»."""
    prof = {**PROFILE_DEFAULTS, **(current or {})}
    order = ['base_domain', 'name_pattern']  # шаблон проверяется вместе с доменом — сначала сохраним оба как есть
    for k in order:
        if k in data and data[k] is not None:
            prof[k] = str(data[k]).strip().lower() if k == 'name_pattern' else naming.norm_domain(str(data[k]))
    errors = []
    for k, (label, _, secret, check) in PFIELDS.items():
        if k not in data or data[k] is None:
            continue
        raw = str(data[k])
        if secret and raw.strip() == '' and prof.get(k):
            continue  # оставить текущий
        if raw.strip() == '' and k not in REQUIRED:
            prof[k] = '' if k != 'name_pattern' else naming.DEFAULT_PATTERN
            if k == 'node_port':
                prof[k] = 2222
            continue
        try:
            prof[k] = check(raw if raw.strip() else '-', prof) if k not in REQUIRED else check(raw, prof)
        except ValueError as e:
            errors.append(f'{label}: {e}')
    if 'traffic_control' in data:
        prof['traffic_control'] = bool(data['traffic_control'])
    for k in REQUIRED:
        if not prof.get(k) and not any(e.startswith(PFIELDS[k][0]) for e in errors):
            errors.append(f'{PFIELDS[k][0]}: обязательно')
    if errors:
        raise ValueError('; '.join(errors))
    return prof
