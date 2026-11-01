"""Аватарки пользователей из Telegram (как в Bobr Cloud): фото профиля через Bot API, кэш в памяти.

Бот видит фото только тех, кто ему писал и не скрыл фото настройками приватности — иначе остаются инициалы.
"""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

_AVA: dict[int, tuple[float, bytes | None, str]] = {}
TTL, MISS_TTL, MAX = 6 * 3600, 3600, 1000


async def avatar(token: str, tg_id: int) -> tuple[bytes, str] | None:
    hit = _AVA.get(tg_id)
    if hit and time.time() - hit[0] < (TTL if hit[1] else MISS_TTL):
        return (hit[1], hit[2]) if hit[1] else None
    data, ctype = None, 'image/jpeg'
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f'https://api.telegram.org/bot{token}/getUserProfilePhotos', params={'user_id': tg_id, 'limit': 1})
            photos = (r.json().get('result') or {}).get('photos') or []
            if photos:
                sizes = sorted(photos[0], key=lambda x: x.get('width', 0))
                pick = next((x for x in sizes if x.get('width', 0) >= 160), sizes[-1])
                f = (await c.get(f'https://api.telegram.org/bot{token}/getFile', params={'file_id': pick['file_id']})).json()
                path = (f.get('result') or {}).get('file_path')
                if path:
                    img = await c.get(f'https://api.telegram.org/file/bot{token}/{path}')
                    if img.status_code == 200 and len(img.content) < 2_000_000:
                        data = img.content
                        ctype = 'image/png' if path.endswith('.png') else 'image/jpeg'
    except Exception:  # noqa: BLE001 — нет фото или Telegram недоступен: будут инициалы
        log.debug('avatar %s failed', tg_id, exc_info=True)
    if len(_AVA) >= MAX:
        for k in sorted(_AVA, key=lambda k: _AVA[k][0])[: MAX // 4]:
            _AVA.pop(k, None)
    _AVA[tg_id] = (time.time(), data, ctype)
    return (data, ctype) if data else None
