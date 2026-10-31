"""Вход через Россию: проброс порта со своего сервера в России на иностранную ноду.

На российском сервере нет ни Xray, ни ноды — только HAProxy (своя служба bobr-relay со своим конфигом, чужой
HAProxy на сервере не трогаем): он принимает TCP на порт входа и отдаёт его как есть на ноду. Шифрование — сквозное,
от клиента до иностранной ноды. В панели — копии хостов ноды с адресом российского сервера: клиенты подключаются к
российскому IP (это помогает, когда операторы пропускают только российские адреса), а нода и все её дополнения,
мониторинг и починка остаются обычными. Нода видит вместо адреса клиента адрес российского сервера.

Ноду лучше указывать доменом: HAProxy сам перечитывает A-запись, и после смены IP ноды проброс не ломается.
"""
from __future__ import annotations

import base64
import re
import shlex

from . import engine

UNIT = 'bobr-relay'
DIR = '/etc/bobr-relay'
MARK = 'BOBR_RELAY'  # метка команд — по ней их узнают тесты


def entries_of(relays: list[dict]) -> list[tuple[int, str, int]]:
    """(порт входа, адрес ноды, порт ноды) для всех входов одного сервера, по порту."""
    return sorted({(int(r['listen']), r['target'], int(r['target_port'])) for r in relays})


def haproxy_cfg(entries: list[tuple[int, str, int]]) -> str:
    out = ['# Bobr Nodes: вход через Россию. Файл пишет бот — правки вручную пропадут.',
           'global', '    maxconn 100000', '',
           'defaults', '    mode tcp', '    option splice-auto', '    timeout connect 10s',
           '    timeout client 1h', '    timeout server 1h', '    timeout tunnel 24h', '',
           # домен ноды перечитывается: после смены её IP проброс сам переходит на новый адрес
           'resolvers sys', '    parse-resolv-conf', '    hold valid 30s', '']
    for port, host, tport in entries:
        out += [f'frontend in_{port}', f'    bind 0.0.0.0:{port}', f'    default_backend to_{port}', '',
                f'backend to_{port}',
                f'    server node {host}:{tport}' + ('' if engine.is_ip(host) else ' resolvers sys init-addr libc,none'), '']
    return '\n'.join(out)


def unit_text() -> str:
    return ('[Unit]\nDescription=Bobr Nodes: вход через Россию (HAProxy)\n'
            'After=network-online.target\nWants=network-online.target\n'
            '[Service]\nType=notify\n'
            f'ExecStart=/bin/sh -c \'exec "$(command -v haproxy)" -Ws -f {DIR}/haproxy.cfg -p /run/{UNIT}.pid\'\n'
            'ExecReload=/bin/kill -USR2 $MAINPID\nRestart=always\nRestartSec=3\nLimitNOFILE=1048576\n'
            '[Install]\nWantedBy=multi-user.target\n')


def apply_cmd(entries: list[tuple[int, str, int]], closed: list[int] = ()) -> str:
    """Поставить HAProxy (если нет), записать конфиг и службу, открыть порты в ufw, проверить связь с нодами."""
    ports = ' '.join(str(p) for p, _, _ in entries)
    cfg = base64.b64encode(haproxy_cfg(entries).encode()).decode()
    unit = base64.b64encode(unit_text().encode()).decode()
    checks = ' '.join(shlex.quote(f'{h}:{tp}') for _, h, tp in entries)
    return (
        f': {MARK}; '
        # порт занят другой программой (наш HAProxy при переустановке — не помеха)
        f'for p in {ports}; do if ss -ltnpH "sport = :$p" 2>/dev/null | grep -v \'"haproxy"\' | grep -q .; then '
        'echo "RELAY_BUSY $p"; exit 0; fi; done; '
        'if ! command -v haproxy >/dev/null; then export DEBIAN_FRONTEND=noninteractive; '
        'apt-get update -qq >/dev/null 2>&1; apt-get install -y -qq haproxy >/dev/null 2>&1 || { echo RELAY_NOPKG; exit 0; }; '
        'systemctl disable --now haproxy >/dev/null 2>&1; fi; '  # штатная служба пакета нам не нужна
        f'mkdir -p {DIR} && echo {cfg} | base64 -d > {DIR}/haproxy.cfg.new && '
        f'if ! haproxy -c -q -f {DIR}/haproxy.cfg.new; then echo RELAY_BADCFG; exit 0; fi; '
        f'mv {DIR}/haproxy.cfg.new {DIR}/haproxy.cfg && echo {unit} | base64 -d > /etc/systemd/system/{UNIT}.service && '
        f'systemctl daemon-reload && systemctl enable -q {UNIT} && systemctl restart {UNIT}; sleep 2; '
        f'if ! systemctl is-active --quiet {UNIT}; then echo RELAY_DOWN; journalctl -u {UNIT} -n 5 --no-pager 2>&1 | tail -n 5; exit 0; fi; '
        'if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then '
        f'for p in {ports}; do ufw allow "$p/tcp" comment {UNIT} >/dev/null; done; '
        + (f'for p in {" ".join(map(str, closed))}; do ufw delete allow "$p/tcp" >/dev/null 2>&1; done; ' if closed else '') +
        'fi; '
        f'for t in {checks}; do h=${{t%:*}}; pt=${{t##*:}}; '
        'if timeout 6 bash -c "</dev/tcp/$h/$pt" 2>/dev/null; then echo "RELAY_TARGET_OK $t"; else echo "RELAY_TARGET_FAIL $t"; fi; done; '
        'echo RELAY_OK')


def remove_cmd(ports: list[int]) -> str:
    return (f': {MARK}; systemctl disable --now {UNIT} >/dev/null 2>&1; rm -f /etc/systemd/system/{UNIT}.service; '
            f'rm -rf {DIR}; systemctl daemon-reload; '
            'if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then '
            f'for p in {" ".join(map(str, ports)) or "0"}; do ufw delete allow "$p/tcp" >/dev/null 2>&1; done; fi; '
            'echo RELAY_REMOVED')


def apply(r: engine.Remote, relays: list[dict], log, closed: list[int] = ()) -> dict:
    """Конфиг для всех входов сервера. {'bad': [недоступные с сервера ноды]}; StepError — если не встало."""
    entries = entries_of(relays)
    rc, out = r.run(apply_cmd(entries, closed), 600)
    out = engine.strip_ansi(out)
    m = re.search(r'RELAY_BUSY (\d+)', out)
    if m:
        raise engine.StepError(f'порт {m.group(1)} на сервере уже занят другой программой — выберите другой')
    if 'RELAY_NOPKG' in out:
        raise engine.StepError('не удалось установить HAProxy (apt) — проверьте, что на сервере работает apt')
    if 'RELAY_BADCFG' in out:
        raise engine.StepError('HAProxy не принял конфиг: ' + out.strip()[-300:])
    if 'RELAY_DOWN' in out:
        raise engine.StepError('служба bobr-relay не запустилась: ' + out.split('RELAY_DOWN', 1)[1].strip()[-300:])
    if 'RELAY_OK' not in out:
        raise engine.StepError('проброс не настроен: ' + (out.strip()[-300:] or f'код {rc}'))
    bad = re.findall(r'RELAY_TARGET_FAIL (\S+)', out)
    log('HAProxy на сервере: входы ' + ', '.join(f'{p} → {h}:{tp}' for p, h, tp in entries))
    for t in bad:
        log(f'с сервера не открывается {t} — проверьте, что нода работает и порт не закрыт')
    return {'bad': bad}


def remove(r: engine.Remote, ports: list[int], log) -> None:
    rc, out = r.run(remove_cmd(ports), 180)
    if 'RELAY_REMOVED' not in out:
        raise engine.StepError('не удалось убрать проброс: ' + (engine.strip_ansi(out).strip()[-300:] or f'код {rc}'))
    log('проброс на сервере убран')


# ── хосты в панели ──
def main_hosts(hosts: list[dict]) -> list[dict]:
    """Хосты ноды, которые есть смысл копировать: без запасного входа через Cloudflare CDN."""
    return [h for h in hosts if not str(h.get('address') or '').startswith(engine.CDN_PREFIX)]


def target_port_of(hosts: list[dict]) -> int:
    hs = main_hosts(hosts)
    return int(hs[0].get('port') or 443) if hs else 443


def clone_hosts(panel: engine.Panel, hosts: list[dict], address: str, port: int, target_port: int,
                remark: str, log) -> list[str]:
    """Копии хостов ноды (того же порта) с адресом российского сервера. SNI остаётся доменом ноды — сертификат его."""
    made = []
    src = [h for h in main_hosts(hosts) if int(h.get('port') or 443) == int(target_port)]
    if not src:
        raise engine.StepError(f'у ноды нет хостов на порту {target_port}, копировать нечего')
    for i, h in enumerate(src, 1):
        body = {k: h[k] for k in engine.HOST_FIELDS if h.get(k) is not None}
        body['inbound'] = {k: (h.get('inbound') or {}).get(k) for k in ('configProfileUuid', 'configProfileInboundUuid')}
        if h.get('xrayJsonTemplateUuid'):
            body['xrayJsonTemplateUuid'] = h['xrayJsonTemplateUuid']
        old = str(h.get('address') or '')
        if not body.get('sni') and old and not engine.is_ip(old):
            body['sni'] = old  # адрес теперь российский — SNI оставляем доменом ноды
        body.update({'address': address, 'port': int(port),
                     'remark': (remark if len(src) == 1 else f'{remark} {i}')[:100]})
        body.pop('overrideSniFromAddress', None)
        made.append(panel.call('POST', '/api/hosts', body)['uuid'])
    log(f'панель: хостов через Россию — {len(made)}')
    return made


def delete_hosts(panel: engine.Panel, uuids: list[str], log) -> None:
    have = {h['uuid'] for h in panel.call('GET', '/api/hosts')}
    n = 0
    for u in uuids:
        if u in have:
            panel.call('DELETE', f'/api/hosts/{u}')
            n += 1
    if n:
        log(f'панель: хостов через Россию удалено — {n}')
