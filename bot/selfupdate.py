"""Автообновление самого бота.

Обновляет сервер, а не контейнер: таймер systemd раз в час запускает `bnodes _auto`. Тот смотрит, есть ли новая
версия в GitHub и готов ли для неё образ (тесты прошли), и спрашивает бота, можно ли сейчас:
`docker compose exec nodes python -m bot.selfupdate check` — код 0, если автообновление включено, сейчас нужный
час (с запасом в 3 часа, если бот был занят) и нет задач. После перезапуска бот пишет администраторам, что нового.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

KEY = 'self_update'
WINDOW = 3  # часов после выбранного: бот был занят установкой — обновится чуть позже, но в ту же ночь


def cfg(store) -> dict:
    try:
        c = json.loads(store.get(KEY) or '{}')
    except ValueError:
        c = {}
    return {'on': bool(c.get('on')), 'hour': int(c.get('hour', 4)) % 24, 'tz': int(c.get('tz', 0)), 'last': c.get('last', ''),
            'once': bool(c.get('once'))}  # once — «обновить ночью» один раз, даже если автообновление выключено


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
    if not c['on'] and not c.get('once'):
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


# ───────── проверка новой версии на GitHub ─────────
NOW_FILE = 'update-now'   # «обновить сейчас»: файл в data/, его видит systemd (bnodes-update-now.path) и запускает обновление
_REMOTE: dict = {}        # кэш ответа GitHub на 5 минут — у API без токена лимит 60 запросов в час


def repo_slug(data_dir: str | Path) -> str:
    url = 'https://github.com/I22rusIBarney/bobr-nodes.git'
    try:
        url = (Path(data_dir) / 'origin.txt').read_text().strip() or url
    except OSError:
        pass
    return re.sub(r'(\.git)?/*$', '', re.sub(r'^.*github\.com[:/]', '', url))


def current_sha(data_dir: str | Path) -> str:
    try:
        sha = (Path(data_dir) / 'version.txt').read_text().split('\t', 1)[0].strip()
    except OSError:
        return ''
    return sha if re.fullmatch(r'[0-9a-f]{40}', sha) else ''


def newer(log: list[dict], than: str) -> list[dict]:
    """Выпуски новее версии than (по истории изменений новой версии)."""
    return [r for r in log if vkey(r['version']) > vkey(than)] if than else log[:1]


async def check_remote(data_dir: str | Path, force: bool = False, client=None) -> dict:
    """Есть ли новая версия: последний коммит main, прошёл ли он тесты (и собран ли образ), что в нём нового.
    state: latest — новее нет; ready — можно ставить; testing — тесты идут; failed — не прошли; unknown — GitHub не ответил."""
    import httpx
    slug, cur = repo_slug(data_dir), current_sha(data_dir)
    hit = _REMOTE.get(slug)
    if hit and not force and time.time() - hit['at'] < 300 and hit['cur'] == cur:
        return hit['res']
    res = {'state': 'unknown', 'current': current(), 'current_sha': cur[:7], 'version': '', 'sha': '', 'changes': [], 'checked_at': int(time.time())}
    own = client is None
    c = client or httpx.AsyncClient(timeout=15, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'bobr-nodes'})
    try:
        r = await c.get(f'https://api.github.com/repos/{slug}/commits/main')
        r.raise_for_status()
        sha = r.json()['sha']
        res['sha'] = sha[:7]
        if cur and sha == cur:
            res['state'] = 'latest'
        else:
            try:
                log = (await c.get(f'https://raw.githubusercontent.com/{slug}/{sha}/bot/changelog.json')).json()
            except Exception:  # noqa: BLE001 — истории нет: покажем без неё
                log = []
            if isinstance(log, list) and log:
                res['version'] = log[0].get('version', '')
                res['changes'] = newer(log, current())[:5]
            runs = (await c.get(f'https://api.github.com/repos/{slug}/actions/runs', params={'head_sha': sha, 'per_page': 5})).json()
            runs = [x for x in runs.get('workflow_runs') or [] if x.get('event') == 'push']
            if not runs:
                res['state'] = 'testing'
            elif any(x.get('conclusion') == 'failure' for x in runs):
                res['state'] = 'failed'
            elif all(x.get('status') == 'completed' and x.get('conclusion') == 'success' for x in runs):
                res['state'] = 'ready'
            else:
                res['state'] = 'testing'
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).info('update check: %s', e)
        res['error'] = 'GitHub не ответил — попробуйте позже'
    finally:
        if own:
            await c.aclose()
    if res['state'] != 'unknown':
        _REMOTE[slug] = {'at': time.time(), 'cur': cur, 'res': res}
    return res


def request_now(store, data_dir: str | Path, sha: str) -> None:
    """Обновить сейчас: файл увидит systemd на сервере и сразу запустит обновление (без ожидания часа)."""
    (Path(data_dir) / NOW_FILE).write_text(sha or 'now')
    store.set('self_update_req', json.dumps({'sha': sha, 'at': int(time.time()), 'mode': 'now'}))


def request_night(store, sha: str) -> dict:
    """Обновить один раз в ближайшее «время обновления», даже если автообновление выключено."""
    c = cfg(store)
    store.set(KEY, json.dumps({**c, 'once': True}))
    store.set('self_update_req', json.dumps({'sha': sha, 'at': int(time.time()), 'mode': 'night'}))
    return cfg(store)


def main(argv: list[str]) -> int:
    if argv[1:2] != ['check']:
        print('usage: python -m bot.selfupdate check [--now]')
        return 2
    from .store import Store
    data_dir = os.environ.get('DATA_DIR', 'data')
    store = Store(f'{data_dir}/bot.db')
    c = cfg(store)
    busy = bool(store.active_jobs())
    if '--now' in argv or store.get('self_update_pending'):  # «Обновить сейчас» — без расписания, но не посреди установки
        if busy:
            store.set('self_update_pending', '1')  # таймер раз в час попробует снова, когда задачи закончатся
            print('идут задачи — обновлюсь, когда закончатся')
            return 1
        store.set('self_update_pending', '')
        print('ok')
        return 0
    ok, why = due(c, busy)
    if ok:
        store.set(KEY, json.dumps({**c, 'last': why, 'once': False}))
    print('ok' if ok else why)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
