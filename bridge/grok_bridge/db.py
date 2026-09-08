"""SQLite state store (plan v1.3 §1 "状态持久化").

Stores session/job/device metadata and lightweight message summaries --
never full message content or tool-call detail (that's grok's own session
files to own); this is enough for the sessions list page and for surviving
a Bridge restart without leaving a job stuck showing "generating...".
"""

from __future__ import annotations

import hmac
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    grok_session_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    title TEXT,
    created_at REAL NOT NULL,
    last_active_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    preview TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    status TEXT NOT NULL,
    started_at REAL,
    ended_at REAL
);

CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    secret TEXT NOT NULL,
    name TEXT,
    created_at REAL NOT NULL,
    last_used_at REAL
);

CREATE TABLE IF NOT EXISTS pairing_tokens (
    token TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    used_at REAL
);
"""

PREVIEW_LEN = 200


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class Store:
    def __init__(self, db_path: Path = config.DB_PATH):
        self.db_path = db_path
        with _connect(self.db_path) as conn:
            conn.executescript(SCHEMA)
            # Bridge just (re)started: any job still marked "running" belongs
            # to a process that no longer exists. Don't let a client sit
            # staring at "generating..." forever.
            conn.execute(
                "UPDATE jobs SET status = 'error', ended_at = ? WHERE status = 'running'",
                (time.time(),),
            )

    @contextmanager
    def _conn(self):
        conn = _connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- sessions --------------------------------------------------------

    def create_session(self, cwd: str, title: str | None = None) -> str:
        session_id = str(uuid.uuid4())
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO sessions (id, grok_session_id, cwd, title, created_at, last_active_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, session_id, cwd, title, now, now),
            )
        return session_id

    def rotate_grok_session_id(self, session_id: str) -> str:
        """Grok's session lock can get stuck after a SIGTERM-cancelled turn
        (confirmed empirically: 'Session ID ... is already in use' even once
        the process is dead and the .lock file removed). Rather than leaving
        the Bridge session permanently unusable, give it a fresh grok-side
        session id -- the Bridge-facing `id` (used by the app/URLs) doesn't
        change, only what gets passed to `grok -s`. Costs that session's
        grok-side conversation continuity from before the cancel point."""
        new_grok_id = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET grok_session_id = ? WHERE id = ?",
                (new_grok_id, session_id),
            )
        return new_grok_id

    def touch_session(self, session_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET last_active_at = ? WHERE id = ?",
                (time.time(), session_id),
            )

    def get_session(self, session_id: str) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()

    def list_sessions(self) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM sessions ORDER BY last_active_at DESC"
            ).fetchall()

    def recent_dirs(self, limit: int = 10) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT cwd FROM sessions ORDER BY last_active_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [r["cwd"] for r in rows]

    # -- messages (summary only) ------------------------------------------

    def add_message_preview(self, session_id: str, role: str, text: str) -> None:
        preview = text[:PREVIEW_LEN]
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO messages (id, session_id, role, preview, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), session_id, role, preview, time.time()),
            )

    def last_message_preview(self, session_id: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT preview FROM messages WHERE session_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return row["preview"] if row else None

    # -- jobs --------------------------------------------------------------

    def create_job(self, session_id: str) -> str:
        job_id = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO jobs (id, session_id, status, started_at) "
                "VALUES (?, ?, 'running', ?)",
                (job_id, session_id, time.time()),
            )
        return job_id

    def finish_job(self, job_id: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, ended_at = ? WHERE id = ?",
                (status, time.time(), job_id),
            )

    # -- devices -------------------------------------------------------------

    def create_pairing_token(self) -> str:
        token = uuid.uuid4().hex
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO pairing_tokens (token, created_at) VALUES (?, ?)",
                (token, time.time()),
            )
        return token

    def redeem_pairing_token(self, token: str, device_name: str = "iPhone") -> tuple[str, str] | None:
        """Consume a one-time pairing token, mint a device (id, secret). None if invalid/used."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM pairing_tokens WHERE token = ?", (token,)
            ).fetchone()
            if row is None or row["used_at"] is not None:
                return None
            conn.execute(
                "UPDATE pairing_tokens SET used_at = ? WHERE token = ?",
                (time.time(), token),
            )
            device_id = str(uuid.uuid4())
            secret = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO devices (device_id, secret, name, created_at) "
                "VALUES (?, ?, ?, ?)",
                (device_id, secret, device_name, time.time()),
            )
        return device_id, secret

    def check_device_token(self, device_id: str, secret: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT secret FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
            # constant-time compare: this runs on every authenticated request,
            # unlike the one-shot pairing token, so a naive `!=` would leak
            # timing information about how much of the secret is correct.
            if row is None or not hmac.compare_digest(row["secret"], secret):
                return False
            conn.execute(
                "UPDATE devices SET last_used_at = ? WHERE device_id = ?",
                (time.time(), device_id),
            )
        return True

    def revoke_device(self, device_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))
        return cur.rowcount > 0

    def list_devices(self) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT device_id, name, created_at, last_used_at FROM devices "
                "ORDER BY created_at DESC"
            ).fetchall()
