"""Обёртка для pytest: сквозной сценарий запускается отдельным процессом.
Нужны BCLOUD_DIR (клон bobr-cloud с /api/svc) и NV_SRC (папка src из Nuvrion-Vision-Installer)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not (os.environ.get('BCLOUD_DIR') and os.environ.get('NV_SRC')), reason='нет BCLOUD_DIR / NV_SRC')
def test_e2e():
    r = subprocess.run([sys.executable, str(ROOT / 'tests' / 'e2e_test.py')], capture_output=True, text=True,
                       timeout=600, cwd=ROOT, env={**os.environ, 'PYTHONPATH': os.pathsep.join(
                           x for x in (str(ROOT), os.environ.get('PYTHONPATH', '')) if x)})
    out = r.stdout + r.stderr
    fails = [x for x in out.splitlines() if x.startswith('FAIL ')]
    assert r.returncode == 0 and 'FAILS: 0' in out, '\n'.join(fails) + '\n' + out[-3000:]
