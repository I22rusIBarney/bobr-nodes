"""Новый роутинг для хостов: присланные Xray JSON (по одному или архивом) → в Xray JSON-шаблоны выбранных хостов.

Хост в панели уже настроен (адрес, профиль, inbound) — меняется только содержимое его шаблона. Какой файл к какому
хосту, бот предлагает сам: у каждого конфига свой «главный» сервер (первый прокси-outbound), и в текущем шаблоне
хоста он тот же. Перед заменой старое содержимое сохраняется — изменение можно откатить."""
from __future__ import annotations

import io
import json
import re
import zipfile

from .engine import StepError

MAX_FILE = 8 * 1024 * 1024        # один загружаемый файл (архив или JSON)
MAX_UNZIPPED = 40 * 1024 * 1024   # всё содержимое архива — защита от «zip-бомбы»
MAX_FILES = 60
PROXY = ('vless', 'vmess', 'trojan', 'shadowsocks', 'hysteria', 'hysteria2', 'wireguard', 'socks', 'http')
KEEP = ('remnawave',)  # служебные настройки панели в шаблоне (например, injectHosts) — переносим из старого
SUP = str.maketrans('⁰¹²³⁴⁵⁶⁷⁸⁹', '0123456789')


def _zip_name(info: zipfile.ZipInfo) -> str:
    """Имя файла в архиве: без флага UTF-8 zipfile читает его как cp437 — архивы с Mac/Windows часто в UTF-8 или cp866."""
    if info.flag_bits & 0x800:
        return info.filename
    raw = info.filename.encode('cp437')
    for enc in ('utf-8', 'cp866'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return info.filename


def _config(name: str, data: bytes) -> dict:
    try:
        tj = json.loads(data.decode('utf-8-sig'))
    except (ValueError, UnicodeDecodeError) as e:
        raise StepError(f'{name}: это не JSON ({e})') from None
    if not isinstance(tj, dict):
        raise StepError(f'{name}: Xray JSON — это объект {{ … }}')
    if tj.get('kind') == 'bobr-nodes/hosts':
        raise StepError(f'{name}: это набор хостов — его загружают в «Перенос хостов»')
    if not isinstance(tj.get('outbounds'), list) or not tj['outbounds']:
        raise StepError(f'{name}: в конфиге нет outbounds — это не Xray JSON')
    return tj


def read_files(name: str, data: bytes) -> list[dict]:
    """Загруженный файл → [{name, json}]: .json — один конфиг, .zip — все .json внутри (служебные файлы macOS пропускаем)."""
    if len(data) > MAX_FILE:
        raise StepError(f'{name}: файл больше {MAX_FILE // 1024 // 1024} МБ')
    if data[:4] != b'PK\x03\x04':
        return [{'name': name, 'json': _config(name, data)}]
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise StepError(f'{name}: архив повреждён') from None
    items = [i for i in zf.infolist() if not i.is_dir()]
    items = [i for i in items if not (_zip_name(i).startswith('__MACOSX/') or _zip_name(i).rsplit('/', 1)[-1].startswith('._'))]
    jsons = [i for i in items if _zip_name(i).lower().endswith('.json')]
    if not jsons:
        raise StepError(f'{name}: в архиве нет .json')
    if len(jsons) > MAX_FILES or sum(i.file_size for i in jsons) > MAX_UNZIPPED:
        raise StepError(f'{name}: в архиве слишком много или слишком большие файлы')
    out = []
    for i in jsons:
        short = _zip_name(i).rsplit('/', 1)[-1]
        out.append({'name': short, 'json': _config(short, zf.read(i))})
    return out


def sort_key(name: str) -> list:
    """Порядок как у людей: 1, 2, 3¹, 3², 4¹, 10 (надстрочные цифры — подномер)."""
    s = re.sub(r'([⁰¹²³⁴⁵⁶⁷⁸⁹]+)', lambda m: '.' + m.group(1).translate(SUP), name)
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r'(\d+)', s)]


def main_of(tj) -> dict | None:
    """Главный сервер конфига: первый прокси-outbound — {tag, address, port, sni}."""
    for o in (tj or {}).get('outbounds') or []:
        if not isinstance(o, dict) or o.get('protocol') not in PROXY:
            continue
        st = o.get('settings') or {}
        srv = ((st.get('vnext') or st.get('servers') or [{}]) or [{}])[0] or {}
        rs = (o.get('streamSettings') or {})
        sni = (rs.get('realitySettings') or {}).get('serverName') or (rs.get('tlsSettings') or {}).get('serverName') or ''
        return {'tag': o.get('tag') or '', 'address': srv.get('address') or '', 'port': srv.get('port'), 'sni': sni}
    return None


def summary(tj: dict) -> dict:
    r = tj.get('routing') or {}
    return {'main': main_of(tj), 'outbounds': len(tj.get('outbounds') or []), 'rules': len(r.get('rules') or []),
            'balancers': len(r.get('balancers') or []), 'size': len(json.dumps(tj))}


def suggest(files: list[dict], hosts: list[dict], tpls: dict[str, dict]) -> list[str | None]:
    """Какой хост к какому файлу: тот, у чьего текущего шаблона тот же главный сервер (тег и адрес; иначе — адрес).
    Хост с неоднозначным совпадением не предлагаем: выберет человек."""
    cand = []
    for h in hosts:
        t = tpls.get(h.get('xrayJsonTemplateUuid') or '')
        m = main_of(t.get('templateJson')) if t else None
        if m:
            cand.append((h['uuid'], m))
    out, used = [], set()
    for f in files:
        m = main_of(f['json']) or {}
        pick = None
        for same in (lambda c: c['tag'] == m.get('tag') and c['address'] == m.get('address'), lambda c: c['address'] == m.get('address')):
            hit = [u for u, c in cand if m.get('address') and same(c) and u not in used]
            if len(hit) == 1:
                pick = hit[0]
                break
            if len(hit) > 1:
                break
        if pick:
            used.add(pick)
        out.append(pick)
    return out


def apply(panel, pairs: list[tuple[str, str, dict]], log) -> tuple[dict, list[dict]]:
    """pairs: [(uuid хоста, имя файла, Xray JSON)] → новое содержимое шаблонов хостов. Всё проверяем до первого изменения.
    Возвращает итог и то, что было до замены (для отката): [{uuid, name, json}]."""
    hosts = {h['uuid']: h for h in panel.call('GET', '/api/hosts')}
    tpls = {t['uuid']: t for t in panel.templates()}
    plan, seen = [], {}
    for hu, fname, tj in pairs:
        h = hosts.get(hu)
        if not h:
            raise StepError(f'{fname}: хоста больше нет в панели — обновите список')
        tu = h.get('xrayJsonTemplateUuid')
        t = tpls.get(tu or '')
        if not t or t.get('templateType') != 'XRAY_JSON':
            raise StepError(f'у хоста «{h.get("remark", "")}» нет Xray JSON-шаблона — назначьте его в панели, потом обновляйте')
        if tu in seen:
            raise StepError(f'«{seen[tu]}» и «{h.get("remark", "")}» используют один шаблон «{t["name"]}» — '
                            'новый конфиг поменял бы оба хоста. Выберите для этих файлов хосты с разными шаблонами')
        seen[tu] = h.get('remark', '')
        old = t.get('templateJson') if isinstance(t.get('templateJson'), dict) else {}
        new = {**tj, **{k: old[k] for k in KEEP if k in old and k not in tj}}
        plan.append((h, t, old, new, fname))
    res, backup = {'updated': 0, 'same': 0}, []
    for h, t, old, new, fname in plan:
        if old == new:
            res['same'] += 1
            log(f'«{h.get("remark", "")}» ← {fname}: шаблон «{t["name"]}» уже такой же')
            continue
        backup.append({'uuid': t['uuid'], 'name': t['name'], 'json': old})
        panel.call('PATCH', '/api/subscription-templates', {'uuid': t['uuid'], 'templateJson': new})
        res['updated'] += 1
        log(f'«{h.get("remark", "")}» ← {fname}: шаблон «{t["name"]}» обновлён')
    return res, backup


def restore(panel, backup: list[dict], log) -> int:
    """Вернуть шаблоны, какими они были до последнего обновления."""
    n = 0
    for b in backup:
        panel.call('PATCH', '/api/subscription-templates', {'uuid': b['uuid'], 'templateJson': b['json']})
        log(f'шаблон «{b["name"]}» возвращён')
        n += 1
    return n
