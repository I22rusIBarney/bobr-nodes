"""Кэш ответов панели: ноды, профили, хосты по каждой группе.

Фоновые опросы (монитор, автопилот) кладут сюда то, что уже скачали, а веб-апп берёт отсюда, вместо того чтобы
ждать панель при каждом открытии страницы. Данные моложе FRESH отдаются как есть; постарше (до STALE) — тоже
отдаются сразу, но в фоне запрашиваются свежие. Старше STALE или кэша нет — ждём панель.
После любого изменения группы через веб-апп и после завершения задачи кэш группы сбрасывается.
"""
from __future__ import annotations

import asyncio
import logging
import time

from . import engine

log = logging.getLogger(__name__)

FRESH = 30         # секунд: моложе — не идём в панель вовсе
STALE = 10 * 60    # секунд: старше — не показываем, ждём панель

PATHS = {'nodes': '/api/nodes', 'profiles': '/api/config-profiles', 'hosts': '/api/hosts'}


class PanelCache:
    def __init__(self) -> None:
        self._data: dict[tuple[int, str, str], tuple[float, object]] = {}
        self._inflight: dict[tuple[int, str, str], asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()

    @staticmethod
    def _key(prof: dict, what: str) -> tuple[int, str, str]:
        # адрес панели в ключе: сменили панель у группы — старые данные не подхватятся
        return int(prof['id']), str(prof.get('panel_url') or '').rstrip('/'), what

    @staticmethod
    def panel(prof: dict) -> engine.Panel:
        return engine.Panel(prof['panel_url'], prof['panel_token'], prof.get('panel_cookie', ''), prof.get('panel_headers'))

    def put(self, prof: dict, what: str, data) -> None:
        """Положить то, что уже скачано из панели (фоновые опросы)."""
        self._data[self._key(prof, what)] = (time.time(), data)

    def age(self, prof: dict, what: str) -> float | None:
        hit = self._data.get(self._key(prof, what))
        return None if hit is None else time.time() - hit[0]

    def drop(self, pid: int) -> None:
        """Сбросить кэш группы: что-то в панели поменялось."""
        pid = int(pid)
        for k in [k for k in self._data if k[0] == pid]:
            self._data.pop(k, None)

    def _fetch(self, prof: dict, what: str) -> asyncio.Task:
        """Один поход в панель на ключ, даже если страницу открыли несколько раз подряд."""
        key = self._key(prof, what)
        t = self._inflight.get(key)
        if t is None or t.done():
            async def run():
                try:
                    data = await asyncio.to_thread(self.panel(prof).call, 'GET', PATHS[what])
                    self._data[key] = (time.time(), data)
                    return data
                finally:
                    self._inflight.pop(key, None)
            t = self._inflight[key] = asyncio.ensure_future(run())
            t.add_done_callback(lambda f: f.cancelled() or f.exception())  # ошибка фонового обновления — не в лог asyncio
        return t

    async def get(self, prof: dict, what: str, stale_ok: bool = True) -> tuple[object, bool]:
        """(данные, устарели ли). stale_ok: устаревшие отдаются сразу, а свежие уже запрошены в фоне;
        иначе ждём свежие (или уже идущий за ними запрос)."""
        hit = self._data.get(self._key(prof, what))
        age = None if hit is None else time.time() - hit[0]
        if age is not None and age < FRESH:
            return hit[1], False
        if stale_ok and age is not None and age < STALE:
            self._fetch(prof, what)
            return hit[1], True
        return await asyncio.shield(self._fetch(prof, what)), False


CACHE = PanelCache()
