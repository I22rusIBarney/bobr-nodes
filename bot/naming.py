"""Имена и домены нод, выбор ОС."""
from __future__ import annotations

import re
from typing import Callable

DEFAULT_PATTERN = '{cc}{n}'
FORBIDDEN = ('vpn',)  # в автоматических именах не светим назначение сервера

DOMAIN_RE = re.compile(r'^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$')
LABEL_RE = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$')

OS_PREFERENCE = ('ubuntu2404', 'debian12', 'ubuntu2204', 'debian13')


def norm_domain(d: str) -> str:
    d = (d or '').strip().lower().removeprefix('https://').removeprefix('http://').split('/')[0].rstrip('.')
    return d


def valid_domain(d: str) -> bool:
    return bool(DOMAIN_RE.match(d))


def check_pattern(pattern: str, base: str) -> str | None:
    """Ошибка в шаблоне автоимён или None."""
    if '{n}' not in pattern:
        return 'в шаблоне нужен номер {n}, например {cc}{n}'
    low = (pattern + '.' + base).lower()
    for w in FORBIDDEN:
        if w in low:
            return f'в автоматических именах не должно быть «{w}»'
    sample = render(pattern, 'de', 12)
    if not LABEL_RE.match(sample):
        return f'получается некорректное имя «{sample}» — только латиница, цифры и дефис'
    if not valid_domain(f'{sample}.{base}'):
        return f'некорректный базовый домен «{base}»'
    return None


def cc_of(location: str) -> str:
    cc = re.sub(r'[^a-z]', '', (location or '').lower())
    return cc or 'n'


def render(pattern: str, cc: str, n: int) -> str:
    return pattern.replace('{cc}', cc).replace('{n}', str(n)).lower()


def auto_names(pattern: str, base: str, location: str, count: int, used: set[str],
               exists: Callable[[str], bool] | None = None, limit: int = 500) -> list[str]:
    """Следующие свободные домены по шаблону: de1.example.com, de2.example.com… Занятые (в задачах бота
    или уже с DNS-записью) пропускаются."""
    cc = cc_of(location)
    out: list[str] = []
    n = 0
    while len(out) < count and n < limit:
        n += 1
        d = f'{render(pattern, cc, n)}.{base}'
        if d in used or d in out:
            continue
        if exists and exists(d):
            continue
        out.append(d)
    if len(out) < count:
        raise ValueError('не удалось подобрать свободные имена')
    return out


def node_name(domain: str) -> str:
    """Имя ноды в панели: первая часть домена, 3–30 символов."""
    label = domain.split('.')[0]
    if len(label) < 3:
        label = (label + '-node')[:30]
    return label[:30]


def pick_os(codes: list[str]) -> str | None:
    low = {c.lower(): c for c in codes}
    for want in OS_PREFERENCE:
        for k, orig in low.items():
            if re.sub(r'[^a-z0-9]', '', k) == want:
                return orig
    for k, orig in low.items():
        if k.startswith(('ubuntu', 'debian')):
            return orig
    return None
