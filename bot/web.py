"""Веб-админка (Telegram Mini App): страница /admin и JSON API /api/*.
Доступ — только из Telegram: подпись initData проверяется токеном бота. Администраторы (ADMIN_IDS) видят всё;
приглашённые пользователи — только свои группы, их задачи и мониторинг, без настроек бота (см. bot/access.py)."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import socket
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

from . import engine, forms, naming, notify, plugins, relay as rl, scripts as sc, services as svc, upgrade as up
from .pcache import CACHE as PC
from .access import INVITE_PREFIX, Access, uname
from .bcloud import BcloudError
from .runner import SERVICE_KINDS, Runner, bcloud_from, buyer_of, stages_for
from .store import ACTIVE, Store

HERE = Path(__file__).resolve().parent.parent / 'web'
STATIC = {'ui.css': 'text/css', 'ui.js': 'application/javascript',
          'geist.woff2': 'font/woff2', 'geist-mono.woff2': 'font/woff2',  # Geist (SIL OFL 1.1, web/static/geist-OFL.txt)
          'fx.js': 'application/javascript',
          'logo.png': 'image/png',
          'flags.woff': 'font/woff',  # флаги стран для Windows и Telegram Desktop (Twemoji, CC-BY 4.0, web/static/flags-LICENSE.txt)
          **{f'hero-{k}.webp': 'image/webp' for k in (f'{s}-{t}' for s in ('spring', 'summer', 'autumn', 'winter') for t in ('day', 'night'))}}
# картинка на главной: по сезону и времени суток (auto), одна из восьми или без картинки
THEME_HEROES = ('auto', 'none', *(f'{s}-{t}' for s in ('spring', 'summer', 'autumn', 'winter') for t in ('day', 'night')))
# оформление, как в bcloud: фон и сезонные эффекты (auto — по календарю)
THEME_BGS = ('none', 'aurora', 'night', 'ocean', 'sunset', 'forest', 'image')
THEME_EFFECTS = ('auto', 'none', 'snow', 'newyear', 'halloween', 'leaves', 'hearts', 'flowers', 'confetti')
IMG_TYPES = {b'\xff\xd8\xff': ('jpg', 'image/jpeg'), b'\x89PNG\r\n\x1a\n': ('png', 'image/png'), b'RIFF': ('webp', 'image/webp')}
BG_MAX = 4 * 1024 * 1024
GLASS_MAX = 80  # прозрачность списков, %: дальше текст на ярком фоне читается плохо


def validate_init_data(init_data: str, bot_token: str, max_age_s: int = 86400, now: float | None = None) -> dict | None:
    """Пользователь из initData или None, если подпись неверна или устарела (как в Bobr Cloud)."""
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

# только для администраторов: настройки и подключения бота, «Мои скрипты» (кроме списка утверждённых),
# пользователи и приглашения
ADMIN_ONLY = [
    (r'.*', r'/api/settings'),
    (r'.*', r'/api/check/(bcloud|cloudflare|gemini)'),
    (r'.*', r'/api/gemini/.*'),
    (r'POST|PUT|DELETE', r'/api/scripts(/.*)?'),
    (r'GET', r'/api/scripts/.+'),
    (r'.*', r'/api/(users|invites)(/.*)?'),
    (r'.*', r'/api/host'),
    (r'POST', r'/api/selfupdate'),
    (r'.*', r'/api/selfupdate/.+'),
    (r'.*', r'/api/migrate(/.*)?'),
    (r'GET', r'/api/reports'),
    (r'.*', r'/api/reports/\d+/resolve'),
]


def admin_only(method: str, path: str) -> bool:
    return any(re.fullmatch(m, method) and re.fullmatch(pt, path) for m, pt in ADMIN_ONLY)


def bc_ui(store, tg_id=None):
    """Клиент Bobr Cloud для ответов админке: недоступный Bobr Cloud ждём недолго, чтобы страница не висела."""
    b = bcloud_from(store, tg_id)
    b.RETRY_FOR = 8
    return b


def client_ip(request) -> str:
    # бот доступен только через свой Caddy или Caddy bcloud (порт не опубликован) — им можно верить
    xff = request.headers.get('X-Forwarded-For', '')
    return (xff.split(',')[0].strip() if xff else '') or (request.remote or '?')


def build_web(store: Store, runner: Runner, bot_token: str, admins: set[int], public_url: str = '',
              bot_username: str = '', security=None, data_dir: str | Path = 'data', monitor=None,
              access: Access | None = None, autopilot=None, hostload=None, mover=None, feedback=None) -> web.Application:
    media = Path(data_dir) / 'media'
    access = access or Access(store, admins)
    NT = notify.Notifier(store, None, access, admins)  # веб: только лента и настройки, без отправки
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
        role = access.role(int(user.get('id', 0))) if user else None
        if not role:
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
        request['user'], request['role'] = user, role
        if role == 'member':
            access.touch(user)
            if admin_only(request.method, request.path):
                return web.json_response({'detail': 'Это доступно только администратору бота.'}, status=403)
        if request.method != 'GET':  # что-то меняем в группе — её кэш панели больше не верен
            m = re.match(r'/api/profiles/(\d+)(/|$)', request.path)
            if m:
                PC.drop(int(m.group(1)))
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

    def is_admin(request) -> bool:
        return request.get('role') == 'admin'

    def uid_of(request) -> int:
        return int(request['user'].get('id', 0))

    def can(request, p: dict | None) -> bool:
        return bool(p) and (is_admin(request) or access.can(p, uid_of(request), uname(request['user'])))

    def my_profiles(request) -> list[dict]:
        return store.profiles() if is_admin(request) else access.profiles_for(uid_of(request), uname(request['user']))

    def profile_or_404(pid, request) -> dict:
        try:
            p = store.profile(int(pid))
        except (TypeError, ValueError):
            p = None
        if not p or not can(request, p):  # чужая группа для пользователя — как несуществующая
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
        cache = ('public, max-age=31536000, immutable' if name.endswith(('.woff2', '.woff'))  # шрифты не меняются
                 else 'public, max-age=604800' if name.endswith(('.webp', '.png')) else 'no-cache')
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

    # оформление у каждого своё (администраторы и пользователи); кто ещё не менял — общее, как было до разделения
    BG_NAME = re.compile(r'bg-\d+-[A-Za-z0-9]{12}\.(jpg|png|webp)')

    @r.get('/media/bg/{name}')
    async def media_bg_user(request):  # своя картинка фона пользователя; имя случайное, его не угадать
        name = request.match_info['name']
        f = media / name
        if not BG_NAME.fullmatch(name) or not f.exists():
            raise web.HTTPNotFound()
        ctype = next(t for e, t in IMG_TYPES.values() if f.suffix == '.' + e)
        return web.FileResponse(f, headers={'Content-Type': ctype, 'Cache-Control': 'public, max-age=31536000, immutable'})

    def theme_raw(uid: int) -> dict:
        try:
            own = json.loads(store.get(f'theme:{uid}') or 'null')
        except ValueError:
            own = None
        if isinstance(own, dict):
            return own
        return {'bg': store.get('theme_bg'), 'effect': store.get('theme_effect'), 'glass': store.get('theme_glass'),
                'image': store.get('theme_bg_image')}

    def theme(uid: int = 0) -> dict:
        t = theme_raw(uid)
        bg, effect = t.get('bg') or 'none', t.get('effect') or 'auto'
        try:
            glass = max(0, min(GLASS_MAX, int(t.get('glass') or 0)))
        except (TypeError, ValueError):
            glass = 0
        return {'bg': bg if bg in THEME_BGS else 'none', 'effect': effect if effect in THEME_EFFECTS else 'auto',
                'image': t.get('image') or '', 'glass': glass, 'hero': t.get('hero') if t.get('hero') in THEME_HEROES else 'auto'}

    def theme_put(uid: int, t: dict) -> dict:
        store.set(f'theme:{uid}', json.dumps(t))
        return theme(uid)

    def own_bg(image: str) -> Path | None:
        """Файл своей картинки пользователя (общая старая /media/bg — не его, её не удаляем)."""
        name = image.split('?')[0].rsplit('/', 1)[-1]
        return media / name if image.startswith('/media/bg/') and BG_NAME.fullmatch(name) else None

    @r.post('/api/theme')
    async def theme_save(request):
        uid = uid_of(request)
        data = await body(request)
        t = theme(uid)
        if 'bg' in data:
            if data['bg'] not in THEME_BGS or (data['bg'] == 'image' and not t['image']):
                raise ApiError('Такого фона нет')
            t['bg'] = data['bg']
        if 'effect' in data:
            if data['effect'] not in THEME_EFFECTS:
                raise ApiError('Такого эффекта нет')
            t['effect'] = data['effect']
        if 'hero' in data:
            if data['hero'] not in THEME_HEROES:
                raise ApiError('Такой картинки нет')
            t['hero'] = data['hero']
        if 'glass' in data:
            g = data['glass']
            if not isinstance(g, int) or isinstance(g, bool) or not 0 <= g <= GLASS_MAX:
                raise ApiError(f'Прозрачность — от 0 до {GLASS_MAX}%')
            t['glass'] = g
        return J(theme_put(uid, t))

    @r.put('/api/theme/background')
    async def theme_bg_upload(request):
        uid = uid_of(request)
        data = await request.content.read(BG_MAX + 1)
        if not data:
            raise ApiError('Файл пустой.')
        if len(data) > BG_MAX:
            raise ApiError('Картинка больше 4 МБ. Уменьшите её и загрузите снова.')
        kind = next((v for sig, v in IMG_TYPES.items() if data.startswith(sig) and (sig != b'RIFF' or data[8:12] == b'WEBP')), None)
        if not kind:
            raise ApiError('Подойдут JPG, PNG или WebP.')
        media.mkdir(parents=True, exist_ok=True)
        t = theme(uid)
        old = own_bg(t['image'])
        name = f'bg-{uid}-{secrets.token_hex(6)}.{kind[0]}'
        (media / name).write_bytes(data)
        if old:
            old.unlink(missing_ok=True)
        return J(theme_put(uid, {**t, 'bg': 'image', 'image': f'/media/bg/{name}'}))

    @r.delete('/api/theme/background')
    async def theme_bg_delete(request):
        uid = uid_of(request)
        t = theme(uid)
        old = own_bg(t['image'])
        if old:
            old.unlink(missing_ok=True)
        return J(theme_put(uid, {**t, 'image': '', 'bg': 'none' if t['bg'] == 'image' else t['bg']}))

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
                'created_by': p.get('created_by'), 'owner_tg_id': p.get('owner_tg_id'),
                'traffic_control': p.get('traffic_control', True), 'owner_label': p.get('owner_label', ''),
                'name_pattern': p.get('name_pattern') or naming.DEFAULT_PATTERN,
                'script': ((store.script(p['script_id']) or {}).get('recipe') or {}).get('title') if p.get('script_id') else '',
                'script_profile': ((((store.script(p['script_id']) or {}).get('recipe') or {}).get('profile') or {}).get('mode')
                                   if p.get('script_id') else None),
                'panel_profile_uuid': p.get('panel_profile_uuid') or '',
                'servers': sum(1 for j in mine if j['status'] == 'done' and j['data'].get('kind') not in SERVICE_KINDS),
                'active': sum(1 for j in mine if j['status'] in ACTIVE),
                'failed': sum(1 for j in mine if j['status'] == 'failed')}

    def issues(request=None) -> list[str]:
        from .handlers import ready_issues
        if request is None or is_admin(request):
            return ready_issues(store)
        return ready_issues(store, my_profiles(request), 'member')

    def group_kind(request, p: dict) -> str:
        """mine — группа пользователя (создал сам), shared — завёл администратор; у администратора: own / user."""
        if is_admin(request):
            return 'user' if p.get('created_by') else 'own'
        return 'mine' if str(p.get('created_by') or '') == str(uid_of(request)) else 'shared'

    def user_label(tg_id) -> str:
        u = store.user(int(tg_id)) if str(tg_id or '').isdigit() else None
        if not u:
            return str(tg_id or '')
        return '@' + u['username'] if u.get('username') else (u.get('first_name') or str(u['tg_id']))

    @r.get('/api/state')
    async def state(request):
        ps = my_profiles(request)
        ids = {p['id'] for p in ps}
        jobs = [j for j in store.jobs(1000) if j['profile_id'] in ids]
        admin = is_admin(request)
        return J({'settings': settings_view() if admin else {}, 'role': request['role'],
                  'profiles': [{**profile_summary(p, jobs), 'kind': group_kind(request, p),
                                'created_by_label': user_label(p['created_by']) if admin and p.get('created_by') else ''}
                               for p in ps],
                  'issues': issues(request), 'bot_username': bot_username,
                  'security': security.info() if security and admin else None, 'theme': theme(uid_of(request)),
                  'users': sum(1 for u in store.users() if not u.get('blocked')) if admin else None,
                  'host': ({'level': hostload.verdict()[0], 'cpu': hostload.now.get('cpu'), 'mem': hostload.now.get('mem')}
                           if admin and hostload else None),
                  'jobs': {'active': sum(1 for j in jobs if j['status'] in ACTIVE),
                           'done': sum(1 for j in jobs if j['status'] == 'done'),
                           'failed': sum(1 for j in jobs if j['status'] == 'failed')},
                  'notif_unread': NT.unread(uid_of(request)),
                  'tour_done': bool(store.get(f'tour_done:{uid_of(request)}')),
                  'reports_open': len(store.reports(status='open', limit=1000)) if admin else None,
                  'me': request['user'].get('first_name', ''), 'me_id': uid_of(request)})

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
        return J({'ok': True, 'settings': settings_view(), 'issues': issues(request)})

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
    def profile_view(p: dict, request=None) -> dict:
        out = {k: ('' if k in forms.SECRET_FIELDS else p.get(k, '')) for k in forms.PFIELDS}
        if request is not None:
            kind = group_kind(request, p)
            # пользователь в группе, которую завёл администратор, не меняет клиента bcloud и Cloudflare
            out.update({'kind': kind, 'locked': ['owner', 'cf_token'] if kind == 'shared' else (['owner'] if kind == 'mine' else []),
                        'can_delete': kind != 'shared',
                        'shared_cf': kind == 'shared' and not p.get('cf_token')})
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
        return J(profile_view(profile_or_404(request.match_info['pid'], request), request))

    @r.post('/api/profiles')
    async def create_profile(request):
        data = member_data(request, await body(request), None)
        prof = forms.clean_profile(data)
        if not is_admin(request):
            prof['owner'] = str(uid_of(request))
        prof = await with_script(await with_owner(prof), data)
        if not is_admin(request):
            prof['created_by'] = uid_of(request)
            if not prof.get('cf_token'):
                raise ApiError('Токен Cloudflare группы: укажите свой — домены нод создаются в вашем аккаунте Cloudflare')
        prof['id'] = store.save_profile(prof)
        return J({'id': prof['id'], 'check': await panel_check(prof)})

    @r.put('/api/profiles/{pid}')
    async def update_profile(request):
        cur = profile_or_404(request.match_info['pid'], request)
        data = member_data(request, await body(request), cur)
        prof = forms.clean_profile(data, cur)
        if not is_admin(request) and group_kind(request, cur) == 'mine':
            prof['owner'] = str(uid_of(request))
        prof = await with_script(await with_owner(prof, cur), data)
        if cur.get('created_by'):
            prof['created_by'] = cur['created_by']
            if not prof.get('cf_token'):
                raise ApiError('Токен Cloudflare группы: у группы пользователя нужен свой токен')
        prof['id'] = cur['id']
        store.save_profile(prof)
        return J({'id': prof['id'], 'check': await panel_check(prof)})

    def member_data(request, data: dict, cur: dict | None) -> dict:
        """Что пользователь может менять в группе: клиент Bobr Cloud — всегда он сам (покупки только с его баланса);
        в группе, которую завёл администратор, — без клиента и токена Cloudflare."""
        if is_admin(request):
            return data
        data = {**data}
        data.pop('owner', None)  # клиента bcloud ставим сами после проверки формы
        if cur is not None and str(cur.get('created_by') or '') != str(uid_of(request)):
            data.pop('cf_token', None)
        return data

    async def with_owner(prof: dict, cur: dict | None = None) -> dict:
        """Владелец группы в Bobr Cloud: по ID или @username находим клиента и запоминаем его ID и подпись."""
        owner = prof.get('owner') or ''
        if not owner:
            prof['owner_tg_id'], prof['owner_label'] = '', ''
            return prof
        if cur and owner == cur.get('owner') and cur.get('owner_tg_id'):
            return prof
        try:
            u = await bc_ui(store).resolve(owner)
        except BcloudError as e:
            raise ApiError(f'Клиент Bobr Cloud: {e}') from None
        if u.get('banned'):
            raise ApiError('Клиент Bobr Cloud заблокирован')
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
        p = profile_or_404(request.match_info['pid'], request)
        if group_kind(request, p) == 'shared':
            raise ApiError('Эту группу завёл администратор — удалить её может только он')
        if any(x['pid'] == p['id'] for x in store.relays()):
            raise ApiError('У группы есть входы через Россию — сначала уберите их на страницах нод')
        if any(j['profile_id'] == p['id'] for j in store.active_jobs()):
            raise ApiError('У группы есть задачи в работе — дождитесь их')
        store.delete_profile(p['id'])
        for key in store.mon_all(f'{p["id"]}:'):
            store.set_mon(key, None)
        if p.get('mon_sub') and monitor is not None:  # подписку группы — убрать с чекеров
            monitor.resync()
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
        return J(await panel_check(profile_or_404(request.match_info['pid'], request)))

    @r.get('/api/profiles/{pid}/panel')
    async def panel_meta(request):
        """Сквады и Xray-шаблоны панели группы — для выбора при установке."""
        p = profile_or_404(request.match_info['pid'], request)
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
            out['xhttp'] = {'port': engine.XHTTP_PORT, 'mode': engine.xhttp_mode(xh.get('mode'))}
        return out

    # ── ноды панели: дополнения на уже работающих нодах ──
    def panel_of(p: dict) -> engine.Panel:
        return engine.Panel(p['panel_url'], p['panel_token'], p.get('panel_cookie', ''))

    def bot_jobs_by_ip(pid: int) -> dict[str, dict]:
        return svc.bot_jobs_by_ip(store, pid)

    def ssh_view(pid: int, uuid: str, ip: str) -> dict:
        from . import sshaccess
        saved = runner.ssh_of(pid, uuid)
        if saved:
            return {**sshaccess.view(saved), 'source': 'saved', 'summary': sshaccess.describe(saved)}
        if bot_jobs_by_ip(pid).get(ip):  # нода куплена ботом: root-пароль известен из задачи покупки
            return {**sshaccess.view({'user': 'root', 'password': 'x'}), 'source': 'bot', 'summary': 'root, пароль из покупки'}
        return {**sshaccess.view({}), 'source': '', 'summary': 'не указан'}

    def node_ssh(pid: int, uuid: str, ip: str) -> dict:
        return svc.node_ssh(store, runner, pid, uuid, ip)

    async def cached(p: dict, *what: str, stale_ok: bool = True) -> tuple[list, bool]:
        """Данные панели из кэша (см. pcache): [данные по каждому what], устарело ли хоть что-то."""
        try:
            res = await asyncio.gather(*(PC.get(p, w, stale_ok) for w in what))
        except engine.StepError as e:
            raise ApiError(f'Панель: {e}') from None
        return [d for d, _ in res], any(st for _, st in res)

    @r.get('/api/profiles/{pid}/nodes')
    async def panel_nodes(request):
        """Список нод группы — из кэша, если он свежий; устаревший тоже отдаём сразу (stale: true), а свежий
        тем временем уже запрошен — веб-апп перезапросит с fresh=1. Повторные открытия не ходят в панель."""
        p = profile_or_404(request.match_info['pid'], request)
        return J(await panel_nodes_load(p, stale_ok=not request.query.get('fresh')))

    async def panel_nodes_load(p: dict, stale_ok: bool = True) -> dict:
        ((nodes, profiles), stale), versions = await asyncio.gather(
            cached(p, 'nodes', 'profiles', stale_ok=stale_ok), node_versions(p))
        profiles = {x['uuid']: x for x in profiles['configProfiles']}
        bot = bot_jobs_by_ip(p['id'])
        busy = {j['data'].get('node_uuid') for j in store.active_jobs() if j['profile_id'] == p['id']}
        out = []
        for n in nodes:
            pr = profiles.get((n.get('configProfile') or {}).get('activeConfigProfileUuid')) or {}
            out.append({'version': up.node_version(n), 'xray': up.xray_version(n),
                        'load': autopilot.load_of(p['id'], n['uuid']) if autopilot else {},
                        'has_ssh': bool(runner.ssh_of(p['id'], n['uuid']) or n['address'] in bot), 'busy': n['uuid'] in busy,
                        'uuid': n['uuid'], 'name': n['name'], 'address': n['address'], 'connected': bool(n.get('isConnected')),
                        'disabled': bool(n.get('isDisabled')), 'country': n.get('countryCode') or '',
                        'online': n.get('usersOnline'),
                        'profile': pr.get('name', ''), 'features': sorted(engine.features_of(pr.get('config'))),
                        'bot': n['address'] in bot, 'domain': (bot.get(n['address']) or {}).get('domain', '')})
        return {'nodes': out, 'bot_key': runner.bot_key()[1], 'versions': versions, 'stale': stale}

    async def node_versions(p: dict) -> dict:
        """Версия панели и последняя подходящая к ней версия ноды (Docker Hub) — цель обновления по умолчанию."""
        tags, pv = await asyncio.gather(asyncio.to_thread(up.hub_tags),
                                        asyncio.to_thread(lambda: up.panel_version(panel_of(p))))
        return {'panel': pv, 'latest': tags[0] if tags else '', 'target': up.pick_target(tags, pv)}

    @r.post('/api/profiles/{pid}/nodes/upgrade')
    async def nodes_upgrade(request):
        """Обновить ноды (выбранные или все) до версии tag: задача на каждую, с проверкой и откатом."""
        p = profile_or_404(request.match_info['pid'], request)
        b = await body(request)
        try:
            tag = up.check_tag(b.get('tag') or 'latest')
        except ValueError as e:
            raise ApiError(str(e)) from None
        try:
            nodes = await asyncio.to_thread(lambda: panel_of(p).call('GET', '/api/nodes'))
        except engine.StepError as e:
            raise ApiError(f'Панель: {e}') from None
        want = None if b.get('all') else {u for u in (b.get('uuids') or []) if isinstance(u, str)}
        if want is not None and not want:
            raise ApiError('Выберите ноды')
        busy = {j['data'].get('node_uuid') for j in store.active_jobs() if j['profile_id'] == p['id']}
        ids, skipped = [], []
        for n in nodes:
            if want is not None and n['uuid'] not in want:
                continue
            if n.get('isDisabled'):
                skipped.append({'name': n['name'], 'why': 'выключена в панели'})
                continue
            if n['uuid'] in busy:
                skipped.append({'name': n['name'], 'why': 'по ноде уже идёт задача'})
                continue
            try:
                ids.append(await svc.start_upgrade(store, runner, p, n, tag, uid_of(request)))
            except ValueError as e:
                skipped.append({'name': n['name'], 'why': str(e)})
        return J({'ids': ids, 'skipped': skipped})

    @r.get('/api/profiles/{pid}/nodes/{uuid}')
    async def panel_node(request):
        p = profile_or_404(request.match_info['pid'], request)
        uuid = request.match_info['uuid']

        pl = panel_of(p)
        # ноды, профили и хосты — из кэша (одновременно, если надо идти в панель)
        (nodes, profiles, all_hosts), stale = await cached(p, 'nodes', 'profiles', 'hosts',
                                                           stale_ok=not request.query.get('fresh'))
        n = next((x for x in nodes if x['uuid'] == uuid), None)
        if not n and stale:  # нода могла появиться недавно — кэш старый, смотрим в панели
            (nodes, profiles, all_hosts), stale = await cached(p, 'nodes', 'profiles', 'hosts', stale_ok=False)
            n = next((x for x in nodes if x['uuid'] == uuid), None)
        if not n:
            raise ApiError('Ноды нет в панели', 404)
        pr = next((x for x in profiles['configProfiles']
                   if x['uuid'] == (n.get('configProfile') or {}).get('activeConfigProfileUuid')), {}) or {}
        bd = bot_jobs_by_ip(p['id']).get(n['address']) or {}

        def own_hosts():
            ip = bd.get('ip') or (n['address'] if engine.is_ip(n['address']) else resolve(n['address']))
            dom = bd.get('domain') or ('' if engine.is_ip(n['address']) else n['address'])
            return pl.node_hosts(n, {dom} if dom else set(), {ip} if ip else set(), all_hosts), ip, dom

        async def bcloud_info():
            if not bd.get('server_id'):
                return None
            bc = {'server_id': bd['server_id'], 'price_kop': None}
            try:
                srv = await bc_ui(store, bd.get('buyer') or buyer_of(store, p)).server(int(bd['server_id']))
                bc['price_kop'] = srv.get('change_ip_price_kop')
            except BcloudError as e:
                bc['error'] = str(e)
            return bc
        try:  # поиск хостов ноды (DNS) и Bobr Cloud — тоже параллельно
            (hosts, ip, dom), bc = await asyncio.gather(asyncio.to_thread(own_hosts), bcloud_info())
        except engine.StepError as e:
            raise ApiError(f'Панель: {e}') from None
        return J({'uuid': n['uuid'], 'name': n['name'], 'address': n['address'], 'connected': bool(n.get('isConnected')),
                  'profile': pr.get('name', ''), 'features': sorted(engine.features_of(pr.get('config'))),
                  'inbound_port': next((i.get('port') for i in pr.get('inbounds', [])
                                        if i['uuid'] in engine.active_inbounds(n)), None),
                  'country': n.get('countryCode') or '', 'online': n.get('usersOnline'),
                  'hosts': [{'uuid': h['uuid'], 'remark': h.get('remark', ''), 'address': h.get('address', ''),
                             'port': h.get('port'), 'final_mask': bool(h.get('finalMask')), 'hidden': bool(h.get('isHidden'))}
                            for h in hosts],
                  'ip': ip, 'domain': dom, 'address_is_ip': engine.is_ip(n['address']), 'bcloud': bc,
                  'ssh': ssh_view(p['id'], uuid, n['address']), 'bot_key': runner.bot_key()[1],
                  'relays': [relay_view(x) for x in store.relays() if x['pid'] == p['id'] and x['node_uuid'] == uuid],
                  'version': up.node_version(n), 'xray': up.xray_version(n),
                  'relay_port': rl.target_port_of(hosts),
                  'load': autopilot.load_of(p['id'], uuid, hist=True) if autopilot else {},
                  'load_on': load_watched(p), 'disabled': bool(n.get('isDisabled')),
                  'busy': any(j['data'].get('node_uuid') == uuid for j in store.active_jobs() if j['profile_id'] == p['id']),
                  'stale': stale})

    def load_watched(p: dict) -> bool:
        from . import autopilot as ap
        return ap.auto_cfg(p)['load']

    @r.delete('/api/profiles/{pid}/nodes/{uuid}')
    async def panel_node_delete(request):
        """Убрать ноду из панели: саму ноду, её хосты и A-запись домена. Сервер в Bobr Cloud не трогаем."""
        p = profile_or_404(request.match_info['pid'], request)
        uuid = request.match_info['uuid']
        if any(j['data'].get('node_uuid') == uuid for j in store.active_jobs() if j['profile_id'] == p['id']):
            raise ApiError('По ноде идёт задача — дождитесь её окончания')
        if any(x['pid'] == p['id'] and x['node_uuid'] == uuid for x in store.relays()):
            raise ApiError('У ноды есть входы через Россию — сначала уберите их, чтобы бот снял проброс с российского сервера')
        lines: list[str] = []

        def run():
            _pl, n, _hosts, ip, dom = node_ctx(p, uuid)
            return n, engine.remove_node(p, ip or n['address'], dom, lines.append)
        try:
            n, removed = await asyncio.to_thread(run)
        except engine.StepError as e:
            raise ApiError(f'Панель: {e}') from None
        logging.getLogger(__name__).info('node %s removed from panel of profile %s by %s: %s', uuid, p['id'], uid_of(request), removed)
        return J({'ok': True, 'removed': removed, 'log': lines,
                  'message': f'Нода «{n["name"]}» убрана: хостов {removed["hosts"]}, DNS-записей {removed["dns"]}'})

    # ── вход через Россию: проброс со своего сервера в России на эту ноду ──
    relay_locks: dict[str, asyncio.Lock] = {}

    def relay_view(x: dict) -> dict:
        from . import sshaccess
        return {'id': x['id'], 'ip': x['ip'], 'address': x.get('address') or x['ip'], 'listen': x['listen'],
                'target': x['target'], 'target_port': x['target_port'], 'remark': x.get('remark', ''),
                'status': x.get('status', 'new'), 'error': x.get('error', ''), 'bad': x.get('bad') or [],
                'hosts': len(x.get('hosts') or []), 'ssh': sshaccess.view(x.get('ssh')),
                'access': sshaccess.describe(x.get('ssh')), 'log': x.get('log') or []}

    def relay_or_404(rid, request) -> dict:
        try:
            x = store.relay(int(rid))
        except (TypeError, ValueError):
            x = None
        if not x or not can(request, store.profile(x['pid'])):
            raise ApiError('Вход не найден', 404)
        return x

    def relay_remote(acc: dict) -> engine.Remote:
        from . import sshaccess
        return engine.Remote({'ip': acc['ip'], **sshaccess.to_node(acc.get('ssh'), runner.bot_key()[0]),
                              'known_hosts': runner.known_hosts()}, None)

    def node_ctx(p: dict, uuid: str):
        pl = panel_of(p)
        n = next((x for x in pl.call('GET', '/api/nodes') if x['uuid'] == uuid), None)
        if not n:
            raise ApiError('Ноды нет в панели', 404)
        bd = bot_jobs_by_ip(p['id']).get(n['address']) or {}
        ip = bd.get('ip') or (n['address'] if engine.is_ip(n['address']) else resolve(n['address']))
        dom = bd.get('domain') or ('' if engine.is_ip(n['address']) else n['address'])
        return pl, n, pl.node_hosts(n, {dom} if dom else set(), {ip} if ip else set()), ip, dom

    async def relay_sync(ip: str, log, closed: list[int] = ()) -> dict:
        """Конфиг HAProxy на сервере — по всем его входам; входов нет — проброс с сервера убираем."""
        mine = [x for x in store.relays() if x['ip'] == ip]
        if not mine:
            return {'bad': []}
        return await asyncio.to_thread(rl.apply, relay_remote(mine[-1]), mine, log, closed)

    async def relay_hosts(x: dict, p: dict, log) -> list[str]:
        def run():
            pl, n, hosts, _ip, _dom = node_ctx(p, x['node_uuid'])
            return rl.clone_hosts(pl, hosts, x.get('address') or x['ip'], x['listen'], x['target_port'], x['remark'], log)
        return await asyncio.to_thread(run)

    def check_listen(v, ssh_port: int) -> int:
        try:
            port = int(v or 443)
        except (TypeError, ValueError):
            raise ApiError('Порт входа: число от 1 до 65535') from None
        if not 1 <= port <= 65535:
            raise ApiError('Порт входа: число от 1 до 65535')
        if port == int(ssh_port or 22):
            raise ApiError('Этот порт занят SSH сервера — выберите другой')
        return port

    @r.post('/api/profiles/{pid}/nodes/{uuid}/relays')
    async def relay_create(request):
        """Новый вход через Россию: HAProxy на российском сервере пробрасывает порт на ноду, в панели — копии её хостов."""
        from . import sshaccess
        p = profile_or_404(request.match_info['pid'], request)
        uuid, b = request.match_info['uuid'], await body(request)
        ip = str(b.get('ip') or '').strip()
        if not engine.is_ip(ip) or ':' in ip:
            raise ApiError('IP сервера в России: адрес IPv4, например 203.0.113.10')
        others = [x for x in store.relays() if x['ip'] == ip]
        if any(not can(request, store.profile(x['pid'])) for x in others):
            raise ApiError('Этот сервер уже используют для входа в другой группе')
        try:
            acc = sshaccess.clean(b, others[-1].get('ssh') if others else None, runner.keydir)
        except ValueError as e:
            raise ApiError(str(e)) from None
        listen = check_listen(b.get('listen'), acc.get('port'))
        if any(int(x['listen']) == listen for x in others):
            raise ApiError(f'Порт {listen} на этом сервере уже занят другим входом — выберите другой')
        address = naming.norm_domain(str(b.get('address') or '')) or ip
        if address != ip and not (engine.is_ip(address) or naming.valid_domain(address)):
            raise ApiError('Адрес для клиентов: IP или домен российского сервера')
        try:
            pl, n, hosts, nip, dom = await asyncio.to_thread(node_ctx, p, uuid)
        except engine.StepError as e:
            raise ApiError(f'Панель: {e}') from None
        target = dom or n['address'] or nip
        if not target:
            raise ApiError('Не знаю адрес ноды')
        try:
            tport = int(b.get('target_port') or rl.target_port_of(hosts))
        except (TypeError, ValueError):
            raise ApiError('Порт ноды: число') from None
        remark = str(b.get('remark') or '').strip()[:90] or f'🇷🇺 {n["name"]}'
        x = {'pid': p['id'], 'node_uuid': uuid, 'node_name': n['name'], 'ip': ip, 'address': address, 'listen': listen,
             'target': target, 'target_port': tport, 'remark': remark, 'ssh': acc, 'status': 'new', 'hosts': [],
             'created_by': uid_of(request), 'created_at': int(time.time())}
        x['id'] = store.save_relay(x)
        return J(await relay_run(x, p, ip))

    async def relay_run(x: dict, p: dict, ip: str, closed: list[int] = ()) -> dict:
        """Применить проброс на сервере и, если хостов ещё нет, создать их. Итог — в записи входа."""
        lines: list[str] = []
        lock = relay_locks.setdefault(ip, asyncio.Lock())
        async with lock:
            for o in store.relays():  # доступ по SSH — общий у всех входов одного сервера
                if o['ip'] == ip and o['id'] != x['id'] and o.get('ssh') != x['ssh']:
                    store.save_relay({**o, 'ssh': x['ssh']})
            store.save_relay({**x, 'status': 'installing', 'error': ''})
            try:
                res = await relay_sync(ip, lines.append, closed)
                if not x.get('hosts'):
                    x['hosts'] = await relay_hosts(x, p, lines.append)
            except (engine.StepError, ApiError) as e:
                x.update(status='failed', error=str(e)[:300], log=lines[-20:])
                store.save_relay(x)
                return {'id': x['id'], 'ok': False, 'message': str(e), 'log': lines}
            x.update(status='ok' if not res['bad'] else 'warn', error='', bad=res['bad'], log=lines[-20:])
            store.save_relay(x)
        msg = (f'Готово: клиенты подключаются к {x.get("address") or ip}:{x["listen"]}, трафик идёт на {x["target"]}:{x["target_port"]}'
               if not res['bad'] else f'Проброс стоит, но с российского сервера не открывается {", ".join(res["bad"])} — проверьте ноду')
        return {'id': x['id'], 'ok': not res['bad'], 'message': msg, 'log': lines}

    @r.post('/api/relays/{rid}/apply')
    async def relay_apply(request):
        """Повторить установку (можно с новым доступом по SSH)."""
        from . import sshaccess
        x = relay_or_404(request.match_info['rid'], request)
        b = await body(request)
        if any(str(b.get(k) or '').strip() for k in ('user', 'port', 'password', 'key', 'key_text', 'sudo_password')):
            try:
                x['ssh'] = sshaccess.clean(b, x.get('ssh'), runner.keydir)
            except ValueError as e:
                raise ApiError(str(e)) from None
        return J(await relay_run(x, store.profile(x['pid']), x['ip']))

    @r.post('/api/relays/{rid}/forget-hostkey')
    async def relay_forget(request):
        x = relay_or_404(request.match_info['rid'], request)
        engine.forget_host(runner.known_hosts(), x['ip'], int((x.get('ssh') or {}).get('port') or 22))
        return J({'ok': True, 'message': 'Ключ забыт — при следующем входе бот запомнит новый'})

    @r.delete('/api/relays/{rid}')
    async def relay_delete(request):
        """Убрать вход: с сервера (остальные входы сервера остаются) и его хосты из панели.
        force — если до сервера не достучаться, убрать хотя бы из бота и панели."""
        x = relay_or_404(request.match_info['rid'], request)
        force = request.query.get('force') == '1'
        p = store.profile(x['pid'])
        lines: list[str] = []
        lock = relay_locks.setdefault(x['ip'], asyncio.Lock())
        async with lock:
            store.delete_relay(x['id'])
            rest = [o for o in store.relays() if o['ip'] == x['ip']]
            try:
                if rest:
                    await relay_sync(x['ip'], lines.append, closed=[] if any(o['listen'] == x['listen'] for o in rest) else [x['listen']])
                else:
                    await asyncio.to_thread(rl.remove, relay_remote(x), [x['listen']], lines.append)
            except engine.StepError as e:
                if not force:
                    store.save_relay(x)  # вернуть: на сервере проброс ещё стоит
                    return J({'ok': False, 'ssh_error': True, 'message': f'Сервер: {e}', 'detail': f'Сервер: {e}'}, status=400)
                lines.append(f'сервер: {e} — убираю только из бота и панели')
            if p and x.get('hosts'):
                try:
                    await asyncio.to_thread(rl.delete_hosts, panel_of(p), x['hosts'], lines.append)
                except engine.StepError as e:
                    lines.append(f'панель: {e}')
        return J({'ok': True, 'message': 'Вход убран', 'log': lines})

    @r.post('/api/profiles/{pid}/nodes/{uuid}/ssh')
    async def panel_node_ssh(request):
        """Сохранить доступ по SSH (пароль, свой ключ или ключ бота, можно вместе; не-root — через sudo) и проверить вход."""
        from . import sshaccess
        p = profile_or_404(request.match_info['pid'], request)
        uuid, b = request.match_info['uuid'], await body(request)
        try:
            new = sshaccess.clean(b, runner.ssh_of(p['id'], uuid), runner.keydir)
        except ValueError as e:
            raise ApiError(str(e)) from None
        ip = str(b.get('ip') or '')
        result = {'ok': True, 'message': 'Сохранено'}
        if ip:
            node = {'ip': ip, **sshaccess.to_node(new, runner.bot_key()[0]), 'known_hosts': runner.known_hosts()}

            def check():
                rr = engine.Remote(node, None)
                rc, out = rr.run('id -u', 25)
                return rc == 0 and out.strip().splitlines()[-1:] == ['0'], out
            try:
                good, out = await asyncio.to_thread(check)
                result = {'ok': good, 'message': ('Вход по SSH работает, права root ' + ('через sudo' if new['user'] != 'root' else 'есть'))
                          if good else 'Вход есть, но прав root нет'}
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
        """Перепривязка IP: mode bcloud — сменить в Bobr Cloud (платно, с баланса владельца), sync — взять уже сменённый
        из Bobr Cloud, manual — новый IP вручную. Дальше задача: Cloudflare, SSH, адрес в панели."""
        p = profile_or_404(request.match_info['pid'], request)
        uuid, b = request.match_info['uuid'], await body(request)
        mode = b.get('mode')
        if mode not in ('bcloud', 'sync', 'manual'):
            raise ApiError('Неизвестный способ')
        nodes = await asyncio.to_thread(lambda: panel_of(p).call('GET', '/api/nodes'))
        n = next((x for x in nodes if x['uuid'] == uuid), None)
        if not n and stale:  # нода могла появиться недавно — кэш старый, смотрим в панели
            (nodes, profiles, all_hosts), stale = await cached(p, 'nodes', 'profiles', 'hosts', stale_ok=False)
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
        p = profile_or_404(request.match_info['pid'], request)
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
        p = profile_or_404(request.match_info['pid'], request)
        ip = str((await body(request)).get('ip') or '')
        if not re.fullmatch(r'[0-9a-fA-F:.]{3,45}', ip):
            raise ApiError('Неверный адрес')
        if not is_admin(request):  # пользователь забывает ключи только серверов своей группы
            uuid = request.match_info['uuid']
            nodes = await asyncio.to_thread(lambda: panel_of(p).call('GET', '/api/nodes'))
            n = next((x for x in nodes if x['uuid'] == uuid), None)
            known = {(bot_jobs_by_ip(p['id']).get(n['address']) or {}).get('ip'), n['address']} if n else set()
            if n and not engine.is_ip(n['address']):
                known.add(await asyncio.to_thread(resolve, n['address']))
            if ip not in known:
                raise ApiError('Это не адрес этой ноды')
        engine.forget_host(runner.known_hosts(), ip)
        return J({'ok': True, 'message': 'Ключ забыт — при следующем входе бот запомнит новый'})

    @r.post('/api/profiles/{pid}/nodes/{uuid}/rollback')
    async def panel_node_rollback(request):
        """Убрать дополнения: нода и её хосты — обратно в исходный профиль (Psiphon/WARP на ноде остаются, но не нужны)."""
        p = profile_or_404(request.match_info['pid'], request)
        uuid = request.match_info['uuid']
        nodes = await asyncio.to_thread(lambda: panel_of(p).call('GET', '/api/nodes'))
        n = next((x for x in nodes if x['uuid'] == uuid), None)
        if not n and stale:  # нода могла появиться недавно — кэш старый, смотрим в панели
            (nodes, profiles, all_hosts), stale = await cached(p, 'nodes', 'profiles', 'hosts', stale_ok=False)
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

    def parse_apply(p: dict, b: dict) -> tuple[dict, object, object]:
        """Дополнения, порт и Final Mask из запроса; ApiError — если что-то не так или менять нечего."""
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
        return extras, port, fm

    async def node_apply(p: dict, n: dict, parsed, uid: int) -> int:
        """Задача «дополнения на ноде» для ноды n из панели; ApiError — почему нельзя."""
        extras, port, fm = parsed
        ssh = node_ssh(p['id'], n['uuid'], n['address'])
        if (extras or port != '') and not ssh:
            raise ApiError('нет доступа по SSH — без него Psiphon, WARP, XHTTP и порт не поставить')
        bd = bot_jobs_by_ip(p['id']).get(n['address']) or {}
        data = {'kind': 'node', 'node_uuid': n['uuid'], 'name': n['name'], 'ip': bd.get('ip') or n['address'],
                'domain': bd.get('domain') or n['name'], 'domains': [bd['domain']] if bd.get('domain') else [],
                'flag': '🛠', 'loc_name': n['name'], 'spec': 'дополнения на ноде', 'ssh': ssh,
                'psiphon': extras.get('psiphon'), 'warp': extras.get('warp'), 'xhttp': extras.get('xhttp'),
                'port': port, 'final_mask': fm}
        jid = store.add_job('node-' + n['uuid'][:8], p['id'], uid, data)
        await runner.render(jid, force=True)
        runner.start(jid)
        return jid

    @r.post('/api/profiles/{pid}/nodes/{uuid}/apply')
    async def panel_node_apply(request):
        """Задача «дополнения на ноде»: Psiphon/WARP (с переводом ноды в вариант профиля), порт, Final Mask."""
        p = profile_or_404(request.match_info['pid'], request)
        uuid, b = request.match_info['uuid'], await body(request)
        parsed = parse_apply(p, b)
        nodes = await asyncio.to_thread(lambda: panel_of(p).call('GET', '/api/nodes'))
        n = next((x for x in nodes if x['uuid'] == uuid), None)
        if not n and stale:  # нода могла появиться недавно — кэш старый, смотрим в панели
            (nodes, profiles, all_hosts), stale = await cached(p, 'nodes', 'profiles', 'hosts', stale_ok=False)
            n = next((x for x in nodes if x['uuid'] == uuid), None)
        if not n:
            raise ApiError('Ноды нет в панели', 404)
        try:
            return J({'id': await node_apply(p, n, parsed, uid_of(request))})
        except ApiError as e:
            raise ApiError(str(e)[:1].upper() + str(e)[1:]) from None

    @r.post('/api/profiles/{pid}/nodes/apply-many')
    async def panel_nodes_apply(request):
        """То же сразу на нескольких нодах: задача на каждую; кого нельзя — в skipped с причиной."""
        p = profile_or_404(request.match_info['pid'], request)
        b = await body(request)
        want = {u for u in (b.get('uuids') or []) if isinstance(u, str)}
        if not want:
            raise ApiError('Выберите ноды')
        parsed = parse_apply(p, b)
        try:
            nodes = await asyncio.to_thread(lambda: panel_of(p).call('GET', '/api/nodes'))
        except engine.StepError as e:
            raise ApiError(f'Панель: {e}') from None
        busy = {j['data'].get('node_uuid') for j in store.active_jobs() if j['profile_id'] == p['id']}
        ids, skipped = [], []
        for n in nodes:
            if n['uuid'] not in want:
                continue
            if n['uuid'] in busy:
                skipped.append({'name': n['name'], 'why': 'по ноде уже идёт задача'})
                continue
            try:
                ids.append(await node_apply(p, n, parsed, uid_of(request)))
            except ApiError as e:
                skipped.append({'name': n['name'], 'why': str(e)})
        return J({'ids': ids, 'skipped': skipped})

    # ── перенос хостов между панелями ──
    @r.post('/api/hosts/export')
    async def hosts_export(request):
        """Набор хостов (с профилями и Xray JSON-шаблонами) из панели группы pid."""
        b = await body(request)
        p = profile_or_404(b.get('pid', 0), request)
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
        p = profile_or_404(b.get('pid', 0), request)
        squads = [x for x in (b.get('squads') or []) if isinstance(x, str)]
        mode = 'update' if b.get('template_mode') == 'update' else 'copy'
        if any(x['pid'] == p['id'] and x['node_uuid'] == uuid for x in store.relays()):
            raise ApiError('У ноды есть входы через Россию — сначала уберите их, чтобы бот снял проброс с российского сервера')
        lines: list[str] = []

        def run():
            if b.get('from_pid'):
                src = profile_or_404(b['from_pid'], request)
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
        p = profile_or_404(request.query.get('pid', 0), request)
        cat = await svc.load_catalog(store, p)
        try:
            bal = await bc_ui(store, buyer_of(store, p)).balance()
        except BcloudError:
            bal = None
        return J({'locations': cat['locations'], 'periods': cat['periods'], 'os_label': cat['os_label'], 'balance_kop': bal,
                  'buyer': p.get('owner_label') or 'ваш аккаунт', 'configurator': cat.get('configurator')})

    @r.get('/api/quote')
    async def custom_quote(request):
        """Цена своей конфигурации (конструктор) для покупателя группы."""
        q = request.query
        p = profile_or_404(q.get('pid', 0), request)
        cat = await svc.load_catalog(store, p)
        try:
            o = await svc.custom_offer(store, p, cat, str(q.get('location', '')),
                                       {k: q.get(k) for k in ('cpu', 'ram', 'disk')})
        except ValueError as e:
            raise ApiError(str(e)) from None
        return J(o)

    @r.get('/api/profiles/{pid}/servers')
    async def own_servers(request):
        """Уже купленные серверы покупателя группы в Bobr Cloud — для установки нод без новой покупки."""
        p = profile_or_404(request.match_info['pid'], request)
        servers = [{k: v for k, v in x.items() if k != 'root_password'} for x in await svc.own_servers(store, p)]
        return J({'servers': servers, 'buyer': p.get('owner_label') or 'ваш аккаунт'})

    @r.get('/api/profiles/{pid}/zones')
    async def zones(request):
        """Домены Cloudflare для выбора: зоны аккаунта (токен группы или общий) + домен группы для автоимён."""
        p = profile_or_404(request.match_info['pid'], request)
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
        p = profile_or_404(b.get('pid', 0), request)
        n = int(b.get('count') or 0)
        if not 1 <= n <= 50:
            raise ApiError('Количество: от 1 до 50')
        exclude = {naming.norm_domain(x) for x in (b.get('exclude') or []) if isinstance(x, str)}
        return J({'domains': await svc.auto_domains(store, p, str(b.get('location', '')), n, b.get('base') or None, exclude)})

    @r.post('/api/domains/check')
    async def domains_check(request):
        b = await body(request)
        p = profile_or_404(b.get('pid', 0), request)
        return J({'domains': await svc.check_domains(store, p, b.get('domains') or '', int(b.get('count') or 0))})

    @r.post('/api/deploy')
    async def deploy(request):
        """Одна или несколько локаций: items = [{location, idx, domains}], либо старый вид location/idx/domains."""
        b = await body(request)
        p = profile_or_404(b.get('pid', 0), request)
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
        plan, total, pos = [], 0, 0  # plan: (локация, idx, домены, своя конфигурация, свои серверы)
        own, taken = None, set()
        for it in items:
            n = len(it.get('domains') or [])
            if not n:
                continue
            loc = str(it.get('location') or '')
            doms = checked[pos:pos + n]
            pos += n
            if it.get('servers') is not None:  # уже купленные серверы: покупки нет
                if own is None:
                    own = {x['id']: x for x in await svc.own_servers(store, p)}
                ids = [int(x) for x in it['servers'] if str(x).isdigit()]
                if len(ids) != n or len(set(ids)) != n:
                    raise ApiError(f'Серверов выбрано {len(ids)}, доменов {n} — нужно поровну')
                for sid in ids:
                    x = own.get(sid)
                    if not x or sid in taken:
                        raise ApiError(f'Сервера #{sid} нет в Bobr Cloud у покупателя группы')
                    if not x['ready']:
                        raise ApiError(f'Сервер {x["name"] or x["ip"]}: {x["why"]}')
                    if x['location'] != loc:
                        raise ApiError(f'Сервер {x["name"] or x["ip"]} — в другой локации')
                    taken.add(sid)
                plan.append((loc, None, doms, None, [own[sid] for sid in ids]))
                continue
            custom = None
            try:
                if it.get('spec') is not None:
                    custom = await svc.custom_offer(store, p, cat, loc, it['spec'])
                    q = svc.quote(cat, loc, None, period, n, custom)
                    idx = None
                else:
                    idx = int(it.get('idx'))
                    q = svc.quote(cat, loc, idx, period, n)
            except (TypeError, ValueError) as e:
                raise ApiError(str(e) or 'не выбрана конфигурация') from None
            plan.append((loc, idx, doms, custom, None))
            total += q['total_kop']
        q = {'total_kop': total}
        bal = await bc_ui(store, buyer_of(store, p)).balance() if total else 0
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
                 for loc, *_ in plan]
        places = {}
        order = b.get('host_order')
        if isinstance(order, list) and order:
            try:  # уже существующие хосты — чтобы не ставить «после» несуществующего
                meta = await asyncio.to_thread(lambda: engine.Panel(p['panel_url'], p['panel_token'], p.get('panel_cookie', '')).meta())
            except engine.StepError as e:
                raise ApiError(f'Панель: {e} — не получилось проверить порядок хостов') from None
            doms_of: dict[str, list[str]] = {}
            for loc, _, doms, *_ in plan:
                doms_of.setdefault(loc, []).extend(doms)
            places = svc.host_places(order, doms_of, {x['uuid'] for x in meta['hosts']})
        for host in hosts:
            host.update({'final_mask': mask, 'port': port})
        ids = []
        for (loc, idx, doms, custom, servers), host in zip(plan, hosts):
            ids += await svc.create_jobs(store, runner, int(request['user']['id']), p['id'], cat, loc, idx, period, doms,
                                         squads, str(b.get('remark') or '')[:100], host,
                                         'en' if b.get('remark_lang') == 'en' else 'ru', places, extras, custom, servers)
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
    async def scripts_list(request):
        """Пользователям — только утверждённые скрипты: их можно выбрать в своей группе."""
        ss = store.scripts() if is_admin(request) else [x for x in store.scripts() if x.get('status') == 'approved']
        out = [script_summary(x) for x in ss]
        if not is_admin(request):
            mine = {p['name'] for p in my_profiles(request)}
            for x in out:
                x['used_by'] = [n for n in x['used_by'] if n in mine]
        return J({'scripts': out, 'gemini': bool(store.get('gemini_key')) if is_admin(request) else None})

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
            out['location'] = d.get('location')
            out['ru_check'], out['ru_note'] = d.get('ru_check') or '', d.get('ru_note') or ''
            out['create_failed'] = bool(d.get('create_failed')) and j['status'] == 'failed'
            out['report'] = d.get('report') if j['status'] == 'failed' else None
            out['report_to_admin'] = bool(d.get('report_to_admin'))
            out['survey'] = d.get('survey') or ''
            path = runner.log_path(j['id'])
            if path.exists():
                with open(path, 'rb') as f:
                    f.seek(max(0, path.stat().st_size - 24000))
                    out['log'] = engine.strip_ansi(f.read().decode('utf-8', 'replace')).replace('\r', '')
            else:
                out['log'] = ''
        return out

    def job_or_404(jid, request) -> dict:
        try:
            j = store.job(int(jid))
        except (TypeError, ValueError):
            j = None
        if not j or not (is_admin(request) or can(request, store.profile(j['profile_id']))):
            raise ApiError('Задача не найдена', 404)
        return j

    @r.get('/api/jobs')
    async def jobs(request):
        pid = request.query.get('pid')
        if is_admin(request):
            js = store.jobs(500 if pid else 60)
        else:
            ids = {p['id'] for p in my_profiles(request)}
            js = [j for j in store.jobs(1000) if j['profile_id'] in ids][:500 if pid else 60]
        if pid:
            js = [j for j in js if str(j['profile_id']) == pid]
        return J({'jobs': [job_view(j) for j in js]})

    @r.get('/api/jobs/{jid}')
    async def job(request):
        return J(job_view(job_or_404(request.match_info['jid'], request), full=True))

    @r.post('/api/jobs/{jid}/retry')
    async def retry(request):
        j = job_or_404(request.match_info['jid'], request)
        if j['status'] != 'failed':
            raise ApiError('Повторить можно только задачу с ошибкой')
        store.update_job(j['id'], status='queued', error='')
        runner.start(j['id'])
        return J({'ok': True, 'rebuy': not j['data'].get('server_id') and j['data'].get('kind') not in SERVICE_KINDS})

    @r.post('/api/jobs/{jid}/hide')
    async def hide(request):
        j = job_or_404(request.match_info['jid'], request)
        if j['status'] in ACTIVE:
            raise ApiError('Задача ещё в работе')
        store.update_job(j['id'], hidden=1)
        return J({'ok': True})

    # ── мониторинг доступности из России ──
    def mon():
        if monitor is None:
            raise ApiError('Мониторинг выключен', 404)
        return monitor

    def checker_or_404(cid, request, manage: bool = True) -> dict:
        """Чекеры общие для всех: опросы видят все, а менять и удалять может тот, кто добавил, и администратор."""
        try:
            ch = store.checker(int(cid))
        except (TypeError, ValueError):
            ch = None
        if not ch:
            raise ApiError('Чекер не найден', 404)
        if manage and not is_admin(request) and str(ch.get('created_by') or '') != str(uid_of(request)):
            raise ApiError('Этот чекер добавил другой пользователь — менять его может он или администратор', 403)
        return ch

    @r.get('/api/monitor')
    async def monitor_view(request):
        v = mon().view()
        ids = {p['id'] for p in my_profiles(request)}
        v['groups'] = [g for g in v['groups'] if g['id'] in ids]
        for g in v['groups']:
            g['sub_hint'] = hint((store.profile(g['id']) or {}).get('mon_sub') or '')
        me, admin = str(uid_of(request)), is_admin(request)
        cks = []
        for c in v['checkers']:
            raw = store.checker(c['id']) or {}
            mine = admin or str(raw.get('created_by') or '') == me
            c.update(mine=mine, by=user_label(raw['created_by']) if raw.get('created_by') else ('администратор' if not admin else ''))
            if not mine:  # чужой чекер: только имя и как он проверяет — без адреса, доступа и журнала
                c = {k: c.get(k) for k in ('id', 'name', 'status', 'polled_at', 'poll_error', 'proxies', 'online', 'mine', 'by')}
                c['poll_error'] = 'не отвечает' if c.get('poll_error') else ''
            cks.append(c)
        v['checkers'] = cks
        v['bot_key'] = runner.bot_key()[1]
        v['scripts'] = [{'id': x['id'], 'title': x.get('title') or x.get('path')} for x in store.scripts() if x.get('status') == 'approved']
        return J(v)

    @r.post('/api/checkers')
    async def checker_add(request):
        b = await body(request)
        name = str(b.get('name') or '').strip()[:40] or 'Чекер'
        ip = str(b.get('ip') or '').strip()
        if not engine.is_ip(ip) or ':' in ip:
            raise ApiError('IP сервера: адрес IPv4, например 203.0.113.10')
        if any(x['ip'] == ip for x in store.checkers()):
            raise ApiError('Чекер с этим IP уже есть')
        from . import sshaccess
        try:
            acc = sshaccess.clean(b, None, runner.keydir)
        except ValueError as e:
            raise ApiError(str(e)) from None
        cid = store.save_checker({'name': name, 'ip': ip, **acc, 'status': 'new', 'created_at': int(time.time()),
                                  **({} if is_admin(request) else {'created_by': uid_of(request)})})
        mon().install(cid)
        return J({'id': cid})

    @r.post('/api/checkers/{cid}/install')
    async def checker_install(request):
        ch = checker_or_404(request.match_info['cid'], request)
        if ch.get('status') == 'installing':
            raise ApiError('Установка уже идёт')
        from . import sshaccess
        b = await body(request)
        if any(str(b.get(k) or '').strip() for k in ('user', 'port', 'password', 'key', 'key_text', 'sudo_password')):
            try:  # новый доступ: пустые поля — оставить сохранённое
                store.patch_checker(ch['id'], **sshaccess.clean(b, ch, runner.keydir))
            except ValueError as e:
                raise ApiError(str(e)) from None
        mon().install(ch['id'])
        return J({'ok': True})

    @r.post('/api/checkers/{cid}/forget-hostkey')
    async def checker_forget(request):
        """Забыть ключ сервера чекера — после переустановки или если облако отдало IP новому серверу."""
        ch = checker_or_404(request.match_info['cid'], request)
        engine.forget_host(runner.known_hosts(), ch['ip'], int(ch.get('port') or 22))
        return J({'ok': True, 'message': 'Ключ забыт — при следующем входе бот запомнит новый'})

    @r.delete('/api/checkers/{cid}')
    async def checker_delete(request):
        ch = checker_or_404(request.match_info['cid'], request)
        store.delete_checker(ch['id'])
        if ch.get('api_password'):  # остановить чекер на сервере — по возможности, не дожидаясь
            path, _, kh = mon().keyfiles()
            asyncio.get_running_loop().run_in_executor(None, lambda: ck_stop(ch, path, kh))
        return J({'ok': True})

    def ck_stop(ch, path, kh):
        from . import checker as ck
        try:
            ck.remote(ch, path, kh).run(f'cd {ck.DIR} 2>/dev/null && docker compose down >/dev/null 2>&1; true', 120)
        except Exception:  # noqa: BLE001
            pass

    @r.post('/api/profiles/{pid}/monitor')
    async def monitor_settings(request):
        from . import monitor as M
        p = profile_or_404(request.match_info['pid'], request)
        b = await body(request)
        try:
            cfg = M.clean_settings(b, p, store.scripts())
        except ValueError as e:
            raise ApiError(str(e)) from None
        changed = (cfg['mon_sub'] or '') != (p.get('mon_sub') or '')
        store.save_profile({**p, **cfg})
        if changed:
            mon().resync()
        return J({'ok': True, 'resync': changed})

    @r.post('/api/profiles/{pid}/monitor/notify')
    async def monitor_notify(request):
        """Уведомления мониторинга: всей группы (quiet) или одной ноды (addr + mute). Проверка и починка не меняются."""
        from . import monitor as M
        p = profile_or_404(request.match_info['pid'], request)
        b = await body(request)
        cfg = M.settings_of(p)
        if 'quiet' in b:
            cfg['mon_quiet'] = bool(b['quiet'])
        if b.get('clear'):
            cfg['mon_mute'] = []
        if b.get('addr') is not None:
            addr = str(b['addr']).strip()[:253]
            if not addr:
                raise ApiError('Не указана нода')
            mute = [x for x in cfg['mon_mute'] if x != addr]
            if b.get('mute'):
                if len(mute) >= 500:
                    raise ApiError('Слишком много нод без уведомлений')
                mute.append(addr)
            cfg['mon_mute'] = mute
        store.save_profile({**p, 'mon_quiet': cfg['mon_quiet'], 'mon_mute': cfg['mon_mute']})
        return J({'quiet': cfg['mon_quiet'], 'mute': cfg['mon_mute']})

    @r.post('/api/monitor/action')
    async def monitor_action(request):
        b = await body(request)
        what = b.get('action')
        if what not in ('fix', 'replace', 'ignore'):
            raise ApiError('Неизвестное действие')
        found = mon().find(str(b.get('tok') or ''))
        if found and not can(request, found[0]):
            raise ApiError('Нода не найдена', 404)
        return J({'ok': True, 'message': await mon().action(str(b.get('tok') or ''), what)})

    @r.post('/api/monitor/check')
    async def monitor_check(_):
        m = mon()
        if m._lock.locked():
            raise ApiError('Проверка уже идёт')
        asyncio.get_running_loop().create_task(m.tick())
        return J({'ok': True})

    @r.get('/api/host')
    async def host_view(_):
        """Нагрузка сервера бота: сейчас, за сутки и вывод — хватает ли VPS."""
        if hostload is None:
            raise ApiError('Замеры выключены', 404)
        if not hostload.now:
            await asyncio.to_thread(hostload.sample)
        return J(hostload.view())

    # ── автопилот: автообновление, нагрузка, аренда, утренняя сводка ──
    def pilot():
        if autopilot is None:
            raise ApiError('Автопилот выключен', 404)
        return autopilot

    @r.get('/api/profiles/{pid}/auto')
    async def auto_get(request):
        from . import autopilot as ap
        p = profile_or_404(request.match_info['pid'], request)
        st = pilot().get(f'ap:upd:{p["id"]}')
        return J({**ap.auto_cfg(p), 'last': {k: st.get(k) for k in ('date', 'tag', 'done', 'failed', 'stopped', 'queue', 'reported')},
                  'weekdays': ap.WEEKDAYS})

    @r.post('/api/profiles/{pid}/auto')
    async def auto_save(request):
        from . import autopilot as ap
        p = profile_or_404(request.match_info['pid'], request)
        try:
            a = ap.clean_auto(await body(request), p)
        except ValueError as e:
            raise ApiError(str(e)) from None
        store.save_profile({**p, 'auto': a})
        return J({'ok': True, **a})

    # ── плагины нод: Torrent Blocker, Ingress / Egress Filter (одна запись плагина на группу) ──
    def plug_mem(pid: int) -> dict:
        """Какие записи списков добавлены пресетами — чтобы обновлять и убирать их, не трогая ручные."""
        try:
            m = json.loads(store.get(f'plug:{pid}') or '{}')
        except ValueError:
            m = {}
        return {'ingress': m.get('ingress') or {}, 'egress': m.get('egress') or {}}

    def plug_view(p: dict, st: dict) -> dict:
        mem = plug_mem(p['id'])
        cfg = st['config']
        tb, ig, eg = cfg['torrentBlocker'], cfg['ingressFilter'], cfg['egressFilter']
        preset_ips = {x for v in mem['ingress'].values() for x in v}
        eg_preset_ips = {x for k, v in mem['egress'].items() if k != 'mail' for x in v}
        eg_preset_ports = set(mem['egress'].get('mail') or [])
        return {'exists': bool(st['uuid']), 'name': st['name'], 'bound': st['bound'], 'total': st['total'],
                'foreign': st['foreign'], 'others': st['others'],
                'torrent': {'on': plugins.is_on(tb), 'duration': int(tb.get('blockDuration') or 3600),
                            'ignore_ips': (tb.get('ignoreLists') or {}).get('ip') or []},
                'ingress': {'on': plugins.is_on(ig), 'count': len(ig.get('blockedIps') or []),
                            'manual': [x for x in ig.get('blockedIps') or [] if x not in preset_ips],
                            'presets': [{'id': k, 'title': v['title'], 'sub': v['sub'], 'on': k in mem['ingress'],
                                         'count': len(mem['ingress'].get(k) or [])} for k, v in plugins.INGRESS_PRESETS.items()]},
                'egress': {'on': plugins.is_on(eg),
                           'manual_ips': [x for x in eg.get('blockedIps') or [] if x not in eg_preset_ips],
                           'manual_ports': [x for x in eg.get('blockedPorts') or [] if x not in eg_preset_ports],
                           'presets': [{'id': k, 'title': v['title'], 'sub': v['sub'], 'on': k in mem['egress']}
                                       for k, v in plugins.EGRESS_PRESETS.items()]}}

    async def plug_run(p: dict, fn):
        try:
            return await asyncio.to_thread(fn, panel_of(p))
        except engine.StepError as e:
            msg = str(e)
            if '404' in msg and 'node-plugins' in msg:
                raise ApiError('Панель не знает про плагины нод — обновите Remnawave до 2.2 или новее') from None
            raise ApiError(f'Панель: {msg}') from None
        except plugins.PluginError as e:
            raise ApiError(str(e)) from None

    @r.get('/api/profiles/{pid}/plugins')
    async def plug_get(request):
        p = profile_or_404(request.match_info['pid'], request)
        return J(plug_view(p, await plug_run(p, plugins.state)))

    @r.post('/api/profiles/{pid}/plugins')
    async def plug_save(request):
        """Изменения по секциям: torrent {on, duration, ignore_ips}, ingress {on, manual}, egress {on, manual_ips,
        manual_ports}; bind — заодно перепривязать ноды с чужим плагином. Сохраняется и привязывается ко всем нодам."""
        p = profile_or_404(request.match_info['pid'], request)
        b = await body(request)
        mem = plug_mem(p['id'])

        def run(pl):
            cfg = plugins.state(pl)['config']
            tb, ig, eg = cfg['torrentBlocker'], cfg['ingressFilter'], cfg['egressFilter']
            t, i, e = b.get('torrent') or {}, b.get('ingress') or {}, b.get('egress') or {}
            if 'on' in t:
                tb['enabled'] = bool(t['on'])
            if 'duration' in t:
                d = int(t['duration'] or 0)
                if not 60 <= d <= 30 * 86400:
                    raise ValueError('Время бана: от 1 минуты до 30 дней')
                tb['blockDuration'] = d
            if 'ignore_ips' in t:
                tb['ignoreLists']['ip'] = plugins.clean_ips(t['ignore_ips'], v6=True)
            if 'on' in i:
                ig['enabled'] = bool(i['on'])
            if 'manual' in i:  # ручные записи заменяем, пресетные оставляем как есть
                pre = {x for v in mem['ingress'].values() for x in v}
                ig['blockedIps'] = [x for x in ig['blockedIps'] if x in pre] + \
                    [x for x in plugins.clean_ips(i['manual']) if x not in pre]
            if 'on' in e:
                eg['enabled'] = bool(e['on'])
            if 'manual_ips' in e:
                pre = {x for k, v in mem['egress'].items() if k != 'mail' for x in v}
                eg['blockedIps'] = [x for x in eg['blockedIps'] if x in pre] + \
                    [x for x in plugins.clean_ips(e['manual_ips'], v6=True) if x not in pre]
            if 'manual_ports' in e:
                pre = set(mem['egress'].get('mail') or [])
                eg['blockedPorts'] = sorted({x for x in eg['blockedPorts'] if x in pre} | set(plugins.clean_ports(e['manual_ports'])))
            if ig['enabled'] and not ig['blockedIps']:
                raise ValueError('Список входящих пуст — добавьте пресет или адреса, иначе включать нечего')
            return plugins.save(pl, cfg, take_foreign=bool(b.get('bind')))
        try:
            st = await plug_run(p, run)
        except ValueError as e:
            raise ApiError(str(e)) from None
        return J(plug_view(p, st))

    @r.post('/api/profiles/{pid}/plugins/preset')
    async def plug_preset(request):
        """Пресет списка: kind ingress|egress, id, on. Включение пресета входящих скачивает свежий список
        (повторное — обновляет его), выключение убирает только его записи."""
        p = profile_or_404(request.match_info['pid'], request)
        b = await body(request)
        kind, pid_, on = b.get('kind'), str(b.get('id') or ''), bool(b.get('on'))
        presets = plugins.INGRESS_PRESETS if kind == 'ingress' else plugins.EGRESS_PRESETS if kind == 'egress' else None
        if presets is None or pid_ not in presets:
            raise ApiError('Нет такого пресета')
        mem = plug_mem(p['id'])
        fresh = await asyncio.to_thread(plugins.fetch_preset, pid_) if kind == 'ingress' and on else None

        def run(pl):
            st = plugins.apply_preset(pl, mem, kind, pid_, on, fresh)
            store.set(f'plug:{p["id"]}', json.dumps(mem))
            return st
        try:
            st = await plug_run(p, run)
        except plugins.PluginError as e:
            raise ApiError(str(e)) from None
        return J(plug_view(p, st))

    @r.get('/api/profiles/{pid}/plugins/stats')
    async def plug_stats(request):
        p = profile_or_404(request.match_info['pid'], request)
        return J(await plug_run(p, plugins.stats))

    @r.post('/api/profiles/{pid}/plugins/unblock')
    async def plug_unblock(request):
        p = profile_or_404(request.match_info['pid'], request)
        ip = str((await body(request)).get('ip') or '').strip()
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            raise ApiError('Нужен IP-адрес, например 203.0.113.10') from None
        await plug_run(p, lambda pl: plugins.unblock(pl, ip))
        return J({'ok': True, 'message': f'{ip} разблокирован на всех нодах'})

    # ── уборка: оплаченные серверы без нод и давно мёртвые ноды ──
    @r.get('/api/profiles/{pid}/cleanup')
    async def cleanup_get(request):
        p = profile_or_404(request.match_info['pid'], request)
        try:
            return J(await pilot().cleanup(p))
        except engine.StepError as e:
            raise ApiError(f'Панель: {e}') from None
        except BcloudError as e:
            raise ApiError(f'Bobr Cloud: {e}') from None

    @r.post('/api/profiles/{pid}/cleanup/servers/{sid}')
    async def cleanup_server(request):
        """Удалить сервер в Bobr Cloud вместе с его нодой, хостами и A-записью."""
        p = profile_or_404(request.match_info['pid'], request)
        lines: list[str] = []
        try:
            res = await pilot().delete_server(p, int(request.match_info['sid']), lines.append)
        except BcloudError as e:
            raise ApiError(f'Bobr Cloud: {e}') from None
        logging.getLogger(__name__).info('server %s of profile %s deleted by %s', request.match_info['sid'], p['id'], uid_of(request))
        return J({'ok': True, 'message': 'Сервер удалён' + (f', из панели убрано нод: {res["nodes"]}' if res.get('nodes') else ''), 'log': lines})

    # ── автообновление самого бота (ставит таймер bnodes на сервере, здесь — включение и час) ──
    @r.get('/api/selfupdate')
    async def selfupdate_get(request):
        from . import selfupdate as su
        c = su.cfg(store)
        try:
            req = json.loads(store.get('self_update_req') or '{}')
        except ValueError:
            req = {}
        return J({**c, 'version': su.version(data_dir or 'data'), 'window': su.WINDOW, 'changelog': su.changelog(),
                  'request': req, 'pending': bool(store.get('self_update_pending'))})

    @r.get('/api/selfupdate/check')
    async def selfupdate_check(request):
        from . import selfupdate as su
        return J(await su.check_remote(data_dir or 'data', force=bool(request.query.get('force'))))

    @r.post('/api/selfupdate/now')
    async def selfupdate_now(request):
        """Обновить сейчас: сервер запустит обновление в течение минуты; бот перезапустится на ~10 секунд."""
        from . import selfupdate as su
        rem = await su.check_remote(data_dir or 'data', force=True)
        if rem['state'] == 'latest':
            raise ApiError('У вас уже последняя версия')
        if rem['state'] != 'ready':
            raise ApiError({'testing': 'Новая версия ещё проходит тесты — подождите пару минут',
                            'failed': 'Новая версия не прошла тесты — обновление недоступно'}.get(rem['state'], 'GitHub не ответил — попробуйте позже'))
        su.request_now(store, data_dir or 'data', rem['sha'])
        busy = len(store.active_jobs())
        return J({'ok': True, 'message': 'Обновление начнётся в течение минуты' + (f' — после того как закончатся задачи ({busy})' if busy else '')})

    @r.post('/api/selfupdate/night')
    async def selfupdate_night(request):
        from . import selfupdate as su
        rem = await su.check_remote(data_dir or 'data')
        c = su.request_night(store, rem.get('sha', ''))
        return J({'ok': True, 'message': f'Обновлюсь ночью в {c["hour"]:02d}:00'})

    @r.post('/api/selfupdate')
    async def selfupdate_save(request):
        from . import selfupdate as su
        try:
            c = su.clean(await body(request), su.cfg(store))
        except (TypeError, ValueError) as e:
            raise ApiError(str(e) or 'Неверные данные') from None
        store.set(su.KEY, json.dumps(c))
        return J({**c, 'version': su.version(data_dir or 'data'), 'window': su.WINDOW, 'changelog': su.changelog()})

    # ── центр уведомлений: лента (колокольчик) и настройки каждого пользователя ──
    @r.get('/api/notify/feed')
    async def notify_feed(request):
        q = request.query
        before = int(q['before']) if str(q.get('before', '')).isdigit() else None
        return J(NT.feed(uid_of(request), q.get('cat', ''), before))

    @r.post('/api/notify/read')
    async def notify_read(request):
        NT.mark_read(uid_of(request))
        return J({'ok': True, 'unread': 0})

    def notify_view(request) -> dict:
        admin = is_admin(request)
        cat = [{'type': t, 'cat': d[0], 'title': d[1], 'default': d[2], 'crit': d[3]}
               for t, d in notify.TYPES.items() if admin or not d[4]]
        return {**NT.prefs(uid_of(request)), 'catalog': cat,
                'categories': [{'id': k, 'title': v} for k, v in notify.CATEGORIES.items() if any(x['cat'] == k for x in cat)]}

    @r.get('/api/notify/prefs')
    async def notify_prefs(request):
        tz = request.query.get('tz')
        if tz not in (None, '') and NT.prefs(uid_of(request))['tz'] is None:  # пояс узнаём при первом открытии
            try:
                NT.save_prefs(uid_of(request), {'tz': int(tz)})
            except ValueError:
                pass
        return J(notify_view(request))

    @r.post('/api/notify/prefs')
    async def notify_prefs_save(request):
        try:
            NT.save_prefs(uid_of(request), await body(request))
        except (TypeError, ValueError, KeyError) as e:
            raise ApiError(str(e) or 'Неверные настройки') from None
        return J(notify_view(request))

    # ── обучение: показано ли пользователю (на любом устройстве) ──
    @r.post('/api/tour')
    async def tour_save(request):
        done = bool((await body(request)).get('done', True))
        store.set(f'tour_done:{uid_of(request)}', '1' if done else '')
        return J({'ok': True, 'tour_done': done})

    # ── переезд бота на новый VPS (только администратор) ──
    def mover_or_404():
        if mover is None:
            raise ApiError('Переезд недоступен', 404)
        return mover

    def move_target(b: dict) -> dict:
        from . import sshaccess
        ip = str(b.get('ip') or '').strip()
        if not engine.is_ip(ip) or ':' in ip:
            raise ApiError('IP нового сервера: IPv4, например 203.0.113.10')
        try:
            acc = sshaccess.clean({'user': 'root', 'port': b.get('port') or 22, 'password': b.get('password') or '',
                                   'key': 'own' if str(b.get('key_text') or '').strip() else '', 'key_text': b.get('key_text') or ''},
                                  None, runner.keydir)
        except ValueError as e:
            raise ApiError(str(e)) from None
        return {'ip': ip, 'port': acc['port'], 'password': acc['password'], 'key_file': acc['key_file']}

    @r.get('/api/migrate')
    async def migrate_get(request):
        return J(mover_or_404().view())

    @r.post('/api/migrate/check')
    async def migrate_check(request):
        m = mover_or_404()
        if m.running():
            raise ApiError('Переезд уже идёт')
        t = move_target(await body(request))
        from .migrate import MigrateError
        try:
            await asyncio.to_thread(m.check, t)
        except (engine.StepError, MigrateError) as e:
            m.put(status='idle', error=str(e))
            raise ApiError(f'Сервер: {e}') from None
        return J(m.view())

    @r.post('/api/migrate/start')
    async def migrate_start(request):
        """Переезд целиком. Пароль сервера — только в памяти процесса, в базу не попадает."""
        m = mover_or_404()
        b = await body(request)
        t = move_target(b)
        bc = str(b.get('bcloud_url') or '').strip().rstrip('/')
        from .migrate import MigrateError, internal_bcloud
        if internal_bcloud(store.get('bcloud_url')):
            if not re.fullmatch(r'https://[a-z0-9.-]+\.[a-z]{2,}(/.*)?', bc):
                raise ApiError('Сейчас бот ходит в Bobr Cloud внутри этого сервера. Укажите публичный адрес Bobr Cloud, '
                               'например https://cloud.example.com — с нового сервера бот будет ходить туда')
            from .bcloud import Bcloud
            try:
                b2 = Bcloud(bc, store.get('bcloud_token'), int(store.get('bcloud_tg_id') or 0))
                b2.RETRY_FOR = 5
                await b2.balance()
            except BcloudError as e:
                raise ApiError(f'Bobr Cloud по адресу {bc} не ответил: {e}') from None
        else:
            bc = ''
        if store.active_jobs():
            raise ApiError('Идут задачи бота — дождитесь, пока они закончатся')
        try:
            m.start(t, bool(b.get('dns', True)), bc or None)
        except MigrateError as e:
            raise ApiError(str(e)) from None
        logging.getLogger(__name__).warning('migration to %s started by %s', t['ip'], uid_of(request))
        return J(m.view())

    # ── аватарки из Telegram: свою — всем, чужие — администратору ──
    @r.get('/api/avatar/{tg}')
    async def avatar_get(request):
        from . import avatars
        try:
            tg = int(request.match_info['tg'])
        except ValueError:
            raise ApiError('Нет такого пользователя', 404) from None
        if tg != uid_of(request) and not is_admin(request):
            raise ApiError('Нет доступа', 403)
        a = await avatars.avatar(bot_token, tg)
        if not a:
            return web.Response(status=404)
        return web.Response(body=a[0], content_type=a[1], headers={'Cache-Control': 'private, max-age=3600'})

    # ── отзывы и ошибки ──
    def fb():
        if feedback is None:
            raise ApiError('Отзывы выключены', 404)
        return feedback

    def report_view(r: dict, full: bool = False) -> dict:
        d = r['data']
        out = {'id': r['id'], 'ts': r['ts'], 'updated': r['updated'], 'uid': r['uid'], 'who': user_label(r['uid']) if not access.is_admin(r['uid']) else 'администратор',
               'kind': r['kind'], 'status': r['status'], 'rating': r['rating'], 'text': r['text'], 'job_id': r['job_id'],
               'group': d.get('group', ''), 'domain': d.get('domain') or ', '.join(d.get('domains') or []), 'error': d.get('error', ''),
               'stage': d.get('stage', ''), 'what': d.get('kind', ''), 'count': d.get('count', 1), 'replies': d.get('replies') or []}
        if full:
            out['log'] = d.get('log', '')
        return out

    def my_report(request, rid: int) -> dict:
        r = store.report(rid)
        if not r or (int(r['uid']) != uid_of(request) and not is_admin(request)):
            raise ApiError('Отчёт не найден', 404)
        return r

    @r.get('/api/reports')
    async def reports_list(request):
        q = request.query
        st = q.get('status', '') if q.get('status') in ('open', 'done') else ''
        kind = q.get('kind', '') if q.get('kind') in ('auto', 'survey', 'user') else ''
        rows = [x for x in store.reports(st, kind, 200) if not (x['kind'] == 'survey' and x['rating'] == 1 and not kind)]
        return J({'stats': fb().stats(), 'reports': [report_view(x) for x in rows]})

    @r.get('/api/my/reports')
    async def my_reports(request):
        return J({'reports': [report_view(x) for x in store.reports(uid=uid_of(request), limit=50) if x['kind'] != 'survey' or x['rating'] == -1]})

    @r.get('/api/reports/{rid}')
    async def report_get(request):
        return J(report_view(my_report(request, int(request.match_info['rid'])), full=is_admin(request)))

    @r.post('/api/reports')
    async def report_new(request):
        b = await body(request)
        pid = b.get('pid')
        if pid is not None:
            pid = profile_or_404(pid, request)['id']
        try:
            rid = await fb().manual(uid_of(request), str(b.get('text') or ''), pid)
        except ValueError as e:
            raise ApiError(str(e)) from None
        return J({'ok': True, 'id': rid, 'message': 'Отправлено администратору — писать ему отдельно не нужно'})

    @r.post('/api/reports/{rid}/comment')
    async def report_comment(request):
        r = my_report(request, int(request.match_info['rid']))
        msg = await fb().comment(uid_of(request), r['id'], str((await body(request)).get('text') or ''))
        return J({'ok': True, 'message': msg})

    @r.post('/api/reports/{rid}/resolve')
    async def report_resolve(request):
        b = await body(request)
        try:
            r = await fb().resolve(int(request.match_info['rid']), str(b.get('reply') or ''), bool(b.get('done', True)))
        except ValueError as e:
            raise ApiError(str(e)) from None
        return J(report_view(r, full=True))

    # ── кошелёк: баланс своего аккаунта Bobr Cloud и пополнение ──
    def wallet_tg(request) -> int:
        """Чей баланс: у администратора — аккаунт из «Bobr Cloud», у пользователя — его собственный."""
        tg = int(store.get('bcloud_tg_id') or 0) if is_admin(request) else uid_of(request)
        if not tg or not (store.get('bcloud_url') and store.get('bcloud_token')):
            raise ApiError('Bobr Cloud не подключён — задайте адрес, токен и аккаунт в «Bobr Cloud»')
        return tg

    @r.get('/api/wallet')
    async def wallet(request):
        try:
            o = await bc_ui(store, wallet_tg(request)).topup_options()
        except BcloudError as e:
            raise ApiError(str(e)) from None
        return J({**o, 'account': 'аккаунт из настроек' if is_admin(request) else 'ваш аккаунт'})

    @r.post('/api/wallet/topup')
    async def wallet_topup(request):
        b = await body(request)
        try:
            amount = int(b.get('amount_rub') or 0)
        except (TypeError, ValueError):
            raise ApiError('Сумма — целое число рублей') from None
        mid = b.get('method_id')
        if mid is not None and (not isinstance(mid, int) or isinstance(mid, bool)):
            raise ApiError('Неверный способ оплаты')
        prov = b.get('provider') if isinstance(b.get('provider'), str) else None
        try:
            r = await bc_ui(store, wallet_tg(request)).topup(amount, prov, mid)
        except BcloudError as e:
            raise ApiError(str(e).replace('Bobr Cloud 400: ', '')) from None
        if r.get('url') and not str(r['url']).startswith('https://'):
            raise ApiError('Bobr Cloud вернул странную ссылку на оплату')
        return J(r)

    @r.get('/api/profiles/{pid}/rent')
    async def rent_get(request):
        p = profile_or_404(request.match_info['pid'], request)
        try:
            r = await pilot().rent(p)
        except BcloudError as e:
            raise ApiError(str(e)) from None
        risky = {x['id']: x['why'] for x in pilot().rent_risks(r)}
        for x in r['servers']:
            x['risk'] = risky.get(x['id'], '')
        return J(r)

    async def rent_server(request) -> tuple[dict, int]:
        p = profile_or_404(request.match_info['pid'], request)
        sid = int(request.match_info['sid'])
        if not any(int(d.get('server_id') or 0) == sid for d in bot_jobs_by_ip(p['id']).values()):
            raise ApiError('Сервер не из этой группы', 404)
        return p, sid

    @r.post('/api/profiles/{pid}/rent/{sid}/renew')
    async def rent_renew(request):
        p, sid = await rent_server(request)
        try:
            await bcloud_from(store, buyer_of(store, p)).renew(sid)
        except BcloudError as e:
            raise ApiError(str(e)) from None
        return J({'ok': True, 'message': 'Продлено на 30 дней'})

    @r.post('/api/profiles/{pid}/rent/{sid}/autorenew')
    async def rent_auto(request):
        p, sid = await rent_server(request)
        v = bool((await body(request)).get('value'))
        try:
            await bcloud_from(store, buyer_of(store, p)).set_autorenew(sid, v)
        except BcloudError as e:
            raise ApiError(str(e)) from None
        return J({'ok': True, 'auto_renew': v})

    @r.get('/api/summary')
    async def summary_get(request):
        from . import autopilot as ap
        return J(ap.summary_cfg(store, uid_of(request)))

    @r.post('/api/summary')
    async def summary_save(request):
        from . import autopilot as ap
        b, cur = await body(request), ap.summary_cfg(store, uid_of(request))
        try:
            if 'on' in b:
                cur['on'] = bool(b['on'])
            if 'hour' in b:
                if not isinstance(b['hour'], int) or isinstance(b['hour'], bool) or not 0 <= b['hour'] <= 23:
                    raise ValueError('Час: от 0 до 23')
                cur['hour'] = b['hour']
            if 'tz' in b:
                cur['tz'] = ap.tz_of(b['tz'])
        except ValueError as e:
            raise ApiError(str(e)) from None
        store.set(f'summary:{uid_of(request)}', json.dumps(cur))
        return J(cur)

    @r.post('/api/summary/now')
    async def summary_now(request):
        text = await pilot().summary(uid_of(request))
        await pilot().send(uid_of(request), text, pilot().button())
        return J({'ok': True, 'message': 'Сводка отправлена в чат с ботом'})

    # ── пользователи и приглашения (только администратор) ──
    def invite_view(inv: dict) -> dict:
        return {'id': inv['id'], 'note': inv.get('note', ''), 'max_uses': inv.get('max_uses', 1),
                'used': [{'tg_id': u, 'label': user_label(u)} for u in inv.get('used_by', [])],
                'created_at': inv.get('created_at'), 'expires_at': inv.get('expires_at') or 0,
                'state': access.invite_state(inv),
                'link': f'https://t.me/{bot_username}?start={INVITE_PREFIX}{inv["token"]}' if bot_username else
                        f'/start {INVITE_PREFIX}{inv["token"]}'}

    def user_view(u: dict) -> dict:
        ps = access.profiles_for(u['tg_id'], u.get('username', '')) if not u.get('blocked') else [
            p for p in store.profiles() if access.owns(p, u['tg_id'], u.get('username', ''))]
        return {'tg_id': u['tg_id'], 'username': u.get('username', ''), 'first_name': u.get('first_name', ''),
                'label': user_label(u['tg_id']), 'blocked': bool(u.get('blocked')), 'created_at': u.get('created_at'),
                'note': u.get('note', ''),
                'groups': [{'id': p['id'], 'name': p['name'], 'mine': str(p.get('created_by') or '') == str(u['tg_id'])} for p in ps],
                'checkers': sum(1 for c in store.checkers() if str(c.get('created_by') or '') == str(u['tg_id']))}

    @r.get('/api/users')
    async def users_list(_):
        return J({'users': [user_view(u) for u in store.users()],
                  'invites': [invite_view(i) for i in store.invites()]})

    @r.post('/api/invites')
    async def invite_create(request):
        b = await body(request)
        try:
            inv = access.create_invite(uid_of(request), str(b.get('note') or ''), int(b.get('uses') or 1), int(b.get('days') or 0))
        except (TypeError, ValueError) as e:
            raise ApiError(str(e) or 'неверные значения') from None
        return J(invite_view(inv))

    @r.delete('/api/invites/{iid}')
    async def invite_revoke(request):
        inv = store.invite(int(request.match_info['iid']))
        if not inv:
            raise ApiError('Приглашение не найдено', 404)
        if inv.get('used_by'):  # по нему уже вошли — оставляем в истории, только закрываем
            inv['revoked'] = True
            store.save_invite(inv)
        else:
            store.delete_invite(inv['id'])
        return J({'ok': True})

    def user_or_404(tg) -> dict:
        u = store.user(int(tg)) if str(tg).isdigit() else None
        if not u:
            raise ApiError('Пользователь не найден', 404)
        return u

    @r.post('/api/users/{tg}/block')
    async def user_block(request):
        u = user_or_404(request.match_info['tg'])
        u['blocked'] = bool((await body(request)).get('blocked'))
        store.save_user(u)
        return J(user_view(u))

    @r.delete('/api/users/{tg}')
    async def user_delete(request):
        """Удалить доступ. Группы, которые пользователь создал, остаются — их видит администратор."""
        u = user_or_404(request.match_info['tg'])
        store.delete_user(u['tg_id'])
        return J({'ok': True})

    app.add_routes(r)
    return app
