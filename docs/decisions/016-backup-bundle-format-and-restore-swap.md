# 016: backup bundle format, checksum verification, and the restore swap

- **Status:** accepted
- **Date:** 2026-09-17

## Context

Spec section 13 describes the bundle's contents (`repo/` with `.git`,
`images/`, the encrypted portion of `state/`) and says restore validates
and replaces. It does not say how validation refuses a corrupted or
tampered bundle, how the swap avoids leaving a half-written data directory
if it fails partway, or what happens when the bundle's `.git` history is
missing, which section 16's hand-built migration bundle guarantees will
happen at least once.

## Decision

**Format.** A gzip tarball with `manifest.json` at its root and three
top-level trees: `repo/` (including `.git`, excluding the derived
`repo/index/`, which `chronicle reindex` always rebuilds), `images/`, and
`state/` holding only `tokens.json`, `admin.json`, `github-app.json`, and
`ui_disabled` when present. `manifest.json` carries `schema_version` (1),
`created_at`, `chronicle_version`, per-record `counts`, and a sha256 for
every other member keyed by its archive path; the manifest is not hashed
against itself. Bundle name: `chronicle-backup-<UTC stamp>.tar.gz`, so a
directory of bundles sorts chronologically without reading any of them.

**Restore validates before it touches anything.** Every tar member is
checked before extraction: no absolute path, no `..` segment, no
symlink or hardlink, nothing outside the staging directory. This is
independent of the checksum step and runs first, because a path-traversal
member is a problem with the member itself, not with what it contains.
Extraction happens into a staging directory under the data directory
itself (`<data_dir>/.restore-staging-<stamp>`), never into the live tree
and never under the data directory's parent: `CHRONICLE_DATA_DIR` is the
one path a deployment guarantees is a writable mounted volume (compose, a
PVC in the k8s reference); its parent is the container's root filesystem,
frequently read-only and never owned by the uid the process runs as
(found live in the C6 check: staging under the data directory's parent
raised `PermissionError` against a real compose stack). After extraction,
the member set and every checksum are verified against the manifest; a
missing member, an extra member the manifest never listed, or any
checksum mismatch refuses the whole restore before the data directory's
own `repo/`, `images/`, or `state/` files are touched at all.

**The swap keeps the old tree until reindex succeeds.** `repo/`, `images/`,
and the four `state/` files are moved (not copied) from staging into
place one at a time, and whatever they displace goes to
`<data_dir>/.pre-restore-<stamp>/` rather than being deleted. Only after
`chronicle reindex` (called at the end of `restore_backup`) succeeds are
the staging and pre-restore directories removed. A reindex failure raises
before that cleanup, so the operator has both the new tree (already live)
and the displaced old one to recover from by hand; this is not a full
rollback, because the files are already moved into place by the time
reindex could fail, but nothing that was previously on disk is deleted
until reindex has proven the new tree readable.

**Restore requires the api stopped.** This mirrors ADR 013's own
single-replica assumption for the publisher: nothing in this round adds a
lock between an in-flight request and a restore's tree rename, so `chronicle
backup restore` is an operator command run against a stopped instance, not
an endpoint the running api process serves against itself. The admin
`/admin/backup` upload path is the one exception spec section 10 asks for;
it shells out to the same `restore_backup` function but only after the
confirmation step, and Scott accepts the brief window where in-flight
requests during that call see a data directory mid-swap, the same way any
process restart already does.

**No `.git` is not an error.** A bundle whose `repo/` has no `.git`
directory (the hand-built migration bundle docs/backup.md describes) gets
one initialised on restore, via the same `gitrepo.init_repo` every fresh
`Store.open` already calls: one commit, authored `chronicle`, over
whatever files the bundle carried.

**The instance key is never restored.** `restore_backup` never writes
`state/instance.key`; the running instance's own key (created on first
start, ADR 008) decrypts whatever credentials the bundle's `state/
github-app.json` carries only if that bundle was created by an instance
using the same key. A best-effort decrypt check runs at the end of
restore and is surfaced on the admin page rather than raised as an error,
because a credential store that fails to decrypt is a fact to report
(re-run the GitHub App manifest flow), not a reason to fail a restore that
otherwise succeeded.

## Consequences

- A tampered, truncated, or hand-edited bundle is refused before any file
  in the data directory changes, which is what makes the admin upload path
  safe to expose to a browser upload at all.
- The hand-built import bundle (docs/backup.md) needs no `.git` at all,
  which is what lets it be assembled from the vault's content-drafts and
  Wit's feedback files directly, one JSON file and one sidecar at a time,
  without a git binary in the loop.
- A restore's window with the api stopped is a real operational cost load
  balancers and cron jobs both have to accept; a second api replica or a
  hot restore path both stay explicitly out of scope for this round, the
  same way ADR 013 leaves them for the publisher.
