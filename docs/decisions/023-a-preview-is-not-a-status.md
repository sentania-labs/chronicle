# 023: a preview is not a status, and the UI shows four words

- **Status:** accepted
- **Date:** 2026-09-24

## Context

A successful preview build moved a draft `drafting` or `in_review` to
`previewed` (issue #70). The UI labelled `previewed` as "Draft" and
`in_review` as "In review", so building a preview of a submitted post made
it look like it had gone backwards. From the editor's side "Draft" and "In
review" are one stage: written, waiting on him. `approved` was labelled "In
review" with a separate "Approved" detail badge, so an approved post showed
both.

## Decision

- A preview build never changes a draft's status. Having a current preview
  is a property of the draft, decided by `store.preview_is_current` from the
  run's `built_text` (issue #69), not a stage of the lifecycle.
  `RUN_OUTCOME_TRANSITIONS` has no `preview_succeeded` row, and nothing in
  `DRAFT_TRANSITIONS` produces or leaves `previewed`.
- `previewed` stays in `DRAFT_STATUSES` only so a record written before this
  change loads. `Store.migrate_previewed`, run from `Store.open`, moves each
  such draft to the `from_status` of its latest `draft.preview_succeeded`
  event (`drafting` or `in_review`; `in_review` when there is none), writing
  a `draft.status_migrated` event and one commit. It is idempotent.
- The UI shows four words: Draft (`drafting`, `in_review`,
  `revision_requested`, `unpublished`, and a legacy `previewed`), Approved,
  Published, Rejected. One badge per draft; the "Approved" detail is gone.
  "Came back from review" stays, read from the event stream as before.
- `/v1` keeps the long status names. `previewed` is simply no longer
  produced. `GET /v1/drafts/{id}/status` gains `has_current_preview`, so an
  API consumer can still tell whether the current text has been previewed.
- The editor's "Preview first" gate is unchanged: Publish from `drafting`
  stages `submit` then `approve` once a current preview exists, the same way
  it staged from `previewed`.

## Consequences

- An API consumer that waited for `status == "previewed"` must read
  `has_current_preview` instead. `preview_url` is not a substitute: it keeps
  naming the last successful build after a text save makes that build
  stale.
- The spec's lifecycle (section 5) no longer has a `previewed` branch.
