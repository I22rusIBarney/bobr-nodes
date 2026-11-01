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


def probe_cmd(items: list[tuple[str, str, int, str]]) -> str:
    """Проверка звеньев моста с этого сервера: TCP и TLS-рукопожатие с SNI домена ноды.
    tls: ok — сертификат проверен; cert — рукопожатие прошло, сертификат не проверился; stall — соединение
    есть, а ответа нет (так режет ТСПУ); fail — сброс или ошибка."""
    out = [f': {MARK}; ']
    for label, host, port, sni in items:
        h, p_, sn = shlex.quote(host), int(port), shlex.quote(sni or host)
        out.append(
            f'if timeout 6 bash -c "</dev/tcp/{host}/{p_}" 2>/dev/null; then t=ok; else t=fail; fi; '
            f'o=$(timeout 10 openssl s_client -connect {h}:{p_} -servername {sn} </dev/null 2>&1); '
            'if echo "$o" | grep -q "Verify return code: 0"; then s=ok; '
            'elif echo "$o" | grep -qE "Server certificate|BEGIN CERTIFICATE"; then s=cert; '
            'elif echo "$o" | grep -q "CONNECTED" && ! echo "$o" | grep -qiE "errno|reset|alert|error"; then s=stall; '
            'else s=fail; fi; '
            f'echo "PROBE {label} t=$t s=$s"; ')
    out.append('command -v openssl >/dev/null || echo PROBE_NO_OPENSSL; true')
    return ''.join(out)


def parse_probe(out: str) -> dict:
    res = {}
    for label, t, s in re.findall(r'PROBE (\S+) t=(\w+) s=(\w+)', engine.strip_ansi(out)):
        res[label] = {'tcp': t, 'tls': s}
    if 'PROBE_NO_OPENSSL' in out:
        res['_no_openssl'] = True
    return res


def verdict(o: dict) -> list[str]:
    """Что значит результат проверки — простыми словами, от первого звена к последнему."""
    good = ('ok', 'cert')
    e, b, rus = o['from_entry'] or {}, o['from_bot'] or {}, [c for c in o['checkers'] if 'entry' in c]
    if e.get('error'):
        return [f'Бот не смог зайти на сервер входа: {e["error"]}']
    if e.get('tcp') != 'ok':
        return ['С сервера входа не открывается порт ноды — нода выключена, порт закрыт или фаервол ноды не пускает IP входа.']
    if e.get('tls') not in good:
        return ['Порт ноды с сервера входа открывается, но нода не отвечает по TLS. Так бывает, когда на ноде фильтр по IP '
                '(плагин Ingress Filter или Torrent Blocker заблокировал IP входа) или на этом порту не Xray.']
    if b.get('tls') not in good:
        return ['Нода отвечает входу, но через сам вход TLS не проходит даже из-за рубежа — проблема в пробросе на входе '
                '(порт, облачный фаервол хостера входа). Нажмите «Переустановить».']
    if not rus:
        return ['Вход и нода в порядке из-за рубежа. Чекеров в России нет — проверить путь из России нечем.']
    tcp_bad = [c for c in rus if c['entry'].get('tcp') != 'ok']
    tls_bad = [c for c in rus if c['entry'].get('tcp') == 'ok' and c['entry'].get('tls') not in good]
    node_bad = [c for c in rus if (c.get('node') or {}).get('tls') not in good]
    n = len(rus)
    if len(tcp_bad) == n:
        return ['Из России не открывается даже порт входа — IP входа недоступен из России (заблокирован или режется хостер). '
                'Нужен другой сервер входа.']
    if len(tls_bad) + len(tcp_bad) == n and o.get('mode') == 'xray':
        return [f'Из России порт входа открывается, но TLS с SNI {o.get("entry_sni")} не проходит — этот SNI у ваших '
                'провайдеров режут. Переделайте мост с другим SNI (другой популярный сайт).']
    if len(tls_bad) + len(tcp_bad) == n:
        msg = ['Из России порт входа открывается, но TLS-рукопожатие с доменом ноды не проходит. Значит, режут не IP, а SNI '
               f'— домен {o["sni"] or "ноды"} попал под фильтр. Мост с тем же доменом не поможет: нужен новый домен ноды '
               '(лучше в другой зоне) или REALITY с чужим SNI.']
        if len(node_bad) == n:
            msg.append('Напрямую к ноде из России — то же самое.')
        return msg
    if tcp_bad or tls_bad:
        return [f'Из России проходит у {n - len(tcp_bad) - len(tls_bad)} из {n} чекеров — у разных провайдеров по-разному.']
    return ['Из России вход открывается и TLS проходит — мост работает. Если клиент не подключается: обновите подписку и '
            'выберите хост моста (адрес входа), а не прямой хост ноды.'
            + (' Нода входа должна быть на связи с панелью — это видно в списке нод.' if o.get('mode') == 'xray' else '')]


def convert_cmd(domain: str) -> str:
    """Сервер с нодой → под мост: остановить ноду (Xray освобождает 443) и всё, что ставил установщик и дополнения.
    Ничего не удаляем вслепую: каталог ноды переезжает в /opt/remnanode.bridge-<время> (откатить — переименовать
    обратно и docker compose up -d), фаервол, fail2ban и защита SSH остаются."""
    dom = shlex.quote(domain) if domain else "''"
    return (
        f': {MARK}; set +e; ts=$(date +%Y%m%d-%H%M%S); '
        'if [ -f /opt/remnanode/docker-compose.yml ]; then '
        'docker compose --project-directory /opt/remnanode -f /opt/remnanode/docker-compose.yml down --remove-orphans >/dev/null 2>&1; fi; '
        'docker rm -f remnanode >/dev/null 2>&1; '
        'for u in nuvrion-decoy nuvrion-pokehabitat nuvrion-acme-cleanup nuvrion-two-way-ping nuvrion-traffic-control '
        'vps-psiphon wg-quick@warp; do systemctl disable --now "$u" >/dev/null 2>&1; done; '
        'for f in /etc/systemd/system/bnodes-port-*.service; do [ -e "$f" ] || continue; u=$(basename "$f" .service); '
        'systemctl disable --now "$u" >/dev/null 2>&1; rm -f "$f"; done; systemctl daemon-reload; '
        'rm -f /etc/letsencrypt/renewal-hooks/pre/90-nuvrion-vision /etc/letsencrypt/renewal-hooks/post/90-nuvrion-vision '
        '/etc/letsencrypt/renewal-hooks/deploy/90-nuvrion-vision; '
        f'[ -n {dom} ] && certbot delete --non-interactive --cert-name {dom} >/dev/null 2>&1; '
        '[ -e /opt/remnanode ] && mv /opt/remnanode "/opt/remnanode.bridge-$ts"; '
        # Xray должен отпустить порты — проверяем 443
        'for i in 1 2 3 4 5 6 7 8 9 10; do ss -Hltn "sport = :443" | grep -q . || break; sleep 1; done; '
        'ss -Hltn "sport = :443" | grep -q . && echo CONVERT_BUSY || echo CONVERT_OK')


def convert(r: engine.Remote, domain: str, log) -> None:
    rc, out = r.run(convert_cmd(domain), 300)
    if 'CONVERT_OK' in out:
        log('нода на сервере остановлена, её файлы — в /opt/remnanode.bridge-<время>')
        return
    if 'CONVERT_BUSY' in out:
        raise engine.StepError('после остановки ноды порт 443 всё ещё занят — посмотрите сервер вручную (ss -ltnp)')
    raise engine.StepError('не удалось остановить ноду на сервере: ' + engine.strip_ansi(out).strip()[-200:])


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
