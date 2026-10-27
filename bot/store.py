"""Хранилище: настройки, профили панелей, задачи развёртывания. SQLite, один файл data/bot.db."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS profiles (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch TEXT NOT NULL,
    profile_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    msg_id INTEGER,
    status TEXT NOT NULL,          -- queued | buying | provisioning | deploying | done | failed
    stage TEXT NOT NULL DEFAULT '', -- buy | ip | dns | ssh | install | panel | check
    error TEXT NOT NULL DEFAULT '',
    hidden INTEGER NOT NULL DEFAULT 0,
    data TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""

ACTIVE = ('queued', 'buying', 'provisioning', 'deploying')


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.execute('PRAGMA journal_mode=WAL')

    def _q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    # ── настройки ──
    def get(self, key: str, default: str = '') -> str:
        rows = self._q('SELECT value FROM settings WHERE key=?', (key,))
        return rows[0]['value'] if rows else default

    def set(self, key: str, value: str) -> None:
        self._q('INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (key, value))

    # ── профили ──
    def profiles(self) -> list[dict]:
        return [{**json.loads(r['data']), 'id': r['id']} for r in self._q('SELECT * FROM profiles ORDER BY id')]

    def profile(self, pid: int) -> dict | None:
        rows = self._q('SELECT * FROM profiles WHERE id=?', (pid,))
        return {**json.loads(rows[0]['data']), 'id': rows[0]['id']} if rows else None

    def save_profile(self, data: dict) -> int:
        body = json.dumps({k: v for k, v in data.items() if k != 'id'}, ensure_ascii=False)
        if data.get('id'):
            self._q('UPDATE profiles SET data=? WHERE id=?', (body, data['id']))
            return data['id']
        with self.lock:
            return self.db.execute('INSERT INTO profiles(data) VALUES(?)', (body,)).lastrowid

    def delete_profile(self, pid: int) -> None:
        self._q('DELETE FROM profiles WHERE id=?', (pid,))

    # ── задачи ──
    def add_job(self, batch: str, profile_id: int, chat_id: int, data: dict) -> int:
        now = time.time()
        with self.lock:
            return self.db.execute(
                'INSERT INTO jobs(batch, profile_id, chat_id, status, data, created_at, updated_at) VALUES(?,?,?,?,?,?,?)',
                (batch, profile_id, chat_id, 'queued', json.dumps(data, ensure_ascii=False), now, now)).lastrowid

    def job(self, jid: int) -> dict | None:
        rows = self._q('SELECT * FROM jobs WHERE id=?', (jid,))
        return self._job(rows[0]) if rows else None

    @staticmethod
    def _job(r) -> dict:
        d = dict(r)
        d['data'] = json.loads(d['data'])
        return d

    def update_job(self, jid: int, **fields) -> None:
        if 'data' in fields:
            fields['data'] = json.dumps(fields['data'], ensure_ascii=False)
        fields['updated_at'] = time.time()
        cols = ', '.join(f'{k}=?' for k in fields)
        self._q(f'UPDATE jobs SET {cols} WHERE id=?', (*fields.values(), jid))

    def patch_job_data(self, jid: int, **values) -> dict:
        with self.lock:
            row = self.db.execute('SELECT data FROM jobs WHERE id=?', (jid,)).fetchone()
            data = {**json.loads(row['data']), **values}
            self.db.execute('UPDATE jobs SET data=?, updated_at=? WHERE id=?',
                            (json.dumps(data, ensure_ascii=False), time.time(), jid))
        return data

    def jobs(self, limit: int = 20, include_hidden: bool = False) -> list[dict]:
        where = '' if include_hidden else 'WHERE hidden=0'
        return [self._job(r) for r in self._q(f'SELECT * FROM jobs {where} ORDER BY id DESC LIMIT ?', (limit,))]

    def active_jobs(self) -> list[dict]:
        marks = ','.join('?' * len(ACTIVE))
        return [self._job(r) for r in self._q(f'SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY id', ACTIVE)]

    def used_domains(self) -> set[str]:
        out = set()
        for r in self._q("SELECT data FROM jobs WHERE status != 'failed' OR json_extract(data, '$.server_id') IS NOT NULL"):
            d = json.loads(r['data']).get('domain')
            if d:
                out.add(d.lower())
        return out
