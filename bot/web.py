"""Веб-админка (Telegram Mini App): страница /admin и JSON API /api/*.
Доступ — только из Telegram: подпись initData проверяется токеном бота, пользователь должен быть в ADMIN_IDS."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

from . import engine, forms, services as svc
from .bcloud import BcloudError
from .runner import STAGES, Runner, bcloud_from, buyer_of
from .store import ACTIVE, Store

HERE = Path(__file__).resolve().parent.parent / 'web'
STATIC = {'ui.css': 'text/css', 'ui.js': 'application/javascript'}


def validate_init_data(init_data: str, bot_token: str, max_age_s: int = 86400, now: float | None = None) -> dict | None:
    """Пользователь из initData или None, если подпись неверна или устарела (как в bcloud)."""
    if not init_data:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    got = pairs.pop('hash', None)
    if not got:
        return None
    check = '\n'.join(f'{k}={v}' for k, v in sorted(pairs.items()))
    secret = hmac.new(b'WebAppData', bot_token.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got):
        return None
    try:
        if (now if now is not None else time.time()) - int(pairs.get('auth_date', '0')) > max_age_s:
            return None
        return json.loads(pairs['user'])
    except (KeyError, ValueError):
        return None


class ApiError(Exception):
    def __init__(self, text: str, status: int = 400):
        super().__init__(text)
        self.status = status


def hint(v: str) -> str:
    return f'••••{v[-4:]}' if v else ''


def build_web(store: Store, runner: Runner, bot_token: str, admins: set[int], public_url: str = '',
              bot_username: str = '') -> web.Application:

    @web.middleware
    async def guard(request: web.Request, handler):
        if not request.path.startswith('/api/'):
            return await handler(request)
        user = validate_init_data(request.headers.get('X-Init-Data', ''), bot_token)
        if not user or int(user.get('id', 0)) not in admins:
            return web.json_response({'detail': 'Нет доступа. Откройте панель из Telegram-бота.'}, status=403)
        request['user'] = user
        try:
            return await handler(request)
        except ApiError as e:
            return web.json_response({'detail': str(e)}, status=e.status)
        except (ValueError, BcloudError, engine.StepError) as e:
            return web.json_response({'detail': str(e)}, status=400)

    app = web.Application(middlewares=[guard], client_max_size=256 * 1024)
    r = web.RouteTableDef()
    J = web.json_response

    async def body(request) -> dict:
        try:
            data = await request.json()
        except ValueError:
            raise ApiError('пустой или неверный запрос') from None
        if not isinstance(data, dict):
            raise ApiError('неверный запрос')
        return data

    def profile_or_404(pid) -> dict:
        p = store.profile(int(pid))
        if not p:
            raise ApiError('Профиль не найден', 404)
        return p

    # ── страницы ──
    @r.get('/')
    async def root(_):
        raise web.HTTPFound('/admin')

    @r.get('/admin')
    async def admin_page(_):
        return web.FileResponse(HERE / 'admin.html', headers={'Cache-Control': 'no-cache'})

    @r.get('/static/{name}')
    async def static(request):
        name = request.match_info['name']
        if name not in STATIC:
            raise web.HTTPNotFound()
        return web.FileResponse(HERE / 'static' / name, headers={'Content-Type': STATIC[name], 'Cache-Control': 'no-cache'})

    @r.get('/health')
    async def health(_):
        return J({'ok': True})

    # ── состояние и настройки ──
    def settings_view() -> dict:
        out = {}
        for k, (label, help_, secret, _) in forms.SETTINGS.items():
            v = store.get(k)
            out[k] = {'label': label, 'help': help_, 'secret': secret, 'set': bool(v),
                      'value': '' if secret else v, 'hint': hint(v) if secret else ''}
        return out

    def profile_summary(p: dict, jobs: list[dict]) -> dict:
        mine = [j for j in jobs if j['profile_id'] == p['id']]
        return {'id': p['id'], 'name': p['name'], 'panel_url': p['panel_url'], 'base_domain': p.get('base_domain', ''),
                'traffic_control': p.get('traffic_control', True), 'owner_label': p.get('owner_label', ''),
                'servers': sum(1 for j in mine if j['status'] == 'done'),
                'active': sum(1 for j in mine if j['status'] in ACTIVE),
                'failed': sum(1 for j in mine if j['status'] == 'failed')}

    def issues() -> list[str]:
        from .handlers import ready_issues
        return ready_issues(store)

    @r.get('/api/state')
    async def state(request):
        jobs = store.jobs(1000)
        return J({'settings': settings_view(), 'profiles': [profile_summary(p, jobs) for p in store.profiles()],
                  'issues': issues(), 'bot_username': bot_username,
                  'jobs': {'active': sum(1 for j in jobs if j['status'] in ACTIVE),
                           'done': sum(1 for j in jobs if j['status'] == 'done'),
                           'failed': sum(1 for j in jobs if j['status'] == 'failed')},
                  'me': request['user'].get('first_name', '')})

    @r.post('/api/settings')
    async def save_settings(request):
        values = (await body(request)).get('values') or {}
        errors, clean = [], {}
        for k, v in values.items():
            if k not in forms.SETTINGS:
                continue
            label, _, secret, check = forms.SETTINGS[k]
            v = str(v or '').strip()
            if secret and not v:
                continue  # пусто — оставить текущий
            if not v:
                clean[k] = ''
                continue
            try:
                clean[k] = str(check(v, None))
            except ValueError as e:
                errors.append(f'{label}: {e}')
        if errors:
            raise ApiError('; '.join(errors))
        for k, v in clean.items():
            store.set(k, v)
        return J({'ok': True, 'settings': settings_view(), 'issues': issues()})

    @r.post('/api/check/bcloud')
    async def check_bcloud(_):
        try:
            bal = await bcloud_from(store).balance()
            return J({'ok': True, 'message': f'Подключено. Баланс: {bal / 100:,.0f} ₽'.replace(',', ' ')})
        except BcloudError as e:
            return J({'ok': False, 'message': str(e)})

    @r.post('/api/check/cloudflare')
    async def check_cf(_):
        try:
            st = await asyncio.to_thread(lambda: engine.Cloudflare(store.get('cf_token')).verify())
            return J({'ok': st == 'active', 'message': 'Токен активен' if st == 'active' else f'Статус токена: {st}'})
        except engine.StepError as e:
            return J({'ok': False, 'message': str(e)})

    # ── профили ──
    def profile_view(p: dict) -> dict:
        out = {k: ('' if k in forms.SECRET_FIELDS else p.get(k, '')) for k in forms.PFIELDS}
        out.update({'id': p['id'], 'traffic_control': p.get('traffic_control', True), 'owner_label': p.get('owner_label', ''),
                    'hints': {k: hint(str(p.get(k) or '')) for k in forms.SECRET_FIELDS}})
        return out

    @r.get('/api/fields')
    async def fields(_):
        return J({'profile': {k: {'label': lbl, 'help': h, 'secret': s} for k, (lbl, h, s, _) in forms.PFIELDS.items()},
                  'required': forms.REQUIRED})

    @r.get('/api/profiles/{pid}')
    async def get_profile(request):
        return J(profile_view(profile_or_404(request.match_info['pid'])))

    @r.post('/api/profiles')
    async def create_profile(request):
        prof = await with_owner(forms.clean_profile(await body(request)))
        prof['id'] = store.save_profile(prof)
        return J({'id': prof['id'], 'check': await panel_check(prof)})

    @r.put('/api/profiles/{pid}')
    async def update_profile(request):
        cur = profile_or_404(request.match_info['pid'])
        prof = await with_owner(forms.clean_profile(await body(request), cur), cur)
        prof['id'] = cur['id']
        store.save_profile(prof)
        return J({'id': prof['id'], 'check': await panel_check(prof)})

    async def with_owner(prof: dict, cur: dict | None = None) -> dict:
        """Владелец группы в bcloud: по ID или @username находим клиента и запоминаем его ID и подпись."""
        owner = prof.get('owner') or ''
        if not owner:
            prof['owner_tg_id'], prof['owner_label'] = '', ''
            return prof
        if cur and owner == cur.get('owner') and cur.get('owner_tg_id'):
            return prof
        try:
            u = await bcloud_from(store).resolve(owner)
        except BcloudError as e:
            raise ApiError(f'Клиент bcloud: {e}') from None
        if u.get('banned'):
            raise ApiError('Клиент bcloud заблокирован')
        prof['owner_tg_id'] = str(u['tg_id'])
        name = u.get('first_name') or ''
        prof['owner_label'] = (f'@{u["username"]}' if u.get('username') else name or str(u['tg_id']))
        return prof

    @r.delete('/api/profiles/{pid}')
    async def delete_profile(request):
        p = profile_or_404(request.match_info['pid'])
        if any(j['profile_id'] == p['id'] for j in store.active_jobs()):
            raise ApiError('У группы есть задачи в работе — дождитесь их')
        store.delete_profile(p['id'])
        return J({'ok': True})

    async def panel_check(p: dict) -> dict:
        try:
            info = await asyncio.to_thread(lambda: engine.Panel(p['panel_url'], p['panel_token'], p.get('panel_cookie', '')).check())
            return {'ok': True, 'message': f'Панель отвечает: {info}'}
        except engine.StepError as e:
            return {'ok': False, 'message': str(e)}
        except Exception as e:  # noqa: BLE001 — проверка не должна ронять сохранение
            return {'ok': False, 'message': f'{type(e).__name__}: {e}'}

    @r.post('/api/profiles/{pid}/check')
    async def check_profile(request):
        return J(await panel_check(profile_or_404(request.match_info['pid'])))

    @r.get('/api/profiles/{pid}/panel')
    async def panel_meta(request):
        """Сквады и Xray-шаблоны панели группы — для выбора при установке."""
        p = profile_or_404(request.match_info['pid'])
        try:
            meta = await asyncio.to_thread(lambda: engine.Panel(p['panel_url'], p['panel_token'], p.get('panel_cookie', '')).meta())
        except engine.StepError as e:
            raise ApiError(f'Панель: {e}') from None
        default = [x for x in re.split(r'[\s,]+', p.get('squads') or '') if x]
        return J({**meta, 'default_squads': default})

    # ── развёртывание ──
    @r.get('/api/catalog')
    async def catalog(request):
        p = profile_or_404(request.query.get('pid', 0))
        cat = await svc.load_catalog(store, p)
        try:
            bal = await bcloud_from(store, buyer_of(store, p)).balance()
        except BcloudError:
            bal = None
        return J({'locations': cat['locations'], 'periods': cat['periods'], 'os_label': cat['os_label'], 'balance_kop': bal,
                  'buyer': p.get('owner_label') or 'ваш аккаунт'})

    @r.post('/api/domains/auto')
    async def domains_auto(request):
        b = await body(request)
        p = profile_or_404(b.get('pid', 0))
        n = int(b.get('count') or 0)
        if not 1 <= n <= 50:
            raise ApiError('Количество: от 1 до 50')
        return J({'domains': await svc.auto_domains(store, p, str(b.get('location', '')), n)})

    @r.post('/api/domains/check')
    async def domains_check(request):
        b = await body(request)
        p = profile_or_404(b.get('pid', 0))
        return J({'domains': await svc.check_domains(store, p, b.get('domains') or '', int(b.get('count') or 0))})

    @r.post('/api/deploy')
    async def deploy(request):
        b = await body(request)
        p = profile_or_404(b.get('pid', 0))
        domains = b.get('domains') or []
        if not 1 <= len(domains) <= 50:
            raise ApiError('Нужно от 1 до 50 доменов')
        domains = await svc.check_domains(store, p, domains, len(domains))
        cat = await svc.load_catalog(store, p)
        q = svc.quote(cat, str(b.get('location')), int(b.get('idx')), str(b.get('period') or '1m'), len(domains))
        bal = await bcloud_from(store, buyer_of(store, p)).balance()
        if bal < q['total_kop']:
            who = p.get('owner_label') or 'вашего аккаунта'
            raise ApiError(f'Не хватает денег: на балансе {who} {bal / 100:.0f} ₽, нужно {q["total_kop"] / 100:.0f} ₽')
        squads = b.get('squads')
        if squads is not None and (not isinstance(squads, list) or not all(isinstance(x, str) for x in squads)):
            raise ApiError('сквады: список UUID')
        host = svc.host_options(str(b['location']), b.get('host_mode') or 'simple', b.get('tags'),
                                str(b.get('balancer_tag') or ''), str(b.get('balancer_remark') or ''), b.get('balancer_template'))
        ids = await svc.create_jobs(store, runner, int(request['user']['id']), p['id'], cat, str(b['location']),
                                    int(b['idx']), str(b.get('period') or '1m'), domains, squads,
                                    str(b.get('remark') or '')[:100], host)
        return J({'ids': ids})

    # ── задачи ──
    def job_view(j: dict, full: bool = False) -> dict:
        d = j['data']
        out = {'id': j['id'], 'domain': d['domain'], 'status': j['status'], 'stage': j['stage'], 'error': j['error'],
               'flag': d.get('flag', ''), 'loc_name': d.get('loc_name', ''), 'spec': d.get('spec', ''),
               'ip': d.get('ip'), 'created_at': j['created_at'], 'updated_at': j['updated_at'],
               'profile': (store.profile(j['profile_id']) or {}).get('name', ''), 'profile_id': j['profile_id']}
        if full:
            out['stages'] = [{'code': c, 'label': lbl} for c, lbl in STAGES]
            out['last'] = runner.last.get(j['id'], '')
            out['password'] = d.get('password', '') if j['status'] == 'done' else ''
            out['server_id'] = d.get('server_id')
            path = runner.log_path(j['id'])
            if path.exists():
                with open(path, 'rb') as f:
                    f.seek(max(0, path.stat().st_size - 24000))
                    out['log'] = engine.strip_ansi(f.read().decode('utf-8', 'replace')).replace('\r', '')
            else:
                out['log'] = ''
        return out

    def job_or_404(jid) -> dict:
        j = store.job(int(jid))
        if not j:
            raise ApiError('Задача не найдена', 404)
        return j

    @r.get('/api/jobs')
    async def jobs(request):
        pid = request.query.get('pid')
        js = store.jobs(500 if pid else 60)
        if pid:
            js = [j for j in js if str(j['profile_id']) == pid]
        return J({'jobs': [job_view(j) for j in js]})

    @r.get('/api/jobs/{jid}')
    async def job(request):
        return J(job_view(job_or_404(request.match_info['jid']), full=True))

    @r.post('/api/jobs/{jid}/retry')
    async def retry(request):
        j = job_or_404(request.match_info['jid'])
        if j['status'] != 'failed':
            raise ApiError('Повторить можно только задачу с ошибкой')
        store.update_job(j['id'], status='queued', error='')
        runner.start(j['id'])
        return J({'ok': True, 'rebuy': not j['data'].get('server_id')})

    @r.post('/api/jobs/{jid}/hide')
    async def hide(request):
        j = job_or_404(request.match_info['jid'])
        if j['status'] in ACTIVE:
            raise ApiError('Задача ещё в работе')
        store.update_job(j['id'], hidden=1)
        return J({'ok': True})

    app.add_routes(r)
    return app
