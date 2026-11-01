"""Мониторинг доступности нод из России и починка.

Цикл (раз в ~5 минут): опросить все чекеры → по каждой группе с подпиской сопоставить проверки с нодами панели →
нода «недоступна», если её не пропускает большинство исправных чекеров. После FAILS_TO_ALERT таких циклов подряд —
сообщение и, по настройке группы, починка по шагам: Final Mask и свой порт → переустановка другим скриптом →
запасной XHTTP через CDN → смена IP в Bobr Cloud (платно) → новая нода в той же локации. После каждого шага ждём RECOVER_CYCLES
циклов: нода ожила — готово, нет — следующий шаг.

Состояние ноды хранится по ключу «группа:адрес ноды» (домен не меняется при смене IP и переустановке)."""
from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import checker as ck
from . import engine
from . import services as svc
from . import sshaccess
from .bcloud import BcloudError
from .notify import Notifier
from .pcache import CACHE
from .runner import cf_token_of
from .store import ACTIVE, Store

log = logging.getLogger(__name__)

STEPS = ['mask', 'script', 'xhttp', 'reip', 'replace']  # порядок по умолчанию: платная смена IP — перед покупкой
STEP_LABEL = {'mask': 'Final Mask и свой порт', 'script': 'переустановка другим скриптом',
              'xhttp': 'запасной XHTTP через CDN', 'reip': 'смена IP в Bobr Cloud', 'replace': 'новая нода в той же локации'}
MODES = {'watch': 'только показывать', 'notify': 'сообщать', 'ask': 'сообщать и предлагать починку', 'auto': 'чинить сам'}
OLD = {'panel': 'убрать из панели, сервер оставить', 'delete': 'убрать из панели и удалить в Bobr Cloud', 'keep': 'ничего не трогать'}
BUY_LAST = {'cheapest': 'самый дешёвый тариф в локации', 'none': 'не покупать — сообщить'}
SKIP_DEFAULT = 'АВТО-ВЫБОР, АВТОВЫБОР, AUTO-SELECT, NORMAL'  # общий балансировщик: его ноды проверяются и так
DEFAULTS = {'mon_buy': [{'type': 'same'}], 'mon_buy_last': 'cheapest', 'mon_skip': SKIP_DEFAULT,
            'mon_sub': '', 'mon_mode': 'ask', 'mon_steps': list(STEPS), 'mon_script': None, 'mon_port': 8443,
            'mon_old': 'panel', 'mon_max_buys': 2, 'mon_quiet': False, 'mon_mute': []}
FAILS_TO_ALERT = 3
RECOVER_CYCLES = 2


def settings_of(prof: dict) -> dict:
    out = {k: prof[k] if prof.get(k) is not None else v for k, v in DEFAULTS.items()}
    out['mon_steps'] = list(dict.fromkeys(x for x in (out['mon_steps'] or []) if x in STEPS))  # порядок — как задан
    out['mon_buy'] = list(out['mon_buy'] or [])
    out['mon_quiet'] = bool(out['mon_quiet'])
    out['mon_mute'] = [str(x) for x in (out['mon_mute'] or []) if x][:500]
    return out


def clean_settings(raw: dict, cur: dict, scripts: list[dict]) -> dict:
    """Настройки мониторинга группы из админки → проверенные значения (ValueError с понятным текстом)."""
    out = settings_of(cur)
    if 'mon_sub' in raw and raw['mon_sub'] is not None:
        v = str(raw['mon_sub']).strip()
        if v or not cur.get('mon_sub'):  # пусто — оставить текущую (поле секретное); «-» — убрать
            out['mon_sub'] = ck.check_sub_url(v)
    if raw.get('mon_mode') is not None:
        if raw['mon_mode'] not in MODES:
            raise ValueError('режим мониторинга: watch, notify, ask или auto')
        out['mon_mode'] = raw['mon_mode']
    if raw.get('mon_steps') is not None:
        steps = raw['mon_steps']
        if not isinstance(steps, list) or any(x not in STEPS for x in steps) or len(set(steps)) != len(steps):
            raise ValueError('шаги починки: ' + ', '.join(STEPS) + ', каждый не больше раза')
        out['mon_steps'] = list(steps)  # включённые шаги в заданном порядке
    if raw.get('mon_buy') is not None:
        out['mon_buy'] = clean_buy(raw['mon_buy'])
    if raw.get('mon_quiet') is not None:
        out['mon_quiet'] = bool(raw['mon_quiet'])
    if raw.get('mon_skip') is not None:
        out['mon_skip'] = ', '.join(w.strip() for w in str(raw['mon_skip']).split(',') if w.strip())[:300]
    if raw.get('mon_buy_last') is not None:
        if raw['mon_buy_last'] not in BUY_LAST:
            raise ValueError('если ни одна конфигурация не подошла: cheapest или none')
        out['mon_buy_last'] = raw['mon_buy_last']
    if 'mon_script' in raw:
        sid = raw['mon_script']
        if sid not in (None, '', 0):
            sc = next((x for x in scripts if x['id'] == int(sid)), None)
            if not sc or sc.get('status') != 'approved':
                raise ValueError('скрипт для переустановки: только утверждённый из «Моих скриптов»')
            out['mon_script'] = int(sid)
        else:
            out['mon_script'] = None
    if raw.get('mon_port') is not None:
        try:
            out['mon_port'] = engine.public_port(raw['mon_port'], int(cur.get('node_port') or 2222)) or 443
        except engine.StepError as e:
            raise ValueError(str(e)) from None
    if raw.get('mon_old') is not None:
        if raw['mon_old'] not in OLD:
            raise ValueError('старый сервер: panel, delete или keep')
        out['mon_old'] = raw['mon_old']
    if raw.get('mon_max_buys') is not None:
        try:
            n = int(raw['mon_max_buys'])
        except (TypeError, ValueError):
            n = -1
        if not 0 <= n <= 20:
            raise ValueError('покупок в сутки: от 0 до 20')
        out['mon_max_buys'] = n
    return out


def clean_buy(raw) -> list[dict]:
    """Конфигурации новой ноды по приоритету: same — как у старой, preset — готовый тариф с такими vCPU/памятью/диском,
    custom — своя конфигурация (конструктор Bobr Cloud)."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= 8:
        raise ValueError('конфигурации новой ноды: от 1 до 8 вариантов')
    out = []
    for c in raw:
        t = (c or {}).get('type') if isinstance(c, dict) else None
        if t == 'same':
            item = {'type': 'same'}
        elif t in ('preset', 'custom'):
            try:
                item = {'type': t, **{k: int(c[k]) for k in ('cpu', 'ram', 'disk')}}
            except (KeyError, TypeError, ValueError):
                raise ValueError('конфигурация: vCPU, память и диск числами') from None
            if not (1 <= item['cpu'] <= 256 and 1 <= item['ram'] <= 2048 and 5 <= item['disk'] <= 20000):
                raise ValueError('конфигурация: странные значения vCPU, памяти или диска')
        else:
            raise ValueError('конфигурация: same, preset или custom')
        if item not in out:
            out.append(item)
    return out


def spec_label(c: dict) -> str:
    if c['type'] == 'same':
        return 'как у старой ноды'
    return f'{c["cpu"]} vCPU · {c["ram"]} ГБ · {c["disk"]} ГБ' + (' (своя)' if c['type'] == 'custom' else '')


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r] for r in rows if r])


class Monitor:
    def __init__(self, store: Store, runner, bot, admins: set[int], every: float = ck.INTERVAL, access=None):
        self.store, self.runner, self.bot, self.admins = store, runner, bot, admins
        self.access = access  # кому писать о группе: её пользователю и/или администраторам
        self.notify = Notifier(store, bot, access, admins)
        self.every = every
        self.results: dict[int, list[dict] | None] = {}  # последний опрос каждого чекера
        self.polled_at = 0.0
        self._lock = asyncio.Lock()
        self._bg: set[asyncio.Task] = set()
        self.ru_first, self.ru_every = 75.0, 60.0  # проверка новой ноды: первый опрос и дальше

    # ───────── чекеры ─────────
    def subs(self) -> list[tuple[int, str]]:
        return [(p['id'], p['mon_sub']) for p in self.store.profiles() if p.get('mon_sub')]

    def keyfiles(self) -> tuple[str, str, str]:
        path, pub = self.runner.bot_key()
        return path, pub, self.runner.known_hosts()

    def _bg_task(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    def install(self, cid: int) -> asyncio.Task:
        """Поставить или обновить чекер в фоне; ход — в поле log чекера."""
        return self._bg_task(self._install(cid))

    async def _install(self, cid: int) -> None:
        ch = self.store.checker(cid)
        if not ch:
            return
        lines: list[str] = []

        def say(t: str) -> None:
            lines.append(t)
            self.store.patch_checker(cid, log=lines[-30:])

        self.store.patch_checker(cid, status='installing', error='', log=[])
        path, pub, kh = self.keyfiles()
        subs = self.subs()
        try:
            fields = await asyncio.to_thread(ck.install, ch, subs, path, pub, kh, say, _Sink())
            self.store.patch_checker(cid, **fields, status='running' if subs else 'idle', error='', subs_mark=ck.subs_mark(subs),
                                     installed_at=int(time.time()))
        except Exception as e:  # noqa: BLE001 — любая ошибка установки видна в админке, а не теряется в фоне
            say(f'❌ {e}')
            self.store.patch_checker(cid, status='failed', error=str(e)[:300])
            return
        await self._resync()  # подписки поменяли, пока ставился чекер, — дописать сразу
        self.soon()

    def resync(self) -> asyncio.Task:
        """Подписки групп изменились — записать их на все установленные чекеры."""
        return self._bg_task(self._resync())

    async def _resync(self, force: bool = False) -> bool:
        """Записать текущие подписки на установленные чекеры, где записан другой набор. True — что-то записали."""
        subs = self.subs()
        mark = ck.subs_mark(subs)
        path, _, kh = self.keyfiles()
        done = False
        for ch in self.store.checkers():
            if ch.get('status') not in ('running', 'idle') or not ch.get('api_password'):
                continue
            if not force and ch.get('subs_mark') == mark:
                continue
            try:
                st = await asyncio.to_thread(ck.apply_subs, ch, subs, path, kh, lambda t: None)
                self.store.patch_checker(ch['id'], status=st, error='', subs_mark=mark)
                done = True
            except (engine.StepError, OSError) as e:
                self.store.patch_checker(ch['id'], error=f'не удалось обновить подписки: {e}')
        if done:
            self.soon()
        return done

    # ───────── новая нода: доступна ли из России ─────────
    RU_WAIT = 15 * 60

    def expect(self, jid: int) -> None:
        """После установки: если у группы есть мониторинг и чекеры — проверить новую ноду и дописать итог в задачу."""
        job = self.store.job(jid)
        prof = self.store.profile(job['profile_id']) if job else None
        if not job or not prof or not prof.get('mon_sub') or not any(
                c.get('status') in ('running', 'idle') for c in self.store.checkers()):
            return
        self.store.patch_job_data(jid, ru_check='wait', ru_note='')
        self._bg_task(self._expect(jid, prof['id'], job['data'].get('domain') or ''))

    async def _expect(self, jid: int, pid: int, addr: str) -> None:
        await self.runner.render(jid, force=True)
        try:
            await self._resync(force=True)  # чекеры сразу перечитают подписку с новой нодой и проверят её
        except Exception:  # noqa: BLE001
            log.exception('ru check resync')
        end = time.time() + self.RU_WAIT
        await asyncio.sleep(self.ru_first)
        while time.time() < end:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                log.exception('ru check tick')
            st = self.store.mon(f'{pid}:{addr}') or {}
            if st.get('data'):
                down = st['fail'] * 2 > st['data']
                note = (f'не проходит у {st["fail"]} из {st["data"]} чекеров — мониторинг продолжит следить' if down
                        else f'проходит у {st["data"] - st["fail"]} из {st["data"]} чекеров')
                self.store.patch_job_data(jid, ru_check='bad' if down else 'ok', ru_note=note)
                return await self.runner.render(jid, force=True)
            await asyncio.sleep(self.ru_every)
        self.store.patch_job_data(jid, ru_check='none', ru_note='чекеры не успели проверить — итог будет в мониторинге')
        await self.runner.render(jid, force=True)

    def soon(self, delay: float = 60) -> None:
        """Опрос вскоре — после установки чекера или смены подписок: xray-checker успеет пройти первую проверку."""
        if getattr(self, '_soon', None) and not self._soon.done():
            return

        async def later():
            await asyncio.sleep(delay)
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                log.exception('monitor tick')
        self._soon = asyncio.create_task(later())  # не в _bg: установку чекера не ждём вместе с опросом

    async def poll_all(self) -> dict[int, list[dict] | None]:
        path, _, kh = self.keyfiles()
        out: dict[int, list[dict] | None] = {}

        async def one(ch):
            try:
                res = await asyncio.to_thread(ck.poll, ch, path, kh)
                out[ch['id']] = res
                self.store.patch_checker(ch['id'], polled_at=int(time.time()), poll_error='', proxies=len(res),
                                         online=sum(1 for p in res if p.get('online')))
            except (engine.StepError, OSError) as e:
                out[ch['id']] = None
                self.store.patch_checker(ch['id'], poll_error=str(e)[:200], polled_at=int(time.time()))
        await asyncio.gather(*[one(ch) for ch in self.store.checkers() if ch.get('status') == 'running'])
        self.results, self.polled_at = out, time.time()
        return out

    @staticmethod
    def healthy(res: list[dict] | None) -> bool:
        """Чекер исправен: ответил, и если проверок много — хоть одна прошла (иначе лежит сеть самого чекера).
        Ещё не проверенные (lastCheck пуст — чекер только запустился) не считаются."""
        if res is None:
            return False
        done = [p for p in res if p.get('lastCheck')]
        return bool(done) and (len(done) < 3 or any(p.get('online') for p in done))

    # ───────── цикл ─────────
    async def run(self) -> None:
        await asyncio.sleep(30)  # после запуска бота — первый опрос почти сразу, дальше по расписанию
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — мониторинг не должен ронять бота
                log.exception('monitor tick')
            await asyncio.sleep(self.every)

    async def tick(self) -> None:
        async with self._lock:
            await self._resync()  # чекер, на котором не тот набор подписок, — поправить до опроса
            results = await self.poll_all()
            from . import billing
            stop = billing.paused_profiles(self.store)  # подписка владельца на паузе — группу не проверяем и не чиним
            for prof in self.store.profiles():
                if prof.get('mon_sub') and prof['id'] not in stop:
                    try:
                        await self.check_group(prof, results)
                    except engine.StepError as e:  # панель группы недоступна — в следующий раз
                        log.info('monitor group %s: %s', prof['id'], e)

    @staticmethod
    def skip_group(group: str, skip: str) -> bool:
        """Группа подписки, которую не проверяем (общий балансировщик): совпадение с любым словом из настройки."""
        g = (group or '').lower()
        return bool(g) and any(w.strip().lower() in g for w in (skip or '').split(',') if w.strip())

    @staticmethod
    def assign(nodes: list[dict], hosts: list[dict], dns: dict[str, set[str]], bj: dict,
               injectors: set[str]) -> tuple[dict[str, list[tuple[dict, bool]]], list[dict]]:
        """Хосты панели → ноды. Порядок правил: «Ноды» хоста (явная привязка) → inbound хоста активен на ноде
        (если на нескольких — та, на чей IP указывает адрес) → адрес хоста — это нода или указывает на её IP.
        Возвращает: адрес ноды → [(хост, прямой ли вход)] и хосты без нод (свой профиль, xray.json и сервер не в панели).
        Прямой вход — адрес хоста ведёт на саму ноду; иначе это CDN или посредник.
        Хосты-балансировщики (шаблон с injectHosts) ни к кому не относятся: их хосты проверяются по отдельности."""
        def ips(a: str) -> set[str]:
            return {a} if engine.is_ip(a) else set(dns.get(a) or ())
        by_uuid = {n['uuid']: n for n in nodes}
        alias, node_ips = {}, {}
        for n in nodes:
            al = {n['address']}
            d = bj.get(n['address'])
            if d:
                al |= {d.get('ip') or '', d.get('domain') or ''} - {''}
            alias[n['address']] = al
            node_ips[n['address']] = set().union(*(ips(a) for a in al))
        by_inbound: dict[str, list[dict]] = {}
        for n in nodes:
            for i in engine.active_inbounds(n):
                by_inbound.setdefault(i, []).append(n)

        def points_to(h: dict, n: dict) -> bool:
            a = h.get('address') or ''
            return a in alias[n['address']] or bool(ips(a) & node_ips[n['address']])

        out: dict[str, list[tuple[dict, bool]]] = {n['address']: [] for n in nodes}
        loose = []
        for h in hosts:
            a = h.get('address') or ''
            if not a or h.get('isDisabled') or h.get('xrayJsonTemplateUuid') in injectors:
                continue
            pinned = [by_uuid[x] for x in (h.get('nodes') or []) if isinstance(x, str) and x in by_uuid] + \
                [by_uuid[x['uuid']] for x in (h.get('nodes') or []) if isinstance(x, dict) and x.get('uuid') in by_uuid]
            if pinned:
                owners = pinned
            else:
                cand = by_inbound.get(str(((h.get('inbound') or {}).get('configProfileInboundUuid')) or ''), [])
                if len(cand) > 1:
                    cand = [n for n in cand if points_to(h, n)] or []
                owners = cand or [n for n in nodes if points_to(h, n)][:1]
            if not owners:
                loose.append(h)
            for n in owners:
                out[n['address']].append((h, points_to(h, n) and not a.startswith(engine.CDN_PREFIX)))
        return out, loose

    @staticmethod
    def _norm(s) -> str:
        return ' '.join(str(s or '').split()).casefold()

    def votes(self, prof: dict, results: dict, nodes: list[dict], hosts: list[dict] | None = None,
              dns: dict[str, set[str]] | None = None, injectors: set[str] | None = None) -> dict[str, dict]:
        """Ключ → состояние по чекерам. Нода: голосуют её прямые входы, а если их нет в подписке — входы через CDN;
        нода «не проходит» у чекера, если не прошёл ни один. Хост без ноды (свой xray.json): его проверки —
        по названию хоста (группа или имя в подписке) или адресу; «не проходит», если не прошла ни одна.
        Группы общего балансировщика (настройка «не проверять») пропускаются."""
        sub = ck.sub_name(prof['id'])
        skip = settings_of(prof)['mon_skip']
        bj = svc.bot_jobs_by_ip(self.store, prof['id'])
        mine_all = {cid: [p for p in res if p.get('subName') == sub and not self.skip_group(p.get('groupName'), skip)]
                    for cid, res in results.items() if self.healthy(res)}
        assigned, loose = self.assign(nodes, hosts or [], dns or {}, bj, injectors or set())

        def by_addr(hs: list[dict], ps: list[dict]) -> list[dict]:
            keys = {(h.get('address'), int(h.get('port') or 0)) for h in hs}
            addrs = {h.get('address') for h in hs}
            return [p for p in ps if (p.get('server'), int(p.get('port') or 0)) in keys
                    or (p.get('server') in addrs and not p.get('port'))]

        def tally(pick) -> dict:
            data = fail = 0
            up = total = 0
            by = {}  # чекер → [прошло выходов, всего, лучшая задержка мс] — для карты на странице ноды
            for cid, ps in mine_all.items():
                got = [p for p in pick(ps) if p.get('lastCheck')]
                if not got:
                    continue
                data += 1
                ok_n = sum(1 for p in got if p.get('online'))
                fail += 0 if ok_n else 1
                ms = [int(p['latencyMs']) for p in got if p.get('online') and isinstance(p.get('latencyMs'), (int, float)) and p['latencyMs'] > 0]
                by[str(cid)] = [ok_n, len(got), min(ms) if ms else None]
                if len(got) > total:
                    up, total = ok_n, len(got)
            return {'data': data, 'fail': fail, 'up': up, 'total': total, 'by': by}

        out = {}
        for pos, n in enumerate(nodes):
            hs = assigned[n['address']]
            direct = [h for h, d in hs if d]
            indirect = [h for h, d in hs if not d]

            def pick(ps, direct=direct, indirect=indirect):
                got = by_addr(direct, ps)
                return got or by_addr(indirect, ps)
            out[n['address']] = {'kind': 'node', **tally(pick), 'uuid': n['uuid'], 'name': n.get('name') or n['address'],
                                 'connected': bool(n.get('isConnected')), 'country': n.get('countryCode') or '', 'pos': pos,
                                 'online_users': n.get('usersOnline'),
                                 'hosts': [h.get('address') for h in direct if h.get('address') != n['address']],
                                 'cdn_hosts': sorted({h.get('address') for h in indirect})}
        for i, h in enumerate(loose):
            name = self._norm(h.get('remark'))

            def pick(ps, h=h, name=name):
                return [p for p in ps if name and (self._norm(p.get('groupName')) == name or self._norm(p.get('name')) == name)] \
                    or by_addr([h], ps)
            out['host:' + h['uuid']] = {'kind': 'host', **tally(pick), 'uuid': h['uuid'], 'name': h.get('remark') or h['address'],
                                        'connected': True, 'country': '', 'pos': 10000 + i, 'hosts': [], 'cdn_hosts': [],
                                        'address': h.get('address')}
        return out

    async def check_group(self, prof: dict, results: dict) -> None:
        panel = engine.Panel(prof['panel_url'], prof['panel_token'], prof.get('panel_cookie', ''))
        nodes, hosts = await asyncio.gather(asyncio.to_thread(panel.call, 'GET', '/api/nodes'),
                                            asyncio.to_thread(panel.call, 'GET', '/api/hosts'))
        CACHE.put(prof, 'nodes', nodes)  # веб-апп возьмёт их отсюда, а не из панели
        CACHE.put(prof, 'hosts', hosts)
        try:
            tpls = await asyncio.to_thread(panel.templates)
        except engine.StepError:
            tpls = []
        injectors = {t['uuid'] for t in tpls if isinstance(t.get('templateJson'), dict)
                     and ((t['templateJson'].get('remnawave') or {}).get('injectHosts'))}
        dns = await asyncio.to_thread(resolve_all, {n['address'] for n in nodes} | {h.get('address') or '' for h in hosts})
        votes = self.votes(prof, results, nodes, hosts, dns, injectors)
        cfg = settings_of(prof)
        now = time.time()
        seen = set()
        for addr, v in votes.items():
            key = f'{prof["id"]}:{addr}'
            seen.add(key)
            st = self.store.mon(key) or {'status': 'ok', 'fails': 0}
            st.update(uuid=v['uuid'], name=v['name'], addr=v.get('address') or addr, data=v['data'], fail=v['fail'], pos=v['pos'],
                      hosts=v['hosts'], cdn_hosts=v['cdn_hosts'], kind=v['kind'], up=v['up'], total=v['total'],
                      online_users=v.get('online_users'), connected=v['connected'], country=v['country'], checked_at=int(now),
                      by=v.get('by') or {})
            down = v['data'] > 0 and v['fail'] * 2 > v['data']
            if v['data'] == 0:  # ни один исправный чекер ноду не видит — нет данных, ничего не решаем
                self.store.set_mon(key, st)
                continue
            if not down:
                if st.get('status') in ('down', 'repairing', 'gave_up', 'ignored'):
                    await self.recovered(prof, st)
                st.update(status='ok', fails=0, step=None, job=None, job_done=False, steps_done=[])
                self.store.set_mon(key, st)
                continue
            st['fails'] = int(st.get('fails') or 0) + 1
            if st.get('status', 'ok') == 'ok' and st['fails'] >= FAILS_TO_ALERT:
                st.update(status='down', since=int(now), tok=secrets.token_hex(4), steps_done=[])
                self.store.set_mon(key, st)
                await self.alert(prof, st, cfg)
                if cfg['mon_mode'] == 'auto' and st.get('kind', 'node') == 'node':
                    st = self.store.mon(key)
                    await self.start_repair(prof, st)
                continue
            if st.get('status') == 'repairing':
                await self.drive(prof, st)
                continue
            self.store.set_mon(key, st)
        for key, st in self.store.mon_all(f'{prof["id"]}:').items():  # ноды, которых больше нет в панели
            if key not in seen and st.get('status') == 'repairing':  # переустановка: нода вернётся под тем же адресом
                job = self.store.job(int(st['job'])) if st.get('job') else None
                if job and job['status'] == 'failed':
                    await self.drive(prof, st)
                continue
            if key not in seen and not (st.get('status') == 'repairing' or
                                        st.get('status') == 'replaced' and now - (st.get('checked_at') or 0) < 86400):
                self.store.set_mon(key, None)

    # ───────── сообщения ─────────
    def recipients(self, prof: dict) -> list[int]:
        return self.access.recipients(prof) if self.access is not None else sorted(self.admins)

    @staticmethod
    def muted(prof: dict, st: dict | None = None) -> bool:
        """Уведомления выключены для всей группы или для этой ноды (мониторинг и починка работают, сообщений нет)."""
        cfg = settings_of(prof)
        return cfg['mon_quiet'] or bool(st and (st.get('addr') or '') in cfg['mon_mute'])

    async def say(self, prof: dict, text: str, markup=None, st: dict | None = None, type_: str = 'node.repair') -> None:
        """Событие — в ленту всегда, в Telegram — если нода или группа не заглушена в мониторинге
        и получатель не выключил этот тип в центре уведомлений."""
        muted = self.muted(self.store.profile(prof['id']) or prof, st)
        await self.notify.emit(type_, text, prof, markup, to=self.recipients(prof), tg=not muted)

    def head(self, prof: dict, st: dict) -> str:
        return f'<b>{html.escape(st.get("name") or st["addr"])}</b> · {html.escape(prof["name"])}'

    async def alert(self, prof: dict, st: dict, cfg: dict) -> None:
        if cfg['mon_mode'] == 'watch':
            return
        why = f'не проходит проверку у {st["fail"]} из {st["data"]} чекеров в России уже {st["fails"] * self.every // 60} мин'
        why += '' if st.get('connected') else '; с панелью тоже нет связи — похоже, сервер лежит'
        text = f'🔴 {self.head(prof, st)}\n{why}.'
        if st.get('kind') == 'host':  # свой сервер с xray.json: починить его бот не может — только сообщает
            if st.get('total'):
                text += f'\nИз {st["total"]} выходов хоста не прошёл ни один.'
            return await self.say(prof, text + '\n\nЭто хост без ноды (свой xray.json) — бот его не чинит.',
                                  kb([('🙈 Не напоминать', f'mon:i:{st["tok"]}')]), st=st, type_='node.ru_down')
        steps = [STEP_LABEL[s] for s in cfg['mon_steps']]
        if cfg['mon_mode'] == 'auto':
            text += '\n\nЧиню по шагам: ' + (', '.join(steps) if steps else 'шагов не выбрано') + '.'
            return await self.say(prof, text, st=st, type_='node.ru_down')
        if cfg['mon_mode'] == 'ask':
            text += '\n\nШаги починки: ' + (', '.join(steps) if steps else 'не выбраны') + '.'
            rows = [[('🛠 Починить', f'mon:f:{st["tok"]}')]] if steps else []
            if 'replace' in cfg['mon_steps']:
                rows.append([('🆕 Сразу новая нода', f'mon:r:{st["tok"]}')])
            rows.append([('🙈 Не трогать', f'mon:i:{st["tok"]}')])
            return await self.say(prof, text, kb(*rows), st=st, type_='node.ru_down')
        await self.say(prof, text, st=st, type_='node.ru_down')

    async def recovered(self, prof: dict, st: dict) -> None:
        if settings_of(prof)['mon_mode'] == 'watch':
            return
        done = st.get('steps_done') or []
        how = f' после шага «{STEP_LABEL[done[-1]]}»' if done and st.get('status') == 'repairing' else ''
        await self.say(prof, f'🟢 {self.head(prof, st)}\nснова доступна из России{how}.', st=st, type_='node.ru_up')

    # ───────── починка ─────────
    def find(self, tok: str) -> tuple[dict, str, dict] | None:
        for key, st in self.store.mon_all().items():
            if st.get('tok') == tok:
                prof = self.store.profile(int(key.split(':', 1)[0]))
                if prof:
                    return prof, key, st
        return None

    async def action(self, tok: str, what: str) -> str:
        """Кнопка из сообщения или админки: fix — чинить по шагам, replace — сразу новая нода, ignore — не трогать."""
        found = self.find(tok)
        if not found:
            return 'Нода уже в порядке или удалена'
        prof, key, st = found
        if st.get('status') not in ('down', 'gave_up', 'ignored', 'repairing'):
            return 'Нода уже в порядке'
        if what == 'ignore':
            st['status'] = 'ignored'
            self.store.set_mon(key, st)
            return 'Не трогаю, пока нода не оживёт'
        if st.get('kind') == 'host':
            return 'Хост без ноды бот не чинит — только сообщает'
        if st.get('status') == 'repairing':
            return 'Починка уже идёт'
        st['steps_done'] = [x for x in STEPS if x != 'replace'] if what == 'replace' else []
        self.store.set_mon(key, st)
        return await self.start_repair(prof, st, only_replace=what == 'replace')

    async def start_repair(self, prof: dict, st: dict, only_replace: bool = False) -> str:
        st.update(status='repairing', step=None, job=None, job_done=False)
        self.store.set_mon(f'{prof["id"]}:{st["addr"]}', st)
        return await self.next_step(prof, st, only_replace=only_replace)

    async def next_step(self, prof: dict, st: dict, only_replace: bool = False) -> str:
        cfg = settings_of(prof)
        key = f'{prof["id"]}:{st["addr"]}'
        done = st.get('steps_done') or []
        plan = ['replace'] if only_replace else [x for x in cfg['mon_steps'] if x not in done]
        for step in plan:
            done.append(step)
            st['steps_done'] = done
            try:
                jid, note = await self.run_step(prof, st, step, cfg)
            except (ValueError, engine.StepError, BcloudError) as e:
                await self.say(prof, f'⚠️ {self.head(prof, st)}\nшаг «{STEP_LABEL[step]}» пропущен: {html.escape(str(e))}', st=st)
                continue
            st.update(step=step, job=jid, job_done=False, fails=0)
            self.store.set_mon(key, st)
            await self.say(prof, f'🛠 {self.head(prof, st)}\nшаг «{STEP_LABEL[step]}»: {html.escape(note)}', st=st)
            return f'Шаг: {STEP_LABEL[step]}'
        st.update(status='gave_up', step=None, job=None)
        self.store.set_mon(key, st)
        await self.say(prof, f'❌ {self.head(prof, st)}\nвсе шаги починки испробованы — нода всё ещё недоступна. Посмотрите её сами.', st=st)
        return 'Шагов больше нет'

    async def drive(self, prof: dict, st: dict) -> None:
        """Нода в починке и всё ещё недоступна: ждём задачу шага, потом RECOVER_CYCLES циклов, потом следующий шаг."""
        key = f'{prof["id"]}:{st["addr"]}'
        job = self.store.job(int(st['job'])) if st.get('job') else None
        if job and job['status'] in ACTIVE:
            st['fails'] = 0
            self.store.set_mon(key, st)
            return
        if job and not st.get('job_done'):
            st.update(job_done=True, fails=0)
            if job['status'] == 'failed':
                self.store.set_mon(key, st)
                await self.say(prof, f'⚠️ {self.head(prof, st)}\nшаг «{STEP_LABEL[st["step"]]}» не удался: {html.escape(job["error"][:200])}', st=st)
                await self.next_step(prof, st)
                return
            if st['step'] == 'replace':
                await self.finish_replace(prof, st, job)
                return
            self.store.set_mon(key, st)
            return
        if int(st.get('fails') or 0) >= RECOVER_CYCLES:
            await self.next_step(prof, st)
        else:
            self.store.set_mon(key, st)

    # ── шаги ──
    def chat(self, prof: dict) -> int:
        r = self.recipients(prof)
        return r[0] if r else 0

    async def run_step(self, prof: dict, st: dict, step: str, cfg: dict) -> tuple[int, str]:
        pid = prof['id']
        bd = svc.bot_jobs_by_ip(self.store, pid).get(st['addr']) or {}
        if any(j['status'] in ACTIVE and j['profile_id'] == pid and (j['data'].get('node_uuid') == st['uuid']
               or j['data'].get('domain') == st['addr']) for j in self.store.active_jobs()):
            raise ValueError('по этой ноде уже идёт задача')
        ip = bd.get('ip') or (st['addr'] if engine.is_ip(st['addr']) else '')
        if step == 'replace':
            return await self.replace(prof, st, bd, cfg)
        if step == 'script':
            return await self.reinstall(prof, st, bd, cfg)
        ssh = svc.node_ssh(self.store, self.runner, pid, st['uuid'], st['addr'])
        if not ssh:
            raise ValueError('нет доступа по SSH к ноде (задайте его на странице ноды)')
        doms = [bd['domain']] if bd.get('domain') else ([] if engine.is_ip(st['addr']) else [st['addr']])
        base = {'node_uuid': st['uuid'], 'name': st['name'], 'domain': doms[0] if doms else st['name'], 'domains': doms,
                'loc_name': st['name'], 'ssh': ssh, 'monitor': True}
        if step == 'reip':
            if not bd.get('server_id'):
                raise ValueError('нода куплена не через бота — сменить IP в Bobr Cloud нельзя')
            if not ip:
                raise ValueError('не знаю текущий IP ноды')
            data = {**base, 'kind': 'reip', 'mode': 'bcloud', 'ip': ip, 'old_ip': ip, 'new_ip': '', 'flag': '🔁',
                    'spec': 'смена IP', 'server_id': bd['server_id'], 'buyer': bd.get('buyer'), 'deploy_job': bd.get('_job')}
            note = 'меняю IP в Bobr Cloud'
        elif step == 'mask':
            port = cfg['mon_port']
            data = {**base, 'kind': 'node', 'ip': ip or st['addr'], 'flag': '🛠', 'spec': 'починка: Final Mask и порт',
                    'port': port if port != 443 else '', 'final_mask': engine.final_mask(None)}
            note = 'Final Mask на хостах' + (f', порт {port}' if port != 443 else '')
        elif step == 'xhttp':
            if prof.get('script_id'):
                raise ValueError('группа ставит ноды своим скриптом — запасной XHTTP только у встроенного установщика')
            if not (cf_token_of(self.store, prof)):
                raise ValueError('нет токена Cloudflare')
            if not doms:
                raise ValueError('у ноды нет домена')
            data = {**base, 'kind': 'node', 'ip': ip or st['addr'], 'flag': '🛠', 'spec': 'починка: XHTTP через CDN',
                    'xhttp': {'port': engine.XHTTP_PORT, 'mode': 'cdn'}, 'port': ''}
            note = 'ставлю запасной вход через Cloudflare CDN'
        else:
            raise ValueError('неизвестный шаг')
        jid = self.store.add_job('mon-' + st['uuid'][:8], pid, self.chat(prof), data)
        await self.runner.render(jid, force=True)
        self.runner.start(jid)
        return jid, note

    async def bcloud_server(self, prof: dict, bd: dict, ip: str) -> dict | None:
        """Сервер ноды в Bobr Cloud у покупателя группы: по задаче бота или по IP."""
        try:
            servers = {x['id']: x for x in await svc.own_servers(self.store, prof)}
        except BcloudError:
            return None
        if bd.get('server_id') and int(bd['server_id']) in servers:
            return servers[int(bd['server_id'])]
        return next((x for x in servers.values() if ip and x.get('ip') == ip), None)

    async def reinstall(self, prof: dict, st: dict, bd: dict, cfg: dict) -> tuple[int, str]:
        """Переустановка на том же сервере другим скриптом: нода и хосты — из панели, затем установка с тем же доменом."""
        ip = bd.get('ip') or (st['addr'] if engine.is_ip(st['addr']) else '')
        dom = bd.get('domain') or ('' if engine.is_ip(st['addr']) else st['addr'])
        if not dom:
            raise ValueError('у ноды нет домена — переустановке он нужен для сертификата')
        sid = cfg['mon_script']
        if (sid or None) == (prof.get('script_id') or None):
            raise ValueError('скрипт для переустановки совпадает со скриптом группы — выберите другой в настройках мониторинга')
        if sid:
            sc = self.store.script(int(sid))
            if not sc or sc.get('status') != 'approved':
                raise ValueError('скрипт для переустановки удалён или не утверждён')
        srv = await self.bcloud_server(prof, bd, ip)
        if not srv or srv.get('status') != 'active':
            raise ValueError('сервера ноды нет в Bobr Cloud у покупателя группы — переустановить не на что')
        pconf = {**prof, 'cf_token': cf_token_of(self.store, prof)}
        await asyncio.to_thread(engine.remove_node, pconf, srv['ip'], dom, lambda t: None)
        if bd.get('_job'):
            self.store.patch_job_data(int(bd['_job']), removed=True, removed_at=int(time.time()))
        engine.forget_host(self.runner.known_hosts(), srv['ip'])
        cat = {'locations': [], 'os_code': srv.get('os') or 'debian12'}
        ids = await svc.create_jobs(self.store, self.runner, self.chat(prof), prof['id'], cat, srv['location'], None, '1m', [dom],
                                    squads=bd.get('squads'), host=bd.get('host'), servers=[srv], script_id=sid,
                                    extras={k: bd.get(k) for k in ('psiphon', 'warp') if bd.get(k)},
                                    note={'monitor': True})
        title = (self.store.script(int(sid)) or {}).get('title') if sid else 'Nuvrion Vision'
        return ids[0], f'переустанавливаю на том же сервере скриптом «{title}»'

    def buys_today(self, pid: int) -> list[float]:
        import json
        try:
            ts = json.loads(self.store.get(f'mon_buys:{pid}') or '[]')
        except ValueError:
            ts = []
        return [t for t in ts if t > time.time() - 86400]

    async def replace(self, prof: dict, st: dict, bd: dict, cfg: dict) -> tuple[int, str]:
        import json
        buys = self.buys_today(prof['id'])
        if len(buys) >= cfg['mon_max_buys']:
            raise ValueError(f'лимит покупок за сутки исчерпан ({cfg["mon_max_buys"]}) — поменяйте его в настройках мониторинга')
        cat = await svc.load_catalog(self.store, prof)
        loc = bd.get('location') or (st.get('country') or '').lower()
        L = next((x for x in cat['locations'] if x['code'] == loc), None)
        if not L:
            raise ValueError(f'локации «{loc or "?"}» сейчас нет в Bobr Cloud')
        idx, custom, why = await self.pick_offer(prof, cat, L, bd, cfg)
        doms = await svc.auto_domains(self.store, prof, loc, 1)
        extras = {k: bd.get(k) for k in ('psiphon', 'warp', 'xhttp') if bd.get(k)}
        ids = await svc.create_jobs(self.store, self.runner, self.chat(prof), prof['id'], cat, loc, idx, '1m', doms,
                                    squads=bd.get('squads'), host=bd.get('host'), extras=extras, custom=custom,
                                    note={'monitor': True, 'replaces': st['addr']})
        self.store.set(f'mon_buys:{prof["id"]}', json.dumps(buys + [time.time()]))
        return ids[0], f'покупаю новую ноду {doms[0]} в {L["flag"]} {L["name"]}, {why}'

    async def pick_offer(self, prof: dict, cat: dict, L: dict, bd: dict, cfg: dict) -> tuple[int | None, dict | None, str]:
        """Первая доступная в локации конфигурация из списка группы → (idx, своя конфигурация, подпись)."""
        offers, loc = L['offers'], L['code']
        tried = []

        async def custom_of(spec: dict):
            try:
                return await svc.custom_offer(self.store, prof, cat, loc, spec)
            except (ValueError, BcloudError):
                return None

        def preset_of(spec: dict):
            return next((o for o in offers if (o['cpu'], o['ram'], o['disk']) == (spec['cpu'], spec['ram'], spec['disk'])), None)

        for n, c in enumerate(cfg['mon_buy'], 1):
            note = spec_label(c) + ('' if n == 1 else f' (вариант {n})')
            if c['type'] == 'same':
                if bd.get('custom'):
                    o = await custom_of(bd['custom'])
                    if o:
                        return None, o, note
                elif bd.get('idx') is not None and any(x['idx'] == bd['idx'] for x in offers):
                    return bd['idx'], None, note
            elif c['type'] == 'preset':
                o = preset_of(c)
                if o:
                    return o['idx'], None, note
            else:
                o = await custom_of(c)
                if o:
                    return None, o, note
            tried.append(spec_label(c))
        if cfg['mon_buy_last'] == 'cheapest' and offers:
            o = min(offers, key=lambda x: x['price_kop'])
            return o['idx'], None, f'самый дешёвый тариф {svc.spec_text(o)} — не нашлось: {", ".join(tried)}'
        raise ValueError(f'в {L["name"]} нет ни одной из заданных конфигураций: {", ".join(tried)}')

    async def finish_replace(self, prof: dict, st: dict, job: dict) -> None:
        """Новая нода готова: со старой — по настройке группы."""
        key = f'{prof["id"]}:{st["addr"]}'
        cfg = settings_of(prof)
        bd = svc.bot_jobs_by_ip(self.store, prof['id']).get(st['addr']) or {}
        ip = bd.get('ip') or (st['addr'] if engine.is_ip(st['addr']) else '')
        dom = bd.get('domain') or ('' if engine.is_ip(st['addr']) else st['addr'])
        what = 'старая нода оставлена как есть'
        if cfg['mon_old'] in ('panel', 'delete'):
            pconf = {**prof, 'cf_token': cf_token_of(self.store, prof)}
            try:
                await asyncio.to_thread(engine.remove_node, pconf, ip, dom, lambda t: None)
                what = 'старая нода убрана из панели'
            except engine.StepError as e:
                what = f'старую ноду убрать из панели не удалось: {e}'
            if bd.get('_job'):
                self.store.patch_job_data(int(bd['_job']), removed=True, removed_at=int(time.time()))
        if cfg['mon_old'] == 'delete':
            srv = await self.bcloud_server(prof, bd, ip)
            if srv:
                try:
                    await self.runner.bcloud_factory(self.store, bd.get('buyer') or svc.buyer_of(self.store, prof)).delete(srv['id'])
                    what += ', сервер удалён в Bobr Cloud'
                except BcloudError as e:
                    what += f', удалить сервер в Bobr Cloud не удалось: {e}'
            else:
                what += ', сервера в Bobr Cloud не нашёл'
        st.update(status='replaced', step=None, job=None, replaced_by=job['data'].get('domain'))
        self.store.set_mon(key, st)
        await self.say(prof, f'✅ {self.head(prof, st)}\nзаменена новой нодой <code>{html.escape(job["data"].get("domain", ""))}</code>; '
                       f'{html.escape(what)}.', st=st)

    # ───────── для админки ─────────
    def view(self) -> dict:
        checkers = []
        for ch in self.store.checkers():
            checkers.append({**{k: ch.get(k) for k in ('id', 'name', 'ip', 'status', 'error', 'log', 'hoster_url',
                                                       'polled_at', 'poll_error', 'proxies', 'online', 'installed_at')},
                             'region': ch.get('region') or guess_region(ch.get('name', '')),
                             'ssh': sshaccess.view(ch), 'access': sshaccess.describe(ch)})
        groups = []
        for p in self.store.profiles():
            cfg = settings_of(p)
            nodes = sorted(({k: v for k, v in st.items() if k not in ('job_done',)} | {'key': key}
                            for key, st in self.store.mon_all(f'{p["id"]}:').items()), key=lambda x: (x.get('pos', 9999), x['key']))
            groups.append({'id': p['id'], 'name': p['name'], 'enabled': bool(p.get('mon_sub')),
                           **{k: v for k, v in cfg.items() if k != 'mon_sub'}, 'nodes': nodes,
                           'buys_today': len(self.buys_today(p['id']))})
        return {'checkers': checkers, 'groups': groups, 'polled_at': int(self.polled_at), 'every': int(self.every),
                'steps': STEP_LABEL, 'modes': MODES, 'old': OLD, 'buy_last': BUY_LAST}


# федеральные округа: код → название. Регион чекера выбирают в его карточке; пока не выбран — угадываем по названию
REGIONS = {'nw': 'Северо-Запад', 'c': 'Центр', 's': 'Юг', 'nc': 'Северный Кавказ', 'v': 'Поволжье', 'u': 'Урал',
           'sib': 'Сибирь', 'fe': 'Дальний Восток'}
_CITY = [
    ('c', 'москв мск moscow msk подмоск тул рязан тверь калуг владимир ярослав воронеж белгород курск брянск смоленск '
          'иванов кострома липецк орёл орел тамбов'),
    ('nw', 'петербург спб питер spb leningrad ленинград мурманск архангельск калининград новгород псков карел петрозавод '
           'вологд коми сыктывкар череповец'),
    ('s', 'краснодар ростов сочи волгоград астрахан крым севастопол симферопол адыге калмык'),
    ('nc', 'дагестан махачкал чечн грозн ставропол нальчик владикавказ осети ингуш черкес'),
    ('v', 'казан нижн новгород н.новгород самар уф башкир пермь саратов оренбург ижевск удмурт чебоксар пенз ульянов '
          'киров йошкар мордов саранск тольятти'),
    ('u', 'екатеринбург екб ural урал челябинск тюмен курган ханты сургут ямал нефтеюган магнитогорск'),
    ('sib', 'новосибирск нск сибир омск томск барнаул алта кемеров кузбас новокузнец красноярск иркутск тыва кызыл '
            'хакас абакан'),
    ('fe', 'владивосток хабаровск дальн якут саха магадан камчат сахалин бурят улан-удэ чит забайк благовещ амур '
           'чукот биробиджан'),
]


def guess_region(name: str) -> str:
    n = (name or '').casefold()
    for code, words in _CITY:
        if any(w in n for w in words.split()):
            return code
    return ''


def resolve_all(names: set[str]) -> dict[str, set[str]]:
    """A-записи доменов (параллельно): к какой ноде ведёт хост. IP и пустые — пропускаем."""
    import socket
    from concurrent.futures import ThreadPoolExecutor
    todo = sorted(n for n in names if n and not engine.is_ip(n))

    def one(n):
        try:
            return n, {a[4][0] for a in socket.getaddrinfo(n, None, socket.AF_INET)}
        except (OSError, UnicodeError):
            return n, set()
    with ThreadPoolExecutor(16) as ex:
        return dict(ex.map(one, todo))


class _Sink:
    """Журнал установки чекера не нужен на диске: строки идут в поле log чекера."""
    def write(self, *_):
        pass

    def flush(self):
        pass

