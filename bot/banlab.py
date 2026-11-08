"""Разбор блокировок: чья сеть у IP (ASN, анонс — по RIPEstat), проверка соседей по /24 из России и данные для нейросети.

Соседей проверяем дважды: сначала с сервера бота (из-за рубежа) находим живые адреса той же /24, потом стучимся к ним
с чекеров в России. Если из-за рубежа сосед открыт, а из России нет — его режут тоже: блокировка по подсети."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import random
import re
import time

import httpx

RIPE = 'https://stat.ripe.net/data'
INFO_TTL = 30 * 86400
PROBE_PORTS = (443, 22, 80)
NEIGH_SCAN = 32     # сколько случайных адресов /24 пробуем из-за рубежа
NEIGH_KEEP = 8      # сколько живых соседей проверяем из России
_RNG = random.SystemRandom()


def ip_info(ip: str, get=None) -> dict:
    """{'asn': 'AS24940', 'holder': 'HETZNER-AS', 'prefix': '1.2.0.0/16'} по RIPEstat; пусто, если не узнали."""
    get = get or (lambda url, params: httpx.get(url, params=params, timeout=15).json())
    p = {'resource': ip, 'sourceapp': 'bobr-nodes'}
    net = (get(f'{RIPE}/network-info/data.json', p).get('data') or {})
    asn = str((net.get('asns') or [''])[0])
    holder = ''
    if asn:
        holder = str((get(f'{RIPE}/as-overview/data.json', {**p, 'resource': 'AS' + asn}).get('data') or {}).get('holder') or '')
    return {'asn': 'AS' + asn if asn else '', 'holder': holder[:120], 'prefix': str(net.get('prefix') or '')}


def cached_info(store, ip: str, fetch: bool = True) -> dict | None:
    """ip_info с кэшем в настройках на 30 дней; fetch=False — только из кэша."""
    if not ip or not is_public(ip):
        return None
    key = 'ipinfo:' + ip
    try:
        hit = json.loads(store.get(key) or 'null')
    except ValueError:
        hit = None
    if hit and time.time() - hit.get('ts', 0) < INFO_TTL or not fetch:
        return hit
    try:
        info = {**ip_info(ip), 'ts': int(time.time())}
    except (httpx.HTTPError, ValueError, AttributeError):
        return hit
    store.set(key, json.dumps(info))
    return info


def is_public(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.version == 4 and a.is_global


def net24(ip: str) -> str:
    return str(ipaddress.ip_network(ip + '/24', strict=False)) if is_public(ip) else ''


def neighbors(ip: str, k: int = NEIGH_SCAN, rng=_RNG) -> list[str]:
    """Случайные адреса той же /24, кроме самой ноды, сети и широковещательного."""
    hosts = [str(h) for h in ipaddress.ip_network(ip + '/24', strict=False).hosts() if str(h) != ip and not str(h).endswith('.255')]
    return rng.sample(hosts, min(k, len(hosts)))


async def alive_abroad(ips: list[str], tcp_open, ports=PROBE_PORTS, keep: int = NEIGH_KEEP) -> dict[str, list[int]]:
    """Какие соседи отвечают с сервера бота: {ip: [открытые порты]}, не больше keep адресов."""
    async def one(ip):
        res = await asyncio.gather(*[tcp_open(ip, p, 3) for p in ports])
        return ip, [p for p, ok in zip(ports, res) if ok]
    found = await asyncio.gather(*[one(ip) for ip in ips])
    return dict([x for x in found if x[1]][:keep])


def ru_probe_cmd(targets: dict[str, list[int]]) -> str:
    """Команда для чекера в России: по строке «OK ip port» или «NO ip port» на каждую пару."""
    parts = []
    for ip, ports in targets.items():
        if not re.fullmatch(r'\d{1,3}(\.\d{1,3}){3}', ip):
            continue
        for p in ports:
            p = int(p)
            parts.append(f"(timeout 4 bash -c '</dev/tcp/{ip}/{p}' 2>/dev/null && echo 'OK {ip} {p}' || echo 'NO {ip} {p}') &")
    return ' '.join(parts) + ' wait'


def parse_probe(out: str) -> dict[str, bool]:
    """Ответ чекера → {ip: открыт ли хоть один порт из России}."""
    res: dict[str, bool] = {}
    for m in re.finditer(r'^(OK|NO) (\S+) (\d+)', out or '', re.M):
        res[m.group(2)] = res.get(m.group(2), False) or m.group(1) == 'OK'
    return res


def verdict(abroad: dict[str, list[int]], ru: dict[str, bool]) -> dict:
    """subnet — режут и соседей (бан подсети или хостера); single — соседи открываются, значит узнали именно ноду;
    mixed — по-разному; unknown — живых соседей мало, сказать нельзя."""
    seen = [ip for ip in abroad if ip in ru]
    blocked = [ip for ip in seen if not ru[ip]]
    n = len(seen)
    kind = 'unknown' if n < 2 else 'subnet' if len(blocked) >= .7 * n else 'single' if len(blocked) <= .3 * n else 'mixed'
    return {'kind': kind, 'checked': n, 'blocked': len(blocked), 'ts': int(time.time())}


NEIGH_TEXT = {'subnet': 'режут и соседей по /24 — похоже на бан подсети или хостера',
              'single': 'соседи по /24 открываются — заблокировали именно эту ноду',
              'mixed': 'часть соседей по /24 тоже недоступна', 'unknown': 'живых соседей по /24 слишком мало для вывода'}


def clusters(blocks: list[dict], key: str, window: float = 86400) -> list[dict]:
    """Блокировки, случившиеся почти одновременно (в пределах window) в одной сети key (net24/asn): признак бана пачкой."""
    out = []
    by: dict[str, list[dict]] = {}
    for b in sorted(blocks, key=lambda b: b['start']):
        if b.get(key):
            by.setdefault(b[key], []).append(b)
    for k, bs in by.items():
        run = [bs[0]]
        for b in bs[1:] + [None]:
            if b is not None and b['start'] - run[-1]['start'] <= window:
                run.append(b)
                continue
            if len({x['addr'] for x in run}) >= 2:
                out.append({'key': k, 'n': len({x['addr'] for x in run}), 'start': int(run[0]['start']),
                            'nodes': sorted({x.get('name') or x['addr'] for x in run})[:10]})
            if b is not None:
                run = [b]
    return sorted(out, key=lambda c: -c['start'])


AI_SYSTEM = """Ты аналитик сети VPN-нод (Xray VLESS, панель Remnawave), которые блокирует ТСПУ в России: IP ноды
перестаёт открываться из России целиком. Тебе дают статистику бота: частоту блокировок на ноду в месяц в разных
разрезах (хостер, ASN, подсеть /24, локация, тариф, порт для клиентов, TLS-отпечаток, «уникальные настройки»,
есть ли на ноде Hysteria2 — второй протокол на UDP того же IP),
пачки блокировок в одной сети за сутки, проверки соседей по /24 из России (subnet — соседей режут тоже, single — только ноду)
и последние блокировки со снимком ноды (пользователи онлайн, возраст).

Найди, что сильнее всего связано с блокировками. Правила:
- Опирайся только на данные. Мало наблюдений (меньше ~5 блокировок или меньше месяца нод в разрезе) — так и скажи, не выдумывай.
- Отличай корреляцию от причины; оценивай уверенность: high / medium / low.
- Возможные причины: бан подсети/хостера, детект по трафику (одинаковые параметры, нагрузка), утечка подписки (баны пачкой
  разных хостеров в один день), нестандартные порты. Проверь каждую на данных.
- Советы — конкретные и выполнимые в боте: сменить хостера/локацию, включить уникальные настройки, распределить пользователей и т.п.
Отвечай по-русски. Ответ — строго JSON: {"summary": "2–4 предложения", "findings": [{"factor": "…", "evidence": "цифры из данных",
"confidence": "high|medium|low"}], "actions": ["…"], "need_data": ["чего не хватает для уверенного вывода"]}"""


def ai_input(stats: dict, recent: list[dict], question: str = '') -> str:
    """Сжатые данные для нейросети: таблицы без лишних полей и до 40 последних блокировок."""
    keep = ('label', 'nodes', 'months', 'blocks', 'rate', 'life_days', 'hours')
    tables = {k: [{f: r.get(f) for f in keep} for r in v[:15]] for k, v in stats.items() if isinstance(v, list) and k != 'clusters'}
    data = {'days': stats.get('days'), 'total_blocks': stats.get('total'), 'tables': tables,
            'clusters': stats.get('clusters', [])[:15], 'neighbors': stats.get('neighbors', {}),
            'recent': [{k: b.get(k) for k in ('start', 'name', 'holder', 'asn', 'net24', 'location', 'config', 'port', 'fp',
                                             'stealth', 'users', 'born', 'cause', 'neigh', 'hy2', 'end', 'fix')} for b in recent[-40:]]}
    q = question.strip()[:1000] or 'Что приводит к блокировкам нод и что сделать в первую очередь?'
    return q + '\n\n<<<DATA\n' + json.dumps(data, ensure_ascii=False, default=str) + '\nDATA>>>'


def ai_clean(raw: dict) -> dict:
    conf = {'high', 'medium', 'low'}
    return {'summary': str(raw.get('summary') or '')[:2000],
            'findings': [{'factor': str(f.get('factor') or '')[:300], 'evidence': str(f.get('evidence') or '')[:600],
                          'confidence': f.get('confidence') if f.get('confidence') in conf else 'low'}
                         for f in (raw.get('findings') or []) if isinstance(f, dict)][:10],
            'actions': [str(x)[:500] for x in (raw.get('actions') or [])][:10],
            'need_data': [str(x)[:500] for x in (raw.get('need_data') or [])][:8]}


def collect(store, ps: list[dict], days: int) -> tuple[dict, list[dict], list[str]]:
    """Статистика блокировок групп ps за days дней: (итоги, блокировки, IP нод без данных о сети в кэше).
    Ноды — купленные ботом; порт и отпечаток — с их хостов из кэша панели."""
    from . import blockstats as bs
    from . import services as svc
    from .pcache import CACHE
    now = time.time()
    nodes: dict[tuple, dict] = {}
    missing = []
    for p in ps:
        hosts = sorted(CACHE.peek(p, 'hosts') or [], key=lambda x: bool(x.get('isHidden')))
        for j in store.done_jobs(p['id']):
            d = j['data']
            if not d.get('ip') or d.get('kind') in svc.SERVICE_KINDS or d.get('removed'):
                continue
            info = cached_info(store, d['ip'], fetch=False)
            if info is None and is_public(d['ip']):
                missing.append(d['ip'])
            names = {d['ip'], d.get('domain')} - {None, ''}
            h = next((x for x in hosts if x.get('address') in names), {})
            nodes[(p['id'], d['ip'])] = {
                'location': d.get('location') or '', 'loc_name': d.get('loc_name') or '', 'flag': d.get('flag') or '',
                'config': svc.config_label(store, d), 'born': j['updated_at'],
                'hoster': store.get(f'hoster:{p["id"]}:{d.get("domain") or d["ip"]}'),
                'asn': (info or {}).get('asn') or None, 'holder': (info or {}).get('holder') or '', 'net24': net24(d['ip']) or None,
                'port': h.get('port'), 'fp': h.get('fingerprint') or None, 'stealth': int(bool((d.get('host') or {}).get('stealth'))),
                'hy2': int(bool(d.get('hy2')))}
    blocks = store.blocks(now - days * 86400, [p['id'] for p in ps])
    return bs.summarize(blocks, list(nodes.values()), now, days), blocks, list(dict.fromkeys(missing))


async def ai_report(store, gemini, ps: list[dict], days: int = 90, question: str = '') -> dict:
    """Разбор нейросетью: данные — collect, ответ сохраняется в настройке banlab_ai (его показывает статистика)."""
    res, blocks, missing = await asyncio.to_thread(collect, store, ps, days)
    if missing:  # перед разбором — дособрать сети, иначе разрезы по хостерам неполные
        for ip in missing[:100]:
            await asyncio.to_thread(cached_info, store, ip)
        res, blocks, _ = await asyncio.to_thread(collect, store, ps, days)
    raw = await gemini.ask(AI_SYSTEM, [{'role': 'user', 'text': ai_input(res, blocks, question)}])
    out = {**ai_clean(raw), 'ts': int(time.time()), 'question': question[:1000], 'blocks': res['total']}
    store.set('banlab_ai', json.dumps(out, ensure_ascii=False))
    return out


def ai_text(r: dict) -> str:
    """Отчёт нейросети для Telegram (HTML)."""
    import html
    mark = {'high': '🔴', 'medium': '🟠', 'low': '⚪'}
    lines = ['🧠 <b>Разбор блокировок</b>', html.escape(r['summary'])]
    if r['findings']:
        lines.append('')
        lines += [f"{mark[f['confidence']]} <b>{html.escape(f['factor'])}</b> — {html.escape(f['evidence'])}" for f in r['findings'][:6]]
    if r['actions']:
        lines += ['', '<b>Что сделать:</b>'] + ['• ' + html.escape(a) for a in r['actions'][:5]]
    if r['need_data']:
        lines += ['', '<i>Для уверенности не хватает: ' + html.escape('; '.join(r['need_data'][:3])) + '</i>']
    return '\n'.join(lines)[:4000]
