"""Поддельные Cloudflare, панель Remnawave (HTTP на localhost), Telegram (сессия aiogram) и SSH-сервер."""
from __future__ import annotations

import datetime
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
GH_SHA = 'a318e2c8baf3e1a4b77b27b7a56dc349b36a711b'


class World:
    """Общее состояние фейков."""
    def __init__(self, secret_key: str, cookie: str = 'secretcookie=abc', token: str = 'panel-token-123456'):
        self.secret_key = secret_key
        self.cookie, self.token = cookie, token
        self.zones = {'example.com': 'z1'}
        self.records: list[dict] = []
        self.profiles: list[dict] = []
        self.nodes: list[dict] = []
        self.hosts: list[dict] = []
        self.reorders = 0
        self.squads = {'11111111-1111-1111-1111-111111111111': {'name': 'Default', 'inbounds': []},
                       '22222222-2222-2222-2222-222222222222': {'name': 'Premium', 'inbounds': []}}
        self.templates: list[dict] = []
        self.gh_files: dict[str, bytes] = {}       # путь → содержимое (репозиторий acme/node-scripts на коммите GH_SHA)
        self.gemini_replies: list[dict] = []       # ответы поддельного Gemini по очереди
        self.gemini_calls: list[dict] = []
        self.decoy = None  # без cookie: None — 404 JSON, 'html' — 200 со страницей-заглушкой, 'redirect' — 302
        self.lock = threading.Lock()


def make_server(world: World) -> ThreadingHTTPServer:
    W = world

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def body(self):
            n = int(self.headers.get('Content-Length', 0))
            return json.loads(self.rfile.read(n)) if n else {}

        # ── Cloudflare ──
        def cf(self, method):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            p = u.path.removeprefix('/client/v4')
            ok = lambda res: self.send(200, {'success': True, 'result': res})  # noqa: E731
            if self.headers.get('Authorization') != 'Bearer cf-token-123456':
                return self.send(403, {'success': False, 'errors': [{'message': 'bad token'}]})
            if p == '/user/tokens/verify':
                return ok({'status': 'active'})
            if p == '/zones' and 'name' not in q:
                return ok([{'id': i, 'name': n} for n, i in W.zones.items()])
            if p == '/zones':
                z = W.zones.get(q.get('name', ''))
                return ok([{'id': z, 'name': q['name']}] if z else [])
            parts = p.strip('/').split('/')
            with W.lock:
                if method == 'GET' and parts[-1] == 'dns_records':
                    return ok([r for r in W.records if r['name'] == q.get('name')])
                if method == 'POST':
                    r = {**self.body(), 'id': uuid.uuid4().hex}
                    W.records.append(r)
                    return ok(r)
                if method == 'PUT':
                    r = next(x for x in W.records if x['id'] == parts[-1])
                    r.update(self.body())
                    return ok(r)
                if method == 'DELETE':
                    W.records[:] = [x for x in W.records if x['id'] != parts[-1]]
                    return ok({})
            self.send(404, {'success': False})

        # ── панель ──
        def guard(self):
            if W.cookie not in (self.headers.get('Cookie') or ''):
                if W.decoy == 'html':
                    b = b'<html><head><title>Welcome to nginx!</title></head><body>hi</body></html>'
                    self.send_response(200); self.send_header('Content-Type', 'text/html')
                    self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
                elif W.decoy == 'redirect':
                    self.send_response(302); self.send_header('Location', 'https://google.com/'); self.send_header('Content-Length', '0'); self.end_headers()
                else:
                    self.send(404, {'m': 'no cookie'})
                return False
            if self.headers.get('Authorization') != f'Bearer {W.token}':
                self.send(401, {})
                return False
            return True

        def raw(self, code, body: bytes, ctype='text/plain'):
            self.send_response(code); self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

        def github(self):
            p = urlparse(self.path).path
            if p.startswith('/gh-raw/acme/node-scripts/' + GH_SHA + '/'):
                f = W.gh_files.get(p.split(GH_SHA + '/', 1)[1])
                return self.raw(200, f) if f is not None else self.raw(404, b'404')
            if p == '/gh-api/repos/acme/node-scripts':
                return self.send(200, {'default_branch': 'main'})
            if p == '/gh-api/repos/acme/node-scripts/commits/main':
                return self.send(200, {'sha': GH_SHA})
            if p == f'/gh-api/repos/acme/node-scripts/git/trees/{GH_SHA}':
                return self.send(200, {'tree': [{'path': k, 'type': 'blob', 'size': len(v)} for k, v in W.gh_files.items()]})
            return self.send(404, {'message': 'Not Found'})

        def gemini(self):
            if self.headers.get('x-goog-api-key') != 'gem-key-123456':
                return self.send(400, {'error': {'message': 'API key not valid'}})
            p = urlparse(self.path).path
            if p == '/gemini/v1beta/models':
                return self.send(200, {'models': [
                    {'name': 'models/gemini-2.5-flash', 'supportedGenerationMethods': ['generateContent']},
                    {'name': 'models/gemini-3.0-pro', 'supportedGenerationMethods': ['generateContent']},
                    {'name': 'models/embedding-001', 'supportedGenerationMethods': ['embedContent']}]})
            if p.endswith(':generateContent'):
                b = self.body()
                W.gemini_calls.append({'model': p.split('/')[-1].split(':')[0], 'body': b})
                reply = W.gemini_replies.pop(0)
                return self.send(200, {'candidates': [{'content': {'parts': [{'text': json.dumps(reply, ensure_ascii=False)}]}}]})
            return self.send(404, {})

        def do_GET(self):
            if self.path.startswith('/gh-'):
                return self.github()
            if self.path.startswith('/gemini'):
                return self.gemini()
            if self.path.startswith('/client/v4'):
                return self.cf('GET')
            if not self.guard():
                return
            p = self.path
            if p == '/api/keygen':
                return self.send(200, {'response': {'secretKey': W.secret_key}})
            if p == '/api/config-profiles':
                return self.send(200, {'response': {'total': len(W.profiles), 'configProfiles': W.profiles}})
            if p == '/api/nodes':
                return self.send(200, {'response': W.nodes})
            if p == '/api/hosts':
                return self.send(200, {'response': sorted(W.hosts, key=lambda x: x['viewPosition'])})
            if p == '/api/internal-squads':
                return self.send(200, {'response': {'total': len(W.squads), 'internalSquads': [
                    dict(uuid=u, name=v['name'], info={'membersCount': 0, 'inboundsCount': len(v['inbounds'])}, inbounds=v['inbounds'])
                    for u, v in W.squads.items()]}})
            if p == '/api/subscription-templates':
                return self.send(200, {'response': {'total': len(W.templates), 'templates': W.templates}})
            if p.startswith('/api/internal-squads/'):
                u = p.rsplit('/', 1)[1]
                return self.send(200, {'response': dict(uuid=u, **W.squads[u])})
            self.send(404, {})

        def do_POST(self):
            if self.path.startswith('/gemini'):
                return self.gemini()
            if self.path.startswith('/client/v4'):
                return self.cf('POST')
            if not self.guard():
                return
            b, p = self.body(), self.path
            with W.lock:
                if p == '/api/config-profiles':
                    tags = [i['tag'] for i in b['config'].get('inbounds', [])]
                    taken = {i['tag'] for x in W.profiles for i in x['inbounds']}
                    if set(tags) & taken:  # как в настоящей панели: теги inbound уникальны
                        return self.send(409, {'message': 'Inbounds with same tag already exists in database. Inbound tags must be unique.'})
                    if any(x['name'] == b['name'] for x in W.profiles):
                        return self.send(409, {'message': 'Config profile with this name already exists'})
                    pr = dict(uuid=str(uuid.uuid4()), name=b['name'], config=b['config'],
                              inbounds=[dict(uuid=str(uuid.uuid4()), tag=t) for t in tags])
                    W.profiles.append(pr)
                    return self.send(201, {'response': pr})
                if p == '/api/nodes':
                    assert 3 <= len(b['name']) <= 30 and b['configProfile']['activeInbounds']
                    n = dict(uuid=str(uuid.uuid4()), isConnected=True, lastStatusMessage=None, **b)
                    W.nodes.append(n)
                    return self.send(201, {'response': n})
                if p == '/api/subscription-templates':
                    assert b['templateType'] == 'XRAY_JSON'
                    t = dict(uuid=str(uuid.uuid4()), name=b['name'], templateType=b['templateType'], templateJson=None, tags=[])
                    W.templates.append(t)
                    return self.send(201, {'response': t})
                if p == '/api/hosts':
                    if b.get('securityLayer') == 'TLS':
                        assert b['alpn'] == 'h2,http/1.1' and b['sni'] == b['address']
                    else:
                        assert b['securityLayer'] == 'DEFAULT' and 'alpn' not in b and 'sni' not in b
                    import re as _re
                    assert all(_re.fullmatch(r'[A-Z0-9_:]+', t) for t in b.get('tags', []))
                    h = {'uuid': str(uuid.uuid4()), 'xrayJsonTemplateUuid': None, 'isHidden': False, 'tags': [], **b,
                         'viewPosition': max([x['viewPosition'] for x in W.hosts], default=0) + 1}
                    W.hosts.append(h)
                    return self.send(201, {'response': h})
                if p == '/api/hosts/actions/reorder':
                    by = {x['uuid']: x for x in W.hosts}
                    for x in b['hosts']:
                        assert set(x) == {'uuid', 'viewPosition'} and isinstance(x['viewPosition'], int)
                        by[x['uuid']]['viewPosition'] = x['viewPosition']
                    W.reorders += 1
                    return self.send(200, {'response': {'isUpdated': True}})
            self.send(404, {})

        def do_PUT(self):
            return self.cf('PUT')

        def do_DELETE(self):
            return self.cf('DELETE')

        def do_PATCH(self):
            if not self.guard():
                return
            b, p = self.body(), self.path
            with W.lock:
                if p == '/api/config-profiles':
                    pr = next(x for x in W.profiles if x['uuid'] == b['uuid'])
                    pr['config'] = b['config']
                    return self.send(200, {'response': pr})
                if p == '/api/nodes':
                    return self.send(200, {'response': {}})
                if p == '/api/subscription-templates':
                    t = next(x for x in W.templates if x['uuid'] == b['uuid'])
                    t['templateJson'] = b['templateJson']
                    return self.send(200, {'response': t})
                if p == '/api/internal-squads':
                    W.squads[b['uuid']]['inbounds'] = [dict(uuid=x) for x in b['inbounds']]
                    return self.send(200, {'response': {}})
            self.send(404, {})

    srv = ThreadingHTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ───────────────────────── SSH ─────────────────────────

def make_fake_remote(engine, mock_sh: str, behaviour: dict):
    """Remote, который вместо SSH запускает локальную имитацию установщика с настоящими вопросами.
    behaviour[ip] = 'ok' | 'unknown' (установщик задаст незнакомый вопрос)."""
    import pexpect

    installed: set[str] = set()

    class FakeRemote(engine.Remote):
        calls: list = []
        post: list = []

        def run(self, cmd, timeout=120):
            ip = self.n['ip']
            assert self.n.get('ssh_password'), 'нет пароля root'
            if cmd == 'id -u':
                return 0, '0\n'
            if 'authorized_keys' in cmd:
                return 0, ''
            if 'NV_DONE' in cmd:
                return 0, 'NV_DONE\n' if ip in installed else ''
            if cmd.startswith('ln -sfn live/'):
                assert cmd.split()[2] == f'live/{self.n["domain"]}', cmd
                return 0, 'LINK_OK\n'
            if cmd.startswith('cat /opt/remnanode/vision-config-profile.json'):
                import sys
                sys.path.insert(0, os.environ['NV_SRC'])
                import runtime  # настоящий генератор профиля установщика
                return 0, json.dumps(runtime.profile({'domain': self.n['domain'], 'node_port': 2222,
                                                      'panel_ips': '203.0.113.10', 'email': 'a@b.cd'}))
            if '--check' in cmd:
                return 0, 'rc=0\n'
            if cmd.startswith('ufw ') or cmd.startswith('docker ps'):
                FakeRemote.post.append((ip, cmd))
                return 0, ''
            raise AssertionError(cmd)

        def spawn(self, cmd, tty, timeout):
            if 'bash ./s.sh' in cmd:  # скрипт из «Моих скриптов»
                import shlex as _sh
                assert 'sha256sum -c' in cmd and '/acme/node-scripts/' + GH_SHA + '/' in cmd, cmd
                args = _sh.split(cmd.split('bash ./s.sh', 1)[1])
                FakeRemote.calls.append({'ip': self.n['ip'], 'cmd': cmd, 'args': args})
                env = {**os.environ, 'MOCK_LOG': os.path.join(behaviour.get('tmp', '/tmp'), f'mock-{self.n["ip"]}.txt')}
                if behaviour.get(self.n['ip']) == 'unknown':
                    env['MOCK_EXTRA'] = 'ask'
                child = pexpect.spawn('bash', [os.path.join(HERE, 'mock_remnanode.sh'), *args], encoding='utf-8',
                                      timeout=30, dimensions=(50, 120), env=env)
                child.logfile_read = self.logfile
                return child
            assert tty and 'sha256sum -c' in cmd and engine.DEFAULT_SHA256 in cmd, cmd
            mode = behaviour.get(self.n['ip'], 'ok')
            child = pexpect.spawn('bash', [mock_sh, self.n['ip'], mode], encoding='utf-8', timeout=60,
                                  dimensions=(50, 120))
            child.logfile_read = self.logfile
            if mode == 'ok':
                installed.add(self.n['ip'])
            return child

    return FakeRemote


# ───────────────────────── Telegram ─────────────────────────

def fake_session():
    from aiogram.client.session.base import BaseSession
    from aiogram.methods import AnswerCallbackQuery, DeleteMessage, EditMessageText, SendDocument, SendMessage, \
        SetMyCommands
    from aiogram.types import Chat, Message

    class FakeSession(BaseSession):
        def __init__(self):
            super().__init__()
            self.sent: list = []       # (method_name, kwargs)
            self.messages: dict[int, str] = {}  # message_id -> текущий текст
            self.n = 1000

        async def close(self):
            pass

        async def stream_content(self, *a, **kw):  # pragma: no cover
            raise NotImplementedError

        def _msg(self, chat_id, text):
            self.n += 1
            self.messages[self.n] = text
            return Message(message_id=self.n, date=datetime.datetime.now(), chat=Chat(id=chat_id, type='private'),
                           text=text)

        async def make_request(self, bot, method, timeout=None):
            self.sent.append((type(method).__name__, method))
            if isinstance(method, SendMessage):
                return self._msg(method.chat_id, method.text)
            if isinstance(method, EditMessageText):
                if self.messages.get(method.message_id) == method.text:
                    from aiogram.exceptions import TelegramBadRequest
                    raise TelegramBadRequest(method=method, message='message is not modified')
                self.messages[method.message_id] = method.text
                return Message(message_id=method.message_id, date=datetime.datetime.now(),
                               chat=Chat(id=method.chat_id or 1, type='private'), text=method.text)
            if isinstance(method, SendDocument):
                return self._msg(method.chat_id, '[document]')
            if isinstance(method, (AnswerCallbackQuery, DeleteMessage, SetMyCommands)):
                return True
            return True

        def alerts(self):
            return [m.text for name, m in self.sent if name == 'AnswerCallbackQuery' and m.text]

    return FakeSession()
