"""Нагрузка сервера бота: хватает ли VPS.

Бот работает в контейнере, но /proc/stat, /proc/meminfo и /proc/loadavg в контейнере показывают весь сервер —
поэтому видно и то, что занимают соседи (например, Bobr Cloud на том же сервере). Отдельно — сам бот: память,
процессор, база и журналы. Замер раз в минуту, история — сутки по 5 минут.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)

EVERY = 60
POINT = 300          # шаг истории
KEEP = 24 * 3600     # сутки
LIMITS = {'cpu': 80, 'mem': 85, 'disk': 90}


def read(path: str) -> str:
    try:
        return Path(path).read_text()
    except OSError:
        return ''


def cpu_times() -> tuple[int, int] | None:
    line = read('/proc/stat').split('\n', 1)[0].split()
    if not line or line[0] != 'cpu':
        return None
    v = list(map(int, line[1:]))
    return sum(v), v[3] + (v[4] if len(v) > 4 else 0)


def meminfo() -> dict:
    out = {}
    for ln in read('/proc/meminfo').splitlines():
        k, _, rest = ln.partition(':')
        try:
            out[k] = int(rest.split()[0]) * 1024
        except (IndexError, ValueError):
            pass
    return out


def proc_rss() -> int:
    for ln in read('/proc/self/status').splitlines():
        if ln.startswith('VmRSS:'):
            return int(ln.split()[1]) * 1024
    return 0


def dir_size(p: Path) -> int:
    total = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def cgroup_mem() -> tuple[int, int] | None:
    """Память контейнера и его лимит (cgroup v2), если лимит задан."""
    cur, mx = read('/sys/fs/cgroup/memory.current').strip(), read('/sys/fs/cgroup/memory.max').strip()
    if cur.isdigit() and mx.isdigit():
        return int(cur), int(mx)
    return None


class HostLoad:
    def __init__(self, data_dir: str | Path, store=None):
        self.data_dir = Path(data_dir)
        self.store = store
        self.started = time.time()
        self._cpu = cpu_times()
        self._proc = (time.time(), sum(os.times()[:2]))
        self.now: dict = {}
        self.history: list[dict] = []
        self._sizes_at = 0.0
        self._sizes = (0, 0)
        if store is not None:
            try:
                self.history = [x for x in json.loads(store.get('hostload') or '[]') if x.get('t', 0) > time.time() - KEEP]
            except ValueError:
                self.history = []

    def sample(self) -> dict:
        now = time.time()
        s: dict = {'t': int(now), 'cores': os.cpu_count() or 1}
        c = cpu_times()
        if c and self._cpu and c[0] > self._cpu[0]:
            s['cpu'] = round(100 * (1 - (c[1] - self._cpu[1]) / (c[0] - self._cpu[0])))
        self._cpu = c
        la = read('/proc/loadavg').split()
        if len(la) >= 3:
            s['load'] = [float(x) for x in la[:3]]
        m = meminfo()
        if m.get('MemTotal'):
            s['mem_total'] = m['MemTotal']
            s['mem_avail'] = m.get('MemAvailable', m.get('MemFree', 0))
            s['mem'] = round(100 * (1 - s['mem_avail'] / m['MemTotal']))
            s['swap'] = max(0, m.get('SwapTotal', 0) - m.get('SwapFree', 0))
        try:
            du = shutil.disk_usage(self.data_dir)
            s.update(disk_total=du.total, disk_free=du.free,  # как df: занято / (занято + доступно)
                     disk=round(100 * du.used / (du.used + du.free)) if du.used + du.free else 0)
        except OSError:
            pass
        t, cpu_s = now, sum(os.times()[:2])
        dt = t - self._proc[0]
        if dt > 0:
            s['bot_cpu'] = round(100 * (cpu_s - self._proc[1]) / dt / s['cores'], 1)
        self._proc = (t, cpu_s)
        s['bot_rss'] = proc_rss()
        cg = cgroup_mem()
        if cg:
            s['ct_mem'], s['ct_limit'] = cg
        up = read('/proc/uptime').split()
        if up:
            s['uptime'] = int(float(up[0]))
        if now - self._sizes_at > 600:  # размеры папок — раз в 10 минут: обход журнала не бесплатный
            self._sizes_at = now
            self._sizes = (os.path.getsize(self.data_dir / 'bot.db') if (self.data_dir / 'bot.db').exists() else 0,
                           dir_size(self.data_dir / 'logs'))
        s['db'], s['logs'] = self._sizes
        self.now = s
        if not self.history or s['t'] - self.history[-1]['t'] >= POINT - 5:
            self.history.append({k: s.get(k) for k in ('t', 'cpu', 'mem', 'disk', 'bot_rss')})
            self.history = [x for x in self.history if x['t'] > now - KEEP]
            if self.store is not None:
                self.store.set('hostload', json.dumps(self.history))
        return s

    async def run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.sample)
            except Exception:  # noqa: BLE001 — замер не должен ронять бота
                log.exception('hostload')
            await asyncio.sleep(EVERY)

    def verdict(self) -> tuple[str, list[str]]:
        """('ok' | 'warn' | 'bad', причины) — по последнему часу, а не по одному замеру."""
        hour = [x for x in self.history if x['t'] > time.time() - 3600] or ([self.now] if self.now else [])
        if not hour:
            return 'ok', []
        why, level = [], 'ok'

        def avg(k):
            v = [x[k] for x in hour if x.get(k) is not None]
            return sum(v) / len(v) if v else None
        cpu, mem = avg('cpu'), avg('mem')
        disk = self.now.get('disk')
        if cpu is not None and cpu >= LIMITS['cpu']:
            why.append(f'процессор в среднем {cpu:.0f}% за час')
        if mem is not None and mem >= LIMITS['mem']:
            why.append(f'память занята на {mem:.0f}%')
        if disk is not None and disk >= LIMITS['disk']:
            why.append(f'диск заполнен на {disk}%')
        if self.now.get('swap', 0) > 256 * 1024 * 1024 and (mem or 0) >= 70:
            why.append(f'используется подкачка ({self.now["swap"] // 2**20} МБ)')
        if why:
            level = 'bad' if len(why) > 1 or (cpu or 0) >= 95 or (mem or 0) >= 95 or (disk or 0) >= 97 else 'warn'
        return level, why

    def view(self) -> dict:
        level, why = self.verdict()
        return {'now': self.now, 'history': self.history, 'level': level, 'why': why, 'limits': LIMITS,
                'bot_uptime': int(time.time() - self.started)}
