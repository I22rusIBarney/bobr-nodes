"""Вход через Россию (bot/relay.py): конфиг HAProxy, разбор ответа сервера, копии хостов."""
import base64
import re

import pytest

from bot import engine, relay


class FakeR:
    def __init__(self, out):
        self.out, self.cmds = out, []

    def run(self, cmd, timeout=120):
        self.cmds.append(cmd)
        return 0, self.out


def test_cfg_domain_and_ip():
    cfg = relay.haproxy_cfg(relay.entries_of([
        {'listen': 443, 'target': 'de1.example.com', 'target_port': 443},
        {'listen': 8443, 'target': '198.51.100.7', 'target_port': 2222}]))
    assert 'bind 0.0.0.0:443' in cfg and 'server node de1.example.com:443 resolvers sys init-addr libc,none' in cfg
    assert 'bind 0.0.0.0:8443' in cfg and 'server node 198.51.100.7:2222\n' in cfg
    assert 'mode tcp' in cfg and 'timeout tunnel' in cfg


def test_apply_cmd_carries_config():
    entries = [(443, 'de1.example.com', 443)]
    cmd = relay.apply_cmd(entries, closed=[8443])
    b64 = re.search(r'echo (\S+) \| base64 -d > /etc/bobr-relay/haproxy\.cfg\.new', cmd).group(1)
    assert base64.b64decode(b64).decode() == relay.haproxy_cfg(entries)
    assert relay.MARK in cmd and 'ufw delete allow "$p/tcp"' in cmd and 'for p in 8443' in cmd


@pytest.mark.parametrize('out, err', [
    ('RELAY_BUSY 443\n', 'занят'), ('RELAY_NOPKG\n', 'HAProxy'), ('RELAY_BADCFG\nline 3\n', 'конфиг'),
    ('RELAY_DOWN\nfailed\n', 'не запустилась'), ('что-то пошло не так\n', 'не настроен')])
def test_apply_errors(out, err):
    with pytest.raises(engine.StepError, match=err):
        relay.apply(FakeR(out), [{'listen': 443, 'target': 'x.example.com', 'target_port': 443}], lambda t: None)


def test_apply_reports_unreachable_node():
    log = []
    res = relay.apply(FakeR('RELAY_TARGET_FAIL x.example.com:443\nRELAY_OK\n'),
                      [{'listen': 443, 'target': 'x.example.com', 'target_port': 443}], log.append)
    assert res == {'bad': ['x.example.com:443']} and any('не открывается' in x for x in log)


class FakePanel:
    def __init__(self):
        self.posted = []

    def call(self, method, path, body=None):
        assert (method, path) == ('POST', '/api/hosts')
        self.posted.append(body)
        return {'uuid': f'h{len(self.posted)}'}


def test_clone_hosts_keeps_sni_and_skips_cdn():
    hosts = [{'uuid': 'a', 'remark': 'DE', 'address': 'de1.example.com', 'port': 443, 'securityLayer': 'TLS',
              'inbound': {'configProfileUuid': 'p', 'configProfileInboundUuid': 'i'}, 'finalMask': {'x': 1}},
             {'uuid': 'b', 'remark': 'DE CDN', 'address': 'cdn-de1.example.com', 'port': 443,
              'inbound': {'configProfileUuid': 'p', 'configProfileInboundUuid': 'j'}},
             {'uuid': 'c', 'remark': 'DE XHTTP', 'address': 'de1.example.com', 'port': 2083,
              'inbound': {'configProfileUuid': 'p', 'configProfileInboundUuid': 'k'}}]
    assert relay.target_port_of(hosts) == 443
    pl = FakePanel()
    made = relay.clone_hosts(pl, hosts, '192.0.2.90', 443, 443, '🇷🇺 DE', lambda t: None)
    assert made == ['h1'] and len(pl.posted) == 1
    b = pl.posted[0]
    assert b['address'] == '192.0.2.90' and b['port'] == 443 and b['sni'] == 'de1.example.com' and b['remark'] == '🇷🇺 DE'
    assert b['inbound'] == {'configProfileUuid': 'p', 'configProfileInboundUuid': 'i'} and b['finalMask'] == {'x': 1}
    with pytest.raises(engine.StepError, match='нет хостов'):
        relay.clone_hosts(FakePanel(), hosts, '192.0.2.90', 443, 9999, 'x', lambda t: None)
