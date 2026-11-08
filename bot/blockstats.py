"""Статистика блокировок: где и как поставленные ноды живут дольше. Чистые функции — без сети и базы."""
from __future__ import annotations

import statistics

OUTAGE = ('down', 'node_down', 'server_off', 'panel')  # сервер или нода упали — это не блокировка из России
MONTH = 30 * 86400


def summarize(blocks: list[dict], nodes: list[dict], now: float, days: int) -> dict:
    """blocks — записи истории (store.blocks); nodes — ноды бота: [{location, loc_name, flag, config, born}].
    По локациям и конфигурациям: сколько нод, нод-месяцев наблюдения, блокировок, блокировок на ноду в месяц (rate),
    сколько нода в среднем живёт до блокировки, сколько длится блокировка и что помогало."""
    since = now - days * 86400
    # хостер — подписанный вручную, иначе владелец сети по ASN
    blocks = [{**b, 'hoster': b.get('hoster') or b.get('holder') or '', '_all': 'all'} for b in blocks]
    nodes = [{**n, 'hoster': n.get('hoster') or n.get('holder') or '', '_all': 'all'} for n in nodes]

    def group(key: str, skip_empty: bool = False) -> list[dict]:
        out: dict[str, dict] = {}

        def g(x: dict) -> dict | None:
            k = '' if x.get(key) is None else str(x.get(key))
            if skip_empty and k == '':
                return None
            if k not in out:
                out[k] = {'key': k, 'label': (x.get('loc_name') or k.upper()) if key == 'location' else LABELS.get(key, str)(x) if key in LABELS else k,
                          'flag': x.get('flag') or '' if key == 'location' else '', 'nodes': 0, 'months': 0.0,
                          'blocks': 0, 'outages': 0, 'subnet': 0, 'fixes': {}, '_dur': [], '_life': []}
            elif key == 'location' and not out[k]['flag'] and x.get('flag'):
                out[k].update(flag=x['flag'], label=x.get('loc_name') or out[k]['label'])
            return out[k]
        for n in nodes:
            r = g(n)
            if r is None:
                continue
            r['nodes'] += 1
            r['months'] += max(0.0, now - max(n.get('born') or since, since)) / MONTH
        for b in blocks:
            r = g(b)
            if r is None:
                continue
            if b.get('cause') in OUTAGE:
                r['outages'] += 1
                continue
            r['blocks'] += 1
            r['subnet'] += b.get('neigh') == 'subnet'  # проверка соседей: резали всю /24
            if b.get('end'):
                r['_dur'].append(b['end'] - b['start'])
                r['fixes'][b.get('fix') or 'self'] = r['fixes'].get(b.get('fix') or 'self', 0) + 1
            if b.get('born') and b['start'] >= b['born']:
                r['_life'].append(b['start'] - b['born'])
        res = []
        for r in out.values():
            dur, life = r.pop('_dur'), r.pop('_life')
            r['months'] = round(r['months'], 1)
            r['rate'] = round(r['blocks'] / r['months'], 2) if r['months'] >= 0.5 else None
            r['hours'] = round(statistics.median(dur) / 3600, 1) if dur else None
            r['life_days'] = round(statistics.median(life) / 86400, 1) if life else None
            res.append(r)
        res.sort(key=lambda r: (-r['nodes'], -r['blocks'], r['label']))
        return res
    from .banlab import clusters
    real = [b for b in blocks if b.get('cause') not in OUTAGE]
    neigh: dict[str, int] = {}
    for b in real:
        if b.get('neigh'):
            neigh[b['neigh']] = neigh.get(b['neigh'], 0) + 1
    return {'days': days, 'locations': group('location'), 'configs': group('config'),
            'hosters': group('hoster', True), 'asns': group('asn', True), 'nets': group('net24', True),
            'ports': group('port', True), 'fps': group('fp', True), 'stealth': group('stealth', True), 'hy2': group('hy2', True),
            'clusters': clusters(real, 'net24') + clusters(real, 'asn'), 'neighbors': neigh, 'total': len(real),
            'overall': (group('_all') or [None])[0]}


LABELS = {'asn': lambda x: f"{x.get('asn')} {x.get('holder') or ''}".strip(),
          'port': lambda x: f"порт {x.get('port')}",
          'fp': lambda x: f"отпечаток {x.get('fp')}",
          'stealth': lambda x: 'уникальные настройки' if str(x.get('stealth')) == '1' else 'обычные настройки',
          'hy2': lambda x: 'с Hysteria2' if str(x.get('hy2')) == '1' else 'без Hysteria2'}


def advice(stats: dict, location: str) -> dict | None:
    """Совет при покупке: если в этой локации блокируют заметно чаще, чем в лучшей (с данными хотя бы за месяц),
    — {here, best}. Иначе None: советовать не на чем."""
    locs = [r for r in stats['locations'] if r['rate'] is not None and r['months'] >= 1 and r['key']]
    here = next((r for r in locs if r['key'] == location), None)
    if not here or len(locs) < 2:
        return None
    best = min(locs, key=lambda r: r['rate'])
    # совет и тогда, когда частота не хуже, но здесь уже не раз резали подсеть целиком (бан хостера)
    if best['key'] == location or here['rate'] < max(best['rate'] * 2, best['rate'] + 0.5) and here.get('subnet', 0) < 2:
        return None
    return {'here': here, 'best': best}


COST_KINDS = {'new': 'Новые ноды', 'replace': 'Замены заблокированных', 'scale': 'Докупка при нагрузке', 'reip': 'Смены IP'}


def costs(jobs: list[dict], hoster_of=None) -> dict:
    """Разовые траты бота по задачам: покупки серверов (новые ноды, замены, докупка) и смены IP.
    Покупка считается, если сервер куплен (есть server_id и он не был уже существующим) — даже если установка потом упала.
    hoster_of(job) → хостер заблокированной ноды: во что обошлись блокировки (замены и смены IP) у каждого хостера."""
    out = {k: {'kind': k, 'label': v, 'kop': 0, 'n': 0} for k, v in COST_KINDS.items()}
    items, by_host = [], {}
    for j in jobs:
        d = j['data']
        found = []
        if d.get('kind') == 'reip':
            found.append((d.get('charged_kop'), 'reip'))
        elif not d.get('kind') and d.get('server_id') and not d.get('existing'):
            found.append((d.get('price_kop'), 'replace' if d.get('monitor') else 'scale' if d.get('autoscale') else 'new'))
        if d.get('precheck_kop'):  # IP нового сервера пришёл заблокированным — смена до установки
            found.append((d['precheck_kop'], 'reip'))
        for kop, kind in found:
            if not kop:
                continue
            out[kind]['kop'] += int(kop)
            out[kind]['n'] += 1
            items.append({'ts': j['created_at'], 'kind': kind, 'kop': int(kop), 'name': d.get('domain') or d.get('name') or '', 'pid': j['profile_id'], 'job': j['id']})
            if kind in ('replace', 'reip') and hoster_of is not None:
                who = hoster_of(j) or 'не известен'
                x = by_host.setdefault(who, {'label': who, 'kop': 0, 'n': 0})
                x['kop'] += int(kop)
                x['n'] += 1
    return {'total_kop': sum(x['kop'] for x in out.values()), 'kinds': [x for x in out.values() if x['n']], 'items': items[::-1][:50],
            'hosters': sorted(by_host.values(), key=lambda x: -x['kop'])}
