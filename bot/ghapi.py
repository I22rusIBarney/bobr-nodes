"""Запросы к API GitHub: с токеном из «Подключений» лимит 5000 в час вместо 60 на IP сервера.
Токену не нужны права — только чтение публичных репозиториев (проверка обновлений, версии Xray и панели)."""
from __future__ import annotations

TOKEN = {'v': ''}


def load(store) -> None:
    TOKEN['v'] = store.get('github_token') or ''


def headers() -> dict:
    h = {'Accept': 'application/vnd.github+json', 'User-Agent': 'bobr-nodes'}
    if TOKEN['v']:
        h['Authorization'] = f'Bearer {TOKEN["v"]}'
    return h
