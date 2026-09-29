"""Durable OIDC and token settings stored in SQLite.

File location: ``data/state/settings.db``.  Never the derived index
(``chronicle.db``) — that database is deletable and rebuildable, while these
settings are durable truth.  The database is backed up and restored automatically
(see ``chronicle.backup``).

Schema
------

oidc_settings (id INTEGER PRIMARY KEY CHECK(id=1)
    issuer           TEXT NOT NULL
    client_id        TEXT NOT NULL
    client_secret_file TEXT NOT NULL   -- path to a file with mode 0o600
    redirect_uri     TEXT NOT NULL
    scopes           TEXT NOT NULL     -- JSON array
    groups_claim_name TEXT NOT NULL

oidc_group_role_map
    (id INTEGER PRIMARY KEY,
     group_name     TEXT NOT NULL UNIQUE,
     min_role       TEXT NOT NULL CHECK(min_role IN
                     ('reader','contributor','editor','admin')))

service_accounts
    (id INTEGER PRIMARY KEY,
     name           TEXT NOT NULL UNIQUE,
     role           TEXT NOT NULL CHECK(role IN
                     ('reader','contributor','editor','admin')),
     enabled        INTEGER NOT NULL DEFAULT 1,
     created_at     TEXT NOT NULL)

service_account_tokens
    (id TEXT PRIMARY KEY,
     account_id     INTEGER NOT NULL REFERENCES service_accounts(id),
     token_hash     TEXT NOT NULL,
     created_at     TEXT NOT NULL)

personal_tokens
    (id TEXT PRIMARY KEY,
     token_hash     TEXT NOT NULL,
     name           TEXT NOT NULL,
     role           TEXT NOT NULL CHECK(role IN
                     ('reader','contributor','editor','admin')),
     created_by_issuer TEXT NOT NULL,
     created_by_subject TEXT NOT NULL,
     created_at     TEXT NOT NULL,
     expires_at     TEXT,
     revoked        INTEGER NOT NULL DEFAULT 0)
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

_DB_FILE = "settings.db"

# ── helpers ──────────────────────────────────────────────────────────────

def _make_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


# ── public API ───────────────────────────────────────────────────────────

class SettingsDB:
    """Thread-safe wrapper around the SQLite settings database."""

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / "state" / _DB_FILE
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._schema_version: int = 0
        self._ensure_schema()

    # -- internal --

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = _make_conn(self._path)
        return self._conn

    def _ensure_schema(self) -> None:
        with self._lock:
            conn = self.conn
            conn.execute(self._DDL)
            self._schema_version = 1
            conn.commit()

    _DDL = """
        CREATE TABLE IF NOT EXISTS oidc_settings (
            id                 INTEGER PRIMARY KEY CHECK(id=1),
            issuer             TEXT NOT NULL,
            client_id          TEXT NOT NULL,
            client_secret_file TEXT NOT NULL,
            redirect_uri       TEXT NOT NULL,
            scopes             TEXT NOT NULL,
            groups_claim_name  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS oidc_group_role_map (
            id        INTEGER PRIMARY KEY,
            group_name TEXT NOT NULL UNIQUE,
            min_role  TEXT NOT NULL CHECK(min_role IN
                          ('reader','contributor','editor','admin'))
        );
        CREATE TABLE IF NOT EXISTS service_accounts (
            id        INTEGER PRIMARY KEY,
            name      TEXT NOT NULL UNIQUE,
            role      TEXT NOT NULL CHECK(role IN
                          ('reader','contributor','editor','admin')),
            enabled   INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS service_account_tokens (
            id         TEXT PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES service_accounts(id),
            token_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS personal_tokens (
            id                TEXT PRIMARY KEY,
            token_hash        TEXT NOT NULL,
            name              TEXT NOT NULL,
            role              TEXT NOT NULL CHECK(role IN
                          ('reader','contributor','editor','admin')),
            created_by_issuer TEXT NOT NULL,
            created_by_subject TEXT NOT NULL,
            created_at        TEXT NOT NULL,
            expires_at        TEXT,
            revoked           INTEGER NOT NULL DEFAULT 0
        );
        """

    # -- public helpers --

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def raw_query(self, sql: str, params: tuple | None = None) -> list[dict[str, Any]]:
        """Run a SELECT and return all rows as dicts."""
        with self._lock:
            conn = self.conn
            cur = conn.execute(sql, params or ())
            rows = cur.fetchall()
            return [dict(row) for row in rows]

    def raw_execute(self, sql: str, params: tuple | None = None) -> None:
        """Run a non-SELECT statement."""
        with self._lock:
            conn = self.conn
            conn.execute(sql, params or ())
            conn.commit()

