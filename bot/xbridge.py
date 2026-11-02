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
ROUTED = ('xray', 'vision', 'egames')  # мосты с серверным роутингом (нода на входе)
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
    """Outbound VLESS на ноду-выход: адрес и порт — как у её хоста, безопасность — как у её inbound (TLS или REALITY).
    Из хостов ноды берём прямой: TCP (Vision) на адрес самой ноды, а не CDN-хост (XHTTP через Cloudflare) —
    через CDN тяжёлый трафик рвётся, да и лишнее звено ни к чему: между входом и выходом ТСПУ нет."""
    profiles = {p['uuid']: p for p in panel.call('GET', '/api/config-profiles')['configProfiles']}

    def inbound_of(h):
        prof = profiles.get((h.get('inbound') or {}).get('configProfileUuid'))
        if not prof:
            return None
        tag = next((i['tag'] for i in prof.get('inbounds', []) if i['uuid'] == (h.get('inbound') or {}).get('configProfileInboundUuid')), None)
        return next((i for i in (prof.get('config') or {}).get('inbounds', []) if i.get('tag') == tag), None)

    def score(h, inb):
        ss = (inb or {}).get('streamSettings') or {}
        addr = h.get('address') or ''
        return (bool(inb) and inb.get('protocol') == 'vless',
                (ss.get('network') or 'tcp') in ('tcp', 'raw') and (ss.get('security') or 'none') in ('tls', 'reality'),
                addr == pnode['address'] or (engine.is_ip(addr) and addr == pnode.get('ip')),
                int(h.get('port') or 443) == int(target_port))
    cands = [(h, inbound_of(h)) for h in hosts if not h.get('isDisabled')]
    if not cands:
        raise engine.StepError('у ноды-выхода нет включённых хостов — не знаю, как к ней подключаться')
    h, inb = max(cands, key=lambda c: score(*c))
    if not inb:
        raise engine.StepError('профиль ноды-выхода не найден в панели')
    if inb.get('protocol') != 'vless':
        raise engine.StepError('inbound ноды-выхода — не VLESS: мост с роутингом умеет только VLESS (TLS или REALITY)')
    inb_uuid = (h.get('inbound') or {}).get('configProfileInboundUuid')
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


def profile_name(ip: str, vision: bool = False, eg: bool = False) -> str:
    """Название профиля в панели: только латиница, цифры, пробел, _ и - (иначе панель отвечает Validation failed)."""
    return re.sub(r'[^A-Za-z0-9_ -]', '-', f'Bridge{" V" if vision else " EG" if eg else ""} {ip}')[:30]


def vision_tag(ip: str) -> str:
    """Тег inbound Vision у моста: в панели теги должны быть уникальны между профилями."""
    return 'BRIDGE_VISION_' + re.sub(r'[^0-9A-Za-z]', '_', ip)


# ───────── связь вход → выход по Shadowsocks (как в eGames remnawave-reverse-proxy) ─────────
# Вход ходит на выход не через общий inbound клиентов (с его доменом, Psiphon, WARP и прочим), а через отдельный
# Shadowsocks-inbound на выходе: включён только на ноде-выходе, в нём только служебный пользователь моста.
SS_METHOD = 'chacha20-ietf-poly1305'
SS_PORT = 9999
RU_RULES = [{'type': 'field', 'ip': ['geoip:ru'], 'outboundTag': 'DIRECT'},
            {'type': 'field', 'domain': ['geosite:category-ru'], 'outboundTag': 'DIRECT'}]


def ss_tag(exit_uuid: str) -> str:
    return 'BRIDGE_SS_' + re.sub(r'[^a-z0-9]', '', exit_uuid.lower())[:8].upper()


def ss_squad_name(exit_uuid: str) -> str:
    return 'Bridge SS ' + re.sub(r'[^a-z0-9]', '', exit_uuid.lower())[:8]


def ensure_ss_exit(panel: engine.Panel, pnode: dict, log, port: int = SS_PORT) -> dict:
    """Shadowsocks-inbound для моста в профиле ноды-выхода (включён только на ней) и сквад только с ним."""
    puuid = (pnode.get('configProfile') or {}).get('activeConfigProfileUuid')
    profiles = panel.call('GET', '/api/config-profiles')['configProfiles']
    prof = next((p for p in profiles if p['uuid'] == puuid), None)
    if not prof:
        raise engine.StepError('профиль ноды-выхода не найден в панели')
    tag = ss_tag(pnode['uuid'])
    cfg = prof.get('config') or {}
    inb = next((i for i in cfg.get('inbounds', []) if i.get('tag') == tag), None)
    if inb:
        port = int(inb.get('port') or port)
    else:
        used = {int(i.get('port') or 0) for i in cfg.get('inbounds', [])}
        while port in used:
            port += 1
        new = {'tag': tag, 'port': port, 'listen': '0.0.0.0', 'protocol': 'shadowsocks',
               'settings': {'method': SS_METHOD, 'network': 'tcp,udp', 'clients': []},
               'sniffing': {'enabled': True, 'destOverride': ['http', 'tls', 'quic']}}
        panel.call('PATCH', '/api/config-profiles', {'uuid': prof['uuid'], 'config': {**cfg, 'inbounds': [*cfg.get('inbounds', []), new]}})
        prof = next(p for p in panel.call('GET', '/api/config-profiles')['configProfiles'] if p['uuid'] == prof['uuid'])
        log(f'панель: на выходе добавлен Shadowsocks-inbound {tag} (порт {port}) — только для моста')
    inb_uuid = next((i['uuid'] for i in prof.get('inbounds', []) if i.get('tag') == tag), None)
    if not inb_uuid:
        raise engine.StepError('панель не вернула inbound Shadowsocks на выходе')
    active = engine.active_inbounds(pnode)
    if not active:
        raise engine.StepError('у ноды-выхода не видно активных inbound — не трогаю её, чтобы не выключить клиентов')
    if inb_uuid not in active:
        panel.call('PATCH', '/api/nodes', {'uuid': pnode['uuid'], 'configProfile': {'activeConfigProfileUuid': prof['uuid'],
                                                                                    'activeInbounds': [*active, inb_uuid]}})
        log('панель: Shadowsocks-inbound включён на ноде-выходе (на других нодах этого профиля — нет)')
    name = ss_squad_name(pnode['uuid'])
    sqs = panel.call('GET', '/api/internal-squads')['internalSquads']
    sq = next((q for q in sqs if q.get('name') == name), None)
    if not sq:
        made = panel.call('POST', '/api/internal-squads', {'name': name, 'inbounds': [inb_uuid]})
        # ответ на создание у версий панели разный — сквад берём из списка
        sq = made if isinstance(made, dict) and made.get('uuid') else next(
            (q for q in panel.call('GET', '/api/internal-squads')['internalSquads'] if q.get('name') == name), None)
        if not sq:
            raise engine.StepError(f'панель не создала сквад «{name}»')
        log(f'панель: создан сквад «{name}» — в нём только вход моста на выход')
    elif [i.get('uuid') for i in sq.get('inbounds', [])] != [inb_uuid]:
        panel.call('PATCH', '/api/internal-squads', {'uuid': sq['uuid'], 'inbounds': [inb_uuid]})
    address = pnode.get('address') or ''
    return {'profile': prof['uuid'], 'inbound': inb_uuid, 'tag': tag, 'port': port, 'squad': sq['uuid'], 'address': address,
            'exit_node': pnode['uuid']}


def ss_outbound(ss: dict, password: str) -> dict:
    return {'tag': OUT_TAG, 'protocol': 'shadowsocks',
            'settings': {'servers': [{'address': ss['address'], 'port': int(ss['port']), 'method': SS_METHOD,
                                      'password': password, 'level': 0}]}}


def remove_ss_exit(panel: engine.Panel, ss: dict, log) -> None:
    """Убрать Shadowsocks-inbound моста с выхода и его сквад."""
    for q in panel.call('GET', '/api/internal-squads')['internalSquads']:
        if q['uuid'] == ss.get('squad'):
            panel.call('DELETE', f'/api/internal-squads/{q["uuid"]}')
    node = next((n for n in panel.call('GET', '/api/nodes') if n['uuid'] == ss.get('exit_node')), None)
    if node and ss.get('inbound') in engine.active_inbounds(node):
        cp = node.get('configProfile') or {}
        panel.call('PATCH', '/api/nodes', {'uuid': node['uuid'], 'configProfile': {
            'activeConfigProfileUuid': cp.get('activeConfigProfileUuid'),
            'activeInbounds': [u for u in engine.active_inbounds(node) if u != ss['inbound']]}})
    prof = next((p for p in panel.call('GET', '/api/config-profiles')['configProfiles'] if p['uuid'] == ss.get('profile')), None)
    if prof:
        cfg = prof.get('config') or {}
        if any(i.get('tag') == ss.get('tag') for i in cfg.get('inbounds', [])):
            panel.call('PATCH', '/api/config-profiles', {'uuid': prof['uuid'], 'config': {
                **cfg, 'inbounds': [i for i in cfg.get('inbounds', []) if i.get('tag') != ss.get('tag')]}})
    log('панель: Shadowsocks-вход моста убран с выхода')


def bridge_rules(rules: list[dict], tag: str, block: set, ru_direct: bool) -> list[dict]:
    """Правила профиля входа: блокировки → (Россия напрямую) → «всё из входа → выход» → остальное."""
    def ours(r):
        return r.get('outboundTag') == OUT_TAG or (r.get('outboundTag') == 'DIRECT' and
                                                    (r.get('ip') == ['geoip:ru'] or r.get('domain') == ['geosite:category-ru']))
    rest = [r for r in rules if not ours(r)]
    return ([r for r in rest if r.get('outboundTag') in block] + (RU_RULES if ru_direct else []) +
            [{'type': 'field', 'inboundTag': [tag], 'outboundTag': OUT_TAG}] + [r for r in rest if r.get('outboundTag') not in block])


def vision_config(base: dict, domain: str, tag: str, outbound: dict, ru_direct: bool = False) -> dict:
    """Профиль моста на Vision TLS: inbound — как ставит встроенный скрипт бота (сертификат через общую ссылку),
    выход — на ноду-выход. Правила блокировки профиля остаются первыми, дальше — «всё из входа → выход»."""
    cfg = engine.retag(engine.shared_profile(base, domain), engine.SHARED_TAG, tag)
    out = {k: v for k, v in outbound.items() if not k.startswith('_')}
    outs = [o for o in cfg.get('outbounds', []) if o.get('tag') != OUT_TAG]
    if not any(o.get('protocol') == 'freedom' for o in outs):
        outs.append({'tag': 'DIRECT', 'protocol': 'freedom'})
    block = {o.get('tag') for o in outs if o.get('protocol') == 'blackhole'}
    rules = bridge_rules((cfg.get('routing') or {}).get('rules') or [], tag, block, ru_direct)
    return {**cfg, 'outbounds': [out, *outs], 'routing': {**(cfg.get('routing') or {}), 'rules': rules}}


# ───────── служебный пользователь выхода ─────────
def bridge_username(exit_uuid: str) -> str:
    return 'bridge_' + re.sub(r'[^a-z0-9]', '', exit_uuid.lower())[:12]


def _squad_ids(u: dict) -> set:
    return {s.get('uuid') if isinstance(s, dict) else s for s in (u.get('activeInternalSquads') or [])}


def _unwrap(u):
    if isinstance(u, dict) and isinstance(u.get('user'), dict):
        return u['user']
    return u if isinstance(u, dict) else None


def find_user(panel: engine.Panel, name: str, scan: bool = False) -> dict | None:
    """Пользователь по имени. Адрес поиска разный в версиях панели — пробуем известные; scan — перебрать список."""
    for path in (f'/api/users/by-username/{name}', f'/api/users/get-by/username/{name}'):
        try:
            u = _unwrap(panel.call('GET', path))
        except engine.StepError:
            continue
        if u and u.get('username') == name:
            return u
    if not scan:
        return None
    start = 0
    while start < 20000:  # по 500 — без лишней нагрузки на панель
        try:
            page = panel.call('GET', f'/api/users?start={start}&size=500')
        except engine.StepError:
            return None
        users = page.get('users') if isinstance(page, dict) else page
        if not users:
            return None
        hit = next((x for x in users if x.get('username') == name), None)
        if hit:
            return hit
        start += len(users)
    return None


def user_ref(u: dict) -> dict:
    """Чем указать пользователя в PATCH /api/users: uuid, а в панели 3.x у пользователя его может не быть — тогда username."""
    return {'uuid': u['uuid']} if u.get('uuid') else {'username': u['username']}


def user_key(u: dict) -> str:
    """Для DELETE /api/users/{…}: uuid, в панели 3.x — числовой id."""
    return str(u.get('uuid') or u.get('id') or '')


def ensure_user(panel: engine.Panel, exit_uuid: str, squads: list[str], log) -> dict:
    """Служебный пользователь моста: бессрочный, без лимита трафика, в сквадах выхода."""
    name = bridge_username(exit_uuid)
    u = find_user(panel, name)
    if u and u.get('username'):
        have = _squad_ids(u)
        if not set(squads) <= have:
            u = _merge(u, panel.call('PATCH', '/api/users', {**user_ref(u), 'activeInternalSquads': sorted(have | set(squads))}))
        log(f'панель: служебный пользователь моста {name} уже есть')
        return u
    exp = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3650)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    try:
        u = panel.call('POST', '/api/users', {'username': name, 'expireAt': exp, 'trafficLimitBytes': 0,
                                              'trafficLimitStrategy': 'NO_RESET', 'activeInternalSquads': squads,
                                              'description': 'Bobr Nodes: мост с серверным роутингом — не удалять'})
    except engine.StepError as e:
        if 'already exists' not in str(e):
            raise
        u = find_user(panel, name, scan=True)  # создан прошлой попыткой, а поиск по имени панель не отдала
        if not u:
            raise
        if not set(squads) <= _squad_ids(u):
            u = _merge(u, panel.call('PATCH', '/api/users', {**user_ref(u), 'activeInternalSquads': sorted(_squad_ids(u) | set(squads))}))
        log(f'панель: служебный пользователь моста {name} уже есть')
        return u
    log(f'панель: создан служебный пользователь моста {name}')
    return u


def _merge(u: dict, resp) -> dict:
    """Ответ PATCH — пользователь целиком; если панель вернула не его, оставляем прежнего."""
    r = _unwrap(resp)
    return {**u, **r} if r and r.get('username') == u.get('username') else u


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
        # порт держит наша же нода (Xray в контейнере называется rw-core, раньше xray) — это не помеха
        f'if ss -Hltnp "sport = :{int(listen)}" | grep -vE \'"(rw-core|xray|remnanode)"\' | grep -q . '
        '&& ! docker ps --format "{{.Names}}" 2>/dev/null | grep -qx remnanode; then echo "XB_BUSY"; '
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
    ss = ensure_ss_exit(panel, pnode, log) if x.get('hop') == 'ss' else None
    user = ensure_user(panel, pnode['uuid'], squads + ([ss['squad']] if ss else []), log)
    if ss:
        pw = user.get('ssPassword') or user.get('ss_password')
        if not pw:
            raise engine.StepError('панель не вернула пароль Shadowsocks служебного пользователя')
        outbound = ss_outbound(ss, pw)
    else:
        uid = user.get('vlessUuid') or user.get('vless_uuid')
        if not uid:
            raise engine.StepError('панель не вернула VLESS UUID служебного пользователя')
        outbound = exit_outbound(panel, pnode, hosts, x['target_port'], uid)
    try:  # Torrent Blocker выхода не должен банить весь вход разом
        from . import plugins
        plugins.protect_bridge(panel, x['ip'], user.get('id'), log)
    except engine.StepError as e:
        log(f'Torrent Blocker: исключение для входа не добавилось ({e}) — добавьте IP {x["ip"]} в исключения вручную')
    from . import egames
    vision, eg = x.get('mode') == 'vision', x.get('mode') == 'egames'
    tag = vision_tag(x['ip']) if vision else egames.tag_for(x['ip'], bridge=True) if eg else TAG
    keep = x.get('reality') or {}  # переустановка — те же ключи: у клиентов в подписке ничего не меняется
    priv = keep.get('private') or ('' if vision else reality_keys()[0])
    sid = keep.get('sid') or ('' if vision else secrets.token_hex(8 if eg else 4))
    name = profile_name(x['ip'], vision, eg)
    if eg:  # вход как нода eGames: REALITY «под себя» на домене входа + выход и маршруты моста
        base = egames.profile_config(x['domain'], tag, priv, sid)
        out = {k: v for k, v in outbound.items() if not k.startswith('_')}
        cfg = {**base, 'outbounds': [out, *base['outbounds']],
               'routing': {'rules': bridge_rules(base['routing']['rules'], tag, {'BLOCK'}, bool(ss))}}
    elif vision:
        cfg = vision_config(x['base_cfg'], x['domain'], tag, outbound, ru_direct=bool(ss))
    else:
        cfg = profile_config(x['listen'], x['sni'], priv, sid, outbound)
        if ss:
            cfg['routing']['rules'] = bridge_rules(cfg['routing']['rules'], tag, {'BLOCK'}, True)
    profiles = panel.call('GET', '/api/config-profiles')['configProfiles']
    old = next((p for p in profiles if p['name'] == name), None)
    if ss:
        via = f'выход на {ss["address"]}:{ss["port"]} (Shadowsocks, как в eGames), российские сайты — напрямую со входа'
    else:
        via = (f'выход на {outbound["settings"]["vnext"][0]["address"]}:{outbound["settings"]["vnext"][0]["port"]} '
               f'({outbound["streamSettings"]["network"]}, {outbound["streamSettings"]["security"]})')
    if old:
        # переустановка: inbound входа (SNI, ключи — их могли поменять в панели руками) не трогаем, меняем выход
        oc = old.get('config') or {}
        if x.get('mode', 'xray') == 'xray' and any(i.get('tag') == tag for i in oc.get('inbounds', [])):
            outs = [o for o in oc.get('outbounds', []) if o.get('tag') != OUT_TAG]
            routing = oc.get('routing') or cfg['routing']
            block = {o.get('tag') for o in outs if o.get('protocol') == 'blackhole'}
            cfg = {**oc, 'outbounds': [cfg['outbounds'][0], *outs],
                   'routing': {**routing, 'rules': bridge_rules(routing.get('rules') or [], tag, block, bool(ss))}}
        panel.call('PATCH', '/api/config-profiles', {'uuid': old['uuid'], 'config': cfg})
        prof = next(p for p in panel.call('GET', '/api/config-profiles')['configProfiles'] if p['uuid'] == old['uuid'])
        log(f'панель: профиль «{name}» обновлён — {via}')
    else:
        prof = panel.call('POST', '/api/config-profiles', {'name': name, 'config': cfg})
        log(f'панель: профиль «{name}» создан — ' + (f'Vision TLS на {x["domain"]}' if vision else f'REALITY «под себя» на {x["domain"]}, как у eGames'
                                                    if eg else f'REALITY с SNI {x["sni"]}') + f', {via}')
    inbound = next(i for i in prof['inbounds'] if i['tag'] == tag)
    node = {'ip': x['ip'], 'domain': x['domain'] if vision or eg else (x.get('address') or x['ip']), 'name': (x.get('remark') or name)[:30],
            'country': x.get('country') or '', 'remark': x.get('remark') or name}
    panel.attach(node, prof, [inbound], squads, int(x.get('node_port') or 2222), log, tls=vision)
    entry = next((n for n in panel.call('GET', '/api/nodes') if n['address'] == node['domain']), None)
    hosts_now = [h['uuid'] for h in panel.call('GET', '/api/hosts')
                 if (h.get('inbound') or {}).get('configProfileInboundUuid') == inbound['uuid']]
    sni_now = x['domain'] if vision else ''
    if not vision:
        cur_in = next((i for i in (cfg.get('inbounds') or []) if i.get('tag') == tag), {})
        sni_now = (((cur_in.get('streamSettings') or {}).get('realitySettings') or {}).get('serverNames') or [x.get('sni') or x.get('domain')])[0]
        for hu in hosts_now:  # SNI хоста — как у REALITY (панель подставила бы адрес-IP)
            panel.call('PATCH', '/api/hosts', {'uuid': hu, 'sni': sni_now, 'fingerprint': 'chrome'})
    return {'profile': prof['uuid'], 'inbound': inbound['uuid'], 'node': entry['uuid'] if entry else '',
            'hosts': hosts_now, 'squads': squads, 'user': user_key(user), 'user_id': user.get('id'), 'ip': x['ip'], 'ss': ss, 'username': bridge_username(pnode['uuid']),
            'reality': {'private': priv, 'sid': sid}, 'sni': sni_now}


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
    if made.get('ss') and not user_in_use:
        try:
            remove_ss_exit(panel, made['ss'], log)
        except engine.StepError as e:
            log(f'панель: Shadowsocks-вход моста с выхода убрать не удалось: {e}')
    if not user_in_use and made.get('ip'):
        try:
            from . import plugins
            plugins.protect_bridge(panel, made['ip'], made.get('user_id'), log, remove=True)
        except engine.StepError as e:
            log(f'Torrent Blocker: исключение входа убрать не удалось: {e}')
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



# ───────── вход на встроенном скрипте (Vision TLS) ─────────
def vision_prep_cmd() -> str:
    """Перед встроенным скриптом: убрать то, что ставил сам бот под другие виды моста (HAProxy, контейнер REALITY).
    Установку встроенного скрипта (.nuvrion-managed) не трогаем — повторный запуск её продолжит."""
    return (f': {MARK}; set +e; systemctl disable --now bobr-relay >/dev/null 2>&1; rm -f /etc/systemd/system/bobr-relay.service; '
            'systemctl daemon-reload; '
            f'if [ -e {NODE_DIR} ] && [ ! -f {NODE_DIR}/.nuvrion-managed ]; then '
            f'[ -f {NODE_DIR}/docker-compose.yml ] && (cd {NODE_DIR} && docker compose down >/dev/null 2>&1); '
            f'docker rm -f remnanode >/dev/null 2>&1; mv {NODE_DIR} "{NODE_DIR}.removed-$(date +%Y%m%d-%H%M%S)"; fi; '
            'if command -v ufw >/dev/null; then for n in $(ufw status numbered 2>/dev/null | grep -E "bobr-bridge|bobr-relay" | '
            "sed -E 's/^\\[ *([0-9]+)\\].*/\\1/' | sort -rn); do yes | ufw delete \"$n\" >/dev/null 2>&1; done; fi; echo XB_PREP_OK")


def vision_base(r: engine.Remote, domain: str) -> dict:
    """Профиль, который сделал встроенный скрипт на входе, и общая ссылка на сертификат (как у обычных нод)."""
    import json
    _, out = r.run(f'ln -sfn live/{domain} {engine.CERT_LINK} && test -s {engine.CERT_LINK}/fullchain.pem && echo LINK_OK')
    if 'LINK_OK' not in out:
        raise engine.StepError(f'не нашёл сертификат /etc/letsencrypt/live/{domain} — скрипт выпустил его?')
    rc, raw = r.run('cat /opt/remnanode/vision-config-profile.json')
    if rc != 0 or '{' not in raw:
        raise engine.StepError('не удалось прочитать vision-config-profile.json на входе')
    return json.loads(raw[raw.index('{'):])
