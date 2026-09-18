# 018: submissions are mutable while open, and seed the draft made from them

- **Status:** accepted
- **Date:** 2026-09-18

## Context

Two gaps in the same flow. "Create post from this submission" produced a
blank draft even when the submission carried a finished 13k-character post,
so the post was pasted back in by hand. And a submission was write-once: a
submitter who noticed a mistake had to file a second submission.

## Decision

**Seeding.** `Store.create_draft(from_submission=...)` fills the draft. The
primary material is the first whose text has a frontmatter block or opens
with a markdown heading, else the first with any text. It becomes the body;
frontmatter is parsed with `digest.parse_frontmatter` and allowlisted with
the same drop-and-warn rule an import uses, and the title comes from
frontmatter or, failing that, the leading heading. Every other material is
reference for the writer, so it is written to the draft's feedback log
(action `material`, authored by the creating consumer, labelled with the
material's name and carrying its url) and never to the body. The
submission's `image_ids` attach as `inline` images. A submission with no
materials still yields a blank draft.

**Mutability.** `PUT /v1/submissions/{id}` replaces brief, materials and
image ids, guarded by `base_version` exactly like a draft save (409 with a
unified diff when stale). All three content fields are required, so an
omitted field is a 422 and never "delete every material", and every image id
must already be in the image store (422 `image_not_found`). A submission is version 1 as posted; each
revision writes `submissions/<id>/versions/<n>.json`, one `submission.revise`
event, and one internal git commit authored by the acting consumer, so the
diffs are in `git log -p` like every other record. It is editable while
`new` or `claimed`; once `drafted` or `discarded` it is frozen and the API
answers 409 `submission_frozen`, saying edits belong to the draft. The rule
lives in `transitions.py` next to the rest of the lifecycle.

## Consequences

- `Submission.version_no` defaults to 1, so every record already on disk
  loads unchanged. A legacy record has no version file for its version 1;
  its first revision writes one, in the same commit, from the pre-revision
  content. Until then a stale write against it gets a 409 whose diff summary
  says no diff is available.
- The index gains nothing: it has no version column and does not need one,
  because a single-record read comes from the file (ADR 006). `SCHEMA_VERSION`
  is unchanged.
- A revision after `claimed` is allowed, so a draft can be created from a
  submission whose materials changed since the claim; the draft takes what
  the submission holds at the moment of creation.
- Backup needs no change: `backup.py` collects `repo/` with `rglob`, so the
  version files travel in the bundle, and the reindex a restore runs reads
  only the top-level `submissions/*.json` records.
- Rollback: a release without `version_no` drops the field from any
  submission it rewrites (a claim or discard), and rolling forward again
  reads it as version 1 while the `versions/<n>.json` files remain, so the
  next revision overwrites them. Forward compatibility is unaffected; only a
  rollback followed by a roll forward loses the version counter.
