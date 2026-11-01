import pytest

from bot import engine, plugins


class FakePanel:
    """Панель с плагинами нод: хранит ноды и записи плагинов, пишет все запросы."""

    def __init__(self, nodes, plugs=None):
        self.nodes = nodes
        self.plugs = plugs or {}  # uuid -> {name, pluginConfig}
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if (method, path) == ('GET', '/api/nodes'):
            return [dict(n) for n in self.nodes]
        if (method, path) == ('GET', '/api/node-plugins'):
            return {'nodePlugins': [{'uuid': u, 'name': p['name'], 'pluginConfig': None} for u, p in self.plugs.items()]}
        if method == 'GET' and path.startswith('/api/node-plugins/'):
            u = path.rsplit('/', 1)[-1]
            return {'uuid': u, **self.plugs[u]}
        if (method, path) == ('POST', '/api/node-plugins'):
            self.plugs['p-new'] = {'name': body['name'], 'pluginConfig': {}}
            return {'uuid': 'p-new'}
        if (method, path) == ('PATCH', '/api/node-plugins'):
            self.plugs[body['uuid']]['pluginConfig'] = body['pluginConfig']
            return {'uuid': body['uuid']}
        if (method, path) == ('POST', '/api/node-plugins/actions/sync'):
            return {}
        if (method, path) == ('POST', '/api/nodes/bulk-actions/update'):
            for n in self.nodes:
                if n['uuid'] in body['uuids']:
                    n.update(body['fields'])
            return {}
        raise AssertionError(f'неожиданный запрос {method} {path}')


def node(uuid, plugin=None, disabled=False):
    return {'uuid': uuid, 'name': uuid.upper(), 'isDisabled': disabled, 'activePluginUuid': plugin}


def test_group_plugin_majority_and_exclude():
    ns = [node('a', 'p1'), node('b', 'p1'), node('c', 'p2'), node('d', 'p2', disabled=True), node('e', 'p2'), node('new')]
    assert plugins.group_plugin(ns) in ('p1', 'p2')
    assert plugins.group_plugin([node('a', 'p1'), node('b', 'p1'), node('c', 'p2')]) == 'p1'
    assert plugins.group_plugin([node('a'), node('b')]) is None
    assert plugins.group_plugin([node('new', 'p9')], exclude='new') is None


def test_adopt_binds_new_node_to_group_plugin():
    pl = FakePanel([node('a', 'p1'), node('b', 'p1'), node('new')])
    log = []
    plugins.adopt(pl, 'new', pl.nodes, log.append)
    assert pl.nodes[2]['activePluginUuid'] == 'p1' and log


def test_adopt_noop_without_plugins_and_survives_errors():
    pl = FakePanel([node('a'), node('new')])
    plugins.adopt(pl, 'new', pl.nodes, lambda m: None)
    assert pl.calls == []

    class Broken(FakePanel):
        def call(self, method, path, body=None):
            raise engine.StepError('HTTP 500')
    log = []
    plugins.adopt(Broken([node('a', 'p1'), node('new')]), 'new', [node('a', 'p1'), node('new')], log.append)
    assert '⚠️' in log[0]


def test_engine_attach_adopts_plugin_for_created_node():
    """Нода, созданная ботом, сразу под плагином группы — иначе торренты на ней не блокируются."""
    class P(engine.Panel):
        def __init__(self):
            self.nodes = [{**node('old', 'p1'), 'address': '198.51.100.1'}]
            self.bound = None

        def call(self, method, path, body=None):
            if (method, path) == ('GET', '/api/nodes'):
                return [dict(n) for n in self.nodes]
            if (method, path) == ('POST', '/api/nodes'):
                self.nodes.append({**node('fresh'), 'address': body['address']})
                return {'uuid': 'fresh'}
            if (method, path) == ('POST', '/api/nodes/bulk-actions/update'):
                self.bound = body
                return {}
            if (method, path) == ('GET', '/api/hosts'):
                return []
            if path == '/api/subscription-templates':
                return {'templates': []}
            if (method, path) == ('POST', '/api/hosts'):
                return {'uuid': 'h1'}
            return {}

    p = P()
    nd = {'name': 'n1', 'ip': '203.0.113.5', 'domain': '', 'country': 'DE'}
    prof = {'uuid': 'cp1', 'name': 'prof'}
    inbound = {'uuid': 'in1', 'tag': 'VLESS', 'port': 443}
    try:
        p.attach(nd, prof, [inbound], [], 2222, lambda m: None)
    except (KeyError, TypeError, engine.StepError):  # хосты в упрощённой панели могут не собраться; важна привязка
        pass
    assert p.bound == {'uuids': ['fresh'], 'fields': {'activePluginUuid': 'p1'}}


def test_state_picks_used_plugin_and_counts():
    pl = FakePanel([node('a', 'p1'), node('b'), node('c', 'p2'), node('d', None, disabled=True)],
                   {'p1': {'name': 'Мой', 'pluginConfig': {'torrentBlocker': {'enabled': True}}},
                    'p2': {'name': 'Чужой', 'pluginConfig': {}}})
    st = plugins.state(pl)
    assert st['uuid'] in ('p1', 'p2') and st['total'] == 3
    pl.nodes[1]['activePluginUuid'] = 'p1'
    st = plugins.state(pl)
    assert st['uuid'] == 'p1' and st['bound'] == 2 and st['foreign'] == ['C']
    assert st['config']['torrentBlocker']['enabled'] is True
    assert st['config']['ingressFilter'] == {'enabled': False, 'blockedIps': []}


def test_save_creates_plugin_and_binds_free_nodes_only():
    pl = FakePanel([node('a'), node('b', 'other'), node('c', disabled=True)], {'other': {'name': 'X', 'pluginConfig': {}}})
    pl.plugs.clear()  # плагина 'other' в панели уже нет, но у ноды он записан — её не трогаем без согласия
    cfg = plugins.full_config({})
    cfg['torrentBlocker']['enabled'] = True
    plugins.save(pl, cfg)
    assert pl.plugs['p-new']['name'] == plugins.NAME
    assert pl.plugs['p-new']['pluginConfig']['torrentBlocker']['enabled'] is True
    assert [n['activePluginUuid'] for n in pl.nodes] == ['p-new', 'other', None]
    plugins.save(pl, cfg, take_foreign=True)
    assert pl.nodes[1]['activePluginUuid'] == 'p-new'


def test_full_config_keeps_unknown_keys():
    cfg = plugins.full_config({'torrentBlocker': {'enabled': True, 'ignoreLists': {'ip': ['1.2.3.4']}}, 'future': {'x': 1}})
    assert cfg['future'] == {'x': 1}
    assert cfg['torrentBlocker']['ignoreLists'] == {'ip': ['1.2.3.4'], 'userId': []}
    assert cfg['torrentBlocker']['blockDuration'] == 3600


def test_clean_lists():
    assert plugins.clean_ips('1.2.3.4\n10.0.0.0/8, 1.2.3.4') == ['1.2.3.4', '10.0.0.0/8']
    assert plugins.clean_ips('2001:db8::/32', v6=True) == ['2001:db8::/32']
    with pytest.raises(ValueError):
        plugins.clean_ips('2001:db8::/32')  # входящий фильтр — только IPv4
    with pytest.raises(ValueError):
        plugins.clean_ips('300.1.1.1')
    assert plugins.clean_ports('587 25\n25') == [25, 587]
    with pytest.raises(ValueError):
        plugins.clean_ports('70000')


def test_swap_keeps_manual_and_other_presets():
    cur = ['1.0.0.0/8', 'm1', 'shared', 'other']
    assert plugins.swap(cur, ['1.0.0.0/8', 'shared'], ['2.0.0.0/8'], protected={'shared'}) == ['m1', 'shared', 'other', '2.0.0.0/8']
    assert plugins.swap(cur, ['1.0.0.0/8'], []) == ['m1', 'shared', 'other']


def test_fetch_preset_filters_garbage():
    class R:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    class C:
        def get(self, url):
            return R('# comment\n1.2.3.0/24\n\n5.6.7.8\nnot an ip\n')
    got = plugins.fetch_preset('ru', C())
    assert got == ['1.2.3.0/24', '5.6.7.8']

    class Empty(C):
        def get(self, url):
            return R('<html>404</html>')
    with pytest.raises(plugins.PluginError):
        plugins.fetch_preset('ru', Empty())


def test_autopilot_refreshes_changed_presets(tmp_path, monkeypatch):
    """Раз в сутки: изменившийся список сканеров уезжает в плагин, ручные адреса остаются."""
    import asyncio
    import json

    from bot import autopilot as ap
    from bot.store import Store

    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'g', 'panel_url': 'https://p', 'panel_token': 't'})
    pl = FakePanel([node('a', 'p1')], {'p1': {'name': plugins.NAME, 'pluginConfig': {
        'ingressFilter': {'enabled': True, 'blockedIps': ['1.1.1.0/24', '9.9.9.9']}}}})
    store.set(f'plug:{pid}', json.dumps({'ingress': {'ru': ['1.1.1.0/24']}, 'egress': {}}))
    monkeypatch.setattr(plugins, 'fetch_preset', lambda p: ['2.2.2.0/24'])
    pilot = ap.Autopilot(store, None, None, None)
    monkeypatch.setattr(pilot, 'panel', lambda prof: pl)
    asyncio.run(pilot.refresh_presets())
    assert pl.plugs['p1']['pluginConfig']['ingressFilter']['blockedIps'] == ['9.9.9.9', '2.2.2.0/24']
    assert json.loads(store.get(f'plug:{pid}'))['ingress']['ru'] == ['2.2.2.0/24']
    n = len(pl.calls)
    asyncio.run(pilot.refresh_presets())  # список не менялся — в панель не пишем
    assert not any(c[0] == 'PATCH' for c in pl.calls[n:])
