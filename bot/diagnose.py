"""Диагностика ноды: почему она недоступна и что с этим делать.

Сначала собираем факты (панель, Bobr Cloud, DNS, порт и TLS с сервера бота — то есть «из-за рубежа», SSH, итоги чекеров
в России по округам), потом verdict() ставит диагноз и предлагает исправления по порядку: бесплатные — раньше платных.
verdict() — чистая функция, без сети: её легко проверить тестами.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import time

from . import engine

# домен за прокси Cloudflare (оранжевое облако) указывает на адреса Cloudflare, а не на сервер — это не ошибка DNS
CF_NETS = [ipaddress.ip_network(x) for x in ('173.245.48.0/20', '103.21.244.0/22', '103.22.200.0/22', '103.31.4.0/22', '141.101.64.0/18',
                                             '108.162.192.0/18', '190.93.240.0/20', '188.114.96.0/20', '197.234.240.0/22', '198.41.128.0/17',
                                             '162.158.0.0/15', '104.16.0.0/13', '104.24.0.0/14', '172.64.0.0/13', '131.0.72.0/22')]


def cloudflare(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
        return any(a in n for n in CF_NETS)
    except ValueError:
        return False


REGION_NAMES = {'nw': 'Северо-Запад', 'c': 'Центр', 's': 'Юг', 'nc': 'Северный Кавказ', 'v': 'Поволжье', 'u': 'Урал',
                'sib': 'Сибирь', 'fe': 'Дальний Восток'}


async def tcp_open(host: str, port: int, timeout: float = 5) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        w.close()
        return True
    except (OSError, asyncio.TimeoutError, ValueError):
        return False


def tls_probe(host: str, port: int, sni: str, timeout: float = 6) -> dict:
    """TLS-рукопожатие с SNI домена: ok — сертификат проверен (и сколько дней ему осталось), cert — рукопожатие есть,
    сертификат не проверился (у REALITY это нормально), fail — рукопожатия нет."""
    def once(verify: bool):
        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=timeout) as s, ctx.wrap_socket(s, server_hostname=sni) as t:
            return t.getpeercert() if verify else {}
    try:
        cert = once(True)
        days = None
        if cert.get('notAfter'):
            days = int((ssl.cert_time_to_seconds(cert['notAfter']) - time.time()) // 86400)
        return {'tls': 'ok', 'days': days}
    except ssl.SSLCertVerificationError as e:
        try:
            once(False)
            return {'tls': 'cert', 'why': str(e.verify_message or e)[:120]}
        except (OSError, ssl.SSLError):
            return {'tls': 'fail'}
    except (OSError, ssl.SSLError):
        return {'tls': 'fail'}


def _public(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def resolve_all(host: str) -> list[str]:
    try:
        return sorted({a[4][0] for a in socket.getaddrinfo(host, None, socket.AF_INET)})
    except (OSError, UnicodeError):
        return []


async def collect(n: dict, bd: dict, mon: dict | None, checkers: list[dict], port: int | None, ssh_port: int,
                  bcloud_status=None, public_only: bool = False) -> dict:
    """Факты о ноде. n — нода из панели (address, isConnected/connected), bd — данные покупки ботом (ip, domain, server_id),
    mon — состояние мониторинга (by, checked_at), checkers — чекеры, bcloud_status — корутина статуса сервера или None."""
    addr = n.get('address') or ''
    dom = bd.get('domain') or ('' if engine.is_ip(addr) else addr)
    ip = bd.get('ip') or (addr if engine.is_ip(addr) else '')
    facts: dict = {'panel': bool(n.get('isConnected', n.get('connected'))), 'domain': dom, 'ip': ip, 'port': port or 443}
    dns = await asyncio.to_thread(resolve_all, dom) if dom else []
    facts['dns'] = dns
    target = ip or (dns[0] if dns else addr)
    if public_only and target and not _public(target):
        target = ''  # частный или локальный адрес — порт, SSH и TLS не проверяем
    facts['target'] = target
    tasks = [tcp_open(target, facts['port']), tcp_open(target, ssh_port)] if target else []
    if bcloud_status is not None:
        tasks.append(bcloud_status)
    res = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []
    facts['tcp'] = res[0] is True if res else None
    facts['ssh'] = res[1] is True if len(res) > 1 else None
    if bcloud_status is not None:
        st = res[-1]
        facts['bcloud'] = st if isinstance(st, str) else None
    facts['tls'] = await asyncio.to_thread(tls_probe, target, facts['port'], dom or target) if facts['tcp'] else None
    by = (mon or {}).get('by') or {}
    reg: dict[str, list[str]] = {}
    for c in checkers:
        r = by.get(str(c['id']))
        if not r:
            continue
        verdict_ = 'ok' if r[0] == r[1] else 'warn' if r[0] else 'bad'
        reg.setdefault(c.get('region') or '', []).append(verdict_)
    facts['russia'] = reg
    facts['checked_at'] = (mon or {}).get('checked_at')
    return facts


def verdict(f: dict, now: float | None = None, every: int = 300) -> dict:
    """Диагноз по фактам: code, title, text, severity (ok|warn|bad) и исправления по порядку.
    fixes: [{id, label, paid, why}] — id понимает интерфейс: fix (бесплатные шаги починки), sync_ip (взять IP из Bobr Cloud
    и поправить DNS), upgrade (переустановить контейнер ноды), reip (сменить IP — платно), replace (новая нода — платно), hoster."""
    now = now or time.time()
    ru = f.get('russia') or {}
    ru_all = [v for vs in ru.values() for v in vs]
    ru_bad_regions = sorted(k for k, vs in ru.items() if vs and all(v == 'bad' for v in vs))
    ru_ok = bool(ru_all) and all(v == 'ok' for v in ru_all)
    stale = bool(f.get('checked_at')) and now - f['checked_at'] > every * 3
    abroad_ok = bool(f.get('tcp'))
    paid = [{'id': 'reip', 'label': 'Сменить IP', 'paid': True, 'why': 'новый адрес у того же сервера — блокировка по IP останется позади'},
            {'id': 'replace', 'label': 'Новая нода', 'paid': True, 'why': 'новый сервер в той же локации; старый уберётся после проверки'}]

    bc = f.get('bcloud')
    if bc and bc != 'active':
        return {'code': 'server_off', 'severity': 'bad', 'title': f'Сервер в Bobr Cloud: {bc}',
                'text': 'Сервер не в рабочем состоянии у хостера — нода не может работать. Проверьте аренду и баланс в Bobr Cloud.',
                'fixes': [{'id': 'hoster', 'label': 'Открыть Bobr Cloud', 'paid': False, 'why': 'продлить или запустить сервер'}, paid[1]]}
    proxied = bool(f.get('dns')) and all(cloudflare(x) for x in f['dns'])
    if f.get('dns') is not None and f.get('domain') and f.get('ip') and f['ip'] not in f['dns'] and not proxied:
        where = ', '.join(f['dns']) or 'никуда (записи нет)'
        return {'code': 'dns', 'severity': 'bad', 'title': 'Домен указывает не на сервер ноды',
                'text': f'{f["domain"]} ведёт на {where}, а сервер ноды — {f["ip"]}. Клиенты подключаются не туда.',
                'fixes': [{'id': 'sync_ip', 'label': 'Поправить DNS', 'paid': False, 'why': 'бот возьмёт адрес сервера из Bobr Cloud и обновит A-запись и панель'}]}
    if f.get('tcp') is None and 'target' in f and not f['target']:
        return {'code': 'no_target', 'severity': 'warn', 'title': 'Адрес ноды не проверить',
                'text': 'У ноды нет публичного адреса, который бот мог бы проверить из-за рубежа. Проверьте адрес ноды в панели.', 'fixes': []}
    if not abroad_ok and f.get('ssh') is False:
        return {'code': 'down', 'severity': 'bad', 'title': 'Сервер не отвечает',
                'text': 'Ни порт ноды, ни SSH не открываются даже с сервера бота (из-за рубежа) — сервер выключен, завис или потерял сеть. '
                        'Это не блокировка из России: менять порт или маскировку бесполезно.',
                'fixes': [{'id': 'hoster', 'label': 'Проверить у хостера', 'paid': False, 'why': 'перезагрузить сервер в панели хостера'}, paid[1]]}
    if not abroad_ok:
        return {'code': 'node_down', 'severity': 'bad', 'title': 'Сервер жив, нода не работает',
                'text': 'SSH открывается, а порт ноды — нет, даже из-за рубежа. Контейнер ноды остановлен или упал.',
                'fixes': [{'id': 'upgrade', 'label': 'Переустановить контейнер', 'paid': False, 'why': 'бот заново поднимет ноду той же версии и проверит её'}]}
    if not f.get('panel'):
        return {'code': 'panel', 'severity': 'warn', 'title': 'Панель не видит ноду',
                'text': 'Порт ноды открыт, но панель Remnawave с нодой не связана — пользователи могут не получать её в подписке. '
                        'Чаще всего помогает перезапуск ноды; проверьте и что API ноды открыт для IP панели.',
                'fixes': [{'id': 'upgrade', 'label': 'Переустановить контейнер', 'paid': False, 'why': 'нода заново подключится к панели'}]}
    if not ru_all:
        return {'code': 'no_ru', 'severity': 'warn', 'title': 'Из-за рубежа работает, из России — нет данных',
                'text': 'Сервер и нода отвечают. Чекеры в России ещё не проверяли ноду — добавьте чекер или подождите опроса.',
                'fixes': []}
    if ru_ok:
        tls = f.get('tls') or {}
        if tls.get('tls') == 'ok' and tls.get('days') is not None and tls['days'] < 10:
            return {'code': 'cert', 'severity': 'warn', 'title': f'Сертификат истекает через {tls["days"]} дн.',
                    'text': 'Нода работает, но сертификат скоро кончится — клиенты перестанут подключаться.',
                    'fixes': [{'id': 'upgrade', 'label': 'Переустановить контейнер', 'paid': False, 'why': 'установщик выпустит сертификат заново'}]}
        return {'code': 'ok', 'severity': 'ok', 'title': 'Всё в порядке',
                'text': 'Нода отвечает и из-за рубежа, и из России' + (' (данные чекеров устарели — запустите проверку)' if stale else '') + '.',
                'fixes': []}
    free = [{'id': 'fix', 'label': 'Починить бесплатно', 'paid': False, 'why': 'свой порт и Final Mask, запасной XHTTP через CDN — по шагам из настроек группы'}]
    if ru_bad_regions and len(ru_bad_regions) < len([k for k in ru if k]):
        names = ', '.join(REGION_NAMES.get(k, k) for k in ru_bad_regions)
        return {'code': 'ru_partial', 'severity': 'warn', 'title': 'Частичная блокировка из России',
                'text': f'Из-за рубежа нода работает; из России не открывается в округах: {names}, в остальных — открывается. '
                        'Похоже на блокировку у части провайдеров — обычно помогают свой порт и Final Mask.',
                'fixes': free + paid}
    return {'code': 'ru_block', 'severity': 'bad', 'title': 'Блокировка из России',
            'text': 'Сервер и нода работают — из-за рубежа порт открывается, панель видит ноду, — а из России нода не проходит. '
                    'Это блокировка, а не поломка: сначала бесплатные способы, если не помогут — смена IP или новая нода.',
            'fixes': free + paid}
