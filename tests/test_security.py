from bot import engine


def test_ssh_pins_host_keys():
    a = engine.Remote({'ip': '192.0.2.1', 'known_hosts': '/data/ssh/known_hosts', 'ssh_password': 'x'}, None).argv('id', False)
    assert 'StrictHostKeyChecking=accept-new' in a and 'UserKnownHostsFile=/data/ssh/known_hosts' in a
    assert 'StrictHostKeyChecking=no' not in a
    legacy = engine.Remote({'ip': '192.0.2.1'}, None).argv('id', False)  # без файла — как раньше (тесты, проверки)
    assert 'StrictHostKeyChecking=no' in legacy


def test_hostkey_change_stops_login(monkeypatch):
    class Child:  # ssh отказался входить ещё до команды: сменился ключ сервера
        before = '@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\nHost key verification failed.\n'
        exitstatus = 255
        logfile_read = None
        sent: list = []

        def expect(self, patterns, timeout=None):
            return patterns.index(engine.pexpect.EOF)

        def sendline(self, s):
            self.sent.append(s)

        def close(self, force=False):
            pass
    monkeypatch.setattr(engine.pexpect, 'spawn', lambda *a, **kw: Child())
    r = engine.Remote({'ip': '192.0.2.1', 'known_hosts': '/tmp/kh', 'ssh_password': 'x'}, None)
    try:
        r.run('id -u')
    except engine.StepError as e:
        assert str(e) == engine.HOSTKEY_CHANGED and not Child.sent  # пароль не отправлен
    else:
        raise AssertionError('смена ключа не остановила вход')
