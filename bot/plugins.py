"""Плагины нод Remnawave: Torrent Blocker, Ingress Filter, Egress Filter.

У ноды ровно один активный плагин (activePluginUuid), поэтому все три функции — секции одной записи плагина,
как в remnawave-reverse-proxy («Reverse Node Plugins»): настроенное там бот подхватывает, и наоборот.
Конфиг работает только на нодах, к которым плагин привязан, — поэтому после каждого сохранения бот
привязывает его ко всем включённым нодам группы, а каждую новую ноду — сразу при создании.
"""
from __future__ import annotations

import ipaddress
import re
from collections import Counter

import httpx

from . import engine

NAME = 'Reverse Node Plugins'
KNOWN = (NAME, 'Torrent Blocker', 'Ingress Filter', 'Egress Filter')
SECTIONS = ('torrentBlocker', 'ingressFilter', 'egressFilter')
DEFAULTS = {
    'torrentBlocker': {'enabled': False, 'blockDuration': 3600, 'ignoreLists': {'ip': [], 'userId': []}},
    'ingressFilter': {'enabled': False, 'blockedIps': []},
    'egressFilter': {'enabled': False, 'blockedIps': [], 'blockedPorts': []},
}

_GH = 'https://raw.githubusercontent.com/OpenFilters/internet-scanners/main/cidr/'
# Входящие: подсети сканеров. Списки большие и обновляются — бот скачивает их сам.
INGRESS_PRESETS = {
    'ru': {'title': 'Сканеры РФ (Скипа)', 'sub': 'сканеры, по которым находят и блокируют VPN',
           'urls': ['https://raw.githubusercontent.com/tread-lightly/CyberOK_Skipa_ips/main/lists/skipa_cidr.txt']},
    'classic': {'title': 'Поисковики устройств', 'sub': 'Censys, Shodan, Palo Alto, Rapid7 и другие',
                'urls': [_GH + f'{x}_v4.txt' for x in ('censys', 'shodan', 'paloaltonetworks', 'shadowserver', 'driftnet',
                                                        'onyphe', 'zoomeye', 'leakix', 'rapid7', 'internetmeasurementresearch')]},
    'fofa': {'title': 'FOFA и Quake', 'sub': 'китайские поисковики устройств',
             'urls': [_GH + 'fofa_v4.txt', _GH + 'quake_v4.txt']},
}
# Исходящие: 169.254.0.0/16 не блокируем — у облаков там DNS и метаданные, нода осталась бы без DNS.
EGRESS_PRESETS = {
    'private': {'title': 'Частные сети', 'sub': '10/8, 172.16/12, 192.168/16, 100.64/10, fc00::/7',
                'ips': ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10', 'fc00::/7']},
    'mail': {'title': 'Почтовые порты', 'sub': '25, 465, 587 — против рассылки спама', 'ports': [25, 465, 587]},
}
CIDR4 = re.compile(r'^((25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(/([89]|[12]\d|3[0-2]))?$')


class PluginError(Exception):
    pass


# ───────────────────────────── состояние ─────────────────────────────

def enabled_nodes(nodes: list[dict]) -> list[dict]:
    return [n for n in nodes if isinstance(n, dict) and not n.get('isDisabled')]


def group_plugin(nodes: list[dict], exclude: str = '') -> str | None:
    """Плагин группы: тот, что стоит у большинства включённых нод (кроме exclude). Нет ни у одной — None."""
    c = Counter(n.get('activePluginUuid') for n in enabled_nodes(nodes) if n.get('uuid') != exclude and n.get('activePluginUuid'))
    return c.most_common(1)[0][0] if c else None


def pick(plugins: list[dict], nodes: list[dict]) -> dict | None:
    """Наш плагин: стоящий на нодах группы → «Reverse Node Plugins» → старые имена по функциям."""
    by_uuid = {p['uuid']: p for p in plugins}
    used = group_plugin(nodes)
    if used in by_uuid:
        return by_uuid[used]
    for name in KNOWN:
        hit = next((p for p in plugins if p.get('name') == name), None)
        if hit:
            return hit
    return None


def full_config(cfg: dict | None) -> dict:
    """Конфиг со всеми тремя секциями (недостающие — выключенные по умолчанию). Чужие ключи сохраняем."""
    out = dict(cfg or {})
    for k, d in DEFAULTS.items():
        sec = {**d, **(out.get(k) or {})}
        if k == 'torrentBlocker':
            sec['ignoreLists'] = {**d['ignoreLists'], **(sec.get('ignoreLists') or {})}
        out[k] = sec
    return out


def is_on(sec: dict) -> bool:
    return sec.get('enabled') in (True, 'true')


def state(panel: engine.Panel) -> dict:
    """Плагин группы, его конфиг и сколько включённых нод под ним."""
    nodes = panel.call('GET', '/api/nodes')
    plugins = (panel.call('GET', '/api/node-plugins') or {}).get('nodePlugins') or []
    p = pick(plugins, nodes)
    cfg = {}
    if p:  # список отдаёт pluginConfig = null, настоящий конфиг — только по uuid
        cfg = (panel.call('GET', f'/api/node-plugins/{p["uuid"]}') or {}).get('pluginConfig') or {}
    on = enabled_nodes(nodes)
    return {'uuid': p['uuid'] if p else None, 'name': p['name'] if p else NAME, 'config': full_config(cfg),
            'total': len(on), 'bound': sum(1 for n in on if p and n.get('activePluginUuid') == p['uuid']),
            'foreign': [n['name'] for n in on if n.get('activePluginUuid') and (not p or n['activePluginUuid'] != p['uuid'])],
            'others': [x['name'] for x in plugins if not p or x['uuid'] != p['uuid']]}


# ───────────────────────────── изменения ─────────────────────────────

def bind(panel: engine.Panel, plugin_uuid: str, uuids: list[str]) -> None:
    """Привязать плагин к нодам: одним запросом, панель сразу отправит им конфиг."""
    if uuids:
        panel.call('POST', '/api/nodes/bulk-actions/update', {'uuids': uuids, 'fields': {'activePluginUuid': plugin_uuid}})


def save(panel: engine.Panel, config: dict, take_foreign: bool = False) -> dict:
    """Сохранить конфиг (создав плагин, если его нет), разослать нодам и привязать ко всем включённым нодам.
    Ноды с другим плагином перепривязываются только при take_foreign."""
    st = state(panel)
    uuid = st['uuid']
    if not uuid:
        uuid = (panel.call('POST', '/api/node-plugins', {'name': NAME}) or {}).get('uuid')
        if not uuid:
            raise PluginError('панель не создала плагин')
    panel.call('PATCH', '/api/node-plugins', {'uuid': uuid, 'pluginConfig': full_config(config)})
    panel.call('POST', '/api/node-plugins/actions/sync', {'uuid': uuid})
    nodes = panel.call('GET', '/api/nodes')
    bind(panel, uuid, [n['uuid'] for n in enabled_nodes(nodes)
                       if n.get('activePluginUuid') != uuid and (take_foreign or not n.get('activePluginUuid'))])
    return state(panel)


def adopt(panel: engine.Panel, node_uuid: str, nodes: list[dict], log) -> None:
    """Новой ноде — плагин, который стоит у остальных нод группы. Ошибка не мешает установке ноды."""
    plugin = group_plugin(nodes, exclude=node_uuid)
    if not plugin:
        return
    try:
        bind(panel, plugin, [node_uuid])
        log('панель: нода под плагином группы (блокировка торрентов, фильтры)')
    except engine.StepError as e:
        log(f'⚠️ плагин группы к ноде не привязался: {e}')


def protect_bridge(panel: engine.Panel, ip: str, user_id, log, remove: bool = False) -> bool:
    """Мост: весь трафик входа приходит на выход с одного IP под служебным пользователем. Если Torrent Blocker
    выхода забанит этот IP (кто-то из клиентов скачал торрент), на час встанет весь мост. Поэтому IP входа и
    служебный пользователь — в исключениях плагина; торренты ловит плагин на самом входе, по настоящим IP клиентов.
    remove — убрать из исключений (мост удалён). Возвращает, поменялось ли что-то."""
    st = state(panel)
    if not st['uuid']:
        return False
    cfg = full_config(st['config'])
    ign = cfg['torrentBlocker']['ignoreLists']
    ips, uids = list(ign.get('ip') or []), list(ign.get('userId') or [])
    if remove:
        new_ips = [x for x in ips if x != ip]
        new_uids = [x for x in uids if str(x) != str(user_id)] if user_id is not None else uids
    else:
        new_ips = ips + ([ip] if ip and ip not in ips else [])
        new_uids = uids + ([user_id] if user_id is not None and str(user_id) not in map(str, uids) else [])
    if (new_ips, new_uids) == (ips, uids):
        return False
    cfg['torrentBlocker']['ignoreLists'] = {**ign, 'ip': new_ips, 'userId': new_uids}
    panel.call('PATCH', '/api/node-plugins', {'uuid': st['uuid'], 'pluginConfig': cfg})
    panel.call('POST', '/api/node-plugins/actions/sync', {'uuid': st['uuid']})
    if not remove:
        try:
            unblock(panel, ip)  # если уже забанил — снять сразу
        except engine.StepError:
            pass
    log(f'Torrent Blocker: IP входа {ip} и служебный пользователь моста ' + ('убраны из исключений' if remove else 'в исключениях, бан снят'))
    return True


def unblock(panel: engine.Panel, ip: str) -> None:
    panel.call('POST', '/api/node-plugins/executor',
               {'command': {'command': 'unblockIps', 'ips': [ip]}, 'targetNodes': {'target': 'allNodes'}})


def stats(panel: engine.Panel) -> dict:
    """Срабатывания Torrent Blocker: итоги и последние записи."""
    s = panel.call('GET', '/api/node-plugins/torrent-blocker/stats') or {}
    try:
        r = panel.call('GET', '/api/node-plugins/torrent-blocker?start=0&size=10') or {}
    except engine.StepError:
        r = {}
    st = s.get('stats') or {}
    return {'total': st.get('totalReports', 0), 'day': st.get('reportsLast24Hours', 0), 'users': st.get('distinctUsers', 0),
            'top_users': [{'name': x.get('username'), 'n': x.get('total')} for x in (s.get('topUsers') or [])[:5]],
            'recent': [{'at': x.get('createdAt'), 'user': (x.get('user') or {}).get('username'),
                        'ip': ((x.get('report') or {}).get('actionReport') or {}).get('ip'), 'node': (x.get('node') or {}).get('name')}
                       for x in (r.get('records') or [])[:10]]}


# ───────────────────────────── списки и пресеты ─────────────────────────────

def clean_ips(raw, v6: bool = False) -> list[str]:
    """IPv4 или подсети (для входящих), для исходящих — ещё IPv6. Мусор — ValueError с примером."""
    items = raw.split() if isinstance(raw, str) else list(raw or [])
    out = []
    for x in (str(i).strip().strip(',;') for i in items):
        if not x:
            continue
        if CIDR4.match(x):
            out.append(x)
            continue
        if v6:
            try:
                net = ipaddress.ip_network(x, strict=False)
            except ValueError:
                net = None
            if net is not None and net.version == 6:
                out.append(x)
                continue
        raise ValueError(f'«{x[:40]}» — не IP-адрес и не подсеть (пример: 203.0.113.0/24)')
    return sorted(set(out), key=out.index)


def clean_ports(raw) -> list[int]:
    items = raw.split() if isinstance(raw, str) else list(raw or [])
    out = []
    for x in (str(i).strip().strip(',;') for i in items):
        if not x:
            continue
        if not re.fullmatch(r'[1-9]\d{0,4}', x) or int(x) > 65535:
            raise ValueError(f'«{x[:12]}» — не порт (1–65535)')
        out.append(int(x))
    return sorted(set(out))


def fetch_preset(pid: str, client: httpx.Client | None = None) -> list[str]:
    """Свежий список подсетей пресета входящих. Пустой файл или ошибка любого из списков — PluginError."""
    pre = INGRESS_PRESETS.get(pid)
    if not pre:
        raise PluginError('нет такого пресета')
    c = client or httpx.Client(timeout=30, follow_redirects=True)
    out: list[str] = []
    try:
        for url in pre['urls']:
            try:
                r = c.get(url)
                r.raise_for_status()
            except httpx.HTTPError as e:
                raise PluginError(f'список {url.rsplit("/", 1)[-1]} не скачался: {e}') from e
            got = [x.strip() for x in r.text.splitlines() if CIDR4.match(x.strip())]
            if not got:
                raise PluginError(f'список {url.rsplit("/", 1)[-1]} пуст')
            out += got
    finally:
        if client is None:
            c.close()
    return sorted(set(out))


def swap(current: list, old: list, new: list, protected=()) -> list:
    """Заменить в списке прежние записи пресета новыми. Остальное (ручные записи, другие пресеты — protected)
    не трогаем, даже если оно совпадает с записью прежней версии пресета."""
    drop = set(old) - set(protected)
    keep = [x for x in current if x not in drop]
    seen = set(keep)
    return keep + [x for x in new if x not in seen]


def apply_preset(panel: engine.Panel, mem: dict, kind: str, pid: str, on: bool, fresh: list[str] | None = None) -> dict:
    """Включить/обновить (on) или убрать пресет. mem — какие записи добавлены пресетами ({'ingress': {...},
    'egress': {...}}), меняется на месте. fresh — скачанный список для пресета входящих. Возвращает state()."""
    presets = INGRESS_PRESETS if kind == 'ingress' else EGRESS_PRESETS
    cfg = state(panel)['config']
    mine = mem.setdefault(kind, {})
    old = mine.get(pid) or []
    if kind == 'ingress':
        sec = cfg['ingressFilter']
        others = {x for k, v in mine.items() if k != pid for x in v}
        manual = set(sec['blockedIps']) - {x for v in mine.values() for x in v}
        new = (fresh or []) if on else []
        sec['blockedIps'] = swap(sec['blockedIps'], old, new, protected=others | manual)
    elif pid == 'mail':
        sec = cfg['egressFilter']
        new = presets[pid]['ports'] if on else []
        sec['blockedPorts'] = sorted(set(swap(sec['blockedPorts'], old, new)))
    else:
        sec = cfg['egressFilter']
        new = presets[pid]['ips'] if on else []
        sec['blockedIps'] = swap(sec['blockedIps'], old, new)
    if on:
        sec['enabled'] = True  # пресет без включённого фильтра ничего не делает
        mine[pid] = new
    else:
        mine.pop(pid, None)
    if not sec.get('blockedIps') and not sec.get('blockedPorts'):
        sec['enabled'] = False
    return save(panel, cfg)
