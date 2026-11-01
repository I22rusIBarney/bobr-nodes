"""Мост с серверным роутингом: на сервере входа — своя нода Remnawave с REALITY, трафик уходит на ноду-выход.

Чем отличается от проброса (HAProxy, bot/relay.py): соединение клиента заканчивается на входе. ТСПУ видит
REALITY с SNI популярного сайта, а не домен ноды-выхода; до выхода трафик идёт внутри Европы (там ТСПУ нет),
в интернет — с IP выхода. Пользователи авторизуются на входе, поэтому лимиты по IP и устройствам в панели работают.

Что делает бот:
- на сервере входа — только контейнер remnawave/node (/opt/remnanode), порт API ноды открыт панели;
- в панели — профиль «Мост …»: inbound REALITY, outbound VLESS на выход, маршрут «всё → выход»; нода входа с этим
  профилем; хост с адресом входа; inbound входа — в те же сквады, что и inbound выхода;
- на выходе — служебный пользователь моста (один на выход): вход подключается к выходу под ним, как обычный клиент.
Убрать мост — всё это обратно (служебный пользователь остаётся, пока им пользуются другие мосты к этому выходу).
"""
from __future__ import annotations

import base64
import datetime as dt
import re
import secrets
import shlex

from . import engine

TAG = 'BRIDGE_REALITY'
OUT_TAG = 'to-exit'
NODE_DIR = '/opt/remnanode'
IMAGE = 'remnawave/node:latest'
MARK = 'BOBR_XBRIDGE'
# SNI для REALITY: крупные сайты с TLS 1.3 и HTTP/2, которые открываются из России
SNI_SUGGEST = ['www.microsoft.com', 'dl.google.com', 'www.apple.com', 'www.amazon.com', 'www.samsung.com']


# ───────── ключи REALITY ─────────
def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip('=')


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def reality_keys() -> tuple[str, str]:
    """(privateKey, publicKey) x25519 в формате Xray (base64url без «=»)."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
    k = X25519PrivateKey.generate()
    priv = k.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub = k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return _b64u(priv), _b64u(pub)


def public_of(private_key: str) -> str:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    return _b64u(X25519PrivateKey.from_private_bytes(_unb64u(private_key)).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


def clean_sni(v: str) -> str:
    v = (v or '').strip().lower()
    if not re.fullmatch(r'(?=.{4,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}', v):
        raise ValueError('SNI: домен сайта, например www.microsoft.com')
    return v


# ───────── выход: как до него достучаться ─────────
def exit_outbound(panel: engine.Panel, pnode: dict, hosts: list[dict], target_port: int, user_uuid: str) -> dict:
    """Outbound VLESS на ноду-выход: адрес и порт — как у её хоста, безопасность — как у её inbound (TLS или REALITY)."""
    h = next((x for x in hosts if int(x.get('port') or 443) == int(target_port) and not x.get('isDisabled')), None) \
        or next((x for x in hosts if not x.get('isDisabled')), None)
    if not h:
        raise engine.StepError('у ноды-выхода нет включённых хостов — не знаю, как к ней подключаться')
    inb_uuid = (h.get('inbound') or {}).get('configProfileInboundUuid')
    prof_uuid = (h.get('inbound') or {}).get('configProfileUuid')
    prof = next((p for p in panel.call('GET', '/api/config-profiles')['configProfiles'] if p['uuid'] == prof_uuid), None)
    if not prof:
        raise engine.StepError('профиль ноды-выхода не найден в панели')
    tag = next((i['tag'] for i in prof.get('inbounds', []) if i['uuid'] == inb_uuid), None)
    inb = next((i for i in (prof.get('config') or {}).get('inbounds', []) if i.get('tag') == tag), None)
    if not inb or inb.get('protocol') != 'vless':
        raise engine.StepError('inbound ноды-выхода — не VLESS: мост с роутингом умеет только VLESS (TLS или REALITY)')
    ss = inb.get('streamSettings') or {}
    net, sec = ss.get('network') or 'tcp', ss.get('security') or 'none'
    address = h.get('address') or pnode['address']
    stream: dict = {'network': net, 'security': sec}
    if sec == 'tls':
        stream['tlsSettings'] = {'serverName': h.get('sni') or (address if not engine.is_ip(address) else pnode['address']),
                                 'fingerprint': 'chrome', 'alpn': ['h2', 'http/1.1']}
    elif sec == 'reality':
        rs = ss.get('realitySettings') or {}
        if not rs.get('privateKey'):
            raise engine.StepError('у REALITY выхода нет privateKey в профиле')
        stream['realitySettings'] = {'serverName': h.get('sni') or (rs.get('serverNames') or [''])[0],
                                     'publicKey': public_of(rs['privateKey']), 'shortId': (rs.get('shortIds') or [''])[0],
                                     'fingerprint': 'chrome'}
    else:
        raise engine.StepError(f'безопасность inbound выхода — {sec}: мосту нужен TLS или REALITY')
    if net == 'xhttp' and ss.get('xhttpSettings'):
        stream['xhttpSettings'] = {k: v for k, v in ss['xhttpSettings'].items() if k in ('path', 'host', 'mode')}
    flow = 'xtls-rprx-vision' if net in ('tcp', 'raw') else ''
    user = {'id': user_uuid, 'encryption': 'none', **({'flow': flow} if flow else {})}
    return {'tag': OUT_TAG, 'protocol': 'vless',
            'settings': {'vnext': [{'address': address, 'port': int(h.get('port') or 443), 'users': [user]}]},
            'streamSettings': stream, '_inbound': inb_uuid}


def profile_config(listen: int, sni: str, private_key: str, short_id: str, outbound: dict) -> dict:
    out = {k: v for k, v in outbound.items() if not k.startswith('_')}
    return {
        'log': {'loglevel': 'warning'},
        'inbounds': [{'tag': TAG, 'port': int(listen), 'listen': '0.0.0.0', 'protocol': 'vless',
                      'settings': {'clients': [], 'decryption': 'none'},
                      'sniffing': {'enabled': True, 'destOverride': ['http', 'tls', 'quic']},
                      'streamSettings': {'network': 'tcp', 'security': 'reality',
                                         'realitySettings': {'show': False, 'dest': f'{sni}:443', 'xver': 0,
                                                             'serverNames': [sni], 'privateKey': private_key,
                                                             'shortIds': [short_id]}}}],
        'outbounds': [out, {'tag': 'DIRECT', 'protocol': 'freedom'}, {'tag': 'BLOCK', 'protocol': 'blackhole'}],
        'routing': {'rules': [{'type': 'field', 'ip': ['geoip:private'], 'outboundTag': 'BLOCK'},
                              {'type': 'field', 'inboundTag': [TAG], 'outboundTag': OUT_TAG}]},
    }


def profile_name(ip: str) -> str:
    """Название профиля в панели: только латиница, цифры, пробел, _ и - (иначе панель отвечает Validation failed)."""
    return re.sub(r'[^A-Za-z0-9_ -]', '-', f'Bridge {ip}')[:30]


# ───────── служебный пользователь выхода ─────────
def bridge_username(exit_uuid: str) -> str:
    return 'bridge_' + re.sub(r'[^a-z0-9]', '', exit_uuid.lower())[:12]


def ensure_user(panel: engine.Panel, exit_uuid: str, squads: list[str], log) -> dict:
    """Служебный пользователь моста: бессрочный, без лимита трафика, в сквадах выхода."""
    name = bridge_username(exit_uuid)
    try:
        u = panel.call('GET', f'/api/users/by-username/{name}')
    except engine.StepError:
        u = None
    if u and u.get('uuid'):
        have = {s.get('uuid') if isinstance(s, dict) else s for s in (u.get('activeInternalSquads') or [])}
        if not set(squads) <= have:
            u = panel.call('PATCH', '/api/users', {'uuid': u['uuid'], 'activeInternalSquads': sorted(have | set(squads))})
        log(f'панель: служебный пользователь моста {name} уже есть')
        return u
    exp = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3650)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    u = panel.call('POST', '/api/users', {'username': name, 'expireAt': exp, 'trafficLimitBytes': 0,
                                          'trafficLimitStrategy': 'NO_RESET', 'activeInternalSquads': squads,
                                          'description': 'Bobr Nodes: мост с серверным роутингом — не удалять'})
    log(f'панель: создан служебный пользователь моста {name}')
    return u


def squads_with(panel: engine.Panel, inbound_uuid: str) -> list[str]:
    return [sq['uuid'] for sq in panel.call('GET', '/api/internal-squads')['internalSquads']
            if any(i.get('uuid') == inbound_uuid for i in sq.get('inbounds', []))]


# ───────── сервер входа ─────────
def compose_text(secret: str, node_port: int) -> str:
    return (f'services:\n  remnanode:\n    container_name: remnanode\n    hostname: remnanode\n    image: {IMAGE}\n'
            f'    network_mode: host\n    restart: always\n    ulimits:\n      nofile:\n        soft: 1048576\n        hard: 1048576\n'
            f'    environment:\n      - NODE_PORT={int(node_port)}\n      - SECRET_KEY={secret}\n')


def install_cmd(secret: str, node_port: int, listen: int, panel_ips: list[str]) -> str:
    """Docker (если нет), контейнер ноды, порты в ufw: вход — всем, API ноды — только панели. HAProxy-проброс этого
    сервера (если был) снимается: порт входа нужен Xray."""
    comp = base64.b64encode(compose_text(secret, node_port).encode()).decode()
    ips = ' '.join(shlex.quote(i) for i in panel_ips if engine.is_ip(i))
    return (
        f': {MARK}; set +e; export DEBIAN_FRONTEND=noninteractive; '
        'systemctl disable --now bobr-relay >/dev/null 2>&1; rm -f /etc/systemd/system/bobr-relay.service; systemctl daemon-reload; '
        f'if ss -Hltnp "sport = :{int(listen)}" | grep -v remnanode | grep -v xray | grep -q .; then echo "XB_BUSY"; '
        f'ss -Hltnp "sport = :{int(listen)}" | head -3; exit 0; fi; '
        'if ! command -v docker >/dev/null; then (apt-get update -qq && apt-get install -y -qq docker.io docker-compose-v2) >/dev/null 2>&1 '
        '|| curl -fsSL https://get.docker.com | sh >/dev/null 2>&1; fi; '
        'command -v docker >/dev/null || { echo XB_NODOCKER; exit 0; }; systemctl enable --now docker >/dev/null 2>&1; '
        'docker compose version >/dev/null 2>&1 || { apt-get install -y -qq docker-compose-plugin docker-compose-v2 >/dev/null 2>&1; }; '
        f'mkdir -p {NODE_DIR} && echo {comp} | base64 -d > {NODE_DIR}/docker-compose.yml && chmod 600 {NODE_DIR}/docker-compose.yml; '
        f'cd {NODE_DIR} && timeout 600 docker compose pull -q >/dev/null 2>&1; docker compose up -d >/dev/null 2>&1 || {{ echo XB_UPFAIL; '
        'docker compose up -d 2>&1 | tail -5; exit 0; }; '
        'if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then '
        f'ufw allow {int(listen)}/tcp comment bobr-bridge >/dev/null; '
        + (f'for ip in {ips}; do ufw allow from "$ip" to any port {int(node_port)} proto tcp comment bobr-bridge >/dev/null; done; ' if ips
           else f'ufw allow {int(node_port)}/tcp comment bobr-bridge >/dev/null; ') +
        'fi; sleep 3; docker ps --format "{{.Names}} {{.Status}}" | grep -q "^remnanode Up" && echo XB_OK || '
        '{ echo XB_DOWN; docker logs --tail 8 remnanode 2>&1; }')


def install(r: engine.Remote, secret: str, node_port: int, listen: int, panel_ips: list[str], log) -> None:
    log('ставлю ноду на вход: Docker и контейнер remnawave/node (первый раз — до пары минут)')
    rc, out = r.run(install_cmd(secret, node_port, listen, panel_ips), 900)
    out = engine.strip_ansi(out)
    if 'XB_BUSY' in out:
        raise engine.StepError(f'порт {listen} на сервере входа занят другой программой: ' + out.split('XB_BUSY', 1)[1].strip()[:200])
    if 'XB_NODOCKER' in out:
        raise engine.StepError('не удалось поставить Docker на сервер входа')
    if 'XB_UPFAIL' in out or 'XB_DOWN' in out:
        raise engine.StepError('контейнер ноды на входе не запустился: ' + out.strip()[-300:])
    if 'XB_OK' not in out:
        raise engine.StepError('нода на входе не поставилась: ' + (out.strip()[-300:] or f'код {rc}'))
    log('нода на входе запущена')


def stop_cmd() -> str:
    return (f': {MARK}; set +e; [ -f {NODE_DIR}/docker-compose.yml ] && (cd {NODE_DIR} && docker compose down >/dev/null 2>&1); '
            'docker rm -f remnanode >/dev/null 2>&1; '
            f'[ -e {NODE_DIR} ] && mv {NODE_DIR} "{NODE_DIR}.removed-$(date +%Y%m%d-%H%M%S)"; '
            'if command -v ufw >/dev/null; then for n in $(ufw status numbered 2>/dev/null | grep bobr-bridge | '
            "sed -E 's/^\\[ *([0-9]+)\\].*/\\1/' | sort -rn); do yes | ufw delete \"$n\" >/dev/null 2>&1; done; fi; echo XB_STOPPED")


# ───────── панель ─────────
def setup_panel(panel: engine.Panel, x: dict, pnode: dict, hosts: list[dict], log) -> dict:
    """Профиль моста, служебный пользователь выхода, нода входа с хостом, сквады. Возвращает, что создано."""
    first = next((h for h in hosts if int(h.get('port') or 443) == int(x['target_port'])), None) or (hosts[0] if hosts else None)
    if not first:
        raise engine.StepError('у ноды-выхода нет хостов')
    exit_inbound = (first.get('inbound') or {}).get('configProfileInboundUuid')
    squads = squads_with(panel, exit_inbound)
    if not squads:
        raise engine.StepError('inbound ноды-выхода не входит ни в один сквад — некому выдавать мост')
    user = ensure_user(panel, pnode['uuid'], squads, log)
    uid = user.get('vlessUuid') or user.get('vless_uuid')
    if not uid:
        raise engine.StepError('панель не вернула VLESS UUID служебного пользователя')
    outbound = exit_outbound(panel, pnode, hosts, x['target_port'], uid)
    keep = x.get('reality') or {}  # переустановка — те же ключи: у клиентов в подписке ничего не меняется
    priv = keep.get('private') or reality_keys()[0]
    sid = keep.get('sid') or secrets.token_hex(4)
    name = profile_name(x['ip'])
    cfg = profile_config(x['listen'], x['sni'], priv, sid, outbound)
    profiles = panel.call('GET', '/api/config-profiles')['configProfiles']
    old = next((p for p in profiles if p['name'] == name), None)
    if old:
        panel.call('PATCH', '/api/config-profiles', {'uuid': old['uuid'], 'config': cfg})
        prof = next(p for p in panel.call('GET', '/api/config-profiles')['configProfiles'] if p['uuid'] == old['uuid'])
        log(f'панель: профиль «{name}» обновлён')
    else:
        prof = panel.call('POST', '/api/config-profiles', {'name': name, 'config': cfg})
        log(f'панель: профиль «{name}» создан — REALITY с SNI {x["sni"]}, выход на {outbound["settings"]["vnext"][0]["address"]}')
    inbound = next(i for i in prof['inbounds'] if i['tag'] == TAG)
    node = {'ip': x['ip'], 'domain': x.get('address') or x['ip'], 'name': (x.get('remark') or name)[:30],
            'country': x.get('country') or '', 'remark': x.get('remark') or name}
    panel.attach(node, prof, [inbound], squads, int(x.get('node_port') or 2222), log, tls=False)
    entry = next((n for n in panel.call('GET', '/api/nodes') if n['address'] == node['domain']), None)
    hosts_now = [h['uuid'] for h in panel.call('GET', '/api/hosts')
                 if (h.get('inbound') or {}).get('configProfileInboundUuid') == inbound['uuid']]
    for hu in hosts_now:  # SNI хоста — как у REALITY (панель подставила бы адрес-IP)
        panel.call('PATCH', '/api/hosts', {'uuid': hu, 'sni': x['sni'], 'fingerprint': 'chrome'})
    return {'profile': prof['uuid'], 'inbound': inbound['uuid'], 'node': entry['uuid'] if entry else '',
            'hosts': hosts_now, 'squads': squads, 'user': user.get('uuid'), 'username': bridge_username(pnode['uuid']),
            'reality': {'private': priv, 'sid': sid}}


def teardown_panel(panel: engine.Panel, made: dict, user_in_use: bool, log) -> None:
    """Убрать из панели всё, что создал мост: хосты, ноду входа, inbound из сквадов, профиль; служебного пользователя —
    если им не пользуются другие мосты к тому же выходу."""
    have = {h['uuid'] for h in panel.call('GET', '/api/hosts')}
    for hu in made.get('hosts') or []:
        if hu in have:
            panel.call('DELETE', f'/api/hosts/{hu}')
    if made.get('node'):
        try:
            panel.call('DELETE', f'/api/nodes/{made["node"]}')
            log('панель: нода входа удалена')
        except engine.StepError as e:
            log(f'панель: ноду входа удалить не удалось: {e}')
    for sq in panel.call('GET', '/api/internal-squads')['internalSquads']:
        ids = [i['uuid'] for i in sq.get('inbounds', [])]
        if made.get('inbound') in ids:
            panel.call('PATCH', '/api/internal-squads', {'uuid': sq['uuid'], 'inbounds': [i for i in ids if i != made['inbound']]})
    if made.get('profile'):
        try:
            panel.call('DELETE', f'/api/config-profiles/{made["profile"]}')
            log('панель: профиль моста удалён')
        except engine.StepError as e:
            log(f'панель: профиль моста удалить не удалось: {e}')
    if made.get('user') and not user_in_use:
        try:
            panel.call('DELETE', f'/api/users/{made["user"]}')
            log('панель: служебный пользователь моста удалён')
        except engine.StepError as e:
            log(f'панель: служебного пользователя удалить не удалось: {e}')


def wait_connected(panel: engine.Panel, node_uuid: str, timeout: float = 150) -> bool:
    import time
    end = time.time() + timeout
    while time.time() < end:
        n = next((x for x in panel.call('GET', '/api/nodes') if x['uuid'] == node_uuid), None)
        if n and n.get('isConnected'):
            return True
        time.sleep(5)
    return False


def env_panel_ips(prof: dict) -> list[str]:
    raw = prof.get('outgoing_ips') or ''
    return [x for x in re.split(r'[\s,]+', str(raw)) if x and engine.is_ip(x)]

