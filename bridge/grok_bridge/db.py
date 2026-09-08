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


def _ensure_column(conn: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    """Additive migration. SQLite has no ADD COLUMN IF NOT EXISTS, and this
    schema predates any versioning, so existing databases are upgraded by
    checking table_info rather than by a migration number."""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class Store:
    def __init__(self, db_path: Path = config.DB_PATH):
        self.db_path = db_path
        # The database holds device secrets in the clear (and, later, permission
        # payloads that can contain shell commands and file contents). It is not
        # ordinary metadata -- keep it owner-only.
        try:
            self.db_path.touch(exist_ok=True)
            self.db_path.chmod(0o600)
        except OSError:
            pass
        with _connect(self.db_path) as conn:
            conn.executescript(SCHEMA)
            _ensure_column(conn, "pairing_tokens", "expires_at", "REAL")
            # Backfill rows written before the TTL existed. Without this they
            # keep expires_at = NULL and stay redeemable forever, which is the
            # exact hole the TTL was added to close -- this database really did
            # contain a 22-day-old unredeemed token still accepted as valid.
            conn.execute(
                "UPDATE pairing_tokens SET expires_at = created_at + ? "
                "WHERE expires_at IS NULL",
                (config.PAIRING_TOKEN_TTL_SECONDS,),
            )
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

    def create_pairing_token(self, ttl_seconds: float | None = None) -> str:
        token = uuid.uuid4().hex
        now = time.time()
        ttl = config.PAIRING_TOKEN_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO pairing_tokens (token, created_at, expires_at) VALUES (?, ?, ?)",
                (token, now, now + ttl),
            )
        return token

    def purge_expired_pairing_tokens(self) -> int:
        """Unredeemed tokens past their TTL are dead weight and, if they were
        ever to leak, a standing credential. Drop them rather than merely
        refusing them."""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM pairing_tokens WHERE used_at IS NULL "
                "AND expires_at IS NOT NULL AND expires_at < ?",
                (time.time(),),
            )
        return cur.rowcount

    def has_redeemable_pairing_token(self) -> bool:
        """Whether any token is currently redeemable. With Access gone, /v1/pair
        is directly internet-facing; when there is nothing to redeem it should
        look like it does not exist rather than advertise itself."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM pairing_tokens WHERE used_at IS NULL "
                "AND (expires_at IS NULL OR expires_at >= ?) LIMIT 1",
                (time.time(),),
            ).fetchone()
        return row is not None

    def redeem_pairing_token(self, token: str, device_name: str = "iPhone") -> tuple[str, str] | None:
        """Consume a one-time pairing token, mint a device (id, secret). None if invalid/used."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM pairing_tokens WHERE token = ?", (token,)
            ).fetchone()
            if row is None or row["used_at"] is not None:
                return None
            expires_at = row["expires_at"] if "expires_at" in row.keys() else None
            if expires_at is not None and expires_at < time.time():
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
