"""Развёртывание — общее для чата и веб-админки: каталог bcloud, цены, домены, создание задач."""
from __future__ import annotations

import asyncio
import math
import re
import secrets
import string

from . import engine, naming
from .bcloud import BcloudError
from .runner import Runner, bcloud_from, buyer_of
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


def cf_for(store: Store, prof: dict) -> engine.Cloudflare:
    return engine.Cloudflare(prof.get('cf_token') or store.get('cf_token'))


async def load_catalog(store: Store, prof: dict | None = None) -> dict:
    """Каталог bcloud для развёртывания (цены — для покупателя группы): только локации с тарифами, ОС выбрана автоматически."""
    cat = await bcloud_from(store, buyer_of(store, prof or {})).catalog()
    if cat.get('maintenance'):
        raise BcloudError('в bcloud включён режим «продажи закрыты»')
    os_code = naming.pick_os([o['code'] for o in cat['os']])
    if not os_code:
        raise BcloudError('в каталоге bcloud нет Ubuntu или Debian — установщику нужна одна из них')
    cat['locations'] = [loc for loc in cat['locations'] if loc['offers']]
    cat['os_code'] = os_code
    cat['os_label'] = next((o['label'] for o in cat['os'] if o['code'] == os_code), os_code)
    return cat


def quote(cat: dict, location: str, idx: int, period: str, count: int) -> dict:
    loc = next(x for x in cat['locations'] if x['code'] == location)
    offer = next(o for o in loc['offers'] if o['idx'] == idx)
    per = next((x for x in cat['periods'] if x['code'] == period), None)
    if per is None:
        raise ValueError('этот срок аренды недоступен')
    one = period_price(offer['price_kop'], per['months'], per['pct'])
    return {'loc': loc, 'offer': offer, 'one_kop': one, 'total_kop': one * count}


async def auto_domains(store: Store, prof: dict, location: str, count: int) -> list[str]:
    if not prof.get('base_domain'):
        raise ValueError('в группе не задан домен для автоимён')

    def pick():
        cf = cf_for(store, prof)
        cf.zone_for(prof['base_domain'])
        return naming.auto_names(prof.get('name_pattern') or naming.DEFAULT_PATTERN, prof['base_domain'], location,
                                 count, store.used_domains(), exists=lambda d: bool(cf.records(d)))
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


def render_remark(tpl: str, loc: dict, name: str, domain: str, i: int) -> str:
    m = re.search(r'(\d+)$', name)
    n = m.group(1) if m else str(i)
    out = (tpl or DEFAULT_REMARK).replace('{flag}', loc.get('flag', '')).replace('{country}', loc.get('name', '')) \
        .replace('{n}', n).replace('{name}', name).replace('{domain}', domain)
    return ' '.join(out.split())[:100] or name


def host_options(location: str, mode: str = 'simple', tags: str | list | None = None, balancer_tag: str = '',
                 balancer_remark: str = '', balancer_template: str | None = None) -> dict:
    """Как регистрировать хосты: обычные или скрытые с тегами + общий хост-балансировщик."""
    if mode != 'balancer':
        return {'mode': 'simple'}
    tag = (balancer_tag or re.sub(r'[^A-Za-z0-9_]', '', location) or 'NODES').upper()
    if not re.fullmatch(r'[A-Z0-9_]{1,30}', tag):
        raise ValueError('тег балансировщика: латиница, цифры, подчёркивание')
    if isinstance(tags, str):
        tags = [t for t in re.split(r'[\s,]+', tags.upper()) if t]
    tags = list(dict.fromkeys([tag, *(tags if tags is not None else ['NORMAL'])]))
    bad = [t for t in tags if not re.fullmatch(r'[A-Z0-9_:]{1,36}', t)]
    if bad:
        raise ValueError('теги: латиница, цифры, подчёркивание — ' + ', '.join(bad))
    return {'mode': 'balancer', 'tags': tags[:10],
            'balancer': {'tag': tag, 'remark': balancer_remark or DEFAULT_BALANCER_REMARK, 'template': balancer_template}}


async def create_jobs(store: Store, runner: Runner, chat_id: int, pid: int, cat: dict, location: str, idx: int,
                      period: str, domains: list[str], squads: list[str] | None = None, remark: str = '',
                      host: dict | None = None) -> list[int]:
    q = quote(cat, location, idx, period, len(domains))
    loc, offer = q['loc'], q['offer']
    batch = secrets.token_hex(4)
    cc = location.upper() if re.fullmatch(r'[a-z]{2}', location) else ''
    ids = []
    host = host or {'mode': 'simple'}
    if host.get('mode') == 'balancer':
        host = {**host, 'balancer': {**host['balancer'], 'remark': render_remark(host['balancer']['remark'], loc, '', '', 1)}}
    for i, dom in enumerate(domains, 1):
        name = naming.node_name(dom)
        data = {'location': location, 'loc_name': loc['name'], 'flag': loc['flag'], 'country': cc, 'idx': idx,
                'spec': spec_text(offer), 'os': cat['os_code'], 'period': period, 'domain': dom, 'name': name,
                'remark': render_remark(remark, loc, name, dom, i), 'password': gen_password(),
                'host': host, 'squads': squads,
                'price_kop': q['one_kop'], 'buyer': buyer_of(store, store.profile(pid) or {})}
        jid = store.add_job(batch, pid, chat_id, data)
        ids.append(jid)
        await runner.render(jid, force=True)
        runner.start(jid)
    return ids
