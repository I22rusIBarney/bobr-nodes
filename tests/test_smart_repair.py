"""Починка с учётом проверки соседей, замена в другой локации, проверка нового IP до установки, расходы по хостерам."""
import asyncio

import pytest

from bot import blockstats as bs
from bot import checker as ck
from bot import monitor as M
from bot.runner import Fail, Runner
from bot.store import Store

DAY = 86400


def setup(tmp_path):
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'g', 'panel_url': 'https://p', 'panel_token': 't', 'mon_mode': 'auto'})
    return store, store.profile(pid), M.Monitor(store, None, None, {1})


def test_subnet_ban_skips_useless_steps(tmp_path):
    store, prof, mon = setup(tmp_path)
    st = {'addr': 'a.example.com', 'uuid': 'u', 'name': 'A', 'since': 1000, 'status': 'down', 'steps_done': [], 'tok': 't1'}
    store.set_mon(f'{prof["id"]}:a.example.com', st)
    store.block_open(prof['id'], 'a.example.com', start=1000, cause='ru_block')
    ran, said = [], []

    async def run_step(prof_, st_, step, cfg):
        ran.append(step)
        return 1, 'ok'

    async def say(prof_, text, markup=None, st=None, type_='node.repair'):
        said.append(text)
    mon.run_step, mon.say = run_step, say
    assert mon.neigh_of(prof, st) == ''
    asyncio.run(mon.next_step(prof, dict(st)))
    assert ran == ['mask']                                   # соседи не проверены — обычный порядок
    store.block_update(prof['id'], 'a.example.com', neigh='subnet')
    ran.clear()
    said.clear()
    asyncio.run(mon.next_step(prof, dict(st)))
    assert ran == ['xhttp'] and 'подсети' in said[0]         # маскировка и переустановка на том же IP пропущены
    s2 = {**st, 'steps_done': ['xhttp']}
    asyncio.run(mon.next_step(prof, s2))
    assert s2['status'] == 'confirm' and s2['step'] == 'replace'   # смену IP пропустили, сразу — новая нода


def test_replace_suggests_better_location(tmp_path):
    store, prof, mon = setup(tmp_path)
    now = __import__('time').time()
    for i in range(4):  # в de часто блокируют, в nl — нет
        jid = store.add_job('b', prof['id'], 1, {'ip': f'1.1.1.{i}', 'domain': f'd{i}', 'location': 'de' if i < 2 else 'nl'})
        store.update_job(jid, status='done')
    store.db.execute('UPDATE jobs SET updated_at=?', (now - 60 * DAY,))
    for i in range(6):
        store.block_open(prof['id'], f'x{i}', start=now - (i + 1) * DAY, location='de', cause='ru_block', born=now - 60 * DAY)
        store.block_close(prof['id'], f'x{i}', 'reip', end=now - (i + 1) * DAY + 3600)
    cat = {'locations': [{'code': 'de', 'name': 'Германия', 'flag': '🇩🇪', 'offers': [{'idx': 0, 'price_kop': 500, 'cpu': 1, 'ram': 1, 'disk': 10}]},
                         {'code': 'nl', 'name': 'Нидерланды', 'flag': '🇳🇱', 'offers': [{'idx': 3, 'price_kop': 400, 'cpu': 1, 'ram': 1, 'disk': 10}]},
                         {'code': 'pl', 'name': 'Польша', 'flag': '🇵🇱', 'offers': []}]}
    alts, suggest, why = mon.replace_alts(prof, {'addr': 'a', 'since': now}, cat, 'de')
    assert [a['loc'] for a in alts] == ['de', 'nl'] and suggest == 'nl' and 'Нидерланды' in why   # без тарифов — не предлагаем
    assert M.clean_choice({'zone': 'x.com', 'squads': ['s'], 'loc': 'NL'})['loc'] == 'nl'
    with pytest.raises(ValueError):
        M.clean_choice({'zone': 'x.com', 'squads': ['s'], 'loc': '../x'})


def test_ip_check_votes(tmp_path, monkeypatch):
    store, prof, mon = setup(tmp_path)
    assert asyncio.run(mon.ip_check('5.9.1.2')) is None                       # чекеров нет — сказать нельзя
    for i in (1, 2):
        store.save_checker({'name': f'c{i}', 'ip': f'10.0.0.{i}', 'status': 'running'})
    mon.keyfiles = lambda: ('k', 'p', 'kh')

    async def tcp(ip, port, timeout):
        return port == 22
    monkeypatch.setattr('bot.diagnose.tcp_open', tcp)
    answers = []

    class R:
        def __init__(self, ch):
            self.ch = ch

        def run(self, cmd, timeout):
            answers.append(self.ch['name'])
            return 0, ('OK' if self.ch['name'] == ok_on else 'NO') + ' 5.9.1.2 22\n'
    monkeypatch.setattr(ck, 'remote', lambda ch, *a: R(ch))
    ok_on = 'none'
    assert asyncio.run(mon.ip_check('5.9.1.2'))['blocked'] is True and answers == ['c1', 'c2']
    answers.clear()
    ok_on = 'c1'
    assert asyncio.run(mon.ip_check('5.9.1.2'))['blocked'] is False and answers == ['c1']


class BC:
    def __init__(self):
        self.ip, self.changed = '5.9.1.2', 0

    async def server(self, sid, sync=False):
        return {'ip': self.ip, 'change_ip_price_kop': 9900}

    async def change_ip(self, sid):
        self.changed += 1
        self.ip = '5.9.1.3'
        return {}


def test_precheck_stop_and_reip(tmp_path):
    store, prof, mon = setup(tmp_path)
    runner = Runner(store, None, tmp_path / 'logs', poll_every=0.01)
    blocked = {'5.9.1.2'}

    async def ip_check(ip):
        return {'blocked': ip in blocked, 'checkers': 2}
    runner.ip_check = ip_check
    jid = store.add_job('b', prof['id'], 1, {'ip': '5.9.1.2', 'server_id': 7, 'domain': 'd'})
    with pytest.raises(Fail, match='уже заблокирован'):
        asyncio.run(runner._precheck(jid, store.job(jid)['data'], prof, BC()))
    assert store.job(jid)['data']['ip'] is None                 # «Повторить» возьмёт IP из Bobr Cloud заново
    store.save_profile({**prof, 'mon_precheck': 'reip'})
    prof = store.profile(prof['id'])
    store.patch_job_data(jid, ip='5.9.1.2')
    bc = BC()
    d = asyncio.run(runner._precheck(jid, store.job(jid)['data'], prof, bc))
    assert bc.changed == 1 and d['ip'] == '5.9.1.3' and d['ip_checked'] == '5.9.1.3' and d['precheck_kop'] == 9900
    blocked.add('5.9.1.3')                                      # второй раз не меняем — останавливаемся
    store.patch_job_data(jid, ip='5.9.1.3', ip_checked=None)
    with pytest.raises(Fail):
        asyncio.run(runner._precheck(jid, store.job(jid)['data'], prof, bc))
    assert bc.changed == 1
    store.save_profile({**prof, 'mon_precheck': 'off'})
    store.patch_job_data(jid, ip='5.9.1.3')
    assert asyncio.run(runner._precheck(jid, store.job(jid)['data'], store.profile(prof['id']), bc))['ip'] == '5.9.1.3'


def test_costs_by_hoster():
    jobs = [{'id': 1, 'profile_id': 1, 'created_at': 1, 'data': {'kind': 'reip', 'charged_kop': 100, 'domain': 'a'}},
            {'id': 2, 'profile_id': 1, 'created_at': 2, 'data': {'server_id': 5, 'price_kop': 300, 'monitor': True, 'replaces': 'b', 'domain': 'n'}},
            {'id': 3, 'profile_id': 1, 'created_at': 3, 'data': {'server_id': 6, 'price_kop': 200, 'precheck_kop': 50, 'domain': 'c'}}]
    c = bs.costs(jobs, lambda j: {'a': 'Aeza', 'b': 'Aeza', 'c': 'Hetzner'}[j['data'].get('replaces') or j['data']['domain']])
    assert c['total_kop'] == 650 and c['hosters'] == [{'label': 'Aeza', 'kop': 400, 'n': 2}, {'label': 'Hetzner', 'kop': 50, 'n': 1}]
    assert bs.costs(jobs)['hosters'] == []


def test_update_node_stealth_keeps_xhttp_port(monkeypatch):
    """Уникальные настройки на стоящей ноде: порт, отпечаток и маска на хостах; хост запасного XHTTP порт не меняет."""
    from bot import engine
    patches = []
    pnode = {'uuid': 'n1', 'address': 'a.example.com', 'configProfile': {'activeConfigProfileUuid': 'p1', 'activeInbounds': ['i1', 'x1']}}
    prof_ = {'uuid': 'p1', 'name': 'NV Vision', 'config': {}, 'inbounds': [{'uuid': 'i1', 'tag': 'Vision-NV', 'port': 443},
                                                                          {'uuid': 'x1', 'tag': 'XHTTP-Vision-NV', 'port': 2083}]}
    hosts = [{'uuid': 'h1', 'remark': 'A', 'port': 443, 'inbound': {'configProfileInboundUuid': 'i1'}},
             {'uuid': 'h2', 'remark': 'A XHTTP', 'port': 2083, 'inbound': {'configProfileInboundUuid': 'x1'}}]

    class P:
        def __init__(self, *a, **k):
            pass

        def call(self, m, path, body=None):
            if m == 'PATCH':
                patches.append(body)
                return {}
            return [pnode] if path == '/api/nodes' else {'configProfiles': [prof_]}

        def node_hosts(self, *a, **k):
            return hosts
    monkeypatch.setattr(engine, 'Panel', P)
    monkeypatch.setattr(engine, 'stealth', lambda: {'port': 31000, 'fingerprint': 'safari', 'final_mask': {'tcp': ['m']}})
    monkeypatch.setattr(engine, 'Remote', lambda node, lf: object())
    for f in ('wait_ssh', 'add_keys'):
        monkeypatch.setattr(engine, f, lambda *a, **k: None)
    monkeypatch.setattr(engine, 'install_extras', lambda *a, **k: {})
    ports = []
    monkeypatch.setattr(engine, 'setup_port', lambda r, public, target, log: ports.append((public, target)))
    engine.update_node({'node_uuid': 'n1', 'ip': '5.9.1.2', 'domains': ['a.example.com'], 'stealth': True},
                       {'panel_url': 'x', 'panel_token': 't'}, None, lambda t: None, lambda s: None)
    assert ports == [(31000, 443)]
    by = {}
    for b in patches:
        by.setdefault(b['uuid'], {}).update({k: v for k, v in b.items() if k != 'uuid'})
    assert by['h1'] == {'port': 31000, 'fingerprint': 'safari', 'finalMask': {'tcp': ['m']}}
    assert 'port' not in by['h2'] and by['h2']['fingerprint'] == 'safari'
