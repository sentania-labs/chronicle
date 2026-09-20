"""The derived query index: SQLite over the files, never the record itself.

Nothing here is durable. Every row restates something a JSON file under
`data/repo/` (or an image sidecar under `data/images/`) already says, so the
database can be deleted at any moment and rebuilt with `chronicle reindex`.
See docs/decisions/006-sqlite-derived-index.md.

The index answers the list queries: records by status, a post by slug, an
image by sha256, events after a cursor. Single-record reads go to the file,
because the file is the truth.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import Draft, Event, Image, Post, Run, Submission, Version

SCHEMA_VERSION = 2
INDEX_DIR_NAME = "index"
INDEX_FILE_NAME = "chronicle.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS submissions (
    id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL,
    from_name TEXT NOT NULL, claimed_by TEXT, draft_id TEXT);
CREATE INDEX IF NOT EXISTS submissions_status ON submissions(status);
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY, status TEXT NOT NULL, slug TEXT, title TEXT NOT NULL,
    updated_at TEXT NOT NULL, version_no INTEGER NOT NULL, claim_author TEXT,
    image_dir TEXT);
CREATE INDEX IF NOT EXISTS drafts_status ON drafts(status);
CREATE INDEX IF NOT EXISTS drafts_slug ON drafts(slug);
CREATE INDEX IF NOT EXISTS drafts_image_dir ON drafts(image_dir);
CREATE TABLE IF NOT EXISTS versions (
    draft_id TEXT NOT NULL, version_no INTEGER NOT NULL, author TEXT NOT NULL,
    created_at TEXT NOT NULL, PRIMARY KEY (draft_id, version_no));
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, kind TEXT NOT NULL,
    status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS runs_draft ON runs(draft_id, created_at);
CREATE TABLE IF NOT EXISTS posts (
    slug TEXT PRIMARY KEY, path TEXT NOT NULL, title TEXT NOT NULL,
    date TEXT NOT NULL, sha TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS images (
    image_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, filename TEXT NOT NULL,
    bytes INTEGER NOT NULL, mime TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS images_sha ON images(sha256);
CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, payload TEXT NOT NULL);
"""


def index_path(repo_dir: Path) -> Path:
    return repo_dir / INDEX_DIR_NAME / INDEX_FILE_NAME


class Index:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._drop_if_stale()
        self.conn.executescript(SCHEMA)
        # Only a genuinely new database gets stamped. Overwriting the stored
        # value would make the readyz schema check compare the constant against
        # itself, so an old build opening a newer database could never say so.
        if self.schema_version() == 0:
            self.conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        self.conn.commit()

    def _drop_if_stale(self) -> None:
        """Drop every table left by an older schema before `SCHEMA` runs.

        `CREATE TABLE IF NOT EXISTS` never adds a column to an existing
        table, so a schema-v1 database opened by schema-v2 code would leave
        `drafts` without `image_dir` and crash on `CREATE INDEX ...
        drafts(image_dir)` before this class, or `_start_services`, ever
        gets a chance to notice the version mismatch. The index is a
        derived cache (ADR 006), so the safe fix is to drop the stale
        tables and let `reindex` rebuild them, not migrate column by
        column.

        Only an older stored version triggers this: a database stamped
        with a version newer than this build's SCHEMA_VERSION was written
        by code this build does not understand, and must be left exactly
        as found so `_index_check`'s readyz mismatch report stays honest
        (test_store.py:test_an_existing_schema_version_is_never_restamped).
        A newer schema is additive by convention, so this build's own
        `CREATE TABLE/INDEX IF NOT EXISTS` statements are no-ops against it
        either way.
        """
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
        ).fetchone()
        if row is None:
            return
        version_row = self.conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        stored = int(version_row["value"]) if version_row else 0
        if stored >= SCHEMA_VERSION:
            return
        tables = [
            r["name"]
            for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        for table in tables:
            self.conn.execute(f"DROP TABLE IF EXISTS {table}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def schema_version(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        return int(row["value"]) if row else 0

    def clear(self) -> None:
        for table in ("submissions", "drafts", "versions", "runs", "posts", "images", "events"):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()

    def upsert_submission(self, record: Submission) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO submissions"
            " (id, status, created_at, from_name, claimed_by, draft_id) VALUES (?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.status,
                record.created_at,
                record.from_,
                record.claimed_by,
                record.draft_id,
            ),
        )
        self.conn.commit()

    def upsert_draft(self, record: Draft) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO drafts"
            " (id, status, slug, title, updated_at, version_no, claim_author, image_dir)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.status,
                record.slug,
                record.title,
                record.updated_at,
                record.version_no,
                record.claim.author if record.claim else None,
                record.image_dir,
            ),
        )
        self.conn.commit()

    def upsert_version(self, record: Version) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO versions (draft_id, version_no, author, created_at)"
            " VALUES (?, ?, ?, ?)",
            (record.draft_id, record.version_no, record.author, record.created_at),
        )
        self.conn.commit()

    def upsert_run(self, record: Run) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO runs (id, draft_id, kind, status, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (record.id, record.draft_id, record.kind, record.status, record.created_at),
        )
        self.conn.commit()

    def upsert_post(self, record: Post) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO posts (slug, path, title, date, sha) VALUES (?, ?, ?, ?, ?)",
            (record.slug, record.path, record.title, record.date, record.sha),
        )
        self.conn.commit()

    def remove_post(self, slug: str) -> None:
        self.conn.execute("DELETE FROM posts WHERE slug = ?", (slug,))
        self.conn.commit()

    def upsert_image(self, record: Image) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO images (image_id, sha256, filename, bytes, mime)"
            " VALUES (?, ?, ?, ?, ?)",
            (record.image_id, record.sha256, record.filename, record.bytes, record.mime),
        )
        self.conn.commit()

    def remove_image(self, image_id: str) -> None:
        self.conn.execute("DELETE FROM images WHERE image_id = ?", (image_id,))
        self.conn.commit()

    def add_event(self, record: Event) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO events (seq, payload) VALUES (?, ?)",
            (record.seq, json.dumps(record.model_dump(mode="json"))),
        )
        self.conn.commit()

    def submission_ids(self, status: str | None = None) -> list[str]:
        return self._ids("submissions", "id", "created_at", status)

    def draft_ids(self, status: str | None = None) -> list[str]:
        return self._ids("drafts", "id", "updated_at", status)

    def _ids(self, table: str, column: str, order: str, status: str | None) -> list[str]:
        sql = f"SELECT {column} AS value FROM {table}"
        params: tuple[Any, ...] = ()
        if status is not None:
            sql += " WHERE status = ?"
            params = (status,)
        sql += f" ORDER BY {order}"
        return [row["value"] for row in self.conn.execute(sql, params)]

    def post_slugs(self) -> list[str]:
        return [row["slug"] for row in self.conn.execute("SELECT slug FROM posts ORDER BY slug")]

    def pinned_slugs(self, exclude_draft_id: str | None = None) -> set[str]:
        rows = self.conn.execute(
            "SELECT slug FROM drafts WHERE slug IS NOT NULL AND id IS NOT ?",
            (exclude_draft_id,),
        )
        taken = {row["slug"] for row in rows}
        return taken | set(self.post_slugs())

    def pinned_image_dirs(self, exclude_draft_id: str | None = None) -> set[str]:
        rows = self.conn.execute(
            "SELECT image_dir FROM drafts WHERE image_dir IS NOT NULL AND id IS NOT ?",
            (exclude_draft_id,),
        )
        return {row["image_dir"] for row in rows}

    def image_id_for_sha(self, sha256: str) -> str | None:
        row = self.conn.execute(
            "SELECT image_id FROM images WHERE sha256 = ?", (sha256,)
        ).fetchone()
        return row["image_id"] if row else None

    def last_run_id(self, draft_id: str, kind: str | None = None) -> str | None:
        sql = "SELECT id FROM runs WHERE draft_id = ?"
        params: tuple[Any, ...] = (draft_id,)
        if kind is not None:
            sql += " AND kind = ?"
            params += (kind,)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT 1"
        row = self.conn.execute(sql, params).fetchone()
        return row["id"] if row else None

    def run_ids_with_status(self, status: str, kind: str | None = None) -> list[str]:
        sql = "SELECT id FROM runs WHERE status = ?"
        params: tuple[Any, ...] = (status,)
        if kind is not None:
            sql += " AND kind = ?"
            params += (kind,)
        sql += " ORDER BY created_at"
        return [row["id"] for row in self.conn.execute(sql, params)]

    def run_counts(self, kind: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS total FROM runs WHERE kind = ? GROUP BY status", (kind,)
        )
        return {row["status"]: row["total"] for row in rows}

    def last_run_id_of_kind(self, kind: str) -> str | None:
        row = self.conn.execute(
            "SELECT id FROM runs WHERE kind = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (kind,),
        ).fetchone()
        return row["id"] if row else None

    def events_since(self, cursor: int, limit: int = 200) -> list[Event]:
        rows = self.conn.execute(
            "SELECT payload FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (cursor, limit)
        )
        return [Event.model_validate_json(row["payload"]) for row in rows]

    # Statuses that a `to_status` an author's own hands never wrote: the
    # first is a reviewer sending the draft back, the rest are every event
    # type that leaves the author's side (a submit to review, and the
    # terminal outcomes review can end in). See `ui_status.came_back_from_review`.
    _REVIEW_ANSWERED_TO_STATUSES = ("in_review", "approved", "published", "rejected", "unpublished")

    def revision_answer_seqs(
        self, draft_ids: list[str]
    ) -> dict[str, tuple[int | None, int | None]]:
        """Per draft id, the newest event seq moving it to `revision_requested`
        and the newest event seq moving it out of the author's hands again
        (a submit back to review, or review ending in approval, publish,
        rejection or unpublish). One query for the whole set of ids, not one
        per id: `events` has no index on a JSON field, so this is a single
        scan of the table rather than a scan per draft. A draft with no
        events at all is absent from the result; one with events but no
        matching `to_status` still appears, with both seqs None."""
        if not draft_ids:
            return {}
        placeholders = ",".join("?" for _ in draft_ids)
        answered = ",".join("?" for _ in self._REVIEW_ANSWERED_TO_STATUSES)
        rows = self.conn.execute(
            "SELECT json_extract(payload, '$.draft_id') AS draft_id,"
            "       MAX(CASE WHEN json_extract(payload, '$.to_status') = 'revision_requested'"
            "                THEN seq END) AS request_seq,"
            f"      MAX(CASE WHEN json_extract(payload, '$.to_status') IN ({answered})"
            "                THEN seq END) AS answered_seq"
            " FROM events"
            f" WHERE json_extract(payload, '$.draft_id') IN ({placeholders})"
            " GROUP BY draft_id",
            (*self._REVIEW_ANSWERED_TO_STATUSES, *draft_ids),
        )
        return {row["draft_id"]: (row["request_seq"], row["answered_seq"]) for row in rows}
