"""Собственный SSH-ключ бота: его публичная часть добавляется на каждую новую ноду, чтобы потом заходить
на неё без пароля (дополнения на уже работающих нодах). Ключ лежит в data/ssh, доступен только root."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def ensure(keydir: str | Path) -> tuple[str, str]:
    """(путь к закрытому ключу, публичный ключ одной строкой) — создаёт ключ при первом обращении.
    Если ssh-keygen недоступен — ('', '')."""
    keydir = Path(keydir)
    path = keydir / 'id_ed25519'
    try:
        if not path.exists():
            keydir.mkdir(parents=True, exist_ok=True)
            os.chmod(keydir, 0o700)
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'bobr-nodes', '-f', str(path)],
                           check=True, capture_output=True, timeout=30)
        return str(path), (keydir / 'id_ed25519.pub').read_text().strip()
    except (OSError, subprocess.SubprocessError):
        return '', ''
