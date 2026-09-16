# 006: SQLite as a derived index, outside git tracking

- **Status:** accepted
- **Date:** 2026-09-16

## Context

Spec section 4 says an index is "derived from the files and rebuildable", and
section 13 puts `index/` inside `repo/`. It does not say what the index is or
how it is kept out of the way of the history that shares that directory.

The API's list routes are all filtered queries: submissions by status, drafts
by status, a post by slug, an image by sha256, events after a cursor. Served
from the filesystem alone, each one is a directory walk that opens and parses
every JSON record to answer a question about one field. That is fine at a
dozen drafts and wrong at the scale the migration already brings in (spec
section 16: 12 pending, 16 published, 12 rejected, plus a growing event log
that supervisors poll on a cursor).

## Decision

The index is a SQLite database at `data/repo/index/chronicle.db`, holding one
table per record type plus `schema_meta` with a `schema_version` row. It is a
query cache and nothing else: every row restates a field of a JSON file under
`data/repo/`, or of an image sidecar under `data/images/`. Single-record reads
go to the file, never to the index, so a stale index can never become the
answer to "what does this draft say".

It is excluded from git tracking by `repo/.gitignore` (`index/*`), committed
once when the repository is initialised. `/readyz` opens the index and
compares its `schema_version` against the constant the running code expects,
so a container running older or newer code than the database on the volume
reports not-ready instead of serving wrong answers.

**What is lost if the database is deleted: nothing.** `chronicle reindex`
walks `repo/` and `images/` and rebuilds every row from the JSON and markdown
files, which remain the durable record. Deleting it while the service is
stopped is a supported recovery step, not a data loss event.

## Consequences

- A binary database is never committed. Committing one on every write would
  make `git log -p` on `repo/` useless (a new opaque blob per commit) and
  would grow the repository without bound, for an artifact that is disposable
  by design.
- Restoring a backup bundle does not restore the index, because the bundle
  does not contain one. The first start after a restore runs `reindex`, or an
  operator runs it by hand; either way the durable files decide.
- The index can drift if a file is edited by hand under `repo/` without going
  through the API. That is a real and accepted gap: the fix is `reindex`, and
  the same command is the answer to any suspicion about a list result.
- Adding a query later means a schema change and a `SCHEMA_VERSION` bump,
  with no migration to write: the rebuild is the migration.

## Alternatives considered

**Directory scans with no index.** Simplest, no second artifact to keep
honest, and the answer for the first month. Rejected because every list route
and every event poll would be O(all records), and the event cursor in
particular is polled on a schedule by consumers that exist on day 0 (spec
section 15).

**A database of record instead of files.** Rejected in ADR 001 and not
reopened here: filesystem first is the point of the service.

**Committing the index to git anyway.** Rejected for the diff and size
reasons above. There is no recovery value in the history of a derived
artifact that a single command regenerates.
