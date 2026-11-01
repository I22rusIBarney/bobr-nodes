"""Автообновление самого бота.

Обновляет сервер, а не контейнер: таймер systemd раз в час запускает `bnodes _auto`. Тот смотрит, есть ли новая
версия в GitHub и готов ли для неё образ (тесты прошли), и спрашивает бота, можно ли сейчас:
`docker compose exec nodes python -m bot.selfupdate check` — код 0, если автообновление включено, сейчас нужный
час (с запасом в 3 часа, если бот был занят) и нет задач. После перезапуска бот пишет администраторам, что нового.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time

KEY = 'self_update'
WINDOW = 3  # часов после выбранного: бот был занят установкой — обновится чуть позже, но в ту же ночь


def cfg(store) -> dict:
    try:
        c = json.loads(store.get(KEY) or '{}')
    except ValueError:
        c = {}
    return {'on': bool(c.get('on')), 'hour': int(c.get('hour', 4)) % 24, 'tz': int(c.get('tz', 0)), 'last': c.get('last', '')}


def clean(raw: dict, cur: dict) -> dict:
    out = dict(cur)
    if 'on' in raw:
        out['on'] = bool(raw['on'])
    if 'hour' in raw:
        h = int(raw['hour'])
        if not 0 <= h <= 23:
            raise ValueError('Час: от 0 до 23')
        out['hour'] = h
    if 'tz' in raw:
        tz = int(raw['tz'])
        if not -14 * 60 <= tz <= 14 * 60:
            raise ValueError('Неверный часовой пояс')
        out['tz'] = tz
    return out


def due(c: dict, busy: bool, now: float | None = None) -> tuple[bool, str]:
    """Можно ли обновляться сейчас и почему нет."""
    if not c['on']:
        return False, 'автообновление выключено'
    t = dt.datetime.fromtimestamp(now if now is not None else time.time(), dt.timezone.utc) + dt.timedelta(minutes=c['tz'])
    if (t.hour - c['hour']) % 24 >= WINDOW:
        return False, f'не время: обновляюсь в {c["hour"]:02d}:00'
    if c.get('last') == t.strftime('%Y-%m-%d'):
        return False, 'сегодня уже обновлялся'
    if busy:
        return False, 'идут задачи — попробую через час'
    return True, t.strftime('%Y-%m-%d')


CHANGELOG = os.path.join(os.path.dirname(__file__), 'changelog.json')


def changelog() -> list[dict]:
    """История версий, новые сверху. Каждый выпуск: version, date, title, items [{kind: new|better|fix, text}]."""
    try:
        return json.load(open(CHANGELOG, encoding='utf-8'))
    except (OSError, ValueError):
        return []


def vkey(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in str(v).split('.'))
    except ValueError:
        return (0,)


def current() -> str:
    log = changelog()
    return log[0]['version'] if log else ''


def version(data_dir: str) -> dict:
    """Текущая версия: номер — из истории изменений, коммит и дата сборки — из файла, который пишет bnodes."""
    out = {'number': current()}
    try:
        sha, date, subject = open(os.path.join(data_dir, 'version.txt'), encoding='utf-8').read().rstrip('\n').split('\t', 2)
        out.update(sha=sha[:7], date=date, subject=subject)
    except (OSError, ValueError):
        pass
    return out


KIND_MARK = {'new': '✨', 'better': '⚡️', 'fix': '🛠'}


def whats_new(store) -> str | None:
    """После обновления: что добавилось с прошлой виденной версии. Первый запуск с версиями — только текущая."""
    import html
    log, seen = changelog(), store.get('seen_version')
    if not log or seen == log[0]['version']:
        return None
    store.set('seen_version', log[0]['version'])
    fresh = [x for x in log if not seen or vkey(x['version']) > vkey(seen)][:5] if seen else log[:1]
    if not fresh:
        return None
    lines = [f'🔄 <b>Бот обновился до версии {html.escape(log[0]["version"])}</b>' + (f' (была {html.escape(seen)})' if seen else '')]
    for r in fresh:
        lines.append(f'\n<b>{html.escape(r["version"])} — {html.escape(r["title"])}</b>')
        lines += [f'{KIND_MARK.get(i.get("kind"), "•")} {html.escape(i["text"])}' for i in r.get('items', [])[:8]]
    return '\n'.join(lines)


def take_note(data_dir: str) -> str | None:
    """Что нового после автообновления (пишет bnodes перед перезапуском). Читается один раз."""
    path = os.path.join(data_dir, 'update-note.txt')
    try:
        lines = open(path, encoding='utf-8').read().splitlines()
        os.remove(path)
    except OSError:
        return None
    if not lines:
        return None
    import html
    head = lines[0].split()
    text = '🔄 <b>Бот обновился</b>' + (f' {html.escape(head[0])} → {html.escape(head[1])}' if len(head) == 2 else '')
    changes = [x for x in lines[1:] if x.strip()][:12]
    if changes:
        text += '\n\n' + '\n'.join('• ' + html.escape(x[:120]) for x in changes)
    return text


def main(argv: list[str]) -> int:
    if argv[1:2] != ['check']:
        print('usage: python -m bot.selfupdate check')
        return 2
    from .store import Store
    data_dir = os.environ.get('DATA_DIR', 'data')
    store = Store(f'{data_dir}/bot.db')
    c = cfg(store)
    ok, why = due(c, bool(store.active_jobs()))
    if ok:
        store.set(KEY, json.dumps({**c, 'last': why}))
    print('ok' if ok else why)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
