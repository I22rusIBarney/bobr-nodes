"""Автопилот: то, что бот делает сам по расписанию.

- автообновление нод группы: в выбранный час (каждый день или раз в неделю) — по одной ноде, с проверкой и откатом;
  после первого отката очередь останавливается (скорее всего, версия не подходит к панели); итог — одним сообщением;
- нагрузка нод: раз в 15 минут по SSH — процессор, память, диск, трафик; держится высокой ~45 минут — сообщение
  с советом (тариф помощнее или ещё одна нода в той же стране), спала — сообщение;
- аренда: раз в 6 часов — срок серверов группы в Bobr Cloud и баланс; кончается через 3 дня без автопродления или
  денег не хватит — сообщение; по настройке группы бот сам включает автопродление серверам;
- утренняя сводка: в выбранный час каждому, кто её включил, — по его группам.

Время — по часовому поясу человека, который настраивал (браузер сообщает смещение).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import json
import logging
import re
import time

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from . import engine, plugins, services as svc, sshaccess, upgrade as up
from .bcloud import BcloudError
from .pcache import CACHE
from .runner import bcloud_from, buyer_of
from .store import ACTIVE, Store

log = logging.getLogger(__name__)

LOAD_EVERY = 15 * 60
PANEL_EVERY = 3 * 60         # нагрузка из панели (Remnawave 2.2+ отдаёт её сама) — без SSH и чаще
RENT_EVERY = 6 * 3600
DEAD_DAYS = 7                 # нода без связи дольше — в отчёт об уборке
CLEAN_GRACE = 6 * 3600       # свежую покупку не считаем лишней: установка могла ещё идти
CLEAN_REMIND = 7 * 86400     # тот же список — напоминаем раз в неделю
PANEL_DOWN = 5 * 60           # панель молчит дольше — пишем
PRESET_EVERY = 24 * 3600     # списки сканеров в фильтре входящих (плагин ноды)
HIGH = {'cpu': 85, 'mem': 90, 'disk': 90}
HIGH_CYCLES = 3              # подряд высоких замеров до сообщения (~45 минут)
REALERT = 12 * 3600
RENT_DAYS = 3
WEEKDAYS = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']
MARK = 'BNODES_LOAD'

LOAD_CMD = (f': {MARK}; a=$(head -1 /proc/stat); sleep 1; b=$(head -1 /proc/stat); echo "CPU1 $a"; echo "CPU2 $b"; '
            "free -b | awk '/^Mem:/{print \"MEM\",$2,$7}'; df -P / | awk 'NR==2{print \"DISK\",$5}'; "
            "awk 'NR>2{gsub(\":\",\" \"); print \"NET\",$1,$2,$10}' /proc/net/dev; echo \"NPROC $(nproc)\"")


# ───────── настройки ─────────
def auto_cfg(prof: dict) -> dict:
    a = prof.get('auto') or {}
    return {'update': bool(a.get('update')), 'hour': int(a.get('hour', 4)), 'weekday': a.get('weekday'),
            'tz': int(a.get('tz', 0)), 'load': a.get('load', True) is not False, 'renew': a.get('renew')}


def clean_auto(raw: dict, cur: dict) -> dict:
    a = auto_cfg(cur)
    if 'update' in raw:
        a['update'] = bool(raw['update'])
    if 'hour' in raw:
        h = raw['hour']
        if not isinstance(h, int) or isinstance(h, bool) or not 0 <= h <= 23:
            raise ValueError('Час: от 0 до 23')
        a['hour'] = h
    if 'weekday' in raw:
        w = raw['weekday']
        if w is not None and (not isinstance(w, int) or isinstance(w, bool) or not 0 <= w <= 6):
            raise ValueError('День недели: 0 (пн) … 6 (вс) или каждый день')
        a['weekday'] = w
    if 'tz' in raw:
        a['tz'] = tz_of(raw['tz'])
    if 'load' in raw:
        a['load'] = bool(raw['load'])
    if 'renew' in raw:
        a['renew'] = None if raw['renew'] is None else bool(raw['renew'])
    return a


def tz_of(v) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or not -14 * 60 <= v <= 14 * 60:
        raise ValueError('Часовой пояс: смещение в минутах')
    return v


def summary_cfg(store: Store, uid: int) -> dict:
    try:
        s = json.loads(store.get(f'summary:{uid}') or '{}')
    except ValueError:
        s = {}
    return {'on': bool(s.get('on')), 'hour': int(s.get('hour', 9)), 'tz': int(s.get('tz', 0))}


def local_now(tz: int, now: float | None = None) -> dt.datetime:
    return dt.datetime.fromtimestamp(now if now is not None else time.time(), dt.timezone.utc) + dt.timedelta(minutes=tz)


def due(hour: int, tz: int, last_date: str, weekday=None, now: float | None = None) -> str | None:
    """Пора ли запускать: местный час наступил, сегодня ещё не запускали, и день недели подходит. Вернёт дату."""
    t = local_now(tz, now)
    if t.hour != hour or (weekday is not None and t.weekday() != weekday):
        return None
    d = t.strftime('%Y-%m-%d')
    return None if d == last_date else d


# ───────── нагрузка ─────────
def parse_load(out: str, prev: dict | None, now: float) -> dict:
    cpu1 = re.search(r'CPU1 cpu\s+([\d\s]+)', out)
    cpu2 = re.search(r'CPU2 cpu\s+([\d\s]+)', out)
    res: dict = {'at': now}
    if cpu1 and cpu2:
        a, b = [list(map(int, m.group(1).split())) for m in (cpu1, cpu2)]
        tot = sum(b) - sum(a)
        idle = (b[3] + (b[4] if len(b) > 4 else 0)) - (a[3] + (a[4] if len(a) > 4 else 0))
        res['cpu'] = round(100 * (1 - idle / tot)) if tot > 0 else 0
    m = re.search(r'MEM (\d+) (\d+)', out)
    if m and int(m.group(1)):
        res['mem'] = round(100 * (1 - int(m.group(2)) / int(m.group(1))))
    m = re.search(r'DISK (\d+)%', out)
    if m:
        res['disk'] = int(m.group(1))
    rx = tx = 0
    for name, r, t in re.findall(r'NET (\S+) (\d+) (\d+)', out):
        if name != 'lo' and not name.startswith(('docker', 'veth', 'br-')):
            rx, tx = rx + int(r), tx + int(t)
    res['rx_total'], res['tx_total'] = rx, tx
    if prev and prev.get('at') and now > prev['at'] and rx >= prev.get('rx_total', 0) and tx >= prev.get('tx_total', 0):
        secs = now - prev['at']
        res['rx'] = round((rx - prev['rx_total']) * 8 / secs / 1e6, 1)
        res['tx'] = round((tx - prev['tx_total']) * 8 / secs / 1e6, 1)
    m = re.search(r'NPROC (\d+)', out)
    if m:
        res['cores'] = int(m.group(1))
    return res


HIST_KEEP = 24 * 3600


def load_hist(prev: list | None, s: dict, now: float, keep: int = 200) -> list[dict]:
    """История замеров ноды за сутки для графика: процессор, память, трафик."""
    pts = [x for x in (prev or []) if isinstance(x, dict) and x.get('t', 0) > now - HIST_KEEP]
    pts.append({'t': int(now), **{k: s.get(k) for k in ('cpu', 'mem', 'rx', 'tx')}})
    return pts[-keep:]


def panel_sample(n: dict) -> dict | None:
    """Нагрузка ноды из ответа панели: system.stats (память, средняя загрузка, трафик интерфейса).
    Процессор — средняя загрузка за минуту на ядро, в процентах (точнее панель не отдаёт). Нет данных — None."""
    sysd = n.get('system') if isinstance(n.get('system'), dict) else {}
    info, st = sysd.get('info') or {}, sysd.get('stats') or {}
    if not st:
        return None
    out: dict = {}
    la, cpus = st.get('loadAvg') or [], info.get('cpus') or 0
    if la and isinstance(la[0], (int, float)) and cpus:
        out['cpu'] = min(100, round(100 * la[0] / cpus))
    used, free = st.get('memoryUsed'), st.get('memoryFree')
    total = info.get('memoryTotal') or ((used or 0) + (free or 0))
    if isinstance(used, (int, float)) and total:
        out['mem'] = min(100, round(100 * used / total))
    iface = st.get('interface') or {}
    if isinstance(iface.get('rxBytesPerSec'), (int, float)):
        out['rx'] = round(iface['rxBytesPerSec'] * 8 / 1e6, 1)
        out['tx'] = round((iface.get('txBytesPerSec') or 0) * 8 / 1e6, 1)
    if cpus:
        out['cores'] = cpus
    return out or None


def hot(sample: dict) -> list[str]:
    """Что упирается: ['cpu', 'mem', 'disk']."""
    return [k for k, lim in HIGH.items() if (sample.get(k) or 0) >= lim]


def load_text(s: dict) -> str:
    parts = []
    if s.get('cpu') is not None:
        parts.append(f'процессор {s["cpu"]}%')
    if s.get('mem') is not None:
        parts.append(f'память {s["mem"]}%')
    if s.get('disk') is not None:
        parts.append(f'диск {s["disk"]}%')
    if s.get('rx') is not None:
        parts.append(f'трафик ↓{s["rx"]:g} ↑{s["tx"]:g} Мбит/с')
    return ', '.join(parts)


ADVICE = {'cpu': 'не хватает процессора', 'mem': 'не хватает памяти', 'disk': 'кончается место на диске'}


class Autopilot:
    def __init__(self, store: Store, runner, bot, access, public_url: str = '', every: float = 60):
        self.store, self.runner, self.bot, self.access = store, runner, bot, access
        self.public_url = public_url
        self.every = every
        self._load_at = 0.0
        self._pload_at = 0.0
        self._rent_at = 0.0
        self._preset_at = 0.0
        self._bg: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        self.hostload = None  # нагрузка сервера бота (задаётся в main)

    # ── служебное ──
    def get(self, key: str) -> dict:
        try:
            v = json.loads(self.store.get(key) or '{}')
        except ValueError:
            v = {}
        return v if isinstance(v, dict) else {}

    def put(self, key: str, v: dict) -> None:
        self.store.set(key, json.dumps(v, ensure_ascii=False))

    def panel(self, prof: dict) -> engine.Panel:
        return engine.Panel(prof['panel_url'], prof['panel_token'], prof.get('panel_cookie', ''), prof.get('panel_headers'))

    def button(self):
        if not self.public_url.startswith('https://'):
            return None
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text='⚙️ Панель управления', web_app=WebAppInfo(url=self.public_url.rstrip('/') + '/admin'))]])

    async def say(self, prof: dict, text: str, markup=None) -> None:
        for uid in self.access.recipients(prof):
            await self.send(uid, text, markup)

    async def send(self, uid: int, text: str, markup=None) -> None:
        try:
            await self.bot.send_message(uid, text, parse_mode='HTML', reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:  # noqa: BLE001
            log.info('autopilot notify %s: %s', uid, e)

    def _bg_task(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    # ── цикл ──
    async def run(self) -> None:
        await asyncio.sleep(45)
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — автопилот не должен ронять бота
                log.exception('autopilot tick')
            await asyncio.sleep(self.every)

    async def tick(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        async with self._lock:
            for prof in self.store.profiles():
                try:
                    await self.drive_update(prof, now)
                except Exception:  # noqa: BLE001
                    log.exception('autopilot update %s', prof.get('id'))
            await self.summaries(now)
            await self.host_alert(now)
        if now - self._pload_at >= PANEL_EVERY:
            self._pload_at = now
            self._bg_task(self.panel_load())
        if now - self._load_at >= LOAD_EVERY:
            self._load_at = now
            self._bg_task(self.check_load())
        if now - self._rent_at >= RENT_EVERY:
            self._rent_at = now
            self._bg_task(self.check_rent())
            self._bg_task(self.report_cleanup())
        if now - self._preset_at >= PRESET_EVERY:
            self._preset_at = now
            self._bg_task(self.refresh_presets())

    # ───────── автообновление ─────────
    async def drive_update(self, prof: dict, now: float) -> None:
        cfg, key = auto_cfg(prof), f'ap:upd:{prof["id"]}'
        st = self.get(key)
        if st.get('job'):
            j = self.store.job(int(st['job']))
            if j and j['status'] in ACTIVE:
                return
            (st.setdefault('done', []) if j and j['status'] == 'done' else st.setdefault('failed', [])).append(
                {'name': (j or {}).get('data', {}).get('name', '?'), 'result': (j or {}).get('data', {}).get('result')
                 or (j or {}).get('error', '')[:160]})
            st['job'] = None
            if j and j['status'] == 'failed':
                st['stopped'] = st.get('queue') or []
                st['queue'] = []  # после отката не продолжаем: скорее всего, версия не подходит к панели
            self.put(key, st)
        if st.get('queue'):
            uuid = st['queue'].pop(0)
            self.put(key, st)
            try:
                nodes = await asyncio.to_thread(lambda: self.panel(prof).call('GET', '/api/nodes'))
                n = next((x for x in nodes if x['uuid'] == uuid), None)
                if n:
                    st['job'] = await svc.start_upgrade(self.store, self.runner, prof, n, st['tag'], 0)
            except (engine.StepError, ValueError) as e:
                st.setdefault('failed', []).append({'name': uuid[:8], 'result': str(e)[:160]})
            self.put(key, st)
            return
        if st.get('started') and not st.get('reported'):
            st['reported'] = True
            self.put(key, st)
            await self.report_update(prof, st)
            return
        if not cfg['update']:
            return
        date = due(cfg['hour'], cfg['tz'], st.get('date', ''), cfg['weekday'], now)
        if not date:
            return
        st = {'date': date}
        self.put(key, st)
        try:
            pl = self.panel(prof)
            nodes, tags, pv = await asyncio.gather(asyncio.to_thread(pl.call, 'GET', '/api/nodes'),
                                                   asyncio.to_thread(up.hub_tags), asyncio.to_thread(up.panel_version, pl))
        except engine.StepError as e:
            log.info('autopilot update %s: %s', prof['id'], e)
            return
        tag = up.pick_target(tags, pv)
        if tag == 'latest':  # без списка версий не знаем, что устарело — ночью наугад не обновляем
            return
        old = [n['uuid'] for n in nodes if not n.get('isDisabled') and n.get('isConnected')
               and up.semver(up.node_version(n)) and up.semver(up.node_version(n)) < up.semver(tag)]
        if old:
            st.update(tag=tag, queue=old, done=[], failed=[], started=now, reported=False)
            self.put(key, st)

    async def report_update(self, prof: dict, st: dict) -> None:
        done, failed = st.get('done') or [], st.get('failed') or []
        lines = [f'⬆️ <b>Автообновление</b> · {html.escape(prof["name"])} → {html.escape(st.get("tag", ""))}']
        if done:
            lines.append(f'Обновлено: {len(done)} — ' + ', '.join(html.escape(x['name']) for x in done[:15]))
        for x in failed:
            lines.append(f'❌ {html.escape(x["name"])}: {html.escape(x["result"] or "")}')
        if st.get('stopped'):
            lines.append(f'Остальные ({len(st["stopped"])}) не трогал: после отката новая версия, похоже, не подходит. '
                         'Проверьте версию панели и обновите вручную.')
        await self.say(prof, '\n'.join(lines))

    # ───────── нагрузка ─────────
    async def check_load(self) -> None:
        sem = asyncio.Semaphore(4)
        tasks = []
        for prof in self.store.profiles():
            if not auto_cfg(prof)['load']:
                continue
            try:
                nodes = await asyncio.to_thread(lambda p=prof: self.panel(p).call('GET', '/api/nodes'))
            except engine.StepError:
                continue
            for n in nodes:
                if n.get('isDisabled'):
                    continue
                ssh = svc.node_ssh(self.store, self.runner, prof['id'], n['uuid'], n['address'])
                if ssh:
                    tasks.append(self.sample(prof, n, ssh, sem))
        await asyncio.gather(*tasks)

    async def sample(self, prof: dict, n: dict, ssh: dict, sem: asyncio.Semaphore) -> None:
        key = f'ap:load:{prof["id"]}:{n["uuid"]}'
        prev = self.get(key)
        bd = svc.bot_jobs_by_ip(self.store, prof['id']).get(n['address']) or {}
        node = {'ip': bd.get('ip') or n['address'], **sshaccess.to_node(ssh, self.runner.bot_key()[0]),
                'known_hosts': self.runner.known_hosts()}
        async with sem:
            try:
                _, out = await asyncio.to_thread(engine.Remote(node, None).run, LOAD_CMD, 40)
            except Exception as e:  # noqa: BLE001 — нет входа: просто пропускаем замер
                self.put(key, {**prev, 'error': str(e)[:160], 'error_at': time.time()})
                return
        now = time.time()
        s = parse_load(engine.strip_ansi(out), prev, now)
        h = hot(s)
        s['high'] = (int(prev.get('high') or 0) + 1) if h else 0
        s['alerted'] = prev.get('alerted') or 0
        s['name'], s['country'] = n['name'], n.get('countryCode') or ''
        s['hist'] = load_hist(prev.get('hist'), s, now)
        if h and s['high'] >= HIGH_CYCLES and now - s['alerted'] >= REALERT:
            s['alerted'] = now
            why = ' и '.join(ADVICE[k] for k in h)
            await self.say(prof, f'🔥 <b>{html.escape(n["name"])}</b> · {html.escape(prof["name"])}\n'
                                 f'{load_text(s)} — уже ~{s["high"] * LOAD_EVERY // 60} мин.\n\n'
                                 f'Ноде {why}: возьмите тариф помощнее или поставьте ещё одну ноду в этой стране — '
                                 'нагрузка разделится.', self.button())
        elif not h and prev.get('alerted') and prev.get('high', 0) >= HIGH_CYCLES:
            s['alerted'] = 0
            await self.say(prof, f'✅ <b>{html.escape(n["name"])}</b> · {html.escape(prof["name"])}\nнагрузка спала: {load_text(s)}.')
        self.put(key, s)

    async def panel_load(self, now: float | None = None) -> None:
        """Нагрузка всех нод из панелей групп: один запрос на группу, история за сутки."""
        for prof in self.store.profiles():
            t = now if now is not None else time.time()
            try:
                nodes = await asyncio.to_thread(lambda p=prof: self.panel(p).call('GET', '/api/nodes'))
            except (engine.StepError, OSError, ValueError) as e:
                await self.panel_down(prof, str(e), t)
                continue
            CACHE.put(prof, 'nodes', nodes)  # веб-апп откроет список нод без похода в панель
            await self.panel_up(prof, t)
            self.track_offline(prof, nodes, t)
            await self.plugin_guard(prof, nodes)
            for n in nodes:
                smp = panel_sample(n) if isinstance(n, dict) and n.get('uuid') else None
                if not smp:
                    continue
                key = f'ap:pload:{prof["id"]}:{n["uuid"]}'
                prev = self.get(key)
                self.put(key, {**smp, 'at': t, 'hist': load_hist(prev.get('hist'), smp, t, keep=600)})

    # ───────── сторож панели ─────────
    async def panel_down(self, prof: dict, err: str, now: float) -> None:
        """Панель не ответила. Молчит дольше PANEL_DOWN — одно сообщение, а не на каждый опрос."""
        key = f'ap:pdown:{prof["id"]}'
        st = self.get(key) or {'since': now}
        st['err'] = err[:300]
        if not st.get('told') and now - st['since'] >= PANEL_DOWN:
            st['told'] = True
            mins = int((now - st['since']) // 60)
            await self.say(prof, f'🔴 <b>Панель «{html.escape(prof.get("name", ""))}» не отвечает</b> уже {mins} мин\n\n'
                                 f'<code>{html.escape(err[:300])}</code>\n\n'
                                 'Пока панель недоступна, ноды работают, но бот не может их обслуживать. '
                                 'Напишу, когда она вернётся.', self.button())
        self.put(key, st)

    async def panel_up(self, prof: dict, now: float) -> None:
        key = f'ap:pdown:{prof["id"]}'
        st = self.get(key)
        if not st:
            return
        self.put(key, {})
        if st.get('told'):
            mins = max(1, int((now - st['since']) // 60))
            await self.say(prof, f'🟢 <b>Панель «{html.escape(prof.get("name", ""))}» снова отвечает</b> — не было связи ~{mins} мин.')

    # ───────── ноды без плагина ─────────
    async def plugin_guard(self, prof: dict, nodes: list) -> None:
        """Нода, добавленная в панель руками, остаётся без плагина группы (торренты, фильтры) — привязываем.
        Ноды с другим плагином не трогаем: это чьё-то решение."""
        plugin = plugins.group_plugin(nodes)
        free = [n['uuid'] for n in plugins.enabled_nodes(nodes) if not n.get('activePluginUuid')]
        if not plugin or not free:
            return
        try:
            await asyncio.to_thread(plugins.bind, self.panel(prof), plugin, free)
        except engine.StepError as e:
            log.info('plugin guard %s: %s', prof['id'], e)
            return
        log.info('plugin guard %s: bound %d node(s)', prof['id'], len(free))

    # ───────── давно недоступные ноды ─────────
    def track_offline(self, prof: dict, nodes: list, now: float) -> None:
        """С какого момента включённая нода без связи с панелью. Вернулась или пропала — забываем."""
        key = f'ap:off:{prof["id"]}'
        prev = self.get(key)
        cur = {}
        for n in plugins.enabled_nodes(nodes):
            if not n.get('isConnected'):
                cur[n['uuid']] = prev.get(n['uuid']) or now
        if cur != prev:
            self.put(key, cur)

    def load_of(self, pid: int, uuid: str, hist: bool = False) -> dict:
        """Нагрузка ноды: замер по SSH (точный процессор, диск, предупреждения) и/или из панели (чаще, без SSH).
        Текущие значения — из более свежего, история — из панели, если она есть."""
        s, p = self.get(f'ap:load:{pid}:{uuid}'), self.get(f'ap:pload:{pid}:{uuid}')
        if not s and not p:
            return {}
        out = {k: s.get(k) for k in ('cpu', 'mem', 'disk', 'rx', 'tx', 'at', 'cores')} | {'hot': hot(s) if s else [], 'error': s.get('error')}
        if p and (not s or s.get('cpu') is None or (p.get('at') or 0) > (s.get('at') or 0)):
            out.update({k: p.get(k) for k in ('cpu', 'mem', 'rx', 'tx', 'at', 'cores') if p.get(k) is not None})
        ph = p.get('hist') or []
        out['source'] = 'panel' if len(ph) >= 2 or not s else 'ssh'
        if hist:
            out['hist'] = ph if out['source'] == 'panel' else (s.get('hist') or [])
        return out

    # ───────── аренда ─────────
    async def rent(self, prof: dict) -> dict:
        """Серверы группы в Bobr Cloud: срок, автопродление, цена; баланс покупателя."""
        bc = bcloud_from(self.store, buyer_of(self.store, prof))
        bc.RETRY_FOR = 8
        mine = {}
        for d in svc.bot_jobs_by_ip(self.store, prof['id']).values():
            if d.get('server_id'):
                mine[int(d['server_id'])] = d
        servers = [x for x in await bc.servers() if int(x['id']) in mine] if mine else []
        bal = await bc.balance()
        now = dt.datetime.now(dt.timezone.utc)
        out = []
        for x in servers:
            exp = None
            if x.get('expires_at'):
                try:
                    exp = dt.datetime.fromisoformat(x['expires_at'].replace('Z', '+00:00'))
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=dt.timezone.utc)
                except ValueError:
                    exp = None
            d = mine[int(x['id'])]
            out.append({'id': int(x['id']), 'name': x.get('name') or '', 'ip': x.get('ip') or '', 'domain': d.get('domain', ''),
                        'flag': x.get('flag') or d.get('flag', ''), 'price_kop': int(x.get('price_kop') or 0),
                        'auto_renew': x.get('auto_renew'), 'expires_at': x.get('expires_at'),
                        'days_left': round((exp - now).total_seconds() / 86400, 1) if exp else None})
        out.sort(key=lambda x: (x['days_left'] is None, x['days_left'] or 0))
        return {'servers': out, 'balance_kop': bal, 'buyer': prof.get('owner_label') or 'ваш аккаунт'}

    @staticmethod
    def rent_risks(r: dict) -> list[dict]:
        """Серверы, которые кончатся в ближайшие дни и сами не продлятся (выключено или не хватит денег)."""
        soon = [x for x in r['servers'] if x['days_left'] is not None and x['days_left'] <= RENT_DAYS]
        need = sum(x['price_kop'] for x in soon if x['auto_renew'] is not False)
        out = []
        for x in soon:
            if x['auto_renew'] is False:
                out.append({**x, 'why': 'автопродление выключено'})
            elif r['balance_kop'] < need:
                out.append({**x, 'why': f'на балансе {r["balance_kop"] // 100} ₽, на продление нужно {need // 100} ₽'})
        return out

    # ───────── списки сканеров в плагине нод ─────────
    async def refresh_presets(self) -> None:
        """Раз в сутки — свежие списки включённых пресетов входящих. Меняется список — плагин обновляется
        и рассылается нодам; не скачался — оставляем прежний и пробуем завтра."""
        lists: dict[str, list[str] | None] = {}
        for prof in self.store.profiles():
            key = f'plug:{prof["id"]}'
            mem = self.get(key)
            for pid, old in list((mem.get('ingress') or {}).items()):
                if pid not in plugins.INGRESS_PRESETS:
                    continue
                if pid not in lists:
                    try:
                        lists[pid] = await asyncio.to_thread(plugins.fetch_preset, pid)
                    except plugins.PluginError as e:
                        log.info('preset %s: %s', pid, e)
                        lists[pid] = None
                fresh = lists[pid]
                if not fresh or set(fresh) == set(old or []):
                    continue
                try:
                    await asyncio.to_thread(plugins.apply_preset, self.panel(prof), mem, 'ingress', pid, True, fresh)
                except (engine.StepError, plugins.PluginError) as e:
                    log.info('preset %s group %s: %s', pid, prof['id'], e)
                    continue
                self.put(key, mem)
                log.info('preset %s group %s: %d → %d', pid, prof['id'], len(old or []), len(fresh))

    # ───────── уборка: серверы без нод и давно мёртвые ноды ─────────
    def paid_servers(self, pid: int) -> dict[int, dict]:
        """Серверы Bobr Cloud, купленные ботом для группы и ещё не удалённые: server_id → данные задачи.
        Удачные и неудачные покупки — сервер оплачен в обоих случаях."""
        now, out = time.time(), {}
        for j in self.store.jobs(5000, include_hidden=True):
            d = j['data']
            if (j['profile_id'] != pid or d.get('kind') in svc.SERVICE_KINDS or not d.get('server_id') or d.get('removed')
                    or j['status'] not in ('done', 'failed')):
                continue
            out[int(d['server_id'])] = {**d, '_job': j['id'], '_status': j['status'], '_age': now - (j.get('updated_at') or 0)}
        return out

    async def cleanup(self, prof: dict, now: float | None = None) -> dict:
        """Что можно убрать: оплаченные серверы, которых нет в панели (установка не удалась, ноду удалили руками),
        и включённые ноды без связи дольше DEAD_DAYS. Сами ничего не удаляем — только список."""
        now = now if now is not None else time.time()
        nodes, _ = await CACHE.get(prof, 'nodes', stale_ok=False)
        addrs = {n.get('address') for n in nodes}
        busy = {j['data'].get('server_id') for j in self.store.active_jobs()}
        paid = self.paid_servers(prof['id'])
        servers = []
        if paid:
            bc = bcloud_from(self.store, buyer_of(self.store, prof))
            bc.RETRY_FOR = 8
            for x in await bc.servers():
                d = paid.get(int(x['id']))
                if not d or x.get('status') == 'deleted' or d['_age'] < CLEAN_GRACE or x['id'] in busy or str(x['id']) in busy:
                    continue
                if ({x.get('ip'), d.get('ip'), d.get('domain')} - {None, ''}) & addrs:
                    continue
                servers.append({'id': int(x['id']), 'name': d.get('domain') or x.get('name') or '', 'ip': x.get('ip') or d.get('ip', ''),
                                'flag': x.get('flag') or d.get('flag', ''), 'price_kop': int(x.get('price_kop') or 0),
                                'why': 'установка не удалась' if d['_status'] == 'failed' else 'ноды нет в панели'})
        off = self.get(f'ap:off:{prof["id"]}')
        by_addr = {}
        for sid, d in paid.items():
            for a in (d.get('ip'), d.get('domain')):
                if a:
                    by_addr[a] = sid
        dead = []
        for n in plugins.enabled_nodes(nodes):
            since = off.get(n['uuid'])
            if n.get('isConnected') or not since or now - since < DEAD_DAYS * 86400:
                continue
            dead.append({'uuid': n['uuid'], 'name': n.get('name', ''), 'address': n.get('address', ''),
                         'days': int((now - since) // 86400), 'server_id': by_addr.get(n.get('address'))})
        return {'servers': servers, 'dead': dead, 'waste_kop': sum(x['price_kop'] for x in servers)}

    async def delete_server(self, prof: dict, sid: int, log_line=lambda t: None) -> dict:
        """Удалить сервер группы в Bobr Cloud, его ноду, хосты и A-запись — как при удалении в самом Bobr Cloud."""
        d = self.paid_servers(prof['id']).get(int(sid))
        if not d:
            raise ValueError('Это не сервер, купленный ботом для этой группы')
        bc = bcloud_from(self.store, d.get('buyer') or buyer_of(self.store, prof))
        bc.RETRY_FOR = 8
        await bc.delete(int(sid))
        res = {'nodes': 0, 'hosts': 0, 'dns': 0}
        if d.get('ip') or d.get('domain'):
            from .runner import cf_token_of
            try:
                res = await asyncio.to_thread(engine.remove_node, {**prof, 'cf_token': cf_token_of(self.store, prof)},
                                              d.get('ip', ''), d.get('domain', ''), log_line)
            except engine.StepError as e:  # панель недоступна — ноду уберёт сверка с Bobr Cloud
                log_line(f'панель: {e}')
        self.store.patch_job_data(d['_job'], removed=True, removed_at=int(time.time()))
        self.store.update_job(d['_job'], hidden=1)
        CACHE.drop(prof['id'])
        return res

    async def report_cleanup(self, now: float | None = None) -> None:
        """Нашлось что убрать — пишем сразу; тот же список — не чаще раза в неделю."""
        now = now if now is not None else time.time()
        for prof in self.store.profiles():
            try:
                c = await self.cleanup(prof, now)
            except (engine.StepError, BcloudError) as e:
                log.info('cleanup %s: %s', prof['id'], e)
                continue
            key = f'ap:clean:{prof["id"]}'
            fp = sorted([f's{x["id"]}' for x in c['servers']] + [f'n{x["uuid"]}' for x in c['dead']])
            st = self.get(key)
            if not fp:
                if st:
                    self.put(key, {})
                continue
            if st.get('fp') == fp and now - st.get('at', 0) < CLEAN_REMIND:
                continue
            self.put(key, {'fp': fp, 'at': now})
            lines = [f'🧹 <b>Можно убрать · {html.escape(prof.get("name", ""))}</b>']
            if c['servers']:
                lines.append(f'\nОплачиваются, но нод в панели нет — {c["waste_kop"] // 100} ₽/мес впустую:')
                lines += [f'• {html.escape(x["flag"])} {html.escape(x["name"])} — {x["why"]}, {x["price_kop"] // 100} ₽/мес' for x in c['servers'][:10]]
            if c['dead']:
                lines.append(f'\nНоды без связи больше {DEAD_DAYS} дней:')
                lines += [f'• {html.escape(x["name"])} — {x["days"]} дн.' for x in c['dead'][:10]]
            lines.append('\nУдалить можно в панели управления: группа → «Уборка».')
            await self.say(prof, '\n'.join(lines), self.button())

    async def check_rent(self) -> None:
        for prof in self.store.profiles():
            try:
                r = await self.rent(prof)
            except BcloudError as e:
                log.info('autopilot rent %s: %s', prof['id'], e)
                continue
            if auto_cfg(prof)['renew'] is True:
                bc = bcloud_from(self.store, buyer_of(self.store, prof))
                for x in r['servers']:
                    if x['auto_renew'] is False:
                        try:
                            await bc.set_autorenew(x['id'], True)
                            x['auto_renew'] = True
                        except BcloudError as e:
                            log.info('autorenew %s: %s', x['id'], e)
            for x in self.rent_risks(r):
                stage = 2 if x['days_left'] <= 1 else 1
                k = f'ap:rent:{x["id"]}'
                st = self.get(k)
                if st.get('stage', 0) >= stage and st.get('exp') == x['expires_at']:
                    continue
                self.put(k, {'stage': stage, 'exp': x['expires_at']})
                await self.say(prof, f'💳 <b>Скоро конец аренды</b> · {html.escape(prof["name"])}\n'
                                     f'{x["flag"]} {html.escape(x["domain"] or x["name"])} · <code>{x["ip"]}</code> — '
                                     f'осталось {max(0, int(x["days_left"]))} дн.: {html.escape(x["why"])}.\n\n'
                                     'Продлите в «Аренде» группы или пополните баланс Bobr Cloud.', self.button())

    # ───────── сервер бота ─────────
    def host_line(self) -> str:
        h = self.hostload
        if not h or not h.now:
            return ''
        n = h.now
        level, why = h.verdict()
        icon = {'ok': '🖥', 'warn': '⚠️', 'bad': '🔥'}[level]
        txt = (f'{icon} <b>Сервер бота</b> — процессор {n.get("cpu", "?")}%, память {n.get("mem", "?")}%, '
               f'диск {n.get("disk", "?")}%')
        return txt + (': ' + html.escape('; '.join(why)) if why else '')

    async def host_alert(self, now: float) -> None:
        """Сервер бота перегружен весь последний час — администраторам, не чаще раза в 12 часов."""
        if not self.hostload:
            return
        level, why = self.hostload.verdict()
        last = float(self.store.get('host_alerted') or 0)
        if level != 'bad' or now - last < REALERT:
            return
        self.store.set('host_alerted', str(now))
        for uid in sorted(self.access.admins):
            await self.send(uid, f'🔥 <b>Серверу бота не хватает ресурсов</b>\n{html.escape("; ".join(why))}.\n\n'
                                 'Возьмите VPS помощнее или перенесите соседей (например, Bobr Cloud) на другой сервер. '
                                 'Подробно — «Нагрузка» в панели управления.', self.button())

    # ───────── утренняя сводка ─────────
    async def summaries(self, now: float) -> None:
        uids = sorted(self.access.admins) + [u['tg_id'] for u in self.store.users() if not u.get('blocked')]
        for uid in uids:
            cfg = summary_cfg(self.store, uid)
            if not cfg['on']:
                continue
            last = self.store.get(f'summary_last:{uid}')
            date = due(cfg['hour'], cfg['tz'], last, None, now)
            if not date:
                continue
            self.store.set(f'summary_last:{uid}', date)
            try:
                await self.send(uid, await self.summary(uid, now), self.button())
            except Exception:  # noqa: BLE001
                log.exception('summary %s', uid)

    async def summary(self, uid: int, now: float | None = None) -> str:
        now = now if now is not None else time.time()
        cfg = summary_cfg(self.store, uid)
        profs = self.access.profiles_for(uid)
        head = f'☀️ <b>Сводка Bobr Nodes</b> · {local_now(cfg["tz"], now).strftime("%d.%m")}'
        if not profs:
            return head + '\n\nГрупп пока нет.'
        tags = await asyncio.to_thread(up.hub_tags)
        blocks = []
        day_ago = now - 86400
        jobs = [j for j in self.store.jobs(2000, include_hidden=True) if j['created_at'] >= day_ago]
        for prof in profs:
            lines = []
            try:
                pl = self.panel(prof)
                nodes, pv = await asyncio.gather(asyncio.to_thread(pl.call, 'GET', '/api/nodes'),
                                                 asyncio.to_thread(up.panel_version, pl))
            except engine.StepError as e:
                blocks.append(f'🗂 <b>{html.escape(prof["name"])}</b> — панель не отвечает: {html.escape(str(e)[:120])}')
                continue
            on = [n for n in nodes if not n.get('isDisabled')]
            conn = [n for n in on if n.get('isConnected')]
            online = sum(int(n.get('usersOnline') or 0) for n in on)
            head_g = f'🗂 <b>{html.escape(prof["name"])}</b> — нод {len(on)}, на связи {len(conn)}, онлайн {online}'
            if len(conn) < len(on):
                lines.append('🔌 нет связи: ' + ', '.join(html.escape(n['name']) for n in on if not n.get('isConnected'))[:300])
            bad = [st for st in self.store.mon_all(f'{prof["id"]}:').values() if st.get('status') in ('down', 'repairing', 'gave_up')]
            if bad:
                lines.append('🔴 недоступны из России: ' + ', '.join(
                    html.escape(st.get('name') or st.get('addr', '')) + (' (чиню)' if st['status'] == 'repairing' else '') for st in bad)[:300])
            mine = [j for j in jobs if j['profile_id'] == prof['id']]
            ups = [j for j in mine if j['data'].get('kind') == 'upgrade']
            if ups:
                ok_n = sum(1 for j in ups if j['status'] == 'done')
                lines.append(f'⬆️ обновлено за сутки: {ok_n}' + (f', откатов {len(ups) - ok_n}' if len(ups) > ok_n else ''))
            fixes = [j for j in mine if j['batch'].startswith('mon-') or j['data'].get('kind') == 'reip']
            if fixes:
                lines.append(f'🛠 починок за сутки: {len(fixes)}')
            fails = [j for j in mine if j['status'] == 'failed' and j['data'].get('kind') != 'upgrade']
            if fails:
                lines.append(f'❌ задач с ошибкой: {len(fails)}')
            hotl = []
            for n in on:
                s = self.get(f'ap:load:{prof["id"]}:{n["uuid"]}')
                if s and hot(s) and s.get('high', 0) >= HIGH_CYCLES:
                    hotl.append(f'{html.escape(n["name"])} ({load_text({k: s.get(k) for k in hot(s)})})')
            if hotl:
                lines.append('🔥 нагрузка: ' + ', '.join(hotl)[:300])
            target = up.pick_target(tags, pv)
            if target != 'latest':
                old = [n for n in on if up.semver(up.node_version(n)) and up.semver(up.node_version(n)) < up.semver(target)]
                if old:
                    lines.append(f'🆕 устарели: {len(old)} {"нода" if len(old) == 1 else "ноды" if len(old) < 5 else "нод"} (есть {target})')
            try:
                risks = self.rent_risks(await self.rent(prof))
                if risks:
                    lines.append('💳 скоро конец аренды: ' + ', '.join(
                        f'{html.escape(x["domain"] or x["name"])} через {max(0, int(x["days_left"]))} дн.' for x in risks)[:300])
            except BcloudError:
                pass
            blocks.append(head_g + ('\n' + '\n'.join('   ' + x for x in lines) if lines else '\n   ✅ всё в порядке'))
        if self.access.is_admin(uid) and self.host_line():
            blocks.append(self.host_line())
        return head + '\n\n' + '\n\n'.join(blocks)
