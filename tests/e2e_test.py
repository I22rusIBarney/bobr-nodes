"""Сквозной сценарий: настоящий aiogram (Telegram подменён), настоящий bcloud с тестовой Astra,
тестовые Cloudflare и панель на localhost, вместо SSH — имитация установщика с его настоящими вопросами.

Запуск: python tests/e2e_test.py  (нужны: путь к bcloud в BCLOUD_DIR, исходники установщика в NV_SRC)
"""
import asyncio
import datetime
import functools
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix='nvbot-'))
sys.path.insert(0, str(ROOT))
BCLOUD = os.environ.get('BCLOUD_DIR')
NV_SRC = os.environ.get('NV_SRC')
if not BCLOUD or not NV_SRC:
    sys.exit('нужны BCLOUD_DIR (репозиторий bcloud) и NV_SRC (src установщика Nuvrion)')

# ── bcloud в этом же процессе ──
os.environ.update(BOT_TOKEN='123:TEST', ASTRA_API_KEY='k', ADMIN_IDS='1', PUBLIC_URL='https://bc.test',
                  WEBHOOK_SECRET='sec', DATABASE_URL=f'sqlite+aiosqlite:///{TMP}/bc.db', SERVICE_TOKEN='svc-token')
sys.path.insert(1, BCLOUD)
from app import db as BD, services as BS  # noqa: E402
from app.web.server import build_app  # noqa: E402

import httpx  # noqa: E402
from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from aiogram.types import CallbackQuery, Chat, Message, Update, User  # noqa: E402

from bot import bcloud as bcmod, engine, handlers  # noqa: E402
from bot.runner import Runner  # noqa: E402
from bot.store import Store  # noqa: E402
from tests import fakes  # noqa: E402

fails = 0


def ok(cond, label):
    global fails
    print(('PASS ' if cond else 'FAIL ') + label, flush=True)
    if not cond:
        fails += 1


def make_key() -> str:
    """Настоящий по структуре SECRET_KEY: CA, сертификат ноды, ключ, JWT-ключ (проверяется установщиком через openssl)."""
    d = TMP / 'k'
    d.mkdir()
    sh = lambda *a: subprocess.run(a, cwd=d, check=True, capture_output=True)  # noqa: E731
    sh('openssl', 'ecparam', '-name', 'prime256v1', '-genkey', '-noout', '-out', 'ca.key')
    sh('openssl', 'req', '-x509', '-new', '-key', 'ca.key', '-subj', '/CN=ca', '-days', '3', '-out', 'ca.pem')
    sh('openssl', 'ecparam', '-name', 'prime256v1', '-genkey', '-noout', '-out', 'n0.key')
    sh('openssl', 'pkcs8', '-topk8', '-nocrypt', '-in', 'n0.key', '-out', 'n.key')
    sh('openssl', 'req', '-new', '-key', 'n.key', '-subj', '/CN=node', '-out', 'n.csr')
    sh('openssl', 'x509', '-req', '-in', 'n.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key', '-CAcreateserial', '-days', '3', '-out', 'n.pem')
    sh('openssl', 'genrsa', '-out', 'j.key', '2048')
    sh('openssl', 'rsa', '-in', 'j.key', '-pubout', '-out', 'j.pub')
    import base64
    r = lambda f: (d / f).read_text()  # noqa: E731
    return base64.b64encode(json.dumps({'caCertPem': r('ca.pem'), 'nodeCertPem': r('n.pem'), 'nodeKeyPem': r('n.key'),
                                        'jwtPublicKey': r('j.pub')}).encode()).decode()


PRESETS = [{'idx': 0, 'cpu': 1, 'ram': 1, 'disk': 10, 'price_month_rub': {'de': 200, 'nl': 190}},
           {'idx': 1, 'cpu': 2, 'ram': 2, 'disk': 30, 'price_month_rub': {'de': 400, 'nl': 380}}]


class FakeAstra:
    def __init__(s):
        s.n = 500
        s.made = {}

    async def catalog(s):
        return {'locations': [{'code': 'de', 'name': 'Германия', 'available': True},
                              {'code': 'nl', 'name': 'Нидерланды', 'available': True}],
                'presets': PRESETS, 'os': ['ubuntu2204', 'debian12', 'win2022']}

    async def balance(s): return {'balance_rub': 9999}

    async def create(s, **kw):
        s.n += 1
        s.made[s.n] = kw
        p = PRESETS[kw['idx']]
        return {'id': s.n, 'spec': {'cpu': p['cpu'], 'ram': p['ram'], 'disk': p['disk'], 'os': kw['os']},
                'charged_rub': 200, 'expires_in_days': 30}

    async def servers(s): return []

    async def server(s, sid):
        return {'id': sid, 'status': 'active', 'ip': f'198.51.100.{sid - 500}', 'root_password': s.made[sid]['password']}

    async def action(s, sid, a, body=None):
        if a == 'rename':
            s.made[sid]['renamed'] = body['name']
        return {'ok': True}

    async def close(s): pass


class Stub:
    configured = False
    enabled = False

    async def close(s): pass


async def bc_notify(uid, t): pass


ADMIN = User(id=1, is_bot=False, first_name='Админ')
CHAT = Chat(id=1, type='private')
UPD = [0]


def upd_msg(text):
    UPD[0] += 1
    return Update(update_id=UPD[0], message=Message(message_id=UPD[0], date=datetime.datetime.now(), chat=CHAT,
                                                    from_user=ADMIN, text=text))


def upd_cb(data, msg_id=1):
    UPD[0] += 1
    return Update(update_id=UPD[0], callback_query=CallbackQuery(
        id=str(UPD[0]), from_user=ADMIN, chat_instance='x', data=data,
        message=Message(message_id=msg_id, date=datetime.datetime.now(), chat=CHAT, from_user=ADMIN, text='…')))


async def main():
    secret = make_key()
    world = fakes.World(secret)
    srv = fakes.make_server(world)
    base = f'http://127.0.0.1:{srv.server_port}'
    engine.Cloudflare.API = base + '/client/v4'
    engine.doh_a = lambda d: [[r['content'] for r in world.records if r['name'] == d and r['type'] == 'A']] * 2
    behaviour = {}
    os.environ['NV_SRC'] = NV_SRC
    engine.Remote = fakes.make_fake_remote(engine, str(ROOT / 'tests' / 'mock_installer.sh'), behaviour)

    # bcloud
    await BD.init_db()
    astra = FakeAstra()
    bctx = BS.Ctx(astra=astra, yk=Stub(), nalog=Stub(), notify=bc_notify, alert_admin=lambda t: bc_notify(0, t))
    bapp = build_app(bctx)
    async with BD.session() as s:
        s.add(BD.User(tg_id=1, username='admin'))
        await s.commit()
        await BD.change_balance(s, 1, 100_000, 'admin_adjust')
    real_client = httpx.AsyncClient
    bcmod.httpx.AsyncClient = lambda **kw: real_client(transport=httpx.ASGITransport(app=bapp), **kw)

    # бот
    store = Store(TMP / 'bot.db')
    store.set('bcloud_tg_id', '1')
    sess = fakes.fake_session()
    bot = Bot('42:TEST', session=sess)
    dp = Dispatcher(storage=MemoryStorage())
    mw = handlers.AdminOnly({1})
    dp.message.outer_middleware(mw)
    dp.callback_query.outer_middleware(mw)
    dp.include_router(handlers.router)
    runner = Runner(store, bot, TMP / 'logs', deploy=functools.partial(engine.deploy, check_tries=1, check_every=0),
                    poll_every=0.05, edit_every=0)
    dp['store'], dp['runner'], dp['public_url'] = store, runner, 'https://nodes.test'

    # веб-админка
    from aiohttp.test_utils import TestClient, TestServer
    from bot.web import build_web
    wapp = build_web(store, runner, '42:TEST', {1}, 'https://nodes.test', 'nodes_bot')
    web = TestClient(TestServer(wapp))
    await web.start_server()

    def init_data(uid):
        import hashlib, hmac, time
        from urllib.parse import urlencode
        f = {'auth_date': str(int(time.time())), 'user': json.dumps({'id': uid, 'first_name': 'Админ'}), 'query_id': 'q'}
        chk = '\n'.join(f'{k}={v}' for k, v in sorted(f.items()))
        sec = hmac.new(b'WebAppData', b'42:TEST', hashlib.sha256).digest()
        return urlencode({**f, 'hash': hmac.new(sec, chk.encode(), hashlib.sha256).hexdigest()})
    H = {'X-Init-Data': init_data(1)}

    async def wapi(path, method='GET', body=None, headers=H):
        r = await web.request(method, '/api/' + path, json=body, headers=headers)
        return r.status, await r.json()

    async def say(text):
        n = len(sess.sent)
        await dp.feed_update(bot, upd_msg(text))
        return [m.text for name, m in sess.sent[n:] if name in ('SendMessage', 'EditMessageText')]

    async def tap(data):
        n = len(sess.sent)
        await dp.feed_update(bot, upd_cb(data))
        return [m.text for name, m in sess.sent[n:] if name in ('SendMessage', 'EditMessageText')]

    def last(out):
        return out[-1] if out else ''

    def buttons():
        for name, m in reversed(sess.sent):
            if name in ('SendMessage', 'EditMessageText'):
                return str(m.reply_markup)
        return ''

    # ── доступ
    n = len(sess.sent)
    UPD[0] += 1
    stranger = Update(update_id=UPD[0], message=Message(message_id=999, date=datetime.datetime.now(),
                      chat=Chat(id=7, type='private'), from_user=User(id=7, is_bot=False, first_name='X'), text='/start'))
    await dp.feed_update(bot, stranger)
    ok(any('только для администраторов' in m.text for _, m in sess.sent[n:]), 'чужой пользователь не пускается в чат')
    ok((await wapi('state', headers={}))[0] == 403, 'админка без подписи Telegram — 403')
    ok((await wapi('state', headers={'X-Init-Data': init_data(7)}))[0] == 403, 'админка для не-админа — 403')
    bad = init_data(1)
    bad = bad[:-1] + ('0' if bad[-1] != '0' else '1')
    ok((await wapi('state', headers={'X-Init-Data': bad}))[0] == 403, 'подделанная подпись — 403')
    page = await web.get('/admin')
    ok(page.status == 200 and 'Bobr Nodes' in await page.text(), 'страница админки отдаётся')
    ok((await web.get('/static/ui.css')).status == 200 and (await web.get('/static/../main.py')).status == 404, 'статика только своя')

    out = await say('/start')
    ok('Чтобы начать' in last(out) and 'web_app' in buttons() and 'nodes.test/admin' in buttons(), 'чат: меню с кнопкой панели, что настроить')
    out = await tap('m:deploy')
    ok('Сначала в панели управления' in last(out), 'развёртывание из чата недоступно, пока не настроено')

    # ── настройки через админку
    st, js = await wapi('state')
    ok(st == 200 and 'подключите bcloud' in js['issues'] and js['settings']['bcloud_tg_id']['value'] == '1', 'состояние: что не настроено')
    st, js = await wapi('settings', 'POST', {'values': {'bcloud_url': 'не адрес'}})
    ok(st == 400 and 'нужен адрес' in js['detail'], 'неверный адрес bcloud отклонён')
    st, js = await wapi('settings', 'POST', {'values': {'bcloud_url': 'http://bc.test', 'bcloud_token': 'svc-token', 'cf_token': 'cf-token-123456'}})
    ok(st == 200 and js['settings']['bcloud_token']['hint'] == '••••oken' and js['settings']['bcloud_token']['value'] == '', 'токены сохранены и не отдаются обратно')
    await wapi('settings', 'POST', {'values': {'bcloud_token': '', 'parallel': '2'}})
    ok(store.get('bcloud_token') == 'svc-token' and store.get('parallel') == '2', 'пустой секрет — оставить текущий')
    st, js = await wapi('check/bcloud', 'POST')
    ok(js['ok'] and '1 000 ₽' in js['message'], 'проверка bcloud: баланс')
    st, js = await wapi('check/cloudflare', 'POST')
    ok(js['ok'], 'проверка Cloudflare')

    # ── профиль через админку
    prof_in = {'name': 'Основная', 'panel_url': f'{base}/', 'panel_token': 'panel-token-123456',
               'panel_cookie': 'https://panel.example.com/auth/login?secretcookie=abc', 'outgoing_ips': '203.0.113.10, 203.0.113.11',
               'node_port': '', 'email': 'ops@example.com', 'base_domain': 'vpn.example.com', 'name_pattern': '{cc}{n}',
               'squads': '11111111-1111-1111-1111-111111111111', 'cf_token': '', 'traffic_control': True}
    st, js = await wapi('profiles', 'POST', prof_in)
    ok(st == 400 and 'vpn' in js['detail'], 'домен с «vpn» для автоимён запрещён')
    # панель за cookie-защитой, которая без cookie отдаёт HTML-заглушку или редирект
    for mode, word in (('html', 'Welcome to nginx'), ('redirect', 'перенаправление')):
        world.decoy = mode
        st, js = await wapi('profiles', 'POST', {**prof_in, 'base_domain': 'example.com', 'panel_cookie': ''})
        ok(st == 200 and not js['check']['ok'] and word in js['check']['message'] and 'Cookie-защита' in js['check']['message'],
           f'панель без cookie ({mode}) — понятная подсказка: {js.get("check", js)}')
        await wapi(f'profiles/{js["id"]}', 'DELETE')
    world.decoy = 'html'
    st, js = await wapi('profiles', 'POST', {**prof_in, 'base_domain': 'example.com',
                                             'panel_url': f'{base}/auth/login?secretcookie=abc', 'panel_cookie': ''})
    ok(st == 200 and js['check']['ok'] and store.profile(js['id'])['panel_url'] == base
       and store.profile(js['id'])['panel_cookie'] == 'secretcookie=abc', 'ссылка входа в поле адреса: адрес и cookie разобраны')
    await wapi(f'profiles/{js["id"]}', 'DELETE')
    st, js = await wapi('profiles', 'POST', {**prof_in, 'base_domain': 'example.com'})
    ok(st == 200 and js['check']['ok'], f'профиль создан, панель отвечает через cookie: {js}')
    pid = js['id']
    p = store.profile(pid)
    ok(p['panel_cookie'] == 'secretcookie=abc' and p['outgoing_ips'] == '203.0.113.10 203.0.113.11' and p['node_port'] == 2222,
       'cookie из ссылки входа, IP нормализованы, порт по умолчанию')
    st, js = await wapi(f'profiles/{pid}')
    ok(js['panel_token'] == '' and js['hints']['panel_token'] == '••••3456', 'токен панели не отдаётся в админку')
    st, js = await wapi(f'profiles/{pid}', 'PUT', {**prof_in, 'base_domain': 'example.com', 'panel_token': '', 'traffic_control': False})
    ok(st == 200 and store.profile(pid)['panel_token'] == 'panel-token-123456' and store.profile(pid)['traffic_control'] is False,
       'правка профиля: токен сохранён, Traffic Control выключен')
    await wapi(f'profiles/{pid}', 'PUT', {'traffic_control': True})
    st, js = await wapi('state')
    ok(js['issues'] == [], 'всё настроено')

    # занятый номер: de1 уже есть в DNS → автоимена начнутся с de2
    world.records.append({'id': 'x', 'type': 'A', 'name': 'de1.example.com', 'content': '192.0.2.1'})

    # ── развёртывание из админки: 2 ноды в Германии, автоимена
    st, cat = await wapi(f'catalog?pid={pid}')
    ok(st == 200 and cat['os_label'] and cat['balance_kop'] == 100_000 and cat['locations'][0]['offers'], 'каталог bcloud в админке')
    st, js = await wapi('domains/auto', 'POST', {'pid': pid, 'location': 'de', 'count': 2})
    ok(js['domains'] == ['de2.example.com', 'de3.example.com'], f'автоимена пропускают занятый de1: {js}')
    st, bad = await wapi('deploy', 'POST', {'pid': pid, 'location': 'de', 'idx': 0, 'period': '1m', 'domains': ['x.other.org']})
    ok(st == 400 and 'зона' in bad['detail'], 'деплой с чужим доменом отклонён')
    st, js = await wapi('deploy', 'POST', {'pid': pid, 'location': 'de', 'idx': 0, 'period': '1m', 'domains': js['domains']})
    ok(st == 200 and len(js['ids']) == 2, 'задачи созданы из админки')
    await runner.wait_all()
    jobs = sorted(store.jobs(10), key=lambda j: j['id'])
    ok(len(jobs) == 2 and all(j['status'] == 'done' for j in jobs), f'обе ноды развёрнуты: {[(j["status"], j["error"]) for j in jobs]}')
    ok(all(j['chat_id'] == 1 and j['msg_id'] for j in jobs), 'ход задач из админки приходит в чат админа')
    ok({r['name'] for r in world.records if r['type'] == 'A' and r['content'].startswith('198.51')} == {'de2.example.com', 'de3.example.com'}
       and all(not r['proxied'] for r in world.records if r['name'] != 'de1.example.com'), 'A-записи созданы, без проксирования')
    ok({n['name'] for n in world.nodes} == {'de2', 'de3'} and all(n['countryCode'] == 'DE' for n in world.nodes),
       'ноды в панели: имена и страна')
    ok(len(world.hosts) == 2 and {h['address'] for h in world.hosts} == {'de2.example.com', 'de3.example.com'}, 'хосты созданы')
    ok(len(world.squads['11111111-1111-1111-1111-111111111111']['inbounds']) == 2, 'inbound обеих нод добавлены в сквад')
    ok(sorted(m.get('renamed') for m in astra.made.values()) == ['de2', 'de3'], 'серверы переименованы в bcloud')
    ok(all(m['os'] == 'debian12' for m in astra.made.values()), 'выбрана Debian 12 (Ubuntu 24.04 в каталоге нет)')
    card = sess.messages[jobs[0]['msg_id']]
    ok(card.count('✅') >= 7 and 'Root-пароль' in card, 'карточка задачи в чате: все этапы ✅ и пароль')
    st, jv = await wapi(f'jobs/{jobs[0]["id"]}')
    ok(st == 200 and jv['status'] == 'done' and jv['password'] and 'ответ: Домен ноды' in jv['log'] and secret[:30] not in jv['log']
       and jv['password'] not in jv['log'], 'задача в админке: журнал без SECRET_KEY и пароля')
    st, jl = await wapi('jobs')
    ok(len(jl['jobs']) == 2, 'список задач в админке')
    async with BD.session() as s:
        bal = (await s.get(BD.User, 1)).balance_kop
    ok(bal < 100_000, 'покупки списаны с баланса в bcloud')

    # ── группа клиента: покупки от его имени и с его баланса
    st, js = await wapi('profiles', 'POST', {**prof_in, 'name': 'Клиент Вася', 'base_domain': 'example.com', 'owner': '@nobody_here'})
    ok(st == 400 and 'Клиент bcloud' in js['detail'], 'неизвестный клиент bcloud — понятная ошибка')
    async with BD.session() as s_:
        s_.add(BD.User(tg_id=77, username='Vasya_Client', first_name='Вася'))
        await s_.commit()
        await BD.change_balance(s_, 77, 30_000, 'admin_adjust')
    st, js = await wapi('profiles', 'POST', {**prof_in, 'name': 'Клиент Вася', 'base_domain': 'example.com', 'owner': 'vasya_client',
                                             'cf_token': 'cf-token-123456'})
    ok(st == 200, f'группа клиента создана по @username: {js}')
    gid = js['id']
    g = store.profile(gid)
    ok(g['owner_tg_id'] == '77' and g['owner_label'] == '@Vasya_Client', 'клиент найден в bcloud, запомнены ID и подпись')
    st, js = await wapi('state')
    gs = {x['id']: x for x in js['profiles']}
    ok(gs[pid]['servers'] == 2 and gs[gid]['servers'] == 0 and gs[gid]['owner_label'] == '@Vasya_Client', 'группы: серверов по группам и клиент')
    st, cat2 = await wapi(f'catalog?pid={gid}')
    ok(cat2['buyer'] == '@Vasya_Client' and cat2['balance_kop'] == 30_000, 'каталог группы: платит клиент, его баланс')
    st, dj = await wapi('domains/auto', 'POST', {'pid': gid, 'location': 'nl', 'count': 1})
    st, js = await wapi('deploy', 'POST', {'pid': gid, 'location': 'nl', 'idx': 1, 'period': '1m', 'domains': dj['domains']})
    ok(st == 400 and 'Не хватает денег' in js['detail'] and '@Vasya_Client' in js['detail'], 'не хватает денег клиента — понятная ошибка')
    st, js = await wapi('deploy', 'POST', {'pid': gid, 'location': 'nl', 'idx': 0, 'period': '1m', 'domains': dj['domains']})
    ok(st == 200, 'деплой в группу клиента')
    await runner.wait_all()
    cj = store.job(js['ids'][0])
    ok(cj['status'] == 'done' and cj['data']['buyer'] == 77, f'нода клиента развёрнута: {cj["error"]}')
    async with BD.session() as s_:
        csrv = await s_.get(BD.Server, cj['data']['server_id'])
        cbal = (await s_.get(BD.User, 77)).balance_kop
    ok(csrv.tg_id == 77 and cbal < 30_000, 'сервер в аккаунте клиента в bcloud, оплачен с его баланса')
    st, js = await wapi(f'jobs?pid={gid}')
    ok([x['id'] for x in js['jobs']] == [cj['id']], 'серверы группы отдельно от других групп')

    # ── балансировщик: 2 ноды в Польше, шаблон копируется с базового (конфиг пользователя), хост-балансировщик один
    base_tpl = json.load(open(os.path.join(os.path.dirname(__file__), 'balancer_base.json')))
    world.templates.append({'uuid': '33333333-3333-3333-3333-333333333333', 'name': 'Balancer_pl', 'templateType': 'XRAY_JSON',
                            'templateJson': base_tpl, 'tags': []})
    world.templates.append({'uuid': '44444444-4444-4444-4444-444444444444', 'name': 'Default', 'templateType': 'XRAY_JSON',
                            'templateJson': {'outbounds': []}, 'tags': []})
    st, meta = await wapi(f'profiles/{pid}/panel')
    ok(st == 200 and {q['name'] for q in meta['squads']} == {'Default', 'Premium'}
       and meta['default_squads'] == ['11111111-1111-1111-1111-111111111111']
       and [t['balancer_tag'] for t in meta['templates']] == ['PL', None], f'панель: сквады и шаблоны с тегом балансировщика: {meta}')
    st, js = await wapi(f'profiles/{pid}/check', 'POST')
    ok(js['ok'], 'статус связи с панелью группы')
    async with BD.session() as s_:
        await BD.change_balance(s_, 1, 100_000, 'admin_adjust')
    st, dj = await wapi('domains/auto', 'POST', {'pid': pid, 'location': 'de', 'count': 2})
    hosts_before = len(world.hosts)
    st, js = await wapi('deploy', 'POST', {'pid': pid, 'location': 'de', 'idx': 0, 'period': '1m', 'domains': dj['domains'],
                                           'squads': ['22222222-2222-2222-2222-222222222222'], 'remark': '{flag} Германия {n}',
                                           'host_mode': 'balancer', 'balancer_tag': 'de', 'tags': 'normal',
                                           'balancer_remark': '{flag} {country}', 'balancer_template': '33333333-3333-3333-3333-333333333333'})
    ok(st == 200, f'деплой с балансировщиком: {js}')
    await runner.wait_all()
    bj = [store.job(i) for i in js['ids']]
    ok(all(j['status'] == 'done' for j in bj), f'ноды за балансировщиком развёрнуты: {[j["error"] for j in bj]}')
    new_hosts = world.hosts[hosts_before:]
    node_hosts = [h for h in new_hosts if not h.get('xrayJsonTemplateUuid')]
    bal_hosts = [h for h in new_hosts if h.get('xrayJsonTemplateUuid')]
    ok(len(node_hosts) == 2 and all(h['isHidden'] and h['tags'] == ['DE', 'NORMAL'] for h in node_hosts),
       f'хосты нод скрытые, с тегами DE и NORMAL: {[(h["isHidden"], h["tags"]) for h in node_hosts]}')
    ok(sorted(h['remark'] for h in node_hosts) == sorted(f'🇩🇪 Германия {d.split(".")[0][2:]}' for d in dj['domains']),
       f'названия хостов по шаблону: {[h["remark"] for h in node_hosts]}')
    detpl = next((t for t in world.templates if t['name'] == 'Balancer_de'), None)
    ok(detpl and detpl['templateJson']['remnawave']['injectHosts'][0]['selector']['pattern'] == '^DE$'
       and detpl['templateJson']['routing']['balancers'][0]['selector'] == ['de']
       and detpl['templateJson']['burstObservatory']['subjectSelector'] == ['de']
       and detpl['templateJson']['routing']['rules'] == base_tpl['routing']['rules'], 'шаблон Balancer_de — копия базового с тегом DE')
    ok(base_tpl == world.templates[0]['templateJson'], 'исходный шаблон не изменён')
    ok(len(bal_hosts) == 1 and bal_hosts[0]['xrayJsonTemplateUuid'] == detpl['uuid'] and not bal_hosts[0]['isHidden']
       and bal_hosts[0]['remark'] == '🇩🇪 Германия', f'один видимый хост-балансировщик: {[(h["remark"]) for h in bal_hosts]}')
    prem = world.squads['22222222-2222-2222-2222-222222222222']['inbounds']
    ok(len(prem) == 2, 'inbound нод добавлены в выбранный сквад')
    # ещё одна нода в DE: шаблон и балансировщик переиспользуются
    st, dj = await wapi('domains/auto', 'POST', {'pid': pid, 'location': 'de', 'count': 1})
    st, js = await wapi('deploy', 'POST', {'pid': pid, 'location': 'de', 'idx': 0, 'period': '1m', 'domains': dj['domains'],
                                           'host_mode': 'balancer', 'balancer_tag': 'DE'})
    await runner.wait_all()
    ok(store.job(js['ids'][0])['status'] == 'done' and sum(1 for t in world.templates if t['name'] == 'Balancer_de') == 1
       and sum(1 for h in world.hosts if h.get('xrayJsonTemplateUuid') == detpl['uuid']) == 1,
       'третья нода: шаблон и хост-балансировщик не дублируются')
    st, js = await wapi('deploy', 'POST', {'pid': pid, 'location': 'de', 'idx': 0, 'period': '1m', 'domains': ['zz9.example.com'],
                                           'host_mode': 'balancer', 'balancer_tag': 'de-1'})
    ok(st == 400 and 'тег' in js['detail'], 'неверный тег балансировщика отклонён')

    # ── несколько локаций сразу: общий балансировщик NORMAL, домены из списка зон Cloudflare
    world.zones['example.net'] = 'z2'
    st, zs = await wapi(f'profiles/{pid}/zones')
    ok(zs['zones'][0] == 'example.com' and 'example.net' in zs['zones'] and zs['default'] == 'example.com' and not zs['error'],
       f'зоны Cloudflare для выбора: {zs}')
    st, d1 = await wapi('domains/auto', 'POST', {'pid': pid, 'location': 'de', 'count': 2, 'base': 'example.net'})
    st, d2 = await wapi('domains/auto', 'POST', {'pid': pid, 'location': 'nl', 'count': 1, 'base': 'example.net', 'exclude': d1['domains']})
    ok(d1['domains'] == ['de1.example.net', 'de2.example.net'] and d2['domains'] == ['nl1.example.net'], 'имена на выбранном домене')
    async with BD.session() as s_:
        await BD.change_balance(s_, 1, 100_000, 'admin_adjust')
    hb, tb = len(world.hosts), len(world.templates)
    st, js = await wapi('deploy', 'POST', {'pid': pid, 'period': '1m', 'host_mode': 'normal', 'tags': 'NORMAL', 'remark': '{flag} {country} {n}',
                                           'items': [{'location': 'de', 'idx': 0, 'domains': d1['domains']},
                                                     {'location': 'nl', 'idx': 1, 'domains': d2['domains']}]})
    ok(st == 200 and len(js['ids']) == 3, f'деплой в две локации разом: {js}')
    await runner.wait_all()
    mj = [store.job(i) for i in js['ids']]
    ok(all(j['status'] == 'done' for j in mj) and sorted(j['data']['location'] for j in mj) == ['de', 'de', 'nl']
       and [j['data']['idx'] for j in mj if j['data']['location'] == 'nl'] == [1], f'все ноды готовы, у каждой своя локация и тариф: {[j["error"] for j in mj]}')
    nh = world.hosts[hb:]
    ok(len(nh) == 3 and all(not h['isHidden'] and h['tags'] == ['NORMAL'] and not h.get('xrayJsonTemplateUuid') for h in nh)
       and len(world.templates) == tb, 'режим NORMAL: хосты видимые с тегом NORMAL, шаблоны и балансировщики не создаются')
    ok({h['remark'] for h in nh} == {'🇩🇪 Германия 1', '🇩🇪 Германия 2', '🇳🇱 Нидерланды 1'}, f'названия хостов: {[h["remark"] for h in nh]}')
    st, js = await wapi('deploy', 'POST', {'pid': pid, 'period': '1m', 'items': [
        {'location': 'de', 'idx': 0, 'domains': ['q1.example.net']}, {'location': 'nl', 'idx': 0, 'domains': ['q1.example.net']}]})
    ok(st == 400 and 'повторяются' in js['detail'], 'один домен в двух локациях отклонён')

    # ── ручные домены: чужая зона и успешная попытка с ошибкой установщика
    await tap('m:deploy')
    out = await tap(f'd:p:{pid}')
    await tap('d:l:nl')
    out = await tap('d:o:0')
    await tap('d:t:1m') if 'Срок' in last(out) else None
    await tap('d:c:1')
    await tap('d:dom:manual')
    out = await say('node.other.org')
    ok('зона' in last(out) and '❌' in last(out), 'домен вне зон Cloudflare отклонён')
    out = await say('de2.example.com')
    ok('уже есть в задачах' in last(out), 'занятый домен отклонён')
    await say('nl7.example.com')
    nxt = f"198.51.100.{max(astra.made) - 500 + 1}"
    behaviour[nxt] = 'unknown'
    await tap('d:go')
    await runner.wait_all()
    j = store.jobs(1)[0]
    ok(j['status'] == 'failed' and 'неизвестный вопрос' in j['error'] and j['stage'] == 'install',
       f'незнакомый вопрос установщика останавливает задачу: {j["error"][:80]}')
    ok('Повторить' in buttons(), 'у упавшей задачи есть кнопка «Повторить»')
    bought = len(astra.made)
    behaviour[nxt] = 'ok'
    await tap(f'j:{j["id"]}:retry')
    await runner.wait_all()
    j = store.job(j['id'])
    ok(j['status'] == 'done' and len(astra.made) == bought, 'повтор доустановил ноду без новой покупки')
    ok(world.nodes[-1]['countryCode'] == 'NL', 'ещё одна нода в панели')

    # ── перезапуск бота посреди задачи: продолжение
    jid = store.add_job('b', pid, 1, {**j['data'], 'domain': 'nl8.example.com', 'name': 'nl8', 'server_id': None,
                                      'ip': None, 'password': 'Aa1aaaaaaaaaaaaaaaaa'})
    store.update_job(jid, status='buying')  # «упали» во время покупки
    runner2 = Runner(store, bot, TMP / 'logs', deploy=functools.partial(engine.deploy, check_tries=1, check_every=0),
                     poll_every=0.05, edit_every=0)
    await runner2.resume_all()
    await runner2.wait_all()
    jb = store.job(jid)
    ok(jb['status'] == 'failed' and 'перезапустился во время покупки' in jb['error'],
       'неизвестный итог покупки после перезапуска — не покупаем вслепую второй раз')
    async with BD.session() as s_:
        await BD.change_balance(s_, 1, 100_000, 'admin_adjust')
    st, js = await wapi(f'jobs/{jid}/retry', 'POST')
    ok(st == 200 and js['rebuy'] is True, 'повтор из админки предупреждает о новой покупке')
    await runner.wait_all()
    ok(store.job(jid)['status'] == 'done', f'после повтора из админки нода готова: {store.job(jid)["error"]}')
    st, js = await wapi(f'jobs/{jid}/hide', 'POST')
    st, jl = await wapi('jobs')
    ok(jid not in [x['id'] for x in jl['jobs']], 'скрытая задача пропала из списка')

    out = await tap('m:jobs')
    ok('Задачи' in last(out), 'список задач')
    await tap(f'j:{jobs[0]["id"]}:log')
    ok(sess.sent[-2][0] == 'SendDocument', 'журнал отправляется файлом')

    await web.close()
    srv.shutdown()
    print(f'FAILS: {fails}')
    return fails


if __name__ == '__main__':
    sys.exit(1 if asyncio.run(main()) else 0)
