"""The derived index (chronicle/api/index.py): schema upgrade on open.

The index is a rebuildable cache (ADR 006); opening an older on-disk schema
must never crash the process, it must drop and rebuild instead.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from chronicle.api.index import SCHEMA_VERSION, Index, index_path
from chronicle.api.store import Store

SCHEMA_V1 = """
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1');
CREATE TABLE submissions (
    id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL,
    from_name TEXT NOT NULL, claimed_by TEXT, draft_id TEXT);
CREATE INDEX submissions_status ON submissions(status);
CREATE TABLE drafts (
    id TEXT PRIMARY KEY, status TEXT NOT NULL, slug TEXT, title TEXT NOT NULL,
    updated_at TEXT NOT NULL, version_no INTEGER NOT NULL, claim_author TEXT);
CREATE INDEX drafts_status ON drafts(status);
CREATE INDEX drafts_slug ON drafts(slug);
CREATE TABLE versions (
    draft_id TEXT NOT NULL, version_no INTEGER NOT NULL, author TEXT NOT NULL,
    created_at TEXT NOT NULL, PRIMARY KEY (draft_id, version_no));
CREATE TABLE runs (
    id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, kind TEXT NOT NULL,
    status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX runs_status ON runs(status);
CREATE INDEX runs_draft ON runs(draft_id, created_at);
CREATE TABLE posts (
    slug TEXT PRIMARY KEY, path TEXT NOT NULL, title TEXT NOT NULL,
    date TEXT NOT NULL, sha TEXT NOT NULL);
CREATE TABLE images (
    image_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, filename TEXT NOT NULL,
    bytes INTEGER NOT NULL, mime TEXT NOT NULL);
CREATE INDEX images_sha ON images(sha256);
CREATE TABLE events (seq INTEGER PRIMARY KEY, payload TEXT NOT NULL);
"""


def _write_v1_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_V1)
        conn.execute(
            "INSERT INTO drafts (id, status, slug, title, updated_at, version_no, claim_author)"
            " VALUES ('d1', 'drafting', NULL, 'A Post', '2026-01-01T00:00:00-05:00', 0, NULL)"
        )
        conn.commit()
    finally:
        conn.close()


def test_opening_a_schema_v1_database_does_not_raise(tmp_path: Path) -> None:
    path = index_path(tmp_path)
    _write_v1_database(path)

    index = Index(path)
    try:
        assert index.schema_version() == SCHEMA_VERSION
        # The stale drafts row is gone with the dropped table, not carried
        # over: the index is a cache, reindex is what repopulates it.
        assert index.draft_ids() == []
    finally:
        index.close()


def test_store_open_recovers_a_schema_v1_data_directory(tmp_path: Path) -> None:
    _write_v1_database(index_path(tmp_path / "repo"))

    store = Store.open(tmp_path)
    try:
        assert store.index.schema_version() == SCHEMA_VERSION
        found = store.reindex()
        assert found["drafts"] == 0
    finally:
        store.close()
