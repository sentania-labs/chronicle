# Backup bundle format

Precise enough that a hand-built import bundle (spec section 16's
migration: the vault's content-drafts and Wit's feedback files) can be
assembled without reading the code. See ADR 015 (image directory) and
ADR 016 (bundle format and the restore swap) for the reasoning; this
document is the reference for the shape itself.

## Layout

A bundle is a gzip tarball (`chronicle-backup-<UTC stamp>.tar.gz`) with
these top-level entries:

```
manifest.json
repo/
  submissions/<id>.json
  submissions/<id>/versions/<n>.json
  drafts/<id>/draft.json
  drafts/<id>/versions/<n>.json
  feedback/<draft_id>.jsonl
  feedback/<draft_id>.md          (derived, not read back; optional)
  posts/<slug>.json
  runs/<id>.json
  events/log.jsonl
  .git/                            (optional; restore initialises one if absent)
images/
  <sha256[:2]>/<sha256>.<ext>
  <sha256[:2]>/<sha256>.json
state/
  tokens.json
  admin.json
  github-app.json
  ui_disabled                      (only if present: a marker file, empty)
```

Never present, and refused if found (`chronicle backup restore` treats an
unlisted member as tampering): `state/instance.key`, `state/claim-code`,
`state/ui_token.txt`, `preview/`, `site/`, `builder-work/`,
`repo/index/` (the derived SQLite cache, ADR 006; `chronicle reindex`
rebuilds it after restore, so shipping it would only go stale).

## manifest.json

```json
{
  "schema_version": 1,
  "created_at": "2026-09-17T14:30:00+00:00",
  "chronicle_version": "0.1.0.dev0",
  "counts": {
    "drafts": 2,
    "submissions": 0,
    "versions": 3,
    "images": 3,
    "posts": 0,
    "tokens": 1
  },
  "files": {
    "repo/drafts/<id>/draft.json": "<sha256 hex>",
    "...": "..."
  }
}
```

- `schema_version`: currently `1`. Restore refuses any other value with a
  clear error rather than guessing at a migration.
- `files`: every other member's archive path (exactly as it appears in the
  tarball, forward slashes, no leading `/`) mapped to the sha256 of its
  bytes, hex-encoded lowercase. `manifest.json` itself is not in this map
  and is not hashed against itself.
- Restore refuses the whole bundle if any listed file is missing, any
  extracted file is not listed, or any hash does not match. This is
  checked before anything in the data directory is touched.

## Records

Every record is one JSON file, `model_dump(mode="json", ...)` of the
corresponding pydantic model in `chronicle/api/models.py`; fields not
listed below default the same way the model does. Required fields are the
ones a fresh empty string or `null` would break; everything else is
optional and may be omitted (the model's default applies).

### Submission: `repo/submissions/<id>.json`

```json
{
  "id": "3f9a1c2e4b6d47a2b1e0c8d9f7a6b5c4",
  "created_at": "2026-08-01T09:00:00-05:00",
  "from": "ghostwriter",
  "brief": "A post about the UniFi network integration",
  "materials": [{"name": "notes", "text": "raw notes text", "url": null}],
  "image_ids": [],
  "status": "new",
  "claimed_by": null,
  "draft_id": null,
  "version_no": 1
}
```

Required: `id`, `created_at`, `from`, `brief`, `status` (one of `new`,
`claimed`, `drafted`, `discarded`). `version_no` is optional and defaults to
1, so a submission written before submissions could be revised (ADR 018)
restores unchanged.

### Draft: `repo/drafts/<id>/draft.json`

```json
{
  "id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
  "created_at": "2026-08-01T09:00:00-05:00",
  "updated_at": "2026-08-01T10:00:00-05:00",
  "slug": "vcf-operations-can-now-see-my-unifi-network",
  "image_dir": "vcf-operations-can-now-see-my-unifi-network",
  "title": "VCF Operations Can Now See My UniFi Network",
  "frontmatter": {
    "title": "VCF Operations Can Now See My UniFi Network",
    "date": "2026-08-01T14:39:00-05:00",
    "url": "/2026/08/vcf-operations-can-now-see-my-unifi-network/",
    "tags": ["vcf-operations", "unifi"]
  },
  "body": "# The problem\n\n...",
  "status": "published",
  "version_no": 2,
  "source_submission": null,
  "source_post": null,
  "images": [
    {
      "image_id": "9f8e7d6c5b4a...",
      "filename": "featured.png",
      "role": "feature",
      "source_ref": "/images/vcf-operations-can-now-see-my-unifi-network/featured.png"
    }
  ],
  "claim": null,
  "published": null
}
```

Required: `id`, `created_at`, `updated_at`, `status` (one of the values in
`chronicle/api/models.py:DRAFT_STATUSES`), `version_no` (an integer, 0 for
a draft with no saved version yet). `frontmatter.title` is required by
`check_frontmatter` on every real save; a hand-built bundle should set it
to match `title`. `image_dir` (ADR 015): the last non-empty path segment
of `frontmatter.url` when set, the pinned `slug` otherwise; leave it null
only for a draft with no pinned slug yet (`status: drafting`, never
previewed or approved).

**Minting a draft id by hand:** 32 lowercase hex characters, the same
shape `uuid.uuid4().hex` produces (`chronicle/api/store.py:new_id`).
Any random 32-hex-character string works; it only has to be unique
within the bundle.

### Version: `repo/drafts/<id>/versions/<n>.json`

One file per save, `<n>` matching `version_no` in the filename and in the
body. `n` starts at 1; there is no `versions/0.json`.

```json
{
  "draft_id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
  "version_no": 1,
  "author": "scott",
  "created_at": "2026-08-01T09:30:00-05:00",
  "base_version": 0,
  "message": "first draft",
  "frontmatter": {"title": "..."},
  "body": "..."
}
```

Required: all fields except `message` (defaults to `""`). `version_no`
values for one draft must be a contiguous run starting at 1 with no gaps;
`draft.version_no` must equal the highest one present.

### Feedback: `repo/feedback/<draft_id>.jsonl`

One JSON object per line (JSONL, not a JSON array), append-only:

```json
{"draft_id": "a1b2...", "author": "scott", "created_at": "2026-08-01T11:00:00-05:00", "action": "request_revision", "version_no": 1, "text": "tighten the opening paragraph"}
```

Required: all fields. `action` is whatever action produced the feedback
(`request_revision`, `reject`, `pr_closed`, `publish_failed`, ...), not
constrained to a fixed list. The parallel `repo/feedback/<draft_id>.md` is
a human-readable rendering of the same entries; Chronicle never parses it
back, so a hand-built bundle may omit it entirely.

### Image: `images/<sha256[:2]>/<sha256>.json` sidecar, `images/<sha256[:2]>/<sha256>.<ext>` blob

```json
{
  "image_id": "9f8e7d6c5b4a3928f0e1d2c3b4a59687...",
  "sha256": "9f8e7d6c5b4a3928f0e1d2c3b4a59687...",
  "filename": "featured.png",
  "bytes": 182933,
  "mime": "image/png"
}
```

`image_id` and `sha256` are the same value: the sha256 of the normalised
image bytes (`chronicle/api/images.py:normalise`), hex-encoded. **Minting
an image id by hand:** `sha256sum <file>` and use that hex digest for both
`image_id` and `sha256`, and for the sidecar's and blob's directory
(`sha256[:2]`) and filename. `<ext>` is whatever Pillow would have decoded
the format as (`png`, `jpg`, `webp`, `gif`); match the real file's format.
A draft's `images[].image_id` must match a sidecar that exists in the
bundle, or the import silently has no image to attach.

### Post: `repo/posts/<slug>.json`

Only present for a published post digest has already recorded; a
hand-built migration bundle importing unpublished drafts does not need
any of these. `chronicle digest` regenerates them from main after
restore either way.

```json
{"slug": "vcf-operations-can-now-see-my-unifi-network", "path": "content/posts/2026-08-01-vcf-operations-can-now-see-my-unifi-network.md", "title": "...", "date": "2026-08-01T14:39:00-05:00", "sha": "<git blob sha>"}
```

### Run: `repo/runs/<id>.json`

Not needed for a migration bundle (a run is an artifact of a build or
publish attempt, not authored content); omit `repo/runs/` entirely if
there is nothing to carry over.

### Event log: `repo/events/log.jsonl`

Also not needed for a migration bundle. Restore's reindex does not read
it to reconstruct anything drafts, versions, or images depend on; it
exists for the events API and the admin status page's own history. An
absent or empty file is fine; `chronicle reindex` just finds nothing to
add.

### state/tokens.json

```json
{"tokens": [{"name": "ghostwriter", "hash": "<sha256 of the token bytes, hex>", "created_at": "2026-08-01T09:00:00-05:00", "last_used_at": null, "revoked_at": null}]}
```

A hand-built bundle should normally omit this or ship an empty
`{"tokens": []}`: reissuing tokens on the restored instance
(`chronicle token issue <name>`) is simpler and safer than trying to
reconstruct a hash that must match a secret only the original instance
ever saw in plaintext.

### state/admin.json, state/github-app.json

Only ever produced by the running service (a password claim, a completed
GitHub App manifest flow); both are encrypted or hashed under the
originating instance's `instance.key`. Omit both from a hand-built
bundle. Restore leaves the running instance unclaimed (it goes through
`/admin/claim` normally) if `state/admin.json` is absent, and leaves
GitHub unconfigured (re-run the manifest flow) if `state/github-app.json`
is absent or fails its post-restore decrypt check.

## Git history

`repo/.git` is optional. If a bundle's `repo/` has no `.git` directory at
all, `chronicle backup restore` initialises one on restore: a single
commit, authored `chronicle`, over whatever files the bundle carried
(`chronicle/api/gitrepo.py:init_repo`, the same call every fresh
`Store.open` already makes on an empty data directory). This is what lets
a hand-built bundle skip having a git binary in its assembly path
entirely; just lay out the files above and tar them up.

## After a restore

Two steps the restore command itself does not do, because neither is
part of what the bundle carries (ADR 016):

- **Restart the builder.** It holds its own long-lived connection to
  `repo/index/chronicle.db`, opened before the swap; it does not notice
  the file underneath it has been replaced. Found live in the C6 check:
  the next build after a restore failed with `sqlite3.OperationalError:
  attempt to write a readonly database` and kept failing until the
  builder process (or container) was restarted. The CLI path documents
  restore as an operator command run against a stopped instance for this
  reason, meaning the builder as well as the api; the admin page's live
  restore is the one exception, and its confirmation page names the
  restart as the required next step.
- **Run `chronicle digest` again.** `site/` (the Hugo working checkout)
  is deliberately not part of the bundle, the same reasoning as excluding
  the derived `repo/index/`. A preview or publish attempted before the
  first post-restore digest fails (Hugo has no config to build against);
  digest is idempotent and cheap, so running it once right after restore
  is the normal next step, not a special recovery path.

## Worked example: two drafts, one with feedback and two images

```
manifest.json
repo/
  drafts/
    11111111111111111111111111111111/
      draft.json                # status: in_review, version_no: 1, no images
      versions/
        1.json
      # (no feedback/ entry: nothing has reviewed this one yet)
    22222222222222222222222222222222/
      draft.json                # status: revision_requested, version_no: 2, 2 images
      versions/
        1.json
        2.json
  feedback/
    22222222222222222222222222222222.jsonl   # one request_revision entry
images/
  9f/
    9f8e7d6c....json
    9f8e7d6c....png
  a1/
    a1b2c3d4....json
    a1b2c3d4....jpg
```

`draft 22222222222222222222222222222222`'s `images` field lists both
`image_id`s above; its `frontmatter.featureImage` (if set) should
reference one of them by whatever `source_ref` the original post used, or
be left unset for a draft that never had one.

## sha256 rule, restated

Every file under `repo/` (except `repo/index/`) and `images/`, plus the
four listed `state/` files when present, appears once in `manifest.json`'s
`files` map, keyed by its exact archive path, valued by the sha256 hex
digest of its bytes. `sha256sum <path>` on the extracted tree reproduces
every value; a bundle assembled by hand should compute these last, after
every file is in its final place.
