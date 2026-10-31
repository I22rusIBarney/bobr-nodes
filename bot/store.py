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
CREATE TABLE IF NOT EXISTS scripts (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS checkers (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mon (key TEXT PRIMARY KEY, data TEXT NOT NULL);  -- состояние нод в мониторинге
CREATE TABLE IF NOT EXISTS users (tg_id INTEGER PRIMARY KEY, data TEXT NOT NULL);  -- приглашённые пользователи
CREATE TABLE IF NOT EXISTS invites (id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE NOT NULL, data TEXT NOT NULL);
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

    # ── мои скрипты ──
    def scripts(self) -> list[dict]:
        return [{**json.loads(r['data']), 'id': r['id']} for r in self._q('SELECT * FROM scripts ORDER BY id')]

    def script(self, sid: int) -> dict | None:
        rows = self._q('SELECT * FROM scripts WHERE id=?', (sid,))
        return {**json.loads(rows[0]['data']), 'id': rows[0]['id']} if rows else None

    def save_script(self, data: dict) -> int:
        body = json.dumps({k: v for k, v in data.items() if k != 'id'}, ensure_ascii=False)
        if data.get('id'):
            self._q('UPDATE scripts SET data=? WHERE id=?', (body, data['id']))
            return data['id']
        with self.lock:
            return self.db.execute('INSERT INTO scripts(data) VALUES(?)', (body,)).lastrowid

    def delete_script(self, sid: int) -> None:
        self._q('DELETE FROM scripts WHERE id=?', (sid,))

    # ── чекеры доступности (xray-checker на серверах в России) ──
    def checkers(self) -> list[dict]:
        return [{**json.loads(r['data']), 'id': r['id']} for r in self._q('SELECT * FROM checkers ORDER BY id')]

    def checker(self, cid: int) -> dict | None:
        rows = self._q('SELECT * FROM checkers WHERE id=?', (cid,))
        return {**json.loads(rows[0]['data']), 'id': rows[0]['id']} if rows else None

    def save_checker(self, data: dict) -> int:
        body = json.dumps({k: v for k, v in data.items() if k != 'id'}, ensure_ascii=False)
        if data.get('id'):
            self._q('UPDATE checkers SET data=? WHERE id=?', (body, data['id']))
            return data['id']
        with self.lock:
            return self.db.execute('INSERT INTO checkers(data) VALUES(?)', (body,)).lastrowid

    def patch_checker(self, cid: int, **values) -> dict:
        with self.lock:
            row = self.db.execute('SELECT data FROM checkers WHERE id=?', (cid,)).fetchone()
            if not row:
                return {}
            data = {**json.loads(row['data']), **values}
            self.db.execute('UPDATE checkers SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), cid))
        return {**data, 'id': cid}

    def delete_checker(self, cid: int) -> None:
        self._q('DELETE FROM checkers WHERE id=?', (cid,))

    # ── приглашённые пользователи и приглашения ──
    def users(self) -> list[dict]:
        return [{**json.loads(r['data']), 'tg_id': r['tg_id']} for r in self._q('SELECT * FROM users ORDER BY tg_id')]

    def user(self, tg_id: int) -> dict | None:
        rows = self._q('SELECT * FROM users WHERE tg_id=?', (int(tg_id),))
        return {**json.loads(rows[0]['data']), 'tg_id': rows[0]['tg_id']} if rows else None

    def save_user(self, data: dict) -> None:
        body = json.dumps({k: v for k, v in data.items() if k != 'tg_id'}, ensure_ascii=False)
        self._q('INSERT INTO users(tg_id, data) VALUES(?, ?) ON CONFLICT(tg_id) DO UPDATE SET data=excluded.data',
                (int(data['tg_id']), body))

    def delete_user(self, tg_id: int) -> None:
        self._q('DELETE FROM users WHERE tg_id=?', (int(tg_id),))

    def invites(self) -> list[dict]:
        return [{**json.loads(r['data']), 'id': r['id'], 'token': r['token']} for r in self._q('SELECT * FROM invites ORDER BY id DESC')]

    def invite(self, iid: int | None = None, token: str | None = None) -> dict | None:
        rows = (self._q('SELECT * FROM invites WHERE id=?', (int(iid),)) if iid is not None
                else self._q('SELECT * FROM invites WHERE token=?', (token or '',)))
        return {**json.loads(rows[0]['data']), 'id': rows[0]['id'], 'token': rows[0]['token']} if rows else None

    def add_invite(self, token: str, data: dict) -> int:
        with self.lock:
            return self.db.execute('INSERT INTO invites(token, data) VALUES(?, ?)',
                                   (token, json.dumps(data, ensure_ascii=False))).lastrowid

    def save_invite(self, data: dict) -> None:
        body = json.dumps({k: v for k, v in data.items() if k not in ('id', 'token')}, ensure_ascii=False)
        self._q('UPDATE invites SET data=? WHERE id=?', (body, int(data['id'])))

    def delete_invite(self, iid: int) -> None:
        self._q('DELETE FROM invites WHERE id=?', (int(iid),))

    # ── состояние мониторинга: ключ «группа:нода» ──
    def mon(self, key: str) -> dict:
        rows = self._q('SELECT data FROM mon WHERE key=?', (key,))
        return json.loads(rows[0]['data']) if rows else {}

    def mon_all(self, prefix: str = '') -> dict[str, dict]:
        return {r['key']: json.loads(r['data']) for r in self._q('SELECT * FROM mon WHERE key LIKE ?', (prefix + '%',))}

    def set_mon(self, key: str, data: dict | None) -> None:
        if data is None:
            self._q('DELETE FROM mon WHERE key=?', (key,))
        else:
            self._q('INSERT INTO mon(key, data) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET data=excluded.data',
                    (key, json.dumps(data, ensure_ascii=False)))

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
            data = json.loads(r['data'])
            if data.get('removed'):  # сервер удалён, A-запись снята — имя снова свободно
                continue
            d = data.get('domain')
            if d:
                out.add(d.lower())
        return out
