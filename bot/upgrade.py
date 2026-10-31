"""Обновление нод: новый образ remnawave/node на сервере ноды — с проверкой и откатом.

Нода — контейнер remnanode под docker compose (так ставят Nuvrion и обычная инструкция Remnawave). Бот скачивает
образ нужной версии, в docker-compose подменяет ссылку на образ (у Nuvrion он закреплён по digest — закрепляем новый
digest), поднимает контейнер и ждёт, пока нода выйдет на связь с панелью. Не вышла за ~2 минуты — возвращает прежний
образ и прежний docker-compose.

Версия по умолчанию — последняя из той же основной ветки, что и панель (нода и панель должны совпадать по major).
"""
from __future__ import annotations

import json
import re
import shlex
import time
import urllib.request
from typing import Callable

from . import engine

MARK = 'BNODES_UPGRADE'
HUB = 'https://hub.docker.com/v2/namespaces/remnawave/repositories/node/tags?page_size=100&ordering=last_updated'
_cache: dict = {'at': 0.0, 'tags': []}

# контейнер ноды: remnanode или любой из образа remnawave/node; его compose-проект — по меткам docker compose
FIND = ('C=remnanode; docker inspect "$C" >/dev/null 2>&1 || '
        "C=$(docker ps -a --format '{{.Names}} {{.Image}}' | awk '$2 ~ /remnawave\\/node/ {print $1; exit}'); "
        'if [ -z "$C" ] || ! docker inspect "$C" >/dev/null 2>&1; then echo UP_NOCONTAINER; exit 0; fi; '
        "DIR=$(docker inspect -f '{{ index .Config.Labels \"com.docker.compose.project.working_dir\" }}' \"$C\"); "
        "FILES=$(docker inspect -f '{{ index .Config.Labels \"com.docker.compose.project.config_files\" }}' \"$C\"); "
        "SVC=$(docker inspect -f '{{ index .Config.Labels \"com.docker.compose.service\" }}' \"$C\"); "
        'if [ -z "$DIR" ] || [ -z "$FILES" ] || [ -z "$SVC" ]; then echo UP_NOCOMPOSE; exit 0; fi; '
        'ARGS=""; for f in $(echo "$FILES" | tr , " "); do ARGS="$ARGS -f $f"; done; '
        'up() { (cd "$DIR" && timeout 300 docker compose $ARGS up -d "$SVC" >/dev/null 2>&1); }; ')


def semver(s: str) -> tuple[int, int, int] | None:
    m = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', str(s or '').strip())
    return tuple(int(x) for x in m.groups()) if m else None


def hub_tags(timeout: float = 10) -> list[str]:
    """Стабильные версии remnawave/node с Docker Hub (кэш на час). Пусто — Docker Hub недоступен."""
    if time.time() - _cache['at'] < 3600 and _cache['tags']:
        return _cache['tags']
    try:
        with urllib.request.urlopen(urllib.request.Request(HUB, headers={'User-Agent': 'bobr-nodes'}), timeout=timeout) as r:
            data = json.load(r)
        tags = sorted({t['name'] for t in data.get('results', []) if semver(t.get('name'))}, key=semver, reverse=True)
    except Exception:  # noqa: BLE001 — нет связи с Docker Hub: версию можно указать вручную
        return _cache['tags']
    _cache.update(at=time.time(), tags=tags)
    return tags


def panel_version(panel: engine.Panel) -> str:
    """Версия панели Remnawave, если панель её отдаёт."""
    for path in ('/api/system/metadata', '/api/metadata'):
        try:
            d = panel.call('GET', path)
        except Exception:  # noqa: BLE001
            continue
        v = d.get('version') if isinstance(d, dict) else None
        if semver(v):
            return str(v).lstrip('v')
    return ''


def pick_target(tags: list[str], panel_ver: str) -> str:
    """Последняя версия ноды той же основной ветки, что панель; панель неизвестна — последняя вообще."""
    pv = semver(panel_ver)
    good = [t for t in tags if semver(t) and (not pv or semver(t)[0] == pv[0])]
    return max(good, key=semver) if good else 'latest'


def check_tag(tag: str) -> str:
    tag = str(tag or 'latest').strip()
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}', tag):
        raise ValueError('Версия: тег образа, например 2.3.1 или latest')
    return tag


def upgrade_cmd(tag: str) -> str:
    t = shlex.quote(check_tag(tag))
    return (
        f': {MARK}; {FIND}'
        "OLD_IMG=$(docker inspect -f '{{.Config.Image}}' \"$C\"); OLD_ID=$(docker inspect -f '{{.Image}}' \"$C\"); "
        'REPO=${OLD_IMG%%@*}; T=${REPO##*/}; case "$T" in *:*) REPO=${REPO%:*};; esac; '
        f'NEW="$REPO:"{t}; '
        'if ! timeout 900 docker pull -q "$NEW" >/dev/null 2>&1; then echo "UP_PULLFAIL $NEW"; exit 0; fi; '
        "NEW_ID=$(docker image inspect -f '{{.Id}}' \"$NEW\"); NEW_DG=$(docker image inspect -f '{{index .RepoDigests 0}}' \"$NEW\"); "
        'if [ "$NEW_ID" = "$OLD_ID" ]; then echo UP_SAME; exit 0; fi; '
        'case "$OLD_IMG" in *@sha256:*) REF="$NEW_DG";; *) REF="$NEW";; esac; '
        'F=""; for f in $(echo "$FILES" | tr , " "); do grep -qF "$OLD_IMG" "$f" 2>/dev/null && F="$f"; done; '
        'if [ -z "$F" ]; then echo UP_NOREF; exit 0; fi; '
        'cp -p "$F" "$F.bnodes-prev" && printf "%s\\n%s\\n" "$OLD_ID" "$OLD_IMG" > "$DIR/.bnodes-prev"; '
        # в ссылке на образ из особых для sed символов бывает только точка
        'ESC=$(printf "%s" "$OLD_IMG" | sed \'s/\\./\\\\./g\'); sed -i "s|$ESC|$REF|g" "$F"; '
        'if ! up; then cp -p "$F.bnodes-prev" "$F"; case "$OLD_IMG" in *@sha256:*) ;; *) docker tag "$OLD_ID" "$OLD_IMG";; esac; '
        'up; echo UP_UPFAIL; exit 0; fi; '
        'echo "UP_DONE $REF"')


def check_cmd() -> str:
    return (f': {MARK}_CHECK; {FIND}'
            "S=$(docker inspect -f '{{.State.Running}} {{.State.Restarting}}' \"$C\"); "
            'if [ "$S" = "true false" ]; then echo UP_RUNNING; else echo "UP_STOPPED $S"; fi')


def rollback_cmd() -> str:
    return (f': {MARK}_ROLLBACK; {FIND}'
            'if [ ! -f "$DIR/.bnodes-prev" ]; then echo RB_NOBACKUP; exit 0; fi; '
            'OLD_ID=$(sed -n 1p "$DIR/.bnodes-prev"); OLD_IMG=$(sed -n 2p "$DIR/.bnodes-prev"); '
            'for f in $(echo "$FILES" | tr , " "); do [ -f "$f.bnodes-prev" ] && cp -p "$f.bnodes-prev" "$f"; done; '
            'case "$OLD_IMG" in *@sha256:*) ;; *) docker tag "$OLD_ID" "$OLD_IMG";; esac; '
            'if up; then echo RB_DONE; else echo RB_FAIL; fi')


ERRORS = {
    'UP_NOCONTAINER': 'на сервере нет контейнера ноды (образ remnawave/node) — обновить нечего',
    'UP_NOCOMPOSE': 'контейнер ноды запущен не через docker compose — обновите его вручную',
    'UP_NOREF': 'не нашёл ссылку на образ ноды в docker-compose — обновите вручную',
    'UP_UPFAIL': 'docker compose не поднял новую версию — вернул прежнюю',
}


def node_info(panel: engine.Panel, uuid: str) -> dict | None:
    return next((n for n in panel.call('GET', '/api/nodes') if n['uuid'] == uuid), None)


def node_version(n: dict | None) -> str:
    """Версия ноды из ответа панели: старые Remnawave — nodeVersion, новые (2.2+) — versions.node."""
    n = n or {}
    return str(n.get('nodeVersion') or (n.get('versions') or {}).get('node') or '')


def xray_version(n: dict | None) -> str:
    n = n or {}
    return str(n.get('xrayVersion') or (n.get('versions') or {}).get('xray') or '')


def upgrade_node(node: dict, prof: dict, logfile, log: Callable[[str], None], stage: Callable[[str], None],
                 check_tries: int = 12, check_every: float = 10) -> str:
    """node: node_uuid, name, ip, tag, ssh_*. Возвращает итог строкой; StepError — не обновилась (и откачена)."""
    panel = engine.Panel(prof['panel_url'], prof['panel_token'], prof.get('panel_cookie', ''), prof.get('panel_headers'))
    pn = node_info(panel, node['node_uuid'])
    if not pn:
        raise engine.StepError('ноды больше нет в панели')
    was = node_version(pn)
    stage('ssh')
    r = engine.Remote({**node, 'ip': node.get('ip') or pn['address']}, logfile)
    rc, out = r.run('id -u', 60)
    if out.strip().splitlines()[-1:] != ['0']:
        raise engine.StepError('нет прав root на сервере ноды')
    stage('install')
    tag = check_tag(node.get('tag'))
    log(f'скачиваю remnawave/node:{tag}' + (f' (сейчас {was})' if was else ''))
    rc, out = r.run(upgrade_cmd(tag), 1200)
    out = engine.strip_ansi(out)
    for key, text in ERRORS.items():
        if key in out:
            raise engine.StepError(text)
    m = re.search(r'UP_PULLFAIL (\S+)', out)
    if m:
        raise engine.StepError(f'не скачался образ {m.group(1)} — проверьте версию и доступ сервера к Docker Hub')
    if 'UP_SAME' in out:
        log('на ноде уже эта версия')
        return f'уже {tag}' + (f' ({was})' if was else '')
    if 'UP_DONE' not in out:
        raise engine.StepError('обновление прервалось: ' + (out.strip()[-300:] or f'код {rc}'))
    log('новый образ запущен — жду связи с панелью')
    stage('check')
    why = ''
    for _ in range(check_tries):
        time.sleep(check_every)
        _, st = r.run(check_cmd(), 60)
        cur = node_info(panel, node['node_uuid']) or {}
        if 'UP_RUNNING' in st and cur.get('isConnected'):
            now = node_version(cur) or tag
            log(f'нода на связи: {was or "?"} → {now}')
            return f'{was} → {now}' if was else f'версия {now}'
        why = 'контейнер не запущен' if 'UP_RUNNING' not in st else 'нет связи с панелью'
    log(f'новая версия не заработала ({why}) — возвращаю прежнюю')
    _, rb = r.run(rollback_cmd(), 600)
    back = 'вернул прежнюю' if 'RB_DONE' in rb else 'и вернуть прежнюю не удалось — посмотрите сервер'
    raise engine.StepError(f'новая версия не вышла на связь с панелью ({why}) — {back}'
                           + (f' ({was})' if was and 'RB_DONE' in rb else ''))
