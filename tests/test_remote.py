"""Вход по SSH без сети: поддельные ssh и sudo в PATH задают настоящие вопросы (пароль SSH, пароль sudo)."""
import os
import stat
import sys

import pytest

from bot import engine, sshaccess

FAKE_SSH = r'''#!/usr/bin/env python3
import os, subprocess, sys
args = sys.argv[1:]
open(os.environ['FAKE_LOG'], 'a').write(' '.join(args) + '\n')
cmd = args[-1]
want = os.environ.get('FAKE_SSH_PW')
if want:  # как настоящий ssh: три попытки
    for _ in range(3):
        sys.stderr.write("user@host's password: ")
        sys.stderr.flush()
        if sys.stdin.readline().rstrip('\n') == want:
            break
        sys.stderr.write('Permission denied, please try again.\n')
    else:
        sys.exit(255)
sys.exit(subprocess.call(['bash', '-c', cmd]))
'''

FAKE_SUDO = r'''#!/usr/bin/env bash
prompt='[sudo] password: '
while [ $# -gt 0 ]; do
  case "$1" in -S) shift;; -p) prompt="$2"; shift 2;; --) shift; break;; *) break;; esac
done
if [ -n "$FAKE_SUDO_DENY" ]; then echo "user is not in the sudoers file." >&2; exit 1; fi
if [ -n "$FAKE_SUDO_PW" ]; then
  ok=
  for i in 1 2 3; do
    printf '%s' "$prompt" >&2
    read -r pw
    if [ "$pw" = "$FAKE_SUDO_PW" ]; then ok=1; break; fi
    echo "Sorry, try again." >&2
  done
  [ -n "$ok" ] || exit 1
fi
FAKE_ROOT=1 exec "$@"
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    bin_ = tmp_path / 'bin'
    bin_.mkdir()
    for name, body in (('ssh', FAKE_SSH.replace('/usr/bin/env python3', sys.executable)), ('sudo', FAKE_SUDO)):
        p = bin_ / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv('PATH', f'{bin_}:{os.environ["PATH"]}')
    monkeypatch.setenv('FAKE_LOG', str(tmp_path / 'ssh.log'))
    for k in ('FAKE_SSH_PW', 'FAKE_SUDO_PW', 'FAKE_SUDO_DENY'):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def remote(**kw):
    return engine.Remote({'ip': 'h', **kw}, None)


def test_root_password(fake, monkeypatch):
    monkeypatch.setenv('FAKE_SSH_PW', 'pw')
    rc, out = remote(ssh_password='pw').run('echo hi; exit 3')
    assert rc == 3 and out.strip() == 'hi' and engine.READY not in out
    with pytest.raises(engine.StepError, match='не принял пароль'):
        remote(ssh_password='bad').run('echo hi')


def test_user_with_sudo(fake, monkeypatch):
    monkeypatch.setenv('FAKE_SSH_PW', 'pw')
    monkeypatch.setenv('FAKE_SUDO_PW', 'sp')
    rc, out = remote(ssh_user='user', ssh_password='pw', sudo_password='sp').run('echo "root=$FAKE_ROOT"; exit 4')
    assert rc == 4 and 'root=1' in out and 'sp' not in out.replace('root', '')
    with pytest.raises(engine.StepError, match='sudo не принял пароль'):
        remote(ssh_user='user', ssh_password='pw', sudo_password='nope').run('true')
    monkeypatch.setenv('FAKE_SUDO_PW', 'pw')  # пароль sudo не задан — тот же, что SSH
    assert 'root=1' in remote(ssh_user='user', ssh_password='pw').run('echo "root=$FAKE_ROOT"')[1]
    monkeypatch.setenv('FAKE_SUDO_DENY', '1')
    with pytest.raises(engine.StepError, match='не может выполнять команды через sudo'):
        remote(ssh_user='user', ssh_password='pw').run('true')


def test_interactive_after_login(fake, monkeypatch):
    import pexpect
    monkeypatch.setenv('FAKE_SSH_PW', 'pw')
    monkeypatch.setenv('FAKE_SUDO_PW', 'pw')
    child = remote(ssh_user='user', ssh_password='pw').spawn('read -r -p "Вопрос? " a; echo "ответ=$a"', True, 20)
    child.expect('Вопрос\\? ')
    child.sendline('да')
    child.expect('ответ=да')
    child.expect(pexpect.EOF)


def test_key_and_password_argv(fake, monkeypatch):
    monkeypatch.setenv('FAKE_SSH_PW', 'pw')
    remote(ssh_user='user', ssh_password='pw', ssh_key='/k', sudo_password='x').argv('true', False)
    a = remote(ssh_key='/k', ssh_password='pw').argv('true', False)
    assert '-i' in a and 'PreferredAuthentications=publickey,password,keyboard-interactive' in a
    a = remote(ssh_key='/k').argv('true', False)
    assert 'PreferredAuthentications=publickey' in a and 'PubkeyAuthentication=no' not in a


def test_access_forms(tmp_path):
    acc = sshaccess.clean({'user': 'user', 'port': '2200', 'password': 'pw', 'key': 'bot', 'sudo_password': ''}, None, tmp_path)
    assert acc == {'user': 'user', 'port': 2200, 'password': 'pw', 'key': 'bot', 'key_file': '', 'sudo_password': ''}
    n = sshaccess.to_node(acc, '/botkey')
    assert n == {'ssh_user': 'user', 'ssh_port': 2200, 'ssh_password': 'pw', 'ssh_key': '/botkey'}
    # пустое поле — оставить сохранённое; старый вид {mode: key}
    assert sshaccess.clean({'password': ''}, acc, tmp_path)['password'] == 'pw'
    assert sshaccess.to_node({'mode': 'key', 'user': 'root'}, '/b') == {'ssh_user': 'root', 'ssh_port': 22, 'ssh_key': '/b'}
    assert sshaccess.describe(acc) == 'user, пароль + ключ бота, через sudo'
    with pytest.raises(ValueError, match='публичный ключ'):
        sshaccess.clean({'key': 'own', 'key_text': 'ssh-ed25519 AAAA'}, None, tmp_path)
    with pytest.raises(ValueError, match='PuTTYgen'):
        sshaccess.clean({'key': 'own', 'key_text': 'PuTTY-User-Key-File-3: ssh-ed25519'}, None, tmp_path)
    with pytest.raises(ValueError, match='закрытый ключ'):
        sshaccess.clean({'key': 'own', 'key_text': 'просто текст'}, None, tmp_path)
    with pytest.raises(ValueError, match='пароль, ключ'):
        sshaccess.clean({'user': 'root'}, None, tmp_path)


def test_own_key_saved(tmp_path):
    import shutil
    import subprocess
    if not shutil.which('ssh-keygen'):
        pytest.skip('нет ssh-keygen')
    k = tmp_path / 'k'
    if subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(k)], capture_output=True).returncode:
        pytest.skip('нет ssh-keygen')
    text = k.read_text().replace('\n', ' ')  # вставка с телефона: переносы строк потерялись
    acc = sshaccess.clean({'user': 'user', 'key': 'own', 'key_text': text}, None, tmp_path)
    path = acc['key_file']
    assert oct(os.stat(path).st_mode & 0o777) == '0o600' and open(path).read().startswith('-----BEGIN OPENSSH PRIVATE KEY-----\n')
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', 'secret', '-f', str(tmp_path / 'enc')], check=True)
    with pytest.raises(ValueError, match='парольной фразой'):
        sshaccess.clean({'key': 'own', 'key_text': (tmp_path / 'enc').read_text()}, None, tmp_path)
