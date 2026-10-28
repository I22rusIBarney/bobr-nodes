"""Веб-админка (Telegram Mini App): страница /admin и JSON API /api/*.
Доступ — только из Telegram: подпись initData проверяется токеном бота, пользователь должен быть в ADMIN_IDS."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import socket
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

from . import engine, forms, naming, scripts as sc, services as svc
from .bcloud import BcloudError
from .runner import Runner, bcloud_from, buyer_of, stages_for
from .store import ACTIVE, Store

HERE = Path(__file__).resolve().parent.parent / 'web'
STATIC = {'ui.css': 'text/css', 'ui.js': 'application/javascript',
          'geist.woff2': 'font/woff2', 'geist-mono.woff2': 'font/woff2',  # Geist (SIL OFL 1.1, web/static/geist-OFL.txt)
          'fx.js': 'application/javascript'}
# оформление, как в bcloud: фон и сезонные эффекты (auto — по календарю)
THEME_BGS = ('none', 'aurora', 'night', 'ocean', 'sunset', 'forest', 'image')
THEME_EFFECTS = ('auto', 'none', 'snow', 'newyear', 'halloween', 'leaves', 'hearts', 'flowers', 'confetti')
IMG_TYPES = {b'\xff\xd8\xff': ('jpg', 'image/jpeg'), b'\x89PNG\r\n\x1a\n': ('png', 'image/png'), b'RIFF': ('webp', 'image/webp')}
BG_MAX = 4 * 1024 * 1024
GLASS_MAX = 80  # прозрачность списков, %: дальше текст на ярком фоне читается плохо


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


# заголовки безопасности: страница открывается только внутри Telegram, скрипты — свои и telegram.org
SEC_HEADERS = {
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
    'Permissions-Policy': 'camera=(), microphone=(), geolocation=(), payment=()',
    'Strict-Transport-Security': 'max-age=31536000',
    'Content-Security-Policy': ("default-src 'self'; script-src 'self' 'unsafe-inline' https://telegram.org; "
                                "style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self'; "
                                "object-src 'none'; base-uri 'none'; form-action 'none'; "
                                "frame-ancestors https://web.telegram.org https://*.telegram.org"),
}
FAIL_LIMIT, FAIL_WINDOW = 20, 600   # 20 запросов без доступа за 10 минут с одного IP — пауза


def bc_ui(store, tg_id=None):
    """Клиент bcloud для ответов админке: недоступный bcloud ждём недолго, чтобы страница не висела."""
    b = bcloud_from(store, tg_id)
    b.RETRY_FOR = 8
    return b


def client_ip(request) -> str:
    # бот доступен только через свой Caddy или Caddy bcloud (порт не опубликован) — им можно верить
    xff = request.headers.get('X-Forwarded-For', '')
    return (xff.split(',')[0].strip() if xff else '') or (request.remote or '?')


def build_web(store: Store, runner: Runner, bot_token: str, admins: set[int], public_url: str = '',
              bot_username: str = '', security=None, data_dir: str | Path = 'data') -> web.Application:
    media = Path(data_dir) / 'media'
    fails: dict[str, list[float]] = {}

    @web.middleware
    async def headers(request: web.Request, handler):
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = e
        for k, v in SEC_HEADERS.items():
            resp.headers.setdefault(k, v)
        if isinstance(resp, web.HTTPException) and resp.status >= 300:
            raise resp
        return resp

    @web.middleware
    async def guard(request: web.Request, handler):
        if not request.path.startswith('/api/'):
            return await handler(request)
        user = validate_init_data(request.headers.get('X-Init-Data', ''), bot_token)
        if not user or int(user.get('id', 0)) not in admins:
            ip, now = client_ip(request), time.time()
            recent = [t for t in fails.get(ip, []) if t > now - FAIL_WINDOW]
            if len(recent) >= FAIL_LIMIT:
                fails[ip] = recent
                return web.json_response({'detail': 'Слишком много запросов без доступа — подождите 10 минут.'}, status=429)
            fails[ip] = recent + [now]
            if len(fails) > 5000:  # не копим память на сканерах
                for k in [k for k, v in fails.items() if not v or v[-1] < now - FAIL_WINDOW]:
                    fails.pop(k, None)
            return web.json_response({'detail': 'Нет доступа. Откройте панель из Telegram-бота.'}, status=403)
        request['user'] = user
        try:
            return await handler(request)
        except ApiError as e:
            return web.json_response({'detail': str(e)}, status=e.status)
        except (ValueError, BcloudError, engine.StepError, sc.ScriptError) as e:
            return web.json_response({'detail': str(e)}, status=400)
        except web.HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 — показываем причину, а не «сервер не смог»
            logging.getLogger(__name__).exception('api %s', request.path)
            return web.json_response({'detail': f'Ошибка бота: {type(e).__name__}: {e}'[:300]}, status=500)

    app = web.Application(middlewares=[headers, guard], client_max_size=256 * 1024)
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
        cache = 'public, max-age=31536000, immutable' if name.endswith('.woff2') else 'no-cache'  # шрифты не меняются
        return web.FileResponse(HERE / 'static' / name, headers={'Content-Type': STATIC[name], 'Cache-Control': cache})

    @r.get('/health')
    async def health(_):
        return J({'ok': True})

    def bg_file() -> Path | None:
        return next((f for f in (media / f'bg.{e}' for e, _ in IMG_TYPES.values()) if f.exists()), None)

    @r.get('/media/bg')
    async def media_bg(_):  # картинка фона; без авторизации, как и статика: её грузит CSS
        f = bg_file()
        if not f:
            raise web.HTTPNotFound()
        ctype = next(t for e, t in IMG_TYPES.values() if f.suffix == '.' + e)
        return web.FileResponse(f, headers={'Content-Type': ctype, 'Cache-Control': 'public, max-age=86400'})

    def theme() -> dict:
        bg, effect = store.get('theme_bg') or 'none', store.get('theme_effect') or 'auto'
        try:
            glass = max(0, min(GLASS_MAX, int(store.get('theme_glass') or 0)))
        except ValueError:
            glass = 0
        return {'bg': bg if bg in THEME_BGS else 'none', 'effect': effect if effect in THEME_EFFECTS else 'auto',
                'image': store.get('theme_bg_image') or '', 'glass': glass}

    @r.post('/api/theme')
    async def theme_save(request):
        data = await body(request)
        if 'bg' in data:
            if data['bg'] not in THEME_BGS or (data['bg'] == 'image' and not bg_file()):
                raise ApiError('Такого фона нет')
            store.set('theme_bg', data['bg'])
        if 'effect' in data:
            if data['effect'] not in THEME_EFFECTS:
                raise ApiError('Такого эффекта нет')
            store.set('theme_effect', data['effect'])
        if 'glass' in data:
            g = data['glass']
            if not isinstance(g, int) or isinstance(g, bool) or not 0 <= g <= GLASS_MAX:
                raise ApiError(f'Прозрачность — от 0 до {GLASS_MAX}%')
            store.set('theme_glass', str(g))
        return J(theme())

    @r.put('/api/theme/background')
    async def theme_bg_upload(request):
        data = await request.content.read(BG_MAX + 1)
        if not data:
            raise ApiError('Файл пустой.')
        if len(data) > BG_MAX:
            raise ApiError('Картинка больше 4 МБ. Уменьшите её и загрузите снова.')
        kind = next((v for sig, v in IMG_TYPES.items() if data.startswith(sig) and (sig != b'RIFF' or data[8:12] == b'WEBP')), None)
        if not kind:
            raise ApiError('Подойдут JPG, PNG или WebP.')
        media.mkdir(parents=True, exist_ok=True)
        for e, _ in IMG_TYPES.values():
            (media / f'bg.{e}').unlink(missing_ok=True)
        (media / f'bg.{kind[0]}').write_bytes(data)
        store.set('theme_bg_image', f'/media/bg?v={int(time.time())}')
        store.set('theme_bg', 'image')
        return J(theme())

    @r.delete('/api/theme/background')
    async def theme_bg_delete(_):
        for e, _t in IMG_TYPES.values():
            (media / f'bg.{e}').unlink(missing_ok=True)
        store.set('theme_bg_image', '')
        if store.get('theme_bg') == 'image':
            store.set('theme_bg', 'none')
        return J(theme())

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
                'name_pattern': p.get('name_pattern') or naming.DEFAULT_PATTERN,
                'script': ((store.script(p['script_id']) or {}).get('recipe') or {}).get('title') if p.get('script_id') else '',
                'script_profile': ((((store.script(p['script_id']) or {}).get('recipe') or {}).get('profile') or {}).get('mode')
                                   if p.get('script_id') else None),
                'panel_profile_uuid': p.get('panel_profile_uuid') or '',
                'servers': sum(1 for j in mine if j['status'] == 'done' and j['data'].get('kind') not in ('node', 'reip')),
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
                  'security': security.info() if security else None, 'theme': theme(),
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
            bal = await bc_ui(store).balance()
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
                    'script_id': p.get('script_id'), 'panel_profile_uuid': p.get('panel_profile_uuid', ''),
                    'panel_inbounds': p.get('panel_inbounds') or [],
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
        data = await body(request)
        prof = await with_script(await with_owner(forms.clean_profile(data)), data)
        prof['id'] = store.save_profile(prof)
        return J({'id': prof['id'], 'check': await panel_check(prof)})

    @r.put('/api/profiles/{pid}')
    async def update_profile(request):
        cur = profile_or_404(request.match_info['pid'])
        data = await body(request)
        prof = await with_script(await with_owner(forms.clean_profile(data, cur), cur), data)
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
            u = await bc_ui(store).resolve(owner)
        except BcloudError as e:
            raise ApiError(f'Клиент bcloud: {e}') from None
        if u.get('banned'):
            raise ApiError('Клиент bcloud заблокирован')
        prof['owner_tg_id'] = str(u['tg_id'])
        name = u.get('first_name') or ''
        prof['owner_label'] = (f'@{u["username"]}' if u.get('username') else name or str(u['tg_id']))
        return prof

    async def with_script(prof: dict, data: dict) -> dict:
        """Скрипт установки группы: встроенный Nuvrion (пусто) или утверждённый из «Моих скриптов»;
        для скриптов без своего профиля — профиль и inbound'ы панели."""
        if 'script_id' in data:
            sid = data.get('script_id')
            if sid in (None, '', 0):
                prof['script_id'] = None
            else:
                s = store.script(int(sid))
                if not s or s.get('status') != 'approved':
                    raise ApiError('Скрипт установки: выберите утверждённый скрипт из «Моих скриптов»')
                prof['script_id'] = s['id']
        if 'panel_profile_uuid' in data:
            prof['panel_profile_uuid'] = str(data.get('panel_profile_uuid') or '')
        if 'panel_inbounds' in data:
            inb = data.get('panel_inbounds') or []
            if not isinstance(inb, list) or not all(isinstance(x, str) for x in inb):
                raise ApiError('inbound: список UUID')
            prof['panel_inbounds'] = inb
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

    def parse_extras(b: dict) -> dict:
        """{'psiphon': {region, domains}, 'warp': {domains}} из запроса; StepError — если что-то не так."""
        out = {}
        ps, wp = b.get('psiphon'), b.get('warp')
        if isinstance(ps, dict) and ps.get('enabled'):
            out['psiphon'] = {'region': engine.psiphon_regions(str(ps.get('region') or '')),
                              'domains': engine.psiphon_rules(ps.get('domains') or '')}
        if isinstance(wp, dict) and wp.get('enabled'):
            out['warp'] = {'domains': engine.warp_rules(wp.get('domains') or '')}
        xh = b.get('xhttp')
        if isinstance(xh, dict) and xh.get('enabled'):
            out['xhttp'] = {'port': engine.XHTTP_PORT}
        return out

    # ── ноды панели: дополнения на уже работающих нодах ──
    def panel_of(p: dict) -> engine.Panel:
        return engine.Panel(p['panel_url'], p['panel_token'], p.get('panel_cookie', ''))

    def bot_jobs_by_ip(pid: int) -> dict[str, dict]:
        """IP и домен → данные последней удачной задачи покупки (домен, IP, пароль root, сервер bcloud) этой группы.
        Ноды регистрируются по домену, старые — по IP, поэтому ключи — оба."""
        out = {}
        for j in sorted(store.jobs(5000, include_hidden=True), key=lambda x: x['id']):
            d = j['data']
            if (j['profile_id'] == pid and d.get('ip') and d.get('kind') not in ('node', 'reip') and j['status'] == 'done'
                    and not d.get('removed')):
                d = {**d, '_job': j['id']}
                out[d['ip']] = d
                if d.get('domain'):
                    out[d['domain']] = d
        return out

    def ssh_view(pid: int, uuid: str, ip: str) -> dict:
        saved = runner.ssh_of(pid, uuid)
        if saved.get('mode'):
            return {'mode': saved['mode'], 'user': saved.get('user') or 'root', 'port': int(saved.get('port') or 22),
                    'has_password': bool(saved.get('password')), 'source': 'saved'}
        d = bot_jobs_by_ip(pid).get(ip)
        if d:  # нода куплена ботом: root-пароль известен из задачи покупки
            return {'mode': 'password', 'user': 'root', 'port': 22, 'has_password': True, 'source': 'bot'}
        return {'mode': '', 'user': 'root', 'port': 22, 'has_password': False, 'source': ''}

    def node_ssh(pid: int, uuid: str, ip: str) -> dict:
        """Доступ для задачи: сохранённый, иначе — ключ бота и пароль из задачи покупки."""
        saved = runner.ssh_of(pid, uuid)
        if saved.get('mode'):
            return saved
        d = bot_jobs_by_ip(pid).get(ip)
        return {'mode': 'password', 'user': 'root', 'port': 22, 'password': d.get('password', '')} if d else {}

    @r.get('/api/profiles/{pid}/nodes')
    async def panel_nodes(request):
        p = profile_or_404(request.match_info['pid'])

        def load():
            pl = panel_of(p)
            profiles = {x['uuid']: x for x in pl.call('GET', '/api/config-profiles')['configProfiles']}
            return pl.call('GET', '/api/nodes'), profiles
        try:
            nodes, profiles = await asyncio.to_thread(load)
        except engine.StepError as e:
            raise ApiError(f'Панель: {e}') from None
        bot = bot_jobs_by_ip(p['id'])
        out = []
        for n in nodes:
            pr = profiles.get((n.get('configProfile') or {}).get('activeConfigProfileUuid')) or {}
            out.append({'uuid': n['uuid'], 'name': n['name'], 'address': n['address'], 'connected': bool(n.get('isConnected')),
                        'disabled': bool(n.get('isDisabled')), 'country': n.get('countryCode') or '',
                        'online': n.get('usersOnline'),
                        'profile': pr.get('name', ''), 'features': sorted(engine.features_of(pr.get('config'))),
                        'bot': n['address'] in bot, 'domain': (bot.get(n['address']) or {}).get('domain', '')})
        return J({'nodes': out, 'bot_key': runner.bot_key()[1]})

    @r.get('/api/profiles/{pid}/nodes/{uuid}')
    async def panel_node(request):
        p = profile_or_404(request.match_info['pid'])
        uuid = request.match_info['uuid']

        def load():
            pl = panel_of(p)
            n = next((x for x in pl.call('GET', '/api/nodes') if x['uuid'] == uuid), None)
            if not n:
                raise ApiError('Ноды нет в панели', 404)
            pr = next((x for x in pl.call('GET', '/api/config-profiles')['configProfiles']
                       if x['uuid'] == (n.get('configProfile') or {}).get('activeConfigProfileUuid')), {}) or {}
            bd = bot_jobs_by_ip(p['id']).get(n['address']) or {}
            ip = bd.get('ip') or (n['address'] if engine.is_ip(n['address']) else resolve(n['address']))
            dom = bd.get('domain') or ('' if engine.is_ip(n['address']) else n['address'])
            return n, pr, pl.node_hosts(n, {dom} if dom else set(), {ip} if ip else set()), bd, ip, dom
        try:
            n, pr, hosts, bd, ip, dom = await asyncio.to_thread(load)
        except engine.StepError as e:
            raise ApiError(f'Панель: {e}') from None
        bc = None
        if bd.get('server_id'):
            bc = {'server_id': bd['server_id'], 'price_kop': None}
            try:
                srv = await bc_ui(store, bd.get('buyer') or buyer_of(store, p)).server(int(bd['server_id']))
                bc['price_kop'] = srv.get('change_ip_price_kop')
            except BcloudError as e:
                bc['error'] = str(e)
        return J({'uuid': n['uuid'], 'name': n['name'], 'address': n['address'], 'connected': bool(n.get('isConnected')),
                  'profile': pr.get('name', ''), 'features': sorted(engine.features_of(pr.get('config'))),
                  'inbound_port': next((i.get('port') for i in pr.get('inbounds', [])
                                        if i['uuid'] in engine.active_inbounds(n)), None),
                  'country': n.get('countryCode') or '', 'online': n.get('usersOnline'),
                  'hosts': [{'uuid': h['uuid'], 'remark': h.get('remark', ''), 'address': h.get('address', ''),
                             'port': h.get('port'), 'final_mask': bool(h.get('finalMask')), 'hidden': bool(h.get('isHidden'))}
                            for h in hosts],
                  'ip': ip, 'domain': dom, 'address_is_ip': engine.is_ip(n['address']), 'bcloud': bc,
                  'ssh': ssh_view(p['id'], uuid, n['address']), 'bot_key': runner.bot_key()[1]})

    @r.post('/api/profiles/{pid}/nodes/{uuid}/ssh')
    async def panel_node_ssh(request):
        """Сохранить доступ по SSH (mode key|password) и проверить вход."""
        p = profile_or_404(request.match_info['pid'])
        uuid, b = request.match_info['uuid'], await body(request)
        cur = runner.ssh_of(p['id'], uuid)
        mode = b.get('mode') if b.get('mode') in ('key', 'password') else 'password'
        user = str(b.get('user') or 'root').strip()
        if not re.fullmatch(r'[a-z_][a-z0-9_.-]{0,31}', user):
            raise ApiError('Пользователь: латиница, например root')
        try:
            port = int(b.get('port') or 22)
            assert 1 <= port <= 65535
        except (ValueError, AssertionError):
            raise ApiError('SSH-порт: число от 1 до 65535') from None
        pw = b.get('password')
        new = {'mode': mode, 'user': user, 'port': port,
               'password': (str(pw) if pw else cur.get('password', '')) if mode == 'password' else ''}
        if mode == 'password' and not new['password']:
            raise ApiError('Введите пароль')
        ip = str(b.get('ip') or '')
        result = {'ok': True, 'message': 'Сохранено'}
        if ip:
            node = {'ip': ip, 'ssh_user': user, 'ssh_port': port, 'known_hosts': runner.known_hosts()}
            if mode == 'key':
                node['ssh_key'] = runner.bot_key()[0]
            else:
                node['ssh_password'] = new['password']

            def check():
                rr = engine.Remote(node, None)
                rc, out = rr.run('id -u', 25)
                return rc == 0 and out.strip().splitlines()[-1:] == ['0'], out
            try:
                good, out = await asyncio.to_thread(check)
                result = {'ok': good, 'message': 'Вход по SSH работает, права root есть' if good else
                          'Вход есть, но не под root — нужен root или sudo без пароля'}
            except engine.StepError as e:
                result = {'ok': False, 'message': f'SSH: {e}'}
            except Exception as e:  # noqa: BLE001
                result = {'ok': False, 'message': f'SSH: {type(e).__name__}: {e}'}
        store.set(f'node_ssh:{p["id"]}:{uuid}', json.dumps(new))
        return J(result)

    def resolve(host: str) -> str:
        try:
            return socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
        except (OSError, UnicodeError, IndexError):
            return ''

    @r.post('/api/profiles/{pid}/nodes/{uuid}/ip')
    async def panel_node_ip(request):
        """Перепривязка IP: mode bcloud — сменить в bcloud (платно, с баланса владельца), sync — взять уже сменённый
        из bcloud, manual — новый IP вручную. Дальше задача: Cloudflare, SSH, адрес в панели."""
        p = profile_or_404(request.match_info['pid'])
        uuid, b = request.match_info['uuid'], await body(request)
        mode = b.get('mode')
        if mode not in ('bcloud', 'sync', 'manual'):
            raise ApiError('Неизвестный способ')
        nodes = await asyncio.to_thread(lambda: panel_of(p).call('GET', '/api/nodes'))
        n = next((x for x in nodes if x['uuid'] == uuid), None)
        if not n:
            raise ApiError('Ноды нет в панели', 404)
        bd = bot_jobs_by_ip(p['id']).get(n['address']) or {}
        old = bd.get('ip') or (n['address'] if engine.is_ip(n['address']) else await asyncio.to_thread(resolve, n['address']))
        if not old:
            raise ApiError('Не знаю текущий IP ноды')
        new = ''
        if mode == 'manual':
            new = str(b.get('ip') or '').strip()
            if not engine.is_ip(new) or ':' in new:
                raise ApiError('Новый IP: адрес IPv4, например 203.0.113.10')
            if new == old:
                raise ApiError('Это текущий IP ноды')
        elif not bd.get('server_id'):
            raise ApiError('Нода куплена не через бота — укажите новый IP вручную')
        ssh = node_ssh(p['id'], uuid, n['address'])
        if not ssh:
            raise ApiError('Укажите доступ по SSH к ноде — после смены IP бот проверяет вход')
        if any(j['status'] in ACTIVE and j['data'].get('kind') == 'reip' and j['data'].get('node_uuid') == uuid
               for j in store.jobs(200)):
            raise ApiError('Смена IP этой ноды уже идёт')
        doms = [bd['domain']] if bd.get('domain') else []
        if not engine.is_ip(n['address']) and n['address'] not in doms:
            doms.append(n['address'])
        data = {'kind': 'reip', 'mode': mode, 'node_uuid': uuid, 'name': n['name'], 'ip': old, 'old_ip': old, 'new_ip': new,
                'domain': (doms[0] if doms else n['name']), 'domains': doms, 'flag': '🔁', 'loc_name': n['name'],
                'spec': 'смена IP', 'ssh': ssh, 'server_id': bd.get('server_id'), 'buyer': bd.get('buyer'),
                'deploy_job': bd.get('_job')}
        jid = store.add_job('reip-' + uuid[:8], p['id'], int(request['user']['id']), data)
        await runner.render(jid, force=True)
        runner.start(jid)
        return J({'id': jid})

    @r.post('/api/profiles/{pid}/nodes/{uuid}/address-domain')
    async def panel_node_address(request):
        """Адрес ноды в панели: IP → домен (тогда при смене IP хватит A-записи)."""
        p = profile_or_404(request.match_info['pid'])
        uuid = request.match_info['uuid']

        def run():
            pl = panel_of(p)
            n = next((x for x in pl.call('GET', '/api/nodes') if x['uuid'] == uuid), None)
            if not n:
                raise ApiError('Ноды нет в панели', 404)
            dom = (bot_jobs_by_ip(p['id']).get(n['address']) or {}).get('domain')
            if not dom:
                raise ApiError('Домен ноды боту неизвестен')
            if resolve(dom) != n['address'] and engine.is_ip(n['address']):
                raise ApiError(f'{dom} сейчас указывает не на {n["address"]} — сначала исправьте DNS')
            pl.call('PATCH', '/api/nodes', {'uuid': uuid, 'address': dom})
            return dom
        dom = await asyncio.to_thread(run)
        return J({'ok': True, 'message': f'Адрес ноды в панели — {dom}'})

    @r.post('/api/profiles/{pid}/nodes/{uuid}/forget-hostkey')
    async def panel_node_forget(request):
        """Забыть ключ SSH сервера — после переустановки системы на ноде."""
        profile_or_404(request.match_info['pid'])
        ip = str((await body(request)).get('ip') or '')
        if not re.fullmatch(r'[0-9a-fA-F:.]{3,45}', ip):
            raise ApiError('Неверный адрес')
        engine.forget_host(runner.known_hosts(), ip)
        return J({'ok': True, 'message': 'Ключ забыт — при следующем входе бот запомнит новый'})

    @r.post('/api/profiles/{pid}/nodes/{uuid}/rollback')
    async def panel_node_rollback(request):
        """Убрать дополнения: нода и её хосты — обратно в исходный профиль (Psiphon/WARP на ноде остаются, но не нужны)."""
        p = profile_or_404(request.match_info['pid'])
        uuid = request.match_info['uuid']
        nodes = await asyncio.to_thread(lambda: panel_of(p).call('GET', '/api/nodes'))
        n = next((x for x in nodes if x['uuid'] == uuid), None)
        if not n:
            raise ApiError('Ноды нет в панели', 404)
        bd = bot_jobs_by_ip(p['id']).get(n['address']) or {}
        data = {'kind': 'node', 'rollback': True, 'node_uuid': uuid, 'name': n['name'], 'ip': bd.get('ip') or n['address'],
                'domain': bd.get('domain') or n['name'], 'domains': [bd['domain']] if bd.get('domain') else [],
                'flag': '↩️', 'loc_name': n['name'], 'spec': 'убрать дополнения'}
        jid = store.add_job('node-' + uuid[:8], p['id'], int(request['user']['id']), data)
        await runner.render(jid, force=True)
        runner.start(jid)
        return J({'id': jid})

    @r.post('/api/profiles/{pid}/nodes/{uuid}/apply')
    async def panel_node_apply(request):
        """Задача «дополнения на ноде»: Psiphon/WARP (с переводом ноды в вариант профиля), порт, Final Mask."""
        p = profile_or_404(request.match_info['pid'])
        uuid, b = request.match_info['uuid'], await body(request)
        try:
            extras = parse_extras(b)
            port = b.get('port')
            port = '' if port in (None, '') else (engine.public_port(port, int(p.get('node_port') or 2222)) or 443)
            fm = b.get('final_mask')
            fm = None if fm is None else (engine.final_mask(b.get('final_mask_json') or None) if fm else False)
        except engine.StepError as e:
            raise ApiError(str(e)) from None
        if not extras and port == '' and fm is None:
            raise ApiError('Нечего менять')
        nodes = await asyncio.to_thread(lambda: panel_of(p).call('GET', '/api/nodes'))
        n = next((x for x in nodes if x['uuid'] == uuid), None)
        if not n:
            raise ApiError('Ноды нет в панели', 404)
        ssh = node_ssh(p['id'], uuid, n['address'])
        if (extras or port != '') and not ssh:
            raise ApiError('Укажите доступ по SSH к ноде — без него Psiphon, WARP, XHTTP и порт не поставить')
        bd = bot_jobs_by_ip(p['id']).get(n['address']) or {}
        data = {'kind': 'node', 'node_uuid': uuid, 'name': n['name'], 'ip': bd.get('ip') or n['address'],
                'domain': bd.get('domain') or n['name'], 'domains': [bd['domain']] if bd.get('domain') else [],
                'flag': '🛠', 'loc_name': n['name'], 'spec': 'дополнения на ноде', 'ssh': ssh,
                'psiphon': extras.get('psiphon'), 'warp': extras.get('warp'), 'xhttp': extras.get('xhttp'),
                'port': port, 'final_mask': fm}
        jid = store.add_job('node-' + uuid[:8], p['id'], int(request['user']['id']), data)
        await runner.render(jid, force=True)
        runner.start(jid)
        return J({'id': jid})

    # ── перенос хостов между панелями ──
    @r.post('/api/hosts/export')
    async def hosts_export(request):
        """Набор хостов (с профилями и Xray JSON-шаблонами) из панели группы pid."""
        b = await body(request)
        p = profile_or_404(b.get('pid', 0))
        uuids = [u for u in (b.get('hosts') or []) if isinstance(u, str)]
        if not 1 <= len(uuids) <= 200:
            raise ApiError('Выберите хосты')
        try:
            return J(await asyncio.to_thread(engine.export_hosts, panel_of(p), uuids))
        except engine.StepError as e:
            raise ApiError(f'Панель: {e}') from None

    @r.post('/api/hosts/import')
    async def hosts_import(request):
        """Создать хосты в панели группы pid: из другой группы (from_pid + hosts), из набора JSON (bundle)
        или просто шаблон Xray JSON (template_name + template_json)."""
        b = await body(request)
        p = profile_or_404(b.get('pid', 0))
        squads = [x for x in (b.get('squads') or []) if isinstance(x, str)]
        mode = 'update' if b.get('template_mode') == 'update' else 'copy'
        lines: list[str] = []

        def run():
            if b.get('from_pid'):
                src = profile_or_404(b['from_pid'])
                uuids = [u for u in (b.get('hosts') or []) if isinstance(u, str)]
                if not uuids:
                    raise ApiError('Выберите хосты')
                bundle = engine.export_hosts(panel_of(src), uuids)
            elif b.get('template_json'):
                bundle = engine.template_bundle(str(b.get('template_name') or ''), b['template_json'])
            else:
                bundle = engine.check_bundle(b.get('bundle'))
            with engine.PANEL_LOCK:
                return engine.import_bundle(panel_of(p), bundle, squads, mode, lines.append)
        try:
            res = await asyncio.to_thread(run)
        except (engine.StepError, ApiError) as e:  # итог — на странице: что не так и что уже успели сделать
            return J({'ok': False, 'error': str(e), 'log': lines})
        return J({'ok': True, **res, 'log': lines})

    # ── развёртывание ──
    @r.get('/api/catalog')
    async def catalog(request):
        p = profile_or_404(request.query.get('pid', 0))
        cat = await svc.load_catalog(store, p)
        try:
            bal = await bc_ui(store, buyer_of(store, p)).balance()
        except BcloudError:
            bal = None
        return J({'locations': cat['locations'], 'periods': cat['periods'], 'os_label': cat['os_label'], 'balance_kop': bal,
                  'buyer': p.get('owner_label') or 'ваш аккаунт'})

    @r.get('/api/profiles/{pid}/zones')
    async def zones(request):
        """Домены Cloudflare для выбора: зоны аккаунта (токен группы или общий) + домен группы для автоимён."""
        p = profile_or_404(request.match_info['pid'])
        base = p.get('base_domain') or ''
        try:
            names = await asyncio.to_thread(lambda: svc.cf_for(store, p).zone_names())
            err = ''
        except engine.StepError as e:
            names, err = [], str(e)
            if '403' in err or 'uthentication' in err or 'permission' in err.lower():
                err = 'Токену Cloudflare не хватает права Zone → Read, чтобы показать список доменов. Добавьте его — или введите домен в настройках группы.'
        if base and base not in names:
            names = [base, *names]
        return J({'zones': names, 'default': base or (names[0] if names else ''), 'error': err})

    @r.post('/api/domains/auto')
    async def domains_auto(request):
        b = await body(request)
        p = profile_or_404(b.get('pid', 0))
        n = int(b.get('count') or 0)
        if not 1 <= n <= 50:
            raise ApiError('Количество: от 1 до 50')
        exclude = {naming.norm_domain(x) for x in (b.get('exclude') or []) if isinstance(x, str)}
        return J({'domains': await svc.auto_domains(store, p, str(b.get('location', '')), n, b.get('base') or None, exclude)})

    @r.post('/api/domains/check')
    async def domains_check(request):
        b = await body(request)
        p = profile_or_404(b.get('pid', 0))
        return J({'domains': await svc.check_domains(store, p, b.get('domains') or '', int(b.get('count') or 0))})

    @r.post('/api/deploy')
    async def deploy(request):
        """Одна или несколько локаций: items = [{location, idx, domains}], либо старый вид location/idx/domains."""
        b = await body(request)
        p = profile_or_404(b.get('pid', 0))
        items = b.get('items') or [{'location': b.get('location'), 'idx': b.get('idx'), 'domains': b.get('domains') or []}]
        if not isinstance(items, list) or not items:
            raise ApiError('Не выбрано ни одной локации')
        try:
            extras = parse_extras(b)
            mask = engine.final_mask(b.get('final_mask_json') or None) if b.get('final_mask') else None
            port = engine.public_port(b.get('port'), int(p.get('node_port') or 2222))
        except engine.StepError as e:
            raise ApiError(str(e)) from None
        if extras.get('xhttp') and p.get('script_id'):
            raise ApiError('Запасной XHTTP ставится только встроенным установщиком — у группы свой скрипт')
        if extras.get('xhttp') and port == engine.XHTTP_PORT:
            raise ApiError(f'Порт {engine.XHTTP_PORT} занят запасным XHTTP — выберите другой порт для клиентов')
        period = str(b.get('period') or '1m')
        all_domains = [d for it in items for d in (it.get('domains') or [])]
        if not 1 <= len(all_domains) <= 50:
            raise ApiError('Нужно от 1 до 50 доменов')
        checked = await svc.check_domains(store, p, all_domains, len(all_domains))
        cat = await svc.load_catalog(store, p)
        plan, total, pos = [], 0, 0
        for it in items:
            n = len(it.get('domains') or [])
            if not n:
                continue
            q = svc.quote(cat, str(it.get('location')), int(it.get('idx')), period, n)
            plan.append((str(it['location']), int(it['idx']), checked[pos:pos + n]))
            pos += n
            total += q['total_kop']
        q = {'total_kop': total}
        bal = await bc_ui(store, buyer_of(store, p)).balance()
        if bal < q['total_kop']:
            who = p.get('owner_label') or 'вашего аккаунта'
            raise ApiError(f'Не хватает денег: на балансе {who} {bal / 100:.0f} ₽, нужно {q["total_kop"] / 100:.0f} ₽')
        squads = b.get('squads')
        if squads is not None and (not isinstance(squads, list) or not all(isinstance(x, str) for x in squads)):
            raise ApiError('сквады: список UUID')
        mode = b.get('host_mode') or 'simple'
        multi = len(plan) > 1
        hosts = [svc.host_options(loc, mode, b.get('tags'),
                                  '' if multi else str(b.get('balancer_tag') or ''),  # несколько стран — тег у каждой свой
                                  str(b.get('balancer_remark') or ''), b.get('balancer_template'))
                 for loc, _, _ in plan]
        places = {}
        order = b.get('host_order')
        if isinstance(order, list) and order:
            try:  # уже существующие хосты — чтобы не ставить «после» несуществующего
                meta = await asyncio.to_thread(lambda: engine.Panel(p['panel_url'], p['panel_token'], p.get('panel_cookie', '')).meta())
            except engine.StepError as e:
                raise ApiError(f'Панель: {e} — не получилось проверить порядок хостов') from None
            places = svc.host_places(order, {loc: doms for loc, _, doms in plan}, {x['uuid'] for x in meta['hosts']})
        for host in hosts:
            host.update({'final_mask': mask, 'port': port})
        ids = []
        for (loc, idx, doms), host in zip(plan, hosts):
            ids += await svc.create_jobs(store, runner, int(request['user']['id']), p['id'], cat, loc, idx, period, doms,
                                         squads, str(b.get('remark') or '')[:100], host,
                                         'en' if b.get('remark_lang') == 'en' else 'ru', places, extras)
        return J({'ids': ids})

    # ── мои скрипты ──
    sdir = Path(runner.logdir).parent / 'scripts'
    sdir.mkdir(parents=True, exist_ok=True)

    def script_or_404(sid) -> dict:
        s = store.script(int(sid))
        if not s:
            raise ApiError('Скрипт не найден', 404)
        return s

    def gemini() -> sc.Gemini:
        return sc.Gemini(store.get('gemini_key'), store.get('gemini_model'), store.get('gemini_proxy'),
                         base=store.get('gemini_base') or None)

    def script_summary(s: dict) -> dict:
        used = [p['name'] for p in store.profiles() if p.get('script_id') == s['id']]
        return {'id': s['id'], 'title': (s.get('recipe') or {}).get('title') or s['path'], 'path': s['path'],
                'repo': f'{s["owner"]}/{s["repo"]}', 'sha': s['sha'][:12], 'status': s.get('status', 'draft'),
                'has_recipe': bool(s.get('recipe')), 'used_by': used,
                'profile_mode': ((s.get('recipe') or {}).get('profile') or {}).get('mode')}

    def script_view(s: dict) -> dict:
        out = {**script_summary(s), 'url': s.get('url'), 'sha_full': s['sha'], 'sha256': s['sha256'], 'size': s['size'],
               'analysis': s.get('analysis'), 'recipe': s.get('recipe'),
               'describe': sc.describe(s['recipe']) if s.get('recipe') else None,
               'chat': [m for m in s.get('chat', []) if not m.get('hidden')],
               'github': f'https://github.com/{s["owner"]}/{s["repo"]}/blob/{s["sha"]}/{s["path"]}'}
        return out

    @r.get('/api/scripts')
    async def scripts_list(_):
        return J({'scripts': [script_summary(s) for s in store.scripts()], 'gemini': bool(store.get('gemini_key'))})

    @r.post('/api/scripts/resolve')
    async def scripts_resolve(request):
        b = await body(request)
        return J(await sc.resolve(str(b.get('url') or '')))

    @r.post('/api/scripts')
    async def scripts_add(request):
        b = await body(request)
        res = await sc.resolve(str(b.get('url') or ''))
        path = b.get('path') or res['path']
        if not path:
            return J({'choose': res['candidates'], 'sha': res['sha']})
        if res['candidates'] and path not in {c['path'] for c in res['candidates']}:
            raise ApiError('такого файла нет в репозитории')
        f = await sc.fetch(res['owner'], res['repo'], res['sha'], path)
        s = {'url': str(b.get('url')), 'owner': res['owner'], 'repo': res['repo'], 'ref': res['ref'], 'sha': res['sha'],
             'path': path, 'sha256': f['sha256'], 'size': f['size'], 'status': 'draft', 'chat': [], 'analysis': None,
             'recipe': None, 'created_at': time.time()}
        s['id'] = store.save_script(s)
        (sdir / f'{s["id"]}.sh').write_text(f['text'], encoding='utf-8')
        (sdir / f'{s["id"]}.readme').write_text(f['readme'], encoding='utf-8')
        return J({'id': s['id']})

    def merge_answer(s: dict, raw: dict) -> dict:
        a = sc.apply_answer(raw)
        s['analysis'] = {k: a[k] for k in ('summary', 'recommendations', 'risks', 'questions', 'recipe_error')}
        if a['recipe']:
            if a['recipe'] != s.get('recipe'):
                s['status'] = 'draft'  # новый рецепт снова нужно утвердить
            s['recipe'] = a['recipe']
        show = a['summary']
        if a['questions']:
            show += '\n\nВопросы:\n' + '\n'.join(f'• {q}' for q in a['questions'])
        if a['recipe_error']:
            show += f'\n\n⚠️ Рецепт не прошёл проверку: {a["recipe_error"]}'
        s['chat'].append({'role': 'model', 'text': json.dumps(raw, ensure_ascii=False), 'show': show, 'ts': time.time()})
        return s

    def turns(s: dict) -> list[dict]:
        return [{'role': m['role'], 'text': m['text']} for m in s['chat']]

    @r.post('/api/scripts/{sid}/analyze')
    async def scripts_analyze(request):
        s = script_or_404(request.match_info['sid'])
        text = (sdir / f'{s["id"]}.sh').read_text(encoding='utf-8')
        readme = (sdir / f'{s["id"]}.readme').read_text(encoding='utf-8') if (sdir / f'{s["id"]}.readme').exists() else ''
        s['chat'] = [{'role': 'user', 'text': sc.first_turn(s, text, readme), 'hidden': True, 'ts': time.time()}]
        g = gemini()
        raw = await g.ask(sc.SYSTEM, turns(s))
        s['model'] = g.model
        store.save_script(merge_answer(s, raw))
        return J(script_view(s))

    @r.post('/api/scripts/{sid}/chat')
    async def scripts_chat(request):
        s = script_or_404(request.match_info['sid'])
        text = str((await body(request)).get('text') or '').strip()[:4000]
        if not text:
            raise ApiError('пустое сообщение')
        if not s['chat']:
            raise ApiError('сначала нажмите «Разобрать»')
        s['chat'].append({'role': 'user', 'text': text, 'ts': time.time()})
        cur = json.dumps(s.get('recipe'), ensure_ascii=False) if s.get('recipe') else 'нет'
        g = gemini()
        try:
            raw = await g.ask(sc.SYSTEM, turns(s)[:-1] + [{'role': 'user', 'text': (
                f'{text}\n\n(Текущий рецепт после ручных правок администратора: {cur}. '
                'Ответь тем же JSON целиком: summary, recommendations, risks, questions, recipe.)')}])
        except sc.ScriptError:
            s['chat'].pop()
            raise
        store.save_script(merge_answer(s, raw))
        return J(script_view(s))

    @r.get('/api/scripts/{sid}')
    async def scripts_get(request):
        return J(script_view(script_or_404(request.match_info['sid'])))

    @r.put('/api/scripts/{sid}/recipe')
    async def scripts_recipe(request):
        s = script_or_404(request.match_info['sid'])
        rec = (await body(request)).get('recipe')
        if isinstance(rec, str):
            try:
                rec = json.loads(rec)
            except ValueError as e:
                raise ApiError(f'рецепт: неверный JSON ({e})') from None
        s['recipe'] = sc.clean_recipe(rec)
        s['status'] = 'draft'
        store.save_script(s)
        return J(script_view(s))

    @r.post('/api/scripts/{sid}/approve')
    async def scripts_approve(request):
        s = script_or_404(request.match_info['sid'])
        if not s.get('recipe'):
            raise ApiError('сначала получите рецепт')
        s['status'], s['approved_at'] = 'approved', time.time()
        store.save_script(s)
        return J(script_view(s))

    @r.delete('/api/scripts/{sid}')
    async def scripts_delete(request):
        s = script_or_404(request.match_info['sid'])
        used = [p['name'] for p in store.profiles() if p.get('script_id') == s['id']]
        if used:
            raise ApiError('Скрипт выбран в группах: ' + ', '.join(used))
        store.delete_script(s['id'])
        for ext in ('sh', 'readme'):
            (sdir / f'{s["id"]}.{ext}').unlink(missing_ok=True)
        return J({'ok': True})

    @r.post('/api/check/gemini')
    async def check_gemini(_):
        try:
            g = gemini()
            ms = await g.models()
            model = g.model or sc.Gemini.pick(ms)
            ok = model in ms
            return J({'ok': ok, 'message': (f'Ключ работает. Модель: {model}' if ok else f'Модели {model} нет среди доступных')
                      + f' (доступно моделей: {len(ms)})'})
        except sc.ScriptError as e:
            return J({'ok': False, 'message': str(e)})

    @r.get('/api/gemini/models')
    async def gemini_models(_):
        g = gemini()
        ms = await g.models()
        return J({'models': ms, 'recommended': sc.Gemini.pick(ms) if ms else ''})

    # ── задачи ──
    def job_view(j: dict, full: bool = False) -> dict:
        d = j['data']
        out = {'id': j['id'], 'domain': d['domain'], 'status': j['status'], 'stage': j['stage'], 'error': j['error'],
               'flag': d.get('flag', ''), 'loc_name': d.get('loc_name', ''), 'spec': d.get('spec', ''),
               'ip': d.get('ip'), 'created_at': j['created_at'], 'updated_at': j['updated_at'],
               'profile': (store.profile(j['profile_id']) or {}).get('name', ''), 'profile_id': j['profile_id']}
        if full:
            out['stages'] = [{'code': c, 'label': lbl} for c, lbl in stages_for(d)]
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
        return J({'ok': True, 'rebuy': not j['data'].get('server_id') and j['data'].get('kind') not in ('node', 'reip')})

    @r.post('/api/jobs/{jid}/hide')
    async def hide(request):
        j = job_or_404(request.match_info['jid'])
        if j['status'] in ACTIVE:
            raise ApiError('Задача ещё в работе')
        store.update_job(j['id'], hidden=1)
        return J({'ok': True})

    app.add_routes(r)
    return app
