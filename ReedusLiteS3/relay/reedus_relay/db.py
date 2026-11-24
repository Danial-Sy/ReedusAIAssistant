"""storage: Postgres on Railway, SQLite everywhere else. one tiny API so the rest never cares.

SQL is written with %s placeholders and Postgres types; the SQLite backend rewrites both.
every call is synchronous and thread-safe; async code calls it through asyncio.to_thread.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from typing import Any, List, Optional, Sequence

from reedus_relay import config

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS accounts (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        token_hash TEXT NOT NULL,          -- the first sign-in token (later ones: app_tokens)
        balance_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
        spent_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
        settings TEXT NOT NULL DEFAULT '{}',
        created DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS devices (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id),
        name TEXT NOT NULL,
        token_hash TEXT NOT NULL,          -- the S3 sends its device token in hello
        settings TEXT NOT NULL DEFAULT '{}',
        assist TEXT NOT NULL DEFAULT '{}', -- timers, alarms, lists (reedus_companion.assist)
        last_seen DOUBLE PRECISION NOT NULL DEFAULT 0,
        created DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS bulletins (
        account_id TEXT PRIMARY KEY REFERENCES accounts(id),
        text TEXT NOT NULL,
        rev INTEGER NOT NULL,
        updated DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS turns (
        id BIGSERIAL PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id),
        device_id TEXT NOT NULL,
        conversation TEXT NOT NULL,
        at DOUBLE PRECISION NOT NULL,
        said TEXT NOT NULL,
        reply TEXT NOT NULL,
        usd DOUBLE PRECISION NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS turns_account_at ON turns(account_id, at)",
    """CREATE TABLE IF NOT EXISTS secrets (
        name TEXT PRIMARY KEY,             -- e.g. GEMINI_API_KEY, set on the website (vault.py)
        value TEXT NOT NULL,
        updated DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS topups (
        session_id TEXT PRIMARY KEY,       -- Stripe Checkout session (funding.py)
        account_id TEXT NOT NULL REFERENCES accounts(id),
        usd DOUBLE PRECISION NOT NULL,
        status TEXT NOT NULL,              -- open | paid | expired | pending | failed
        credited INTEGER NOT NULL DEFAULT 0,
        kind TEXT NOT NULL DEFAULT 'checkout',  -- checkout | auto (off-session, saved card)
        save_card INTEGER NOT NULL DEFAULT 0,   -- checkout that also saves the card for auto top-up
        detail TEXT NOT NULL DEFAULT '',
        created DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS app_tokens (
        token_hash TEXT PRIMARY KEY,       -- one per signed-in browser (the site mints these)
        account_id TEXT NOT NULL REFERENCES accounts(id),
        created DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS codes (
        code TEXT PRIMARY KEY,             -- invite links and sign-in-on-another-device codes
        kind TEXT NOT NULL,                -- invite | login
        account_id TEXT NOT NULL,          -- invite: who invited; login: who signs in
        expires DOUBLE PRECISION NOT NULL,
        created DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ledger (
        id BIGSERIAL PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id),
        at DOUBLE PRECISION NOT NULL,
        usd DOUBLE PRECISION NOT NULL,     -- + top-up, - spend
        what TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS voices (
        id TEXT PRIMARY KEY,               -- a person he knows by voice (voices.py)
        account_id TEXT NOT NULL REFERENCES accounts(id),
        name TEXT NOT NULL,
        me INTEGER NOT NULL DEFAULT 0,     -- the account holder: their notes are the bulletin
        samples TEXT NOT NULL DEFAULT '[]',  -- voiceprints, base64 float32 (voiceprint.py)
        model TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',    -- what he knows about them (like the bulletin)
        notes_rev INTEGER NOT NULL DEFAULT 0,
        last_heard DOUBLE PRECISION NOT NULL DEFAULT 0,
        created DOUBLE PRECISION NOT NULL,
        updated DOUBLE PRECISION NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS voices_account ON voices(account_id)",
]

# columns added after a table first shipped: (table, column, definition)
COLUMNS = [
    ("turns", "voice_id", "TEXT NOT NULL DEFAULT ''"),   # who said it (voices.py): '' = the
                                                         # account holder, '?' = not recognized
    ("voices", "say", "TEXT NOT NULL DEFAULT ''"),       # how to say their name: a respelling
                                                         # ("DAN-yul") or IPA in slashes
]

# Postgres full-text search over past turns (the history recall). SQLite falls back to LIKE.
PG_EXTRA = [
    "CREATE INDEX IF NOT EXISTS turns_fts ON turns USING GIN "
    "(to_tsvector('english', said || ' ' || reply))",
]


class DB:
    def __init__(self, url: str = ""):
        self.url = url or config.DATABASE_URL
        self.pg = self.url.startswith(("postgres://", "postgresql://"))
        self._lock = threading.RLock()
        self._local = threading.local()
        if not self.pg:
            path = self.url.removeprefix("sqlite:///") if self.url else \
                str(config.DATA / "relay.sqlite")
            if path != ":memory:":
                from pathlib import Path
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._sqlite = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self._sqlite.row_factory = sqlite3.Row
            self._sqlite.execute("PRAGMA journal_mode=WAL")
        else:
            import psycopg                                      # noqa: F401 (fail early)
        self.migrate()

    # ---------- connections ----------
    def _pg(self):
        conn = getattr(self._local, "conn", None)
        if conn is None or conn.closed:
            import psycopg
            from psycopg.rows import dict_row
            conn = psycopg.connect(self.url, autocommit=True, row_factory=dict_row)
            self._local.conn = conn
        return conn

    def _sql(self, sql: str) -> str:
        if self.pg:
            return sql
        sql = sql.replace("%s", "?")
        sql = sql.replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
        sql = sql.replace("DOUBLE PRECISION", "REAL")
        return sql

    def _pg_run(self, fn):
        """Run fn(cursor) on this thread's connection; if the connection turns out to be dead
        (Postgres restarted, or an idle connection was dropped), reconnect once and retry.
        a long-idle thread like a timer's clock is exactly the one that finds it dead."""
        import psycopg
        for attempt in (0, 1):
            try:
                with self._pg().cursor() as cur:
                    return fn(cur)
            except (psycopg.OperationalError, psycopg.InterfaceError):
                conn = getattr(self._local, "conn", None)
                self._local.conn = None
                try:
                    if conn is not None:
                        conn.close()
                except Exception:                                # noqa: BLE001
                    pass
                if attempt:
                    raise

    def execute(self, sql: str, args: Sequence[Any] = ()) -> int:
        if self.pg:
            def _go(cur):
                cur.execute(sql, args)
                return cur.rowcount
            return self._pg_run(_go)
        with self._lock:
            return self._sqlite.execute(self._sql(sql), tuple(args)).rowcount

    def all(self, sql: str, args: Sequence[Any] = ()) -> List[dict]:
        if self.pg:
            def _go(cur):
                cur.execute(sql, args)
                return [dict(r) for r in cur.fetchall()]
            return self._pg_run(_go)
        with self._lock:
            return [dict(r) for r in self._sqlite.execute(self._sql(sql), tuple(args)).fetchall()]

    def one(self, sql: str, args: Sequence[Any] = ()) -> Optional[dict]:
        rows = self.all(sql, args)
        return rows[0] if rows else None

    def migrate(self) -> None:
        for stmt in SCHEMA:
            self.execute(self._sql(stmt))
        for table, col, definition in COLUMNS:
            if self.pg:
                self.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {definition}")
            else:
                have = {r["name"] for r in self.all(f"PRAGMA table_info({table})")}
                if col not in have:
                    self.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
        if self.pg:
            for stmt in PG_EXTRA:
                self.execute(stmt)

    # ---------- helpers ----------
    @staticmethod
    def now() -> float:
        return time.time()

    @staticmethod
    def dumps(obj) -> str:
        return json.dumps(obj or {}, separators=(",", ":"))

    @staticmethod
    def loads(s) -> dict:
        try:
            v = json.loads(s or "{}")
            return v if isinstance(v, dict) else {}
        except ValueError:
            return {}

    def search_turns(self, account_id: str, query: str, limit: int = 3,
                     exclude_conversation: str = "",
                     voices: Optional[Sequence[str]] = None) -> List[dict]:
        """Past turns that mention what was just said, best first. voices: only turns said by
        these people (turns.voice_id), None = everyone's."""
        words = [w for w in re.findall(r"[a-z0-9']{3,}", (query or "").lower())
                 if w not in _STOP][:8]
        if not words:
            return []
        who, who_args = "", ()
        if voices is not None:
            who = f" AND voice_id IN ({', '.join(['%s'] * len(voices))})" if voices else " AND 1 = 0"
            who_args = tuple(voices)
        if self.pg:
            # apostrophes are tsquery syntax; "don't" -> "dont"
            q = " | ".join(dict.fromkeys(w.replace("'", "") for w in words if w.replace("'", "")))
            return self.all(
                "SELECT said, reply, at FROM turns WHERE account_id = %s AND conversation <> %s"
                + who + " AND to_tsvector('english', said || ' ' || reply) @@ to_tsquery('english', %s) "
                "ORDER BY ts_rank(to_tsvector('english', said || ' ' || reply), "
                "to_tsquery('english', %s)) DESC, at DESC LIMIT %s",
                (account_id, exclude_conversation, *who_args, q, q, limit))
        like = " OR ".join(["lower(said || ' ' || reply) LIKE %s"] * len(words))
        rows = self.all(
            f"SELECT said, reply, at FROM turns WHERE account_id = %s AND conversation <> %s{who} "
            f"AND ({like}) ORDER BY at DESC LIMIT 50",
            (account_id, exclude_conversation, *who_args, *[f"%{w}%" for w in words]))
        rows.sort(key=lambda r: -sum(w in (r["said"] + " " + r["reply"]).lower() for w in words))
        return rows[:limit]


_STOP = set("""the and you your for are was what that this with have has how why who when
where about just like can could would should will they them there then than from into its
it's i'm me my our out off not but all any get got does did don't let's yeah okay hey reedus""".split())


_db: Optional[DB] = None
_db_lock = threading.Lock()


def db() -> DB:
    global _db
    with _db_lock:
        if _db is None:
            _db = DB()
        return _db


def use(instance: DB) -> DB:
    """Tests: swap in a fresh database."""
    global _db
    with _db_lock:
        _db = instance
    return instance
