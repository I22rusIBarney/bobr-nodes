"""Нода «как у eGames» (remnawave-reverse-proxy): VLESS REALITY с маскировкой «под себя» (selfsteal).

Что ставит скрипт eGames на ноду — то же ставит бот, только без меню и вопросов:
- контейнер remnawave/node и рядом NGINX, который слушает unix-сокет /dev/shm/nginx.sock (TLS + proxy_protocol)
  и отдаёт сайт-заглушку на домене ноды;
- профиль в панели: inbound VLESS TCP REALITY на 443, target — этот сокет, serverNames — домен ноды; в интернет —
  freedom, блок приватных адресов и торрентов. Для ТСПУ это обычный сайт на своём домене с настоящим сертификатом.

Сертификат Let's Encrypt — через порт 80 (certbot standalone, продление — его же таймером, после продления NGINX
перечитывает сертификат). Токен Cloudflare на сервер не кладём: A-запись делает бот.
"""
from __future__ import annotations

import base64
import re
import secrets
import shlex

from . import engine

MARK = 'BOBR_EGAMES'
NODE_DIR = '/opt/remnanode'
SOCK = '/dev/shm/nginx.sock'
# сайты-заглушки — те же наборы шаблонов, что берёт скрипт eGames (закреплённые коммиты)
TEMPLATES = ['https://github.com/eGamesAPI/simple-web-templates/archive/6b7690b37af85c35117b6da65a36b1b3e0503477.zip',
             'https://github.com/distillium/sni-templates/archive/99fafe48b41060f1225592b472994af6c21b057d.zip']


def tag_for(ip: str, bridge: bool = False) -> str:
    """Тег inbound: уникальный в панели (теги не повторяются между профилями)."""
    return ('BRIDGE_EG_' if bridge else 'STEAL_') + re.sub(r'[^0-9A-Za-z]', '_', ip)


def profile_name(ip: str) -> str:
    return re.sub(r'[^A-Za-z0-9_ -]', '-', f'eGames {ip}')[:30]


def inbound(domain: str, tag: str, private_key: str, short_id: str) -> dict:
    return {'tag': tag, 'port': 443, 'listen': '0.0.0.0', 'protocol': 'vless',
            'settings': {'clients': [], 'decryption': 'none'},
            'sniffing': {'enabled': True, 'destOverride': ['http', 'tls', 'quic']},
            'streamSettings': {'network': 'tcp', 'security': 'reality',
                               'realitySettings': {'show': False, 'xver': 1, 'dest': SOCK, 'spiderX': '',
                                                   'minClientVer': '0.0.0', 'shortIds': [short_id],
                                                   'privateKey': private_key, 'serverNames': [domain]}}}


def profile_config(domain: str, tag: str, private_key: str, short_id: str) -> dict:
    """Профиль ноды точь-в-точь как у eGames (create_config_profile)."""
    return {'log': {'loglevel': 'warning'},
            'dns': {'queryStrategy': 'UseIPv4', 'servers': [{'address': 'https://dns.google/dns-query', 'skipFallback': False}]},
            'inbounds': [inbound(domain, tag, private_key, short_id)],
            'outbounds': [{'tag': 'DIRECT', 'protocol': 'freedom'}, {'tag': 'BLOCK', 'protocol': 'blackhole'}],
            'routing': {'rules': [{'type': 'field', 'ip': ['geoip:private'], 'outboundTag': 'BLOCK'},
                                  {'type': 'field', 'protocol': ['bittorrent'], 'outboundTag': 'BLOCK'}]}}


def keys(keep: dict | None = None) -> dict:
    from .xbridge import reality_keys
    keep = keep or {}
    return {'private': keep.get('private') or reality_keys()[0], 'sid': keep.get('sid') or secrets.token_hex(8)}


# ───────── сервер ─────────
def compose_text(secret: str, node_port: int) -> str:
    return f'''x-common: &common
  ulimits:
    nofile:
      soft: 1048576
      hard: 1048576
  restart: always
  logging:
    driver: json-file
    options:
      max-size: 100m
      max-file: "5"

services:
  remnawave-nginx:
    image: nginx:1.30
    container_name: remnawave-nginx
    hostname: remnawave-nginx
    <<: *common
    network_mode: host
    volumes:
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - /etc/letsencrypt:/etc/letsencrypt:ro
      - /dev/shm:/dev/shm:rw
      - /var/www/html:/var/www/html:ro
    command: sh -c 'rm -f {SOCK} && exec nginx -g "daemon off;"'

  remnanode:
    image: remnawave/node:latest
    container_name: remnanode
    hostname: remnanode
    <<: *common
    network_mode: host
    cap_add:
      - NET_ADMIN
    environment:
      - NODE_PORT={int(node_port)}
      - SECRET_KEY={secret}
    volumes:
      - /dev/shm:/dev/shm:rw
      - /var/log/remnanode:/var/log/remnanode
'''


def nginx_conf(domain: str) -> str:
    live = f'/etc/letsencrypt/live/{domain}'
    return f'''server_names_hash_bucket_size 64;

ssl_protocols TLSv1.2 TLSv1.3;
ssl_ecdh_curve X25519:prime256v1:secp384r1;
ssl_prefer_server_ciphers on;
ssl_session_timeout 1d;
ssl_session_cache shared:MozSSL:10m;
ssl_session_tickets off;

server {{
    server_name {domain};
    listen unix:{SOCK} ssl proxy_protocol;
    http2 on;

    ssl_certificate "{live}/fullchain.pem";
    ssl_certificate_key "{live}/privkey.pem";
    ssl_trusted_certificate "{live}/fullchain.pem";

    root /var/www/html;
    index index.html;
    add_header X-Robots-Tag "noindex, nofollow, noarchive, nosnippet, noimageindex" always;
}}

server {{
    listen unix:{SOCK} ssl proxy_protocol default_server;
    server_name _;
    ssl_reject_handshake on;
    return 444;
}}
'''


FALLBACK_HTML = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,'
                 'initial-scale=1"><title>Welcome</title><style>body{font-family:system-ui,sans-serif;max-width:720px;margin:12vh auto;'
                 'padding:0 20px;color:#222}h1{font-weight:600}</style></head><body><h1>Welcome</h1><p>This site is under '
                 'construction. Please check back later.</p></body></html>')


def install_cmd(domain: str, email: str, secret: str, node_port: int, panel_ips: list[str], extra_ports: list[int] = ()) -> str:
    """Сервер ноды «как у eGames». Старую ноду (бота, Nuvrion, eGames) в /opt/remnanode останавливает и откладывает."""
    b64 = lambda t: base64.b64encode(t.encode()).decode()  # noqa: E731
    d = shlex.quote(domain)
    ips = ' '.join(shlex.quote(i) for i in panel_ips if engine.is_ip(i))
    tpl = ' '.join(shlex.quote(u) for u in TEMPLATES)
    hook = '#!/bin/sh\ndocker exec remnawave-nginx nginx -s reload >/dev/null 2>&1 || true\n'
    return (
        f': {MARK}; set +e; export DEBIAN_FRONTEND=noninteractive; '
        # старое: проброс HAProxy, нода в /opt/remnanode (её compose — в сторону, чтобы можно было вернуть)
        'systemctl disable --now bobr-relay >/dev/null 2>&1; '
        f'if [ -f {NODE_DIR}/docker-compose.yml ] && ! grep -q remnawave-nginx {NODE_DIR}/docker-compose.yml; then '
        f'(cd {NODE_DIR} && docker compose down >/dev/null 2>&1); mv {NODE_DIR} "{NODE_DIR}.before-egames-$(date +%Y%m%d-%H%M%S)"; '
        'echo "EG_MOVED старая нода отложена"; fi; '
        'for p in 443 80; do if ss -Hltnp "sport = :$p" | grep -vE \'"(rw-core|xray|nginx|docker-proxy)"\' | grep -q .; then '
        'echo "EG_BUSY $p"; ss -Hltnp "sport = :$p" | head -2; exit 0; fi; done; '
        '(apt-get update -qq; apt-get install -y -qq curl ca-certificates unzip certbot ufw >/dev/null) >/dev/null 2>&1; '
        'if ! command -v docker >/dev/null; then curl -fsSL https://get.docker.com | sh >/dev/null 2>&1 || '
        'apt-get install -y -qq docker.io docker-compose-v2 >/dev/null 2>&1; fi; '
        'command -v docker >/dev/null || { echo EG_NODOCKER; exit 0; }; systemctl enable --now docker >/dev/null 2>&1; '
        'docker compose version >/dev/null 2>&1 || apt-get install -y -qq docker-compose-plugin docker-compose-v2 >/dev/null 2>&1; '
        # сертификат через порт 80
        'UFW=0; command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active" && UFW=1; '
        '[ $UFW = 1 ] && ufw allow 80/tcp comment bobr-egames >/dev/null; '
        f'if [ ! -s /etc/letsencrypt/live/{d}/fullchain.pem ]; then '
        f'certbot certonly --standalone --non-interactive --agree-tos -m {shlex.quote(email)} -d {d} --preferred-challenges http '
        '>/tmp/eg-certbot.log 2>&1 || { echo EG_CERTFAIL; tail -5 /tmp/eg-certbot.log; exit 0; }; fi; '
        f'mkdir -p /etc/letsencrypt/renewal-hooks/deploy && echo {b64(hook)} | base64 -d > /etc/letsencrypt/renewal-hooks/deploy/bobr-egames.sh '
        '&& chmod +x /etc/letsencrypt/renewal-hooks/deploy/bobr-egames.sh; '
        # сайт-заглушка: случайный шаблон из наборов eGames, не скачалось — простая страница
        'mkdir -p /var/www/html; if [ ! -s /var/www/html/index.html ]; then T=$(mktemp -d); '
        f'for u in $(printf "%s\\n" {tpl} | shuf); do curl -fsSL --max-time 60 -o "$T/t.zip" "$u" && unzip -q -o "$T/t.zip" -d "$T/x" && break; done; '
        'S=$(find "$T/x" -mindepth 2 -maxdepth 3 -name index.html 2>/dev/null | shuf -n1); '
        'if [ -n "$S" ]; then cp -r "$(dirname "$S")"/. /var/www/html/; else '
        f'echo {b64(FALLBACK_HTML)} | base64 -d > /var/www/html/index.html; fi; rm -rf "$T"; fi; '
        f'mkdir -p {NODE_DIR} && cd {NODE_DIR} && echo {b64(compose_text(secret, node_port))} | base64 -d > docker-compose.yml '
        f'&& chmod 600 docker-compose.yml && echo {b64(nginx_conf(domain))} | base64 -d > nginx.conf; '
        'if [ $UFW = 1 ]; then ufw allow 443/tcp comment bobr-egames >/dev/null; '
        + ''.join(f'ufw allow {int(pt)} comment bobr-egames >/dev/null; ' for pt in extra_ports) +
        (f'for ip in {ips}; do ufw allow from "$ip" to any port {int(node_port)} proto tcp comment bobr-egames >/dev/null; done; '
         if ips else f'ufw allow {int(node_port)}/tcp comment bobr-egames >/dev/null; ') + 'fi; '
        'timeout 600 docker compose pull -q >/dev/null 2>&1; docker compose up -d >/dev/null 2>&1 || '
        '{ echo EG_UPFAIL; docker compose up -d 2>&1 | tail -5; exit 0; }; sleep 4; '
        'U=$(docker ps --format "{{.Names}} {{.Status}}"); '
        'echo "$U" | grep -q "^remnanode Up" && echo "$U" | grep -q "^remnawave-nginx Up" && echo EG_OK || '
        '{ echo EG_DOWN; docker logs --tail 6 remnawave-nginx 2>&1; docker logs --tail 6 remnanode 2>&1; }')


def install(r: engine.Remote, domain: str, email: str, secret: str, node_port: int, panel_ips: list[str], log,
            extra_ports: list[int] = ()) -> None:
    log(f'ставлю ноду как в eGames на {domain}: Docker, сертификат, NGINX-заглушка, remnawave/node (до 5 минут)')
    rc, out = r.run(install_cmd(domain, email, secret, node_port, panel_ips, extra_ports), 1200)
    out = engine.strip_ansi(out)
    if 'EG_MOVED' in out:
        log('старая нода на сервере остановлена и отложена в /opt/remnanode.before-egames-…')
    if 'EG_BUSY' in out:
        raise engine.StepError('порт ' + out.split('EG_BUSY', 1)[1].strip()[:200] + ' на сервере занят другой программой')
    if 'EG_NODOCKER' in out:
        raise engine.StepError('не удалось поставить Docker')
    if 'EG_CERTFAIL' in out:
        raise engine.StepError("Let's Encrypt не выдал сертификат (нужен открытый порт 80 и A-запись на этот сервер): "
                               + out.split('EG_CERTFAIL', 1)[1].strip()[-300:])
    if 'EG_UPFAIL' in out or 'EG_DOWN' in out:
        raise engine.StepError('контейнеры не запустились: ' + out.strip()[-300:])
    if 'EG_OK' not in out:
        raise engine.StepError('нода не поставилась: ' + (out.strip()[-300:] or f'код {rc}'))
    log('NGINX-заглушка и remnawave/node запущены')


def check_cmd(domain: str) -> str:
    """Сайт-заглушка открывается через Xray (443 → REALITY → сокет NGINX): так проверяет и скрипт eGames."""
    return (f'curl -s --max-time 10 --resolve {shlex.quote(domain)}:443:127.0.0.1 https://{shlex.quote(domain)} | '
            'grep -qi "<html" && echo EG_SITE_OK || echo EG_SITE_NO')


# ───────── панель: выход (обычная нода) ─────────
def setup_exit(panel: engine.Panel, pnode: dict, ip: str, domain: str, node_port: int, keep: dict | None, log) -> dict:
    """Нода-выход на eGames: свой профиль, нода переводится на него, хост REALITY, inbound — в те же сквады,
    где были её прежние inbound'ы; прежние хосты ноды выключаются (сервер их больше не обслуживает)."""
    old_active = engine.active_inbounds(pnode)
    sqs = panel.call('GET', '/api/internal-squads')['internalSquads']
    squads = [q['uuid'] for q in sqs if any(i.get('uuid') in old_active for i in q.get('inbounds', []))
              and not q.get('name', '').startswith('Bridge SS')]
    if not squads:
        squads = [q['uuid'] for q in sqs if q.get('name') == 'Default-Squad'][:1] or [q['uuid'] for q in sqs][:1]
    k = keys(keep)
    tag = tag_for(ip)
    name = profile_name(ip)
    cfg = profile_config(domain, tag, k['private'], k['sid'])
    old = next((p for p in panel.call('GET', '/api/config-profiles')['configProfiles'] if p['name'] == name), None)
    if old:
        oc = old.get('config') or {}
        others = [i for i in oc.get('inbounds', []) if i.get('tag') != tag]   # Shadowsocks моста и т.п. — оставляем
        panel.call('PATCH', '/api/config-profiles', {'uuid': old['uuid'], 'config': {**cfg, 'inbounds': [*cfg['inbounds'], *others]}})
        prof = next(p for p in panel.call('GET', '/api/config-profiles')['configProfiles'] if p['uuid'] == old['uuid'])
        log(f'панель: профиль «{name}» обновлён')
    else:
        prof = panel.call('POST', '/api/config-profiles', {'name': name, 'config': cfg})
        log(f'панель: профиль «{name}» создан — REALITY на {domain}, как у eGames')
    inb = next(i for i in prof['inbounds'] if i['tag'] == tag)
    keep_on = [i for i in prof['inbounds'] if i['tag'] != tag and i['uuid'] in old_active]
    old_hosts = [h for h in panel.call('GET', '/api/hosts') if (h.get('inbound') or {}).get('configProfileInboundUuid') in old_active
                 and (h.get('inbound') or {}).get('configProfileUuid') != prof['uuid']]
    node = {'ip': ip, 'domain': domain, 'name': pnode['name'], 'remark': pnode['name'], 'country': pnode.get('countryCode') or ''}
    panel.attach(node, prof, [inb], squads, node_port, log, tls=False, also=keep_on)
    for h in panel.call('GET', '/api/hosts'):
        if (h.get('inbound') or {}).get('configProfileInboundUuid') == inb['uuid'] and h.get('address') == domain:
            panel.call('PATCH', '/api/hosts', {'uuid': h['uuid'], 'sni': domain, 'fingerprint': 'chrome'})
    for h in old_hosts:
        if not h.get('isDisabled'):
            try:
                panel.call('PATCH', '/api/hosts', {'uuid': h['uuid'], 'isDisabled': True})
                log(f'панель: прежний хост «{h.get("remark", "")}» выключен — сервер его больше не обслуживает')
            except engine.StepError as e:
                log(f'панель: прежний хост «{h.get("remark", "")}» выключить не удалось: {e}')
    return {'profile': prof['uuid'], 'inbound': inb['uuid'], 'reality': k}
