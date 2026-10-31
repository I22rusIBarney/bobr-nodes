"""Развёртывание — общее для чата и веб-админки: каталог bcloud, цены, домены, создание задач."""
from __future__ import annotations

import asyncio
import math
import re
import secrets
import string

from . import engine, naming
from .bcloud import BcloudError
from .runner import Runner, bcloud_from, buyer_of, cf_token_of
from .store import Store

PERIOD_LABEL = {'1m': '1 мес', '3m': '3 мес', '6m': '6 мес', '1y': '1 год'}


def period_price(month_kop: int, months: int, pct: int) -> int:
    """Как в bcloud: месяц × N со скидкой за срок, вверх до рубля."""
    if months <= 1:
        return month_kop
    return math.ceil(month_kop * months * (100 - max(0, pct)) / 100 / 100) * 100


def spec_text(o: dict) -> str:
    return f'{o["cpu"]} vCPU · {o["ram"]} ГБ · {o["disk"]} ГБ'


def gen_password() -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = ''.join(secrets.choice(alphabet) for _ in range(20))
        if any(c.isdigit() for c in pw) and any(c.isupper() for c in pw) and any(c.islower() for c in pw):
            return pw


def bot_jobs_by_ip(store: Store, pid: int) -> dict[str, dict]:
    """IP и домен → данные последней удачной задачи покупки (домен, IP, пароль root, сервер bcloud) этой группы.
    Ноды регистрируются по домену, старые — по IP, поэтому ключи — оба."""
    out = {}
    for j in sorted(store.jobs(5000, include_hidden=True), key=lambda x: x['id']):
        d = j['data']
        if (j['profile_id'] == pid and d.get('ip') and d.get('kind') not in ('node', 'reip') and j['status'] == 'done'
                and not d.get('removed')):
            d = {**d, '_job': j['id']}
            out[d['ip']] = d
            if d.get('domain'):
                out[d['domain']] = d
    return out


def node_ssh(store: Store, runner: Runner, pid: int, uuid: str, ip: str) -> dict:
    """Доступ для задачи: сохранённый, иначе — пароль из задачи покупки."""
    saved = runner.ssh_of(pid, uuid)
    if saved:
        return saved
    d = bot_jobs_by_ip(store, pid).get(ip)
    return {'user': 'root', 'port': 22, 'password': d.get('password', ''), 'key': ''} if d else {}


def cf_for(store: Store, prof: dict) -> engine.Cloudflare:
    tok = cf_token_of(store, prof)
    if not tok:
        raise engine.StepError('у группы нет своего токена Cloudflare — добавьте его в настройках группы')
    return engine.Cloudflare(tok)


async def load_catalog(store: Store, prof: dict | None = None) -> dict:
    """Каталог bcloud для развёртывания (цены — для покупателя группы): только локации с тарифами, ОС выбрана автоматически."""
    bc = bcloud_from(store, buyer_of(store, prof or {}))
    bc.RETRY_FOR = 8  # каталог открывают в админке — долго не ждём
    cat = await bc.catalog()
    if cat.get('maintenance'):
        raise BcloudError('в bcloud включён режим «продажи закрыты»')
    os_code = naming.pick_os([o['code'] for o in cat['os']])
    if not os_code:
        raise BcloudError('в каталоге bcloud нет Ubuntu или Debian — установщику нужна одна из них')
    cat['locations'] = [loc for loc in cat['locations'] if loc['offers']]
    cat['os_code'] = os_code
    cat['os_label'] = next((o['label'] for o in cat['os'] if o['code'] == os_code), os_code)
    return cat


def quote(cat: dict, location: str, idx: int | None, period: str, count: int, custom: dict | None = None) -> dict:
    """Цена: готовый тариф idx или своя конфигурация custom ({cpu, ram, disk, price_kop} — цена из bcloud)."""
    loc = next((x for x in cat['locations'] if x['code'] == location), None)
    if loc is None:
        raise ValueError('эта локация сейчас недоступна в bcloud')
    offer = custom if idx is None else next((o for o in loc['offers'] if o['idx'] == idx), None)
    if offer is None:
        raise ValueError('этот тариф недоступен в bcloud — обновите страницу')
    per = next((x for x in cat['periods'] if x['code'] == period), None)
    if per is None:
        raise ValueError('этот срок аренды недоступен')
    one = period_price(offer['price_kop'], per['months'], per['pct'])
    return {'loc': loc, 'offer': offer, 'one_kop': one, 'total_kop': one * count}


def check_spec(cat: dict, raw) -> dict:
    """Своя конфигурация из конструктора: {cpu, ram, disk} в диапазонах bcloud (min, max, шаг)."""
    conf = cat.get('configurator')
    if not conf:
        raise ValueError('в bcloud нет конструктора — выберите готовый тариф')
    try:
        spec = {k: int((raw or {})[k]) for k in ('cpu', 'ram', 'disk')}
    except (KeyError, TypeError, ValueError):
        raise ValueError('конфигурация: нужны vCPU, память и диск числами') from None
    names = {'cpu': 'vCPU', 'ram': 'память, ГБ', 'disk': 'диск, ГБ'}
    for k, v in spec.items():
        r = conf[k]
        if not r['min'] <= v <= r['max'] or ((v - r['min']) % r['step'] and not (v == r['max'] and r.get('max_is_allowed', True))):
            step = f', шаг {r["step"]}' if r['step'] > 1 else ''
            raise ValueError(f'{names[k]}: от {r["min"]} до {r["max"]}{step}')
    return spec


async def custom_offer(store: Store, prof: dict, cat: dict, location: str, raw) -> dict:
    """Своя конфигурация с ценой для покупателя группы (свежий расчёт bcloud)."""
    spec = check_spec(cat, raw)
    q = await bcloud_from(store, buyer_of(store, prof)).quote(location, **spec)
    return {'idx': None, **spec, 'price_kop': int(q['price_kop']), 'speed': q.get('speed') or ''}


OS_OK = ('ubuntu', 'debian')


async def own_servers(store: Store, prof: dict) -> list[dict]:
    """Серверы покупателя группы в bcloud (куплены здесь, в боте bcloud или где угодно) с отметками:
    used — на нём уже нода из Bobr Nodes, ready — можно ставить (активен, Ubuntu/Debian, не занят).
    В ответе есть root_password — наружу (в админку) его не отдавать."""
    bc = bcloud_from(store, buyer_of(store, prof))
    bc.RETRY_FOR = 8
    used_ids, used_ips = set(), set()
    for j in store.jobs(5000):
        d = j['data']
        if d.get('kind') in ('node', 'reip') or d.get('removed') or j['status'] == 'failed':  # с ошибкой — сервер свободен
            continue
        if d.get('server_id'):
            used_ids.add(int(d['server_id']))
        if d.get('ip'):
            used_ips.add(d['ip'])
    out = []
    for x in await bc.servers():
        os_ok = any(str(x.get('os') or '').lower().startswith(o) for o in OS_OK)
        used = int(x['id']) in used_ids or (x.get('ip') or '') in used_ips
        why = ('уже нода Bobr Nodes' if used else 'сервер ещё создаётся' if x.get('status') == 'provisioning'
               else f'сервер {x.get("status")}' if x.get('status') != 'active' else '' if os_ok
               else 'нужна Ubuntu или Debian')
        out.append({**{k: x.get(k) for k in ('id', 'name', 'status', 'ip', 'location', 'loc_name', 'flag', 'cpu', 'ram', 'disk',
                                              'os', 'expires_at', 'root_password')},
                    'os_label': naming_os_label(x.get('os') or ''), 'used': used, 'ready': not why, 'why': why})
    return out


def naming_os_label(code: str) -> str:
    m = re.match(r'([a-z]+)(\d+)$', code.lower())
    if not m:
        return code
    v = m.group(2)
    v = f'{v[:2]}.{v[2:]}' if m.group(1) == 'ubuntu' and len(v) == 4 else v
    return f'{m.group(1).capitalize()} {v}'


async def auto_domains(store: Store, prof: dict, location: str, count: int, base: str | None = None,
                       exclude: set[str] | None = None) -> list[str]:
    """Свободные имена по шаблону группы на домене base (по умолчанию — домен группы для автоимён)."""
    base = naming.norm_domain(base or prof.get('base_domain') or '')
    if not base:
        raise ValueError('выберите домен для имён нод')
    pattern = prof.get('name_pattern') or naming.DEFAULT_PATTERN
    err = naming.check_pattern(pattern, base)
    if err:
        raise ValueError(err)

    def pick():
        cf = cf_for(store, prof)
        cf.zone_for(base)
        return naming.auto_names(pattern, base, location, count, store.used_domains() | (exclude or set()),
                                 exists=lambda d: bool(cf.records(d)))
    try:
        return await asyncio.to_thread(pick)
    except engine.StepError as e:
        raise ValueError(str(e)) from None


async def check_domains(store: Store, prof: dict, raw: list[str] | str, count: int) -> list[str]:
    """Ручные домены: формат, количество, повторы, занятость и что зона есть в Cloudflare."""
    items = raw if isinstance(raw, list) else re.split(r'[\s,]+', raw or '')
    doms = [naming.norm_domain(x) for x in items if x and x.strip()]
    errs = []
    if len(doms) != count:
        errs.append(f'нужно {count}, указано {len(doms)}')
    if len(set(doms)) != len(doms):
        errs.append('домены повторяются')
    bad = [d for d in doms if not naming.valid_domain(d)]
    if bad:
        errs.append('некорректные: ' + ', '.join(bad))
    used = store.used_domains() & set(doms)
    if used:
        errs.append('уже есть в задачах: ' + ', '.join(sorted(used)))
    if not errs:
        def zones():
            cf = cf_for(store, prof)
            for d in doms:
                cf.zone_for(d)
        try:
            await asyncio.to_thread(zones)
        except engine.StepError as e:
            errs.append(str(e))
    if errs:
        raise ValueError('; '.join(errs))
    return doms


DEFAULT_REMARK = '{flag} {country} {n}'
DEFAULT_BALANCER_REMARK = '{flag} {country}'


_EN: dict | None = None


def country_name(loc: dict, lang: str = 'ru') -> str:
    """Название страны локации: как в bcloud (русское) или английское по ISO-коду."""
    global _EN
    if lang != 'en':
        return loc.get('name', '')
    if _EN is None:
        import json
        from pathlib import Path
        _EN = json.loads((Path(__file__).parent / 'countries_en.json').read_text(encoding='utf-8'))
    code = re.sub(r'[^a-z]', '', (loc.get('code') or '').lower())[:2]
    return _EN.get(code) or loc.get('name', '')


def render_remark(tpl: str, loc: dict, name: str, domain: str, i: int, lang: str = 'ru') -> str:
    m = re.search(r'(\d+)$', name)
    n = m.group(1) if m else str(i)
    out = (tpl or DEFAULT_REMARK).replace('{flag}', loc.get('flag', '')).replace('{country}', country_name(loc, lang)) \
        .replace('{n}', n).replace('{name}', name).replace('{domain}', domain)
    return ' '.join(out.split())[:100] or name


def _tags(tags) -> list[str]:
    if isinstance(tags, str):
        tags = [t for t in re.split(r'[\s,]+', tags.upper()) if t]
    tags = [str(t).upper() for t in (tags or [])]
    bad = [t for t in tags if not re.fullmatch(r'[A-Z0-9_:]{1,36}', t)]
    if bad:
        raise ValueError('теги: латиница, цифры, подчёркивание — ' + ', '.join(bad))
    return list(dict.fromkeys(tags))[:10]


def host_options(location: str, mode: str = 'simple', tags: str | list | None = None, balancer_tag: str = '',
                 balancer_remark: str = '', balancer_template: str | None = None) -> dict:
    """Как регистрировать хосты:
    simple   — видимые, без тегов;
    normal   — видимые, с тегом NORMAL (общий балансировщик по всем странам, уже настроен в панели);
    balancer — скрытые, с тегом страны (+NORMAL), и балансировщик страны: шаблон Balancer_<тег> и видимый хост."""
    if mode == 'normal':
        return {'mode': 'normal', 'hidden': False, 'tags': _tags(tags if tags else ['NORMAL'])}
    if mode != 'balancer':
        return {'mode': 'simple', 'hidden': False, 'tags': []}
    tag = (balancer_tag or re.sub(r'[^A-Za-z0-9_]', '', location) or 'NODES').upper()
    if not re.fullmatch(r'[A-Z0-9_]{1,30}', tag):
        raise ValueError('тег балансировщика: латиница, цифры, подчёркивание')
    tags = _tags([tag, *(_tags(tags) if tags is not None else ['NORMAL'])])
    return {'mode': 'balancer', 'hidden': True, 'tags': tags,
            'balancer': {'tag': tag, 'remark': balancer_remark or DEFAULT_BALANCER_REMARK, 'template': balancer_template}}


def host_places(order: list, domains_of: dict[str, list[str]], known: set[str]) -> dict[str, dict]:
    """order — желаемый список: UUID уже существующих хостов и '#<локация>' на месте новых.
    Для каждого нового домена: after — UUID ближайшего существующего хоста выше ('' — начало списка),
    before — новые домены, которые стоят между ним и этим доменом."""
    places, anchor, between, seen = {}, '', [], set()
    for tok in order:
        if not isinstance(tok, str):
            continue
        if tok.startswith('#'):
            loc = tok[1:]
            if loc in seen:
                continue
            seen.add(loc)
            for dom in domains_of.get(loc) or []:
                places[dom] = {'after': anchor, 'before': list(between)}
                between.append(dom)
        elif tok in known:
            anchor, between = tok, []
    return places


async def create_jobs(store: Store, runner: Runner, chat_id: int, pid: int, cat: dict, location: str, idx: int | None,
                      period: str, domains: list[str], squads: list[str] | None = None, remark: str = '',
                      host: dict | None = None, lang: str = 'ru', places: dict | None = None,
                      extras: dict | None = None, custom: dict | None = None, servers: list[dict] | None = None,
                      script_id: object = 'group', note: dict | None = None) -> list[int]:
    """Задачи установки. custom — своя конфигурация (покупка по cpu/ram/disk); servers — уже купленные серверы
    bcloud по порядку доменов: покупки нет, сервер, IP и root-пароль берутся из bcloud."""
    if servers:
        if len(servers) != len(domains):
            raise ValueError(f'серверов {len(servers)}, доменов {len(domains)}')
        s0 = servers[0]
        loc = next((x for x in cat.get('locations', []) if x['code'] == location), None) or \
            {'code': location, 'name': s0.get('loc_name') or location, 'flag': s0.get('flag') or ''}
        q = {'loc': loc, 'one_kop': 0}
    else:
        q = quote(cat, location, idx, period, len(domains), custom)
    loc = q['loc']
    batch = secrets.token_hex(4)
    cc = location.upper() if re.fullmatch(r'[a-z]{2}', location) else ''
    ids = []
    host = host or {'mode': 'simple'}
    if host.get('mode') == 'balancer':
        host = {**host, 'balancer': {**host['balancer'], 'remark': render_remark(host['balancer']['remark'], loc, '', '', 1, lang)}}
    for i, dom in enumerate(domains, 1):
        name = naming.node_name(dom)
        srv = servers[i - 1] if servers else None
        offer = srv or q['offer']
        data = {'location': location, 'loc_name': loc['name'], 'flag': loc['flag'], 'country': cc, 'idx': None if srv else idx,
                'spec': spec_text(offer), 'os': srv['os'] if srv else cat['os_code'], 'period': period, 'domain': dom, 'name': name,
                'remark': render_remark(remark, loc, name, dom, i, lang), 'password': (srv or {}).get('root_password') or gen_password(),
                **({'custom': {k: custom[k] for k in ('cpu', 'ram', 'disk')}} if custom and not srv else {}),
                **({'server_id': int(srv['id']), 'existing': True, 'skipped_stages': ['buy']} if srv else {}),
                'host': host, 'squads': squads, 'place': (places or {}).get(dom),
                'psiphon': (extras or {}).get('psiphon'), 'warp': (extras or {}).get('warp'), 'xhttp': (extras or {}).get('xhttp'), 'script_id': (store.profile(pid) or {}).get('script_id') if script_id == 'group' else script_id,
                **(note or {}),
                'price_kop': q['one_kop'], 'buyer': buyer_of(store, store.profile(pid) or {})}
        jid = store.add_job(batch, pid, chat_id, data)
        ids.append(jid)
        await runner.render(jid, force=True)
        runner.start(jid)
    return ids
