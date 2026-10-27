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
        self.squads = {'11111111-1111-1111-1111-111111111111': {'name': 'Default', 'inbounds': []},
                       '22222222-2222-2222-2222-222222222222': {'name': 'Premium', 'inbounds': []}}
        self.templates: list[dict] = []
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

        def do_GET(self):
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
                return self.send(200, {'response': W.hosts})
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
            if self.path.startswith('/client/v4'):
                return self.cf('POST')
            if not self.guard():
                return
            b, p = self.body(), self.path
            with W.lock:
                if p == '/api/config-profiles':
                    assert 'Vision-TLS' in json.dumps(b['config'])
                    pr = dict(uuid=str(uuid.uuid4()), name=b['name'], config=b['config'],
                              inbounds=[dict(uuid=str(uuid.uuid4()), tag='Vision-TLS')])
                    W.profiles.append(pr)
                    return self.send(201, {'response': pr})
                if p == '/api/nodes':
                    assert 3 <= len(b['name']) <= 30 and b['configProfile']['activeInbounds']
                    n = dict(uuid=str(uuid.uuid4()), **b)
                    W.nodes.append(n)
                    return self.send(201, {'response': n})
                if p == '/api/subscription-templates':
                    assert b['templateType'] == 'XRAY_JSON'
                    t = dict(uuid=str(uuid.uuid4()), name=b['name'], templateType=b['templateType'], templateJson=None, tags=[])
                    W.templates.append(t)
                    return self.send(201, {'response': t})
                if p == '/api/hosts':
                    assert b['alpn'] == 'h2,http/1.1' and b['securityLayer'] == 'TLS' and b['sni'] == b['address']
                    import re as _re
                    assert all(_re.fullmatch(r'[A-Z0-9_:]+', t) for t in b.get('tags', []))
                    h = {'uuid': str(uuid.uuid4()), 'xrayJsonTemplateUuid': None, 'isHidden': False, 'tags': [], **b}
                    W.hosts.append(h)
                    return self.send(201, {'response': h})
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
        def run(self, cmd, timeout=120):
            ip = self.n['ip']
            assert self.n.get('ssh_password'), 'нет пароля root'
            if cmd == 'id -u':
                return 0, '0\n'
            if 'authorized_keys' in cmd:
                return 0, ''
            if 'NV_DONE' in cmd:
                return 0, 'NV_DONE\n' if ip in installed else ''
            if cmd.startswith('cat /opt/remnanode/vision-config-profile.json'):
                return 0, json.dumps({'inbounds': [{'tag': 'Vision-TLS', 'port': 443}], 'domain': self.n['domain']})
            if '--check' in cmd:
                return 0, 'rc=0\n'
            raise AssertionError(cmd)

        def spawn(self, cmd, tty, timeout):
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
