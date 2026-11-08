"""Новые релизы панели Remnawave: разбор на русском — что поменялось, не сломает ли это бота, что делать.

Релизы — с GitHub (remnawave/panel). Бот сравнивает изменения со списком методов API панели, которыми пользуется сам
(собирает их из своего кода), и просит нейросеть оценить совместимость. Без ключа Gemini — краткие заметки релиза
и простая проверка на «breaking»."""
from __future__ import annotations

import html
import json
import re
import time
import urllib.request
from pathlib import Path

RELEASES = 'https://api.github.com/repos/remnawave/panel/releases?per_page=10'
KEY = 'panel_release'          # последний разобранный релиз и итог разбора
_SEMVER = re.compile(r'v?(\d+)\.(\d+)\.(\d+)$')


def semver(tag: str) -> tuple[int, int, int] | None:
    m = _SEMVER.match(str(tag or '').strip())
    return tuple(int(x) for x in m.groups()) if m else None


_rcache: dict = {'at': 0.0, 'list': []}


def releases(timeout: float = 20) -> list[dict]:
    """Стабильные релизы панели, новые сверху: [{tag, name, body, url, date}]; кэш на час."""
    if _rcache['list'] and time.time() - _rcache['at'] < 3600:
        return _rcache['list']
    from . import ghapi
    req = urllib.request.Request(RELEASES, headers=ghapi.headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    out = [{'tag': x['tag_name'], 'name': x.get('name') or x['tag_name'], 'body': (x.get('body') or '')[:20000],
            'url': x.get('html_url') or '', 'date': (x.get('published_at') or '')[:10]}
           for x in data if isinstance(x, dict) and semver(x.get('tag_name')) and not x.get('prerelease') and not x.get('draft')]
    out = sorted(out, key=lambda x: semver(x['tag']), reverse=True)
    _rcache.update(at=time.time(), list=out)
    return out


BACKEND = 'https://api.github.com/repos/remnawave/backend/releases/tags/{tag}'


def backend_notes(tag: str, timeout: float = 20) -> str:
    """Изменения релиза: в remnawave/panel лежит только обёртка со ссылками, сами пункты — в remnawave/backend."""
    from . import ghapi
    req = urllib.request.Request(BACKEND.format(tag=tag), headers=ghapi.headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return str(json.loads(r.read().decode()).get('body') or '')
    except (OSError, ValueError):
        return ''


def gather(rels: list[dict], floor: str) -> dict:
    """Новый релиз и изменения всех релизов после floor (до 5), из заметок backend; нет их — из заметок panel."""
    fl = semver(floor)
    todo = [r for r in rels if not fl or semver(r['tag']) > fl][:5]
    parts = []
    for r in todo:
        body = backend_notes(r['tag']) or r['body']
        parts.append(f'## {r["tag"]}\n{body}')
    return {**todo[0], 'body': '\n\n'.join(parts)[:20000], 'span': [r['tag'] for r in todo]}


KIND = [(r'breaking', '⚠️ Ломающее'), (r'feat|feature|enhance|add', 'Новое'), (r'fix|bug|patch', 'Исправлено'),
        (r'perf', 'Быстрее'), (r'refactor', 'Переделано'), (r'chore|deps|ci|build|docs', '')]


def bullets(body: str) -> list[str]:
    """Пункты изменений из заметок: «- …» под заголовками разделов; без ссылок на коммиты, Docker-образов и служебного."""
    out, kind = [], None
    for line in body.splitlines():
        t = line.strip()
        if t.startswith('#'):
            h = t.lstrip('#').strip().lower()
            kind = next((name for rx, name in KIND if re.search(rx, h)), None) if not re.search(r'docker|image', h) else ''
            continue
        if not t.startswith(('- ', '* ')) or kind == '' or re.search(r'docker image|remnawave/backend:|ghcr\.io|compare changes|view full changelog', t, re.I):
            continue
        t = re.sub(r'\s*\(\[[0-9a-f]{6,}\]\([^)]*\)\)', '', t[2:])          # (hash-ссылка на коммит)
        t = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', t).replace('`', '').strip()  # [текст](ссылка) → текст
        if t and not re.match(r'bump version', t, re.I):
            out.append((kind + ': ' if kind else '') + t)
    return out


def api_paths() -> list[str]:
    """Методы API панели, которые вызывает бот: вызовы panel.call('МЕТОД', '/api/…') в его коде (и пути кэша панели)."""
    found = set()
    rx = re.compile(r"""call\s*[(,]\s*['"](GET|POST|PATCH|PUT|DELETE)['"]\s*,\s*f?['"](/api/[^'"]+)""")  # call('…' и to_thread(x.call, '…
    for f in Path(__file__).parent.glob('*.py'):
        src = f.read_text(encoding='utf-8')
        for m in rx.finditer(src):
            path = m.group(2).split('?')[0]
            if '{' in path:  # f-строка: дальше — подстановка (UUID), её кавычки обрезают совпадение
                path = path[:path.index('{')] + ':id'
            found.add(m.group(1) + ' ' + path.rstrip('/'))
    from .pcache import PATHS
    found |= {'GET ' + p for p in PATHS.values()}
    return sorted(found)


SYSTEM = """Ты помогаешь администратору Telegram-бота, который управляет панелью Remnawave через её HTTP API
(ставит ноды, хосты, профили, сквады, шаблоны подписки). Тебе дают заметки нового релиза панели (на английском),
текущие версии панелей администратора и список методов API, которыми пользуется бот.

Объясни по-русски, простыми словами, без маркетинга:
- summary — 2–3 предложения: главное в релизе;
- changes — до 8 пунктов: что поменялось, понятно для админа;
- bot_risk — "none" | "low" | "high": может ли это сломать работу бота (переименованы/удалены поля или методы из
  списка бота, изменился формат ответа, обязательные новые поля). Не выдумывай: если в заметках этого нет — "none" или "low";
- bot_why — одно предложение, почему такая оценка (назови методы, если затронуты);
- api_risk — "none" | "low" | "high": затронет ли изменение API сторонние интеграции — например Telegram-боты продажи
  подписок, которые создают пользователей, продлевают подписки, меняют сквады и лимиты (методы users, subscriptions,
  internal-squads, auth). api_why — одно предложение, что именно им проверить;
- node_update — нужно ли обновлять ноды вместе с панелью (true/false) и node_why — коротко почему;
- actions — 1–4 шага, что сделать администратору (например «обновить панель, потом ноды через бота»).
Ответ — строго JSON: {"summary": "", "changes": [], "bot_risk": "", "bot_why": "", "api_risk": "", "api_why": "",
"node_update": false, "node_why": "", "actions": []}"""


def ai_input(rel: dict, versions: list[str]) -> str:
    return (f'Релиз панели {rel["tag"]} ({rel["date"]}): {rel["name"]}\n\nВерсии панелей администратора: '
            f'{", ".join(versions) or "неизвестны"}\n\nМетоды API панели, которые вызывает бот:\n' + '\n'.join(api_paths())
            + f'\n\n<<<NOTES\n{rel["body"][:15000]}\nNOTES>>>')


def clean(raw: dict) -> dict:
    risk = raw.get('bot_risk') if raw.get('bot_risk') in ('none', 'low', 'high') else 'low'
    api = raw.get('api_risk') if raw.get('api_risk') in ('none', 'low', 'high') else 'low'
    return {'summary': str(raw.get('summary') or '')[:1200], 'changes': [str(x)[:300] for x in (raw.get('changes') or [])][:8],
            'bot_risk': risk, 'bot_why': str(raw.get('bot_why') or '')[:400], 'node_update': bool(raw.get('node_update')),
            'api_risk': api, 'api_why': str(raw.get('api_why') or '')[:400],
            'node_why': str(raw.get('node_why') or '')[:300], 'actions': [str(x)[:300] for x in (raw.get('actions') or [])][:4]}


def plain(rel: dict) -> dict:
    """Без нейросети: пункты изменений из заметок и простая проверка на ломающие изменения."""
    lines = bullets(rel['body'])
    lines.sort(key=lambda x: not x.startswith('⚠️'))
    breaking = bool(re.search(r'breaking|deprecat|removed|renamed', rel['body'], re.I))
    return {'summary': '', 'changes': [x[:200] for x in lines[:10]], 'bot_risk': 'high' if breaking else 'none',
            'api_risk': 'high' if breaking else 'none', 'api_why': 'есть ломающие изменения — проверьте свои интеграции' if breaking else '',
            'bot_why': 'в заметках есть «breaking/removed/renamed» — проверьте бота после обновления' if breaking else '',
            'node_update': False, 'node_why': '', 'actions': [], 'plain': True}


RISK = {'none': '🟢 Бота не затронет', 'low': '🟡 Бота вряд ли затронет', 'high': '🔴 Может сломать работу бота'}
API_RISK = {'none': '🟢 Интеграциям (ботам продажи подписок и т.п.) ничего не грозит', 'low': '🟡 Интеграции вряд ли заденет',
            'high': '🔴 Интеграции по API стоит проверить'}


def text(rel: dict, ex: dict, versions: list[str]) -> str:
    """Уведомление в Telegram (HTML)."""
    e = html.escape
    out = [f'🆕 <b>Вышла панель Remnawave {e(rel["tag"])}</b>' + (f' · у вас {e(", ".join(versions))}' if versions else '')]
    if len(rel.get('span') or []) > 1:
        out.append(f'Изменения сразу за {len(rel["span"])} релиза: {e(", ".join(reversed(rel["span"])))}')
    if ex.get('summary'):
        out += ['', e(ex['summary'])]
    if ex.get('changes'):
        out += ['', '<b>Что нового' + (' (из заметок релиза, на английском)' if ex.get('plain') else '') + ':</b>']
        out += ['• ' + e(x) for x in ex['changes']]
    out += ['', RISK[ex['bot_risk']] + (f' — {e(ex["bot_why"])}' if ex.get('bot_why') else '')]
    if ex.get('api_risk'):
        out.append(API_RISK[ex['api_risk']] + (f' — {e(ex["api_why"])}' if ex.get('api_why') else ''))
    if not ex.get('plain'):
        out.append(('⬆️ Ноды стоит обновить вместе с панелью' if ex.get('node_update') else '✅ Ноды обновлять не обязательно')
                   + (f' — {e(ex["node_why"])}' if ex.get('node_why') else ''))
    if ex.get('actions'):
        out += ['', '<b>Что сделать:</b>'] + [f'{i}. {e(a)}' for i, a in enumerate(ex['actions'], 1)]
    if rel.get('url'):
        out += ['', f'<a href="{e(rel["url"])}">Заметки релиза на GitHub</a>']
    return '\n'.join(out)[:4000]


def newer(rels: list[dict], seen: str, versions: list[str]) -> dict | None:
    """Самый свежий релиз новее и уже разобранного, и всех панелей администратора; None — сообщать не о чем."""
    if not rels:
        return None
    top = rels[0]
    floor = max([semver(v) for v in versions if semver(v)] + ([semver(seen)] if semver(seen) else []), default=None)
    return top if floor is not None and semver(top['tag']) > floor else None  # ничего не знаем — только запомнить


async def report(store, rels: list[dict], floor: str, versions: list[str]) -> tuple[dict, dict] | None:
    """Разбор последнего релиза для панели версии floor: (релиз с изменениями за все версии после floor, итог).
    Кэш в базе по паре «релиз + floor»: одна версия — один запрос к нейросети на всех. None — floor уже последний."""
    import asyncio

    from . import scripts as sc
    if not rels or (semver(floor) and semver(rels[0]['tag']) <= semver(floor)):
        return None
    key = f'{KEY}:{rels[0]["tag"]}:{floor or "-"}'
    hit = json.loads(store.get(key) or 'null')
    if hit:
        return hit['rel'], hit['ex']
    rel = await asyncio.to_thread(gather, rels, floor)
    ex = None
    if store.get('gemini_key'):
        try:
            g = sc.Gemini(store.get('gemini_key'), store.get('gemini_model'), store.get('gemini_proxy'), base=store.get('gemini_base') or None)
            ex = clean(await g.ask(SYSTEM, [{'role': 'user', 'text': ai_input(rel, versions)}]))
        except sc.ScriptError:
            ex = None
    ex = ex or plain(rel)
    small = {k: rel[k] for k in ('tag', 'name', 'url', 'date', 'span')}
    if not ex.get('plain'):  # без нейросети не кэшируем: появится ключ — разберём по-настоящему
        store.set(key, json.dumps({'rel': small, 'ex': ex}, ensure_ascii=False))
    return small, ex
