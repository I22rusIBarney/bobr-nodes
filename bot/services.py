"""Развёртывание — общее для чата и веб-админки: каталог Bobr Cloud, цены, домены, создание задач."""
from __future__ import annotations

import asyncio
import os
import math
import re
import secrets
import string
import time

from . import engine, naming
from .bcloud import BcloudError
from .runner import SERVICE_KINDS, Runner, bcloud_from, buyer_of, cf_token_of
from .store import Store

PERIOD_LABEL = {'1m': '1 мес', '3m': '3 мес', '6m': '6 мес', '1y': '1 год'}


def period_price(month_kop: int, months: int, pct: int) -> int:
    """Как в Bobr Cloud: месяц × N со скидкой за срок, вверх до рубля."""
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
    """IP и домен → данные последней удачной задачи покупки (домен, IP, пароль root, сервер Bobr Cloud) этой группы.
    Ноды регистрируются по домену, старые — по IP, поэтому ключи — оба."""
    return store.cached(('bot_jobs', pid), lambda: _bot_jobs_by_ip(store, pid))  # пересобирается при изменении задач


def _bot_jobs_by_ip(store: Store, pid: int) -> dict[str, dict]:
    out = {}
    for j in store.done_jobs(pid):
        d = j['data']
        if d.get('ip') and d.get('kind') not in SERVICE_KINDS and not d.get('removed'):
            d = {**d, '_job': j['id']}
            out[d['ip']] = d
            if d.get('domain'):
                out[d['domain']] = d
    return out


def admin_ids() -> set[int]:
    """Администраторы бота из .env (ADMIN_IDS)."""
    return {int(x) for x in os.environ.get('ADMIN_IDS', '').replace(' ', '').split(',') if x.isdigit()}


def tenant_group(store: Store, prof: dict | None, admins) -> bool:
    """Группой управляет приглашённый пользователь бота: он её создал или он — её клиент Bobr Cloud.
    Тогда панель (а с ней адреса нод) задаёт он, и доверять им наравне с группами администратора нельзя.
    Клиент Bobr Cloud, который не пользуется ботом (или сам администратор), группу пользовательской не делает."""
    if not prof:
        return False
    if prof.get('created_by'):
        return True
    admins = {str(a) for a in admins}
    tg = str(prof.get('owner_tg_id') or '').strip()
    owner = str(prof.get('owner') or '').strip()
    if not tg and owner.isdigit():
        tg = owner
    if tg:
        return tg not in admins and tg.isdigit() and store.user(int(tg)) is not None
    if owner.startswith('@'):  # доступ пользователю бота даёт и совпадение @username
        name = owner[1:].lower()
        return any((u.get('username') or '').lower() == name and str(u.get('tg_id', u.get('id'))) not in admins
                   for u in store.users())
    return False


def bot_key_ips(store: Store, pid: int) -> set[str]:
    """Адреса серверов группы, на которые бот сам положил свой ключ: купленные и поставленные им ноды
    (IP и домен из задачи) и купленные им входы."""
    out = set(bot_jobs_by_ip(store, pid))
    out |= {x['ip'] for x in store.relays() if x['pid'] == pid and x.get('ip') and (x.get('bought') or x.get('server_id'))}
    return out


def guard_bot_key(store: Store, prof: dict | None, acc: dict, target: str, admins) -> dict:
    """Ключ бота лежит на всех нодах, которые бот ставил, — во всех группах. В группе пользователя ключ бота
    работает только с серверами, которые бот сам купил или поставил в этой группе; иначе — пароль или ничего."""
    if (acc or {}).get('key') != 'bot' or not tenant_group(store, prof, admins) or target in bot_key_ips(store, prof['id']):
        return acc
    acc = {**acc, 'key': ''}
    return acc if acc.get('password') else {}


def node_ssh(store: Store, runner: Runner, pid: int, uuid: str, ip: str) -> dict:
    """Доступ для задачи: сохранённый, иначе — пароль из задачи покупки."""
    saved = runner.ssh_of(pid, uuid)
    if saved:
        target = (bot_jobs_by_ip(store, pid).get(ip) or {}).get('ip') or ip
        return guard_bot_key(store, store.profile(pid), saved, target, runner.admins)
    d = bot_jobs_by_ip(store, pid).get(ip)
    if d and d.get('ssh'):  # свой сервер: доступ, с которым его ставили
        return d['ssh']
    return {'user': 'root', 'port': 22, 'password': d.get('password', ''), 'key': ''} if d else {}


async def start_upgrade(store: Store, runner: Runner, prof: dict, n: dict, tag: str, chat_id: int, xray: str = '') -> int:
    """Задача «обновить ноду n из панели до tag» (или xray — своя версия Xray, 'image' — как в образе).
    ValueError — нет доступа по SSH."""
    ssh = node_ssh(store, runner, prof['id'], n['uuid'], n['address'])
    if not ssh:
        raise ValueError('нет доступа по SSH — укажите его на странице ноды')
    bd = bot_jobs_by_ip(store, prof['id']).get(n['address']) or {}
    data = {'kind': 'upgrade', 'node_uuid': n['uuid'], 'name': n['name'], 'ip': bd.get('ip') or n['address'],
            'domain': bd.get('domain') or n['name'], 'flag': '⬆️', 'loc_name': n['name'],
            'spec': (('Xray как в образе ноды' if xray == 'image' else f'Xray {xray}') if xray else f'обновление ноды до {tag}'),
            'tag': tag, 'ssh': ssh, **({'xray': xray} if xray else {})}
    jid = store.add_job('up-' + n['uuid'][:8], prof['id'], chat_id, data)
    await runner.render(jid, force=True)
    runner.start(jid)
    return jid


def cf_for(store: Store, prof: dict) -> engine.Cloudflare:
    tok = cf_token_of(store, prof)
    if not tok:
        raise engine.StepError('у группы нет своего токена Cloudflare — добавьте его в настройках группы')
    return engine.Cloudflare(tok)


async def load_catalog(store: Store, prof: dict | None = None) -> dict:
    """Каталог Bobr Cloud для развёртывания (цены — для покупателя группы): только локации с тарифами, ОС выбрана автоматически."""
    bc = bcloud_from(store, buyer_of(store, prof or {}))
    bc.RETRY_FOR = 8  # каталог открывают в админке — долго не ждём
    cat = await bc.catalog()
    if cat.get('maintenance'):
        raise BcloudError('в Bobr Cloud включён режим «продажи закрыты»')
    os_code = naming.pick_os([o['code'] for o in cat['os']])
    if not os_code:
        raise BcloudError('в каталоге Bobr Cloud нет Ubuntu или Debian — установщику нужна одна из них')
    cat['locations'] = [loc for loc in cat['locations'] if loc['offers']]
    cat['os_code'] = os_code
    cat['os_label'] = next((o['label'] for o in cat['os'] if o['code'] == os_code), os_code)
    return cat


def quote(cat: dict, location: str, idx: int | None, period: str, count: int, custom: dict | None = None) -> dict:
    """Цена: готовый тариф idx или своя конфигурация custom ({cpu, ram, disk, price_kop} — цена из Bobr Cloud)."""
    loc = next((x for x in cat['locations'] if x['code'] == location), None)
    if loc is None:
        raise ValueError('эта локация сейчас недоступна в Bobr Cloud')
    offer = custom if idx is None else next((o for o in loc['offers'] if o['idx'] == idx), None)
    if offer is None:
        raise ValueError('этот тариф недоступен в Bobr Cloud — обновите страницу')
    per = next((x for x in cat['periods'] if x['code'] == period), None)
    if per is None:
        raise ValueError('этот срок аренды недоступен')
    one = period_price(offer['price_kop'], per['months'], per['pct'])
    return {'loc': loc, 'offer': offer, 'one_kop': one, 'total_kop': one * count}


def check_spec(cat: dict, raw) -> dict:
    """Своя конфигурация из конструктора: {cpu, ram, disk} в диапазонах Bobr Cloud (min, max, шаг)."""
    conf = cat.get('configurator')
    if not conf:
        raise ValueError('в Bobr Cloud нет конструктора — выберите готовый тариф')
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
    """Своя конфигурация с ценой для покупателя группы (свежий расчёт Bobr Cloud)."""
    spec = check_spec(cat, raw)
    q = await bcloud_from(store, buyer_of(store, prof)).quote(location, **spec)
    return {'idx': None, **spec, 'price_kop': int(q['price_kop']), 'speed': q.get('speed') or ''}


OS_OK = ('ubuntu', 'debian')


async def own_servers(store: Store, prof: dict) -> list[dict]:
    """Серверы покупателя группы в Bobr Cloud (куплены здесь, в боте Bobr Cloud или где угодно) с отметками:
    used — на нём уже нода из Bobr Nodes, ready — можно ставить (активен, Ubuntu/Debian, не занят).
    В ответе есть root_password — наружу (в админку) его не отдавать."""
    bc = bcloud_from(store, buyer_of(store, prof))
    bc.RETRY_FOR = 8
    servers = await bc.servers()
    # систему переустановили в Bobr Cloud после установки ноды — ноды там больше нет, сервер свободен
    wiped = {int(x['id']): reinstalled_ts(x) for x in servers if reinstalled_ts(x)}
    wiped_ip = {x['ip']: wiped[int(x['id'])] for x in servers if x.get('ip') and int(x['id']) in wiped}
    used_ids, used_ips = set(), set()
    node_of: dict = {}  # сервер → нода на нём (для переделки в мост)
    stale_of: dict = {}  # сервер → нода, которой нет после переустановки системы (её хвосты в панели и DNS)
    for j in store.jobs(5000):
        d = j['data']
        if d.get('kind') in SERVICE_KINDS or d.get('removed') or j['status'] == 'failed':  # с ошибкой — сервер свободен
            continue
        info = {'job': j['id'], 'pid': j['profile_id'], 'domain': d.get('domain') or '', 'ip': d.get('ip') or ''}
        t = wiped.get(int(d['server_id'])) if d.get('server_id') else wiped_ip.get(d.get('ip') or '')
        if t and j['created_at'] < t:
            for k in ([('id', int(d['server_id']))] if d.get('server_id') else []) + ([('ip', d['ip'])] if d.get('ip') else []):
                stale_of[k] = info
            continue
        if d.get('server_id'):
            used_ids.add(int(d['server_id']))
            node_of[('id', int(d['server_id']))] = info
        if d.get('ip'):
            used_ips.add(d['ip'])
            node_of[('ip', d['ip'])] = info
    relay_ids = {int(r['server_id']) for r in store.relays() if r.get('server_id')}
    relay_ips = {r['ip'] for r in store.relays() if r.get('ip')}
    out = []
    for x in servers:
        os_ok = any(str(x.get('os') or '').lower().startswith(o) for o in OS_OK)
        relay = int(x['id']) in relay_ids or (x.get('ip') or '') in relay_ips  # вход (мост) — под ноду не предлагать
        used = int(x['id']) in used_ids or (x.get('ip') or '') in used_ips or relay
        why = ('вход (мост) для ноды' if relay and not (int(x['id']) in used_ids or (x.get('ip') or '') in used_ips)
               else 'уже нода Bobr Nodes' if used else 'сервер ещё создаётся' if x.get('status') == 'provisioning'
               else f'сервер {x.get("status")}' if x.get('status') != 'active' else '' if os_ok
               else 'нужна Ubuntu или Debian')
        out.append({**{k: x.get(k) for k in ('id', 'name', 'status', 'ip', 'location', 'loc_name', 'flag', 'cpu', 'ram', 'disk',
                                              'os', 'expires_at', 'root_password')},
                    'os_label': naming_os_label(x.get('os') or ''), 'used': used, 'relay': relay, 'ready': not why, 'why': why,
                    'node': None if relay else node_of.get(('id', int(x['id']))) or node_of.get(('ip', x.get('ip') or '')),
                    'stale': None if used else stale_of.get(('id', int(x['id']))) or stale_of.get(('ip', x.get('ip') or ''))})
    return out


def reinstalled_ts(srv: dict) -> float:
    """Когда в Bobr Cloud последний раз переустанавливали систему (0 — не переустанавливали или старый Bobr Cloud)."""
    v = srv.get('reinstalled_at')
    if not v:
        return 0.0
    try:
        from datetime import datetime, timezone
        d = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return 0.0


async def drop_stale(store: Store, stale: dict, log) -> None:
    """Нода, которой нет после переустановки системы: убрать её из панели и DNS и пометить задачу удалённой.
    На сервер не заходим — там уже чистая система."""
    prof = store.profile(int(stale['pid']))
    if prof:
        pconf = {**prof, 'cf_token': cf_token_of(store, prof)}
        try:
            await asyncio.to_thread(engine.remove_node, pconf, stale.get('ip') or '', stale.get('domain') or '', log)
        except engine.StepError as e:
            log(f'панель: {e} — уберите старую ноду из панели вручную')
    store.patch_job_data(int(stale['job']), removed=True, removed_at=int(time.time()), os_reinstalled=True)


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


async def check_domains(store: Store, prof: dict, raw: list[str] | str, count: int, warnings: list | None = None) -> list[str]:
    """Ручные домены: формат, количество, повторы, занятость, зона в Cloudflare и то, что сломает установку уже после
    оплаты: CNAME на имени, CAA без Let's Encrypt. warnings — сюда то, что не мешает, но стоит знать (имя уже ведёт на сервер)."""
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
        tenant = tenant_group(store, prof, admin_ids())

        def zones():
            cf = cf_for(store, prof)
            foreign, problems, caa = [], [], {}
            for d in doms:
                zid = cf.zone_for(d)
                recs = cf.records(d)
                if tenant:  # бот перезаписывает A-запись — в группе пользователя только свои или новые имена
                    if any(r.get('comment') != 'nv-deploy' for r in recs):
                        foreign.append(d)
                    continue
                if any(r['type'] == 'CNAME' for r in recs):
                    problems.append(f'у {d} есть CNAME — установка на нём упадёт, удалите его в Cloudflare или выберите другое имя')
                for r in recs:
                    if r['type'] == 'A' and r.get('comment') != 'nv-deploy' and warnings is not None:
                        warnings.append(f'{d} сейчас ведёт на {r["content"]} — бот перенаправит его на новый сервер')
                if zid not in caa:
                    caa[zid] = [r for r in cf.call('GET', f'/zones/{zid}/dns_records?type=CAA&per_page=100') if r.get('type') == 'CAA']
                if not caa_allows_le(d, caa[zid]):
                    problems.append(f'CAA-записи домена {d} не разрешают Let\'s Encrypt — сертификат не выпустится; '
                                    'добавьте CAA «0 issue letsencrypt.org» в Cloudflare')
            return foreign, problems
        try:
            foreign, problems = await asyncio.to_thread(zones)
            if foreign:
                errs.append('у этих имён уже есть DNS-записи, созданные не ботом (или ботом старой версии — тогда удалите '
                            'запись в Cloudflare) — выберите другие: ' + ', '.join(foreign))
            errs += problems
        except engine.StepError as e:
            errs.append(str(e))
    if errs:
        raise ValueError('; '.join(errs))
    return doms


def config_label(store: Store, bd: dict) -> str:
    """Как поставлена нода — для статистики блокировок: установщик и дополнения."""
    sid = bd.get('script_id')
    name = ((store.script(int(sid)) or {}).get('recipe') or {}).get('title') if sid else ''
    parts = [name or 'встроенный установщик']
    if (bd.get('host') or {}).get('mode') == 'balancer':
        parts.append('балансировщик')
    parts += [label for k, label in (('xhttp', 'XHTTP'), ('warp', 'WARP'), ('psiphon', 'Psiphon')) if bd.get(k)]
    return ' + '.join(parts)


def caa_allows_le(domain: str, caa: list[dict]) -> bool:
    """Разрешает ли CAA выпуск сертификата Let's Encrypt для domain: действует ближайший уровень имени, где CAA есть;
    нет CAA — можно всем. Запись Cloudflare: content вида «0 issue "letsencrypt.org"» или data {tag, value}."""
    labels = domain.split('.')
    for i in range(len(labels) - 1):
        name = '.'.join(labels[i:])
        here = [r for r in caa if r.get('name') == name]
        issue = []
        for r in here:
            d = r.get('data') or {}
            tag, val = (d.get('tag'), str(d.get('value') or '')) if d else ((r.get('content') or '').split(' ', 2)[1:] + ['', ''])[:2]
            if tag == 'issue':
                issue.append(val.strip('"').split(';')[0].strip())
        if issue:
            return 'letsencrypt.org' in issue
    return True


DEFAULT_REMARK = '{flag} {country} {n}'
DEFAULT_BALANCER_REMARK = '{flag} {country}'


_EN: dict | None = None


def country_name(loc: dict, lang: str = 'ru') -> str:
    """Название страны локации: как в Bobr Cloud (русское) или английское по ISO-коду."""
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
                      script_id: object = 'group', note: dict | None = None, own: dict | None = None) -> list[int]:
    """Задачи установки. custom — своя конфигурация (покупка по cpu/ram/disk); servers — уже купленные серверы
    Bobr Cloud по порядку доменов: покупки нет, сервер, IP и root-пароль берутся из Bobr Cloud.
    own — свой сервер у другого хостера: {ip, ssh, loc, hoster}; Bobr Cloud не участвует, вход — по ssh."""
    if own:
        q = {'loc': own['loc'], 'one_kop': 0}
    elif servers:
        if len(servers) != len(domains):
            raise ValueError(f'серверов {len(servers)}, доменов {len(domains)}')
        s0 = servers[0]
        loc = next((x for x in cat.get('locations', []) if x['code'] == location), None) or \
            {'code': location, 'name': s0.get('loc_name') or location, 'flag': s0.get('flag') or ''}
        q = {'loc': loc, 'one_kop': 0}
    else:
        q = quote(cat, location, idx, period, len(domains), custom)
    loc = q['loc']
    for srv in servers or []:  # систему переустанавливали: прежняя нода живёт только в панели и DNS — убираем
        if srv.get('stale'):
            await drop_stale(store, srv['stale'], lambda t: None)
    batch = secrets.token_hex(4)
    cc = location.upper() if re.fullmatch(r'[a-z]{2}', location) else ''
    ids = []
    host = host or {'mode': 'simple'}
    if host.get('mode') == 'balancer':
        host = {**host, 'balancer': {**host['balancer'], 'remark': render_remark(host['balancer']['remark'], loc, '', '', 1, lang)}}
    for i, dom in enumerate(domains, 1):
        name = naming.node_name(dom)
        srv = servers[i - 1] if servers else None
        offer = srv or q.get('offer') or {}
        data = {'location': location, 'loc_name': loc['name'], 'flag': loc['flag'], 'country': cc, 'idx': None if srv else idx,
                'spec': 'свой сервер' + (f' · {own["hoster"]}' if own.get('hoster') else '') if own else spec_text(offer),
                'os': srv['os'] if srv else cat.get('os_code', ''), 'period': period, 'domain': dom, 'name': name,
                'remark': render_remark(remark, loc, name, dom, i, lang), 'password': (srv or {}).get('root_password') or gen_password(),
                **({'custom': {k: custom[k] for k in ('cpu', 'ram', 'disk')}} if custom and not srv else {}),
                **({'server_id': int(srv['id']), 'existing': True, 'skipped_stages': ['buy']} if srv else {}),
                **({'own': True, 'ip': own['ip'], 'ssh': own['ssh'], 'hoster': own.get('hoster') or '', 'skipped_stages': ['buy', 'ip']}
                   if own else {}),
                'host': host, 'squads': squads, 'place': (places or {}).get(dom),
                'psiphon': (extras or {}).get('psiphon'), 'warp': (extras or {}).get('warp'), 'xhttp': (extras or {}).get('xhttp'), 'hy2': (extras or {}).get('hy2'), 'script_id': (store.profile(pid) or {}).get('script_id') if script_id == 'group' else script_id,
                **(note or {}),
                'price_kop': q['one_kop'], 'buyer': buyer_of(store, store.profile(pid) or {})}
        jid = store.add_job(batch, pid, chat_id, data)
        ids.append(jid)
        await runner.render(jid, force=True)
        runner.start(jid)
    return ids


def bridge_marks(store: Store) -> dict[int, dict[int, dict]]:
    """Какие серверы Bobr Cloud — мосты: покупатель → {id сервера: {role, peers, link}}.
    Вход — сервер входа (связан с нодой-выходом по имени), выход — сервер ноды, на которую ведут входы
    (связан со входами: их id в Bobr Cloud или IP своего сервера). link — id связанных серверов того же
    покупателя: в кабинете Bobr Cloud они стоят рядом."""
    want: dict[int, dict[int, dict]] = {}
    jobs = [j for j in store.jobs(5000) if j['status'] != 'failed' and not j['data'].get('removed')
            and j['data'].get('kind') not in SERVICE_KINDS and j['data'].get('server_id')]
    for x in store.relays():
        p = store.profile(x['pid'])
        if not p:
            continue
        entry = ('id', int(x['server_id'])) if x.get('server_id') else ('ip', x.get('ip') or '')
        eb = buyer_of(store, p)
        e = None
        if x.get('server_id'):
            e = want.setdefault(eb, {}).setdefault(int(x['server_id']), {'role': 'entry', 'peers': [], 'link': []})
            if e['role'] == 'entry' and (x.get('node_name') or x.get('target')) not in e['peers']:
                e['peers'].append(x.get('node_name') or x.get('target') or '')
        j = next((j for j in jobs if x.get('target') and x['target'] in (j['data'].get('domain'), j['data'].get('ip'))), None)
        jp = j and store.profile(j['profile_id'])
        if jp:
            xb_, xid = buyer_of(store, jp), int(j['data']['server_id'])
            ex = want.setdefault(xb_, {}).setdefault(xid, {'role': 'exit', 'peers': [], 'link': []})
            if ex['role'] == 'exit' and entry[1] and entry not in ex['peers']:
                ex['peers'].append(entry)
            if e is not None and xb_ == eb and ex['role'] == 'exit' and e['role'] == 'entry':
                if entry[1] not in ex['link']:
                    ex['link'].append(entry[1])
                if xid not in e['link']:
                    e['link'].append(xid)
    return want


async def sync_bridge_marks(store: Store) -> int:
    """Отметки мостов в кабинете Bobr Cloud — как в Bobr Nodes. Меняет только то, что разошлось; старый
    Bobr Cloud без отметок пропускается молча. Возвращает, сколько серверов поправлено."""
    if not (store.get('bcloud_url') and store.get('bcloud_token')):
        return 0
    want = bridge_marks(store)
    buyers = {buyer_of(store, p) for p in store.profiles()} | set(want)
    lists: dict[int, list[dict]] = {}
    for b in buyers:
        if not b:
            continue
        try:
            lists[b] = await bcloud_from(store, b).servers()
        except BcloudError:
            continue
    names = {int(s['id']): s.get('name') or f'#{s["id"]}' for ss in lists.values() for s in ss}

    def peer_text(role: str, peers: list) -> str:
        if role == 'entry':
            return ', '.join(p for p in peers if p)[:120]
        return ', '.join(names.get(v, f'#{v}') if k == 'id' else v for k, v in peers)[:120]
    fixed = 0
    for b, ss in lists.items():
        if ss and 'bridge_role' not in ss[0]:
            continue  # Bobr Cloud ещё без отметок мостов
        mine = want.get(b, {})
        has_link = bool(ss) and 'bridge_link' in ss[0]
        for s in ss:
            w = mine.get(int(s['id'])) or {'role': '', 'peers': [], 'link': []}
            role = w['role']
            peer = peer_text(role, w['peers']) if role else ''
            link = sorted(w['link']) if role else []
            cur = (s.get('bridge_role') or '', s.get('bridge_peer') or '', sorted(s.get('bridge_link') or []) if has_link else link)
            if cur == (role, peer, link):
                continue
            try:
                await bcloud_from(store, b).set_bridge(int(s['id']), role, peer, link)
                fixed += 1
            except BcloudError:
                break  # Bobr Cloud не принимает отметки (старая версия) или недоступен — в другой раз
    return fixed
