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


def test_installer_forgets_changed_hostkey_once(monkeypatch):
    calls, forgot, logs = [], [], []

    def run(self, cmd, timeout=120):
        calls.append(cmd)
        if len(calls) == 1:
            raise engine.StepError(engine.HOSTKEY_CHANGED)
        return 0, '0\n'
    monkeypatch.setattr(engine.Remote, 'run', run)
    monkeypatch.setattr(engine, 'forget_host', lambda kh, h, p=22: forgot.append((kh, h, p)))
    r = engine.Remote({'ip': '192.0.2.1', 'known_hosts': '/tmp/kh', 'ssh_port': 2222}, None)
    engine.wait_ssh(r, logs.append, timeout=60)
    assert forgot == [('/tmp/kh', '192.0.2.1', 2222)] and calls.count('id -u') == 2
    assert any('забыт' in x for x in logs)


def test_installer_stops_if_hostkey_changes_again(monkeypatch):
    forgot = []

    def run(self, cmd, timeout=120):
        raise engine.StepError(engine.HOSTKEY_CHANGED)
    monkeypatch.setattr(engine.Remote, 'run', run)
    monkeypatch.setattr(engine, 'forget_host', lambda kh, h, p=22: forgot.append(h))
    r = engine.Remote({'ip': '192.0.2.1', 'known_hosts': '/tmp/kh'}, None)
    try:
        engine.wait_ssh(r, lambda m: None, timeout=60)
    except engine.StepError as e:
        assert str(e) == engine.HOSTKEY_CHANGED and forgot == ['192.0.2.1']
    else:
        raise AssertionError('повторная смена ключа не остановила установку')


def test_install_waits_for_unattended_upgrades():
    class R:
        def __init__(self, out):
            self.out, self.cmds = out, []

        def run(self, cmd, timeout=120):
            self.cmds.append(cmd)
            return 0, self.out
    logs = []
    r = R('APT_UU\nAPT_FREE 45\n')
    engine.free_apt(r, logs.append)
    assert 'pkill -TERM -x unattended-upgr' in r.cmds[0] and 'apt-daily-upgrade.timer' not in r.cmds[0]
    assert any('автообновления' in x and '45' in x for x in logs)
    logs.clear()
    engine.free_apt(R('APT_FREE 0\n'), logs.append)
    assert logs == []
    engine.free_apt(R('APT_BUSY\n'), logs.append)
    assert any('подождёт' in x for x in logs)
