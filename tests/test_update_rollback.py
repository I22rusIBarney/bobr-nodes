"""bnodes update: новая версия не запустилась — install.sh сам возвращает прежние код и образ.
Настоящий install.sh в git-репозитории; docker и sleep — подставные. Нужны root и bash 4 (Linux)."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from bot import selfupdate

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(os.name != 'posix' or os.geteuid() != 0 or not shutil.which('git'),
                                reason='нужен root и git (как на сервере)')

OLD, NEW = 'ghcr.io/acme/bobr-nodes:sha-old', 'bobr-nodes:local'
DOCKER = r'''#!/usr/bin/env bash
# подставной docker: новый образ (bobr-nodes:local) не отвечает на /health, прежний — отвечает
echo "$*" >> "$LOG"
img=$(sed -n "s/^BNODES_IMAGE='\{0,1\}\([^']*\)'\{0,1\}$/\1/p" .env | tail -1)
case "$1 $2" in
  "compose exec") [[ "$img" == "OLD" ]] && exit 0 || exit 1 ;;
  "image inspect") [[ "$3" == "OLD" ]] && exit 0 || exit 1 ;;
esac
exit 0
'''.replace('OLD', OLD)


def git(cwd, *a):
    subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *a], cwd=cwd, check=True, capture_output=True)


def test_failed_update_rolls_back_to_previous_image(tmp_path):
    repo = tmp_path / 'bot'
    repo.mkdir()
    shutil.copy(ROOT / 'install.sh', repo / 'install.sh')
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'old')
    before = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=repo, capture_output=True, text=True).stdout.strip()
    (repo / 'new.txt').write_text('new')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'new')
    (repo / '.env').write_text(f"BNODES_IMAGE='{OLD}'\n")
    (repo / 'data').mkdir()
    bin_ = tmp_path / 'bin'
    bin_.mkdir()
    (bin_ / 'docker').write_text(DOCKER)
    (bin_ / 'sleep').write_text('#!/bin/sh\nexit 0\n')
    for f in bin_.iterdir():
        f.chmod(0o755)
    env = {**os.environ, 'PATH': f'{bin_}:{os.environ["PATH"]}', 'LOG': str(tmp_path / 'docker.log'), 'BNODES_BUILD': '1'}
    r = subprocess.run(['bash', './install.sh', '_rebuild', before], cwd=repo, env=env, capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert 'не запустилась' in out and 'прежней версии' in out, out
    assert f"BNODES_IMAGE='{OLD}'" in (repo / '.env').read_text()                       # прежний образ снова в работе
    head = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=repo, capture_output=True, text=True).stdout.strip()
    assert head == before                                                                # и прежний код
    assert (repo / 'data' / 'update-skip').read_text().strip()                           # эту версию больше не ставим
    note = selfupdate.rollback_note(str(repo / 'data'))
    assert note and 'не запустилось' in note and before in note
    assert selfupdate.rollback_note(str(repo / 'data')) is None                          # сообщение — один раз


def test_successful_update_keeps_new_version(tmp_path):
    repo = tmp_path / 'bot'
    repo.mkdir()
    shutil.copy(ROOT / 'install.sh', repo / 'install.sh')
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'old')
    (repo / '.env').write_text(f"BNODES_IMAGE='{OLD}'\n")
    (repo / 'data').mkdir()
    (repo / 'data' / 'update-skip').write_text('deadbeef\n')
    bin_ = tmp_path / 'bin'
    bin_.mkdir()
    (bin_ / 'docker').write_text('#!/usr/bin/env bash\nexit 0\n')  # всё отвечает
    (bin_ / 'sleep').write_text('#!/bin/sh\nexit 0\n')
    for f in bin_.iterdir():
        f.chmod(0o755)
    env = {**os.environ, 'PATH': f'{bin_}:{os.environ["PATH"]}', 'BNODES_BUILD': '1'}
    r = subprocess.run(['bash', './install.sh', '_rebuild', 'abc1234'], cwd=repo, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and 'Бот перезапущен' in r.stdout, r.stdout + r.stderr
    assert not (repo / 'data' / 'update-rollback.txt').exists() and not (repo / 'data' / 'update-skip').exists()
