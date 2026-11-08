"""Своя версия Xray на ноде: файл-дополнение docker compose с бинарником, «как в образе» и откат — на поддельном docker."""
import os
import stat
import subprocess

import pytest

from bot import upgrade as up

FAKE_DOCKER = r'''#!/bin/bash
# поддельный docker: контейнер remnanode из compose-проекта в $DIR, вызовы compose — в журнал
case "$1" in
  inspect)
    case "$*" in
      *working_dir*) echo "$DIR";;
      *config_files*) cat "$DIR/.files";;
      *compose.service*) echo remnanode;;
      *) echo ok;;
    esac;;
  ps) echo "remnanode remnawave/node:2.3.1";;
  compose) shift; echo "$*" >> "$DIR/compose.log"
    # как настоящий: запомнить список файлов проекта
    F=""; while [ $# -gt 0 ]; do [ "$1" = "-f" ] && F="$F${F:+,}$2"; shift; done; echo "$F" > "$DIR/.files";;
esac
'''


def run(cmd, env):
    return subprocess.run(['bash', '-c', cmd], capture_output=True, text=True, env=env, timeout=30).stdout


@pytest.mark.skipif(os.name != 'posix', reason='нужен bash')
def test_xray_override_and_rollback(tmp_path, monkeypatch):
    bin_ = tmp_path / 'bin'
    bin_.mkdir()
    d = tmp_path / 'node'
    d.mkdir()
    (d / '.files').write_text(f'{d}/docker-compose.yml')
    (bin_ / 'docker').write_text(FAKE_DOCKER)
    (bin_ / 'docker').chmod(0o755)
    (bin_ / 'timeout').write_text('#!/bin/sh\nshift\nexec "$@"\n')  # на macOS своей нет
    (bin_ / 'timeout').chmod(0o755)
    xd = tmp_path / 'xray'
    monkeypatch.setattr(up, 'XRAY_DIR', str(xd))
    (xd / 'v26.10.31').mkdir(parents=True)
    xb = xd / 'v26.10.31' / 'xray'
    xb.write_text('#!/bin/sh\necho Xray 26.10.31\n')  # уже скачан: сеть в тесте не нужна
    xb.chmod(xb.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, 'PATH': f'{bin_}:{os.environ["PATH"]}', 'DIR': str(d)}
    ov = d / up.XRAY_OVERRIDE

    assert 'XR_DONE' in run(up.xray_cmd('v26.10.31'), env)
    text = ov.read_text()
    assert 'remnanode:' in text and f'{xb}:/usr/local/bin/xray:ro' in text
    assert (d / '.files').read_text().strip().endswith(up.XRAY_OVERRIDE)       # docker запомнил дополнение
    assert 'XR_SAME' in run(up.xray_cmd('v26.10.31'), env)                     # повтор ничего не делает
    # обновление ноды берёт файлы из меток контейнера — дополнение остаётся в проекте
    assert up.XRAY_OVERRIDE in (d / '.files').read_text()

    assert 'XR_DONE' in run(up.xray_cmd('image'), env)                         # вернуть Xray из образа
    assert not ov.exists() and up.XRAY_OVERRIDE not in (d / '.files').read_text()
    assert 'RB_DONE' in run(up.xray_rollback_cmd(), env)                       # откат возвращает прежний файл
    assert ov.exists() and up.XRAY_OVERRIDE in (d / '.files').read_text()


def test_check_xray():
    assert up.check_xray('image') == 'image' and up.check_xray('v26.10.31') == 'v26.10.31'
    for bad in ('26.10.31', 'v1.2', 'latest; rm -rf /', ''):
        with pytest.raises(ValueError):
            up.check_xray(bad)


def test_xray_job_uses_xray_path(tmp_path, monkeypatch):
    import asyncio

    from bot import services as svc
    from bot.runner import Runner
    from bot.store import Store
    store = Store(tmp_path / 'db.sqlite')
    pid = store.save_profile({'name': 'g', 'panel_url': 'https://p', 'panel_token': 't'})
    got = []
    monkeypatch.setattr(up, 'xray_node', lambda node, *a: got.append(node['xray']) or 'Xray 26.6 → 26.10.31')
    runner = Runner(store, None, tmp_path / 'logs', upgrade_node=lambda *a: (_ for _ in ()).throw(AssertionError('не образ')))
    runner.ssh_of = lambda pid_, uuid: {'user': 'root', 'port': 22, 'password': 'x'}

    async def go():
        jid = await svc.start_upgrade(store, runner, store.profile(pid), {'uuid': 'n1', 'name': 'A', 'address': '1.2.3.4'}, 'latest', 1, xray='v26.10.31')
        for _ in range(100):
            if store.job(jid)['status'] in ('done', 'failed'):
                break
            await asyncio.sleep(0.05)
        return store.job(jid)
    job = asyncio.run(go())
    assert job['status'] == 'done', job['error']
    assert got == ['v26.10.31'] and job['data']['spec'] == 'Xray v26.10.31' and job['data']['result'].startswith('Xray')


def test_xray_in_dockerfile():
    assert up.xray_in('FROM alpine\nARG XRAY_CORE_VERSION=v26.10.31\nARG X=1') == 'v26.10.31'
    assert up.xray_in('RUN curl -L https://raw.githubusercontent.com/remnawave/scripts/main/scripts/install-latest-xray.sh | bash -s -- v25.6.8 \\') == 'v25.6.8'
    assert up.xray_in('FROM node') == ''
