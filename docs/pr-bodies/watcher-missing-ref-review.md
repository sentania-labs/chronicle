# Blind adversarial review: watcher stuck on delete_ref_failed (issue 60)

Full diff against origin/main written to a file and read as a reviewer
whose job is to break it, per CONTRIBUTING.md's "reviewed before it opens"
bar. Two passes: the first against the diff as first committed, the second
against the fix from the first pass's findings.

## Findings

### P1: retry after a partial success could flip a published draft back to unpublished (confirmed, fixed)

`store.observe_pr_outcome` decides the status change from
`WATCH_TRANSITIONS`, keyed by `(draft.status, event)`. That table has two
entries for `event == "merged"`: `("approved", "merged") -> "published"`
(a publish watch's own transition) and `("published", "merged") ->
"unpublished"` (an unpublish watch's own transition). The first draft of
this fix guarded a retry only by re-running the exact same
`_handle_merged` steps unconditionally, meaning: if a publish-kind watch's
first `_handle_merged` call already flipped the draft to `published` and
then failed before `clear_watch` (a crash, a disk write failure, anything
after that point), the watch stays open and the next tick calls
`_handle_merged` again. `observe_pr_outcome` runs a second time, reads the
now-`published` status, and matches the *unpublish* kind's table entry:
the freshly published draft flips straight to `unpublished`.

Verified live: reverted watcher.py to the pre-fix commit, ran a new test
(`test_publish_retry_after_clear_watch_failure_does_not_misapply_unpublish`)
that flips a publish-kind watch's `clear_watch` to fail once, then retries.
Failed with `assert 'unpublished' == 'published'` at
`tests/test_watcher.py:259` before the fix.

**Disposition: fixed.** `_handle_merged` now computes the status a merged
watch of its own kind always implies (`"published"` for `publish`,
`"unpublished"` for `unpublish`) once, before touching the draft, and
skips `observe_pr_outcome` (and `record_publish_behind_draft`, see P2)
when the draft's current status already matches it. This is stable across
a retry, unlike the `from_status` `WATCH_TRANSITIONS` keyed off originally,
which a first successful call already moved past.

### P2: a retry between record_publish_behind_draft and observe_pr_outcome could double the content_drift flag (confirmed, fixed)

The first fix's `already_observed` guard is keyed on `draft.status`, and
`draft.status` only moves once `observe_pr_outcome` succeeds. A retry
whose earlier attempt got as far as `record_publish_behind_draft`
succeeding, then failed on the very next line (`observe_pr_outcome`
itself, or anything after it up to `clear_watch`), reaches
`record_publish_behind_draft` again with `draft.status` unchanged from
before, so `already_observed` is still `False` on the retry and
`record_publish_behind_draft` runs a second time: two `content_drift`
flags and two `published_behind_draft` feedback entries for one merge.

Verified live: reverted only the `store.py` guard (kept everything else),
ran a new test
(`test_retry_after_observe_pr_outcome_failure_does_not_double_flag_drift`)
that fails `observe_pr_outcome` once after a successful
`record_publish_behind_draft`. Failed with `assert 2 == 1` at
`tests/test_watcher.py:223` before the fix.

**Disposition: fixed.** `record_publish_behind_draft` now checks for its
own already-open flag before creating one, the same convention
`reconcile.py`'s `_already_flagged` already uses for its own flag types.

### P3: the P2 fix's first draft could suppress a genuinely new flag behind an unrelated one (confirmed, fixed)

The first draft of the P2 fix matched on `flag.draft_id == draft_id`
alone. `reconcile.py`'s own `content_drift` check (`_content_drift`,
main content moved since the last publish, an unrelated cause) creates a
`content_drift` flag for the same draft with `slug=draft.slug` set,
whereas `record_publish_behind_draft` always writes `slug=None`. A draft
that still has an old, unresolved reconcile-created `content_drift` flag
open (admin has not gotten to it yet) when it is later revised,
re-approved, and republished would have this method's own, genuinely new
flag silently suppressed by the unrelated one, matching only on
`draft_id`.

This cannot happen through the call path this round actually exercises
(`record_publish_behind_draft` only ever runs while `draft.status !=
"published"`, and reconcile's `_content_drift` only ever runs once it is),
but the guard as first written did not depend on that invariant holding,
so a later change to either caller could silently reintroduce it.

**Disposition: fixed defensively.** The guard now also checks
`flag.slug is None`, matching only flags this method itself could have
written. Test
`test_publish_behind_draft_flag_ignores_an_unrelated_slug_keyed_content_drift_flag`
seeds an unrelated slug-keyed flag first and asserts the draft still gets
its own.

### N1: _handle_closed has the same class of feedback-duplication gap, out of scope (noticed, not fixed)

`_handle_closed` (the close-without-merge path) calls `observe_pr_outcome`
then `clear_watch`, same shape as `_handle_merged`. A retry after
`observe_pr_outcome` succeeds but `clear_watch` fails would call
`observe_pr_outcome` again: `WATCH_TRANSITIONS` has no entry keyed on the
post-transition status for the closed event (`in_review` is not a
`(status, "closed")` key), so this cannot mis-transition the status the
way P1 did, but it would still append a second `github`-authored
"PR closed" feedback entry and a second `draft.pr_closed` event for the
one close.

Issue 60 and this dispatch's requested outcome both name `_handle_merged`
specifically ("Make `_handle_merged` safely retryable end to end"); this
is the same shape of bug in `_handle_closed` but is not part of what was
asked, and fixing it would touch a code path the review pass did not
otherwise need to touch. Left as a one-line note for a follow-up issue
rather than folded into this diff.

## What did not need a change

- `remove_post` and `clear_watch` were already idempotent (both check the
  target's existence before acting) and needed no guard.
- `refresh_from_target` (digest) is already safe to call repeatedly:
  `apply_digest`'s created/updated/unchanged accounting is the same
  whether it is the first or the Nth call against the same content.
- `delete_ref`'s new 422 branch only matches the literal GitHub message
  "Reference does not exist"; a different 422 (a real conflict, a
  malformed request) still raises `delete_ref_failed`, and a non-JSON 422
  body is treated as a real failure rather than silently swallowed
  (`test_delete_ref_treats_a_non_json_422_body_as_a_real_failure`).

## make check

Green after every fix in this pass. Final run: 1031 passed (unit test
count from the full suite, `chronicle/`'s own watcher- and client-level
tests included).

## Codex round (PR #62)

A second review pass, this time from Codex against the open PR, found two
more real gaps in `observe_pr_outcome` and `record_publish_behind_draft`.
Both fixed on this branch, each with a test that fails against 3e1e9d6
(this PR's head before this round) and passes after.

### Codex finding 1 (P2): a retry after the draft file write but before the commit or index upsert left SQLite permanently stale

`observe_pr_outcome` wrote the draft file's new status first, then the
`pr_closed` feedback entry, then the event, then the commit and the index
upsert. If it raised from `_append_event`, `_commit`, or
`index.upsert_draft`, the watch stayed open (nothing had cleared it) and
the next tick called the method again, but the old code decided whether
to do anything at all by comparing `draft.status` against
`resolve_watch`'s table, which only understands a `from_status` a first
successful call has already moved past. In practice this meant a second
call after a status-only partial write silently skipped every remaining
step, including the index upsert, so `list_drafts` and the UI (both
SQLite-backed) showed the draft stuck at its old status forever while the
draft file itself already carried the new one, and the watch could never
be retried again to fix it since a caller checking `already_observed`
the same way `_handle_merged` does would see the file's new status and
conclude there was nothing left to do.

Verified live: added
`test_retry_after_index_upsert_failure_completes_observation_exactly_once`,
which fails the index upsert once via monkeypatch on a close-without-merge
watch. Before the fix, `store.get_draft(draft.id).status` moved to
`in_review` on the first (raising) call but `list_drafts(status="in_review")`
never found it, even after a clean retry, because the retry's own
`draft.status == to_status` shortcut (there was none in the old code; the
old code's guard was in the caller) meant nothing re-ran the missing
steps. Fails at `assert len(events) == 1` on 3e1e9d6 (asserting `2 == 1`,
the old always-append-event path re-appending on the retry) once the test
is run against that commit, confirming both the staleness and a
double-append hazard in the same call.

**Disposition: fixed.** `observe_pr_outcome` now decides `to_status` from
a durable, already-written event (`_find_pr_event`, keyed on
`draft_id`/event/`pr_number`) when one exists, and from `resolve_watch`
only when it does not; the feedback entry and the event are each written
only if their own durable marker (`pr_number` on each) is not already on
disk; and the commit plus the index upsert always run, gated only on
whether `draft.status` already equals the decided `to_status` for the
file write. A retry after any partial failure completes exactly the
missing steps and appends nothing twice. `_handle_merged`'s
`already_observed` guard now gates only `record_publish_behind_draft`
(the invariant its own P1 fix still needs); `observe_pr_outcome` is
always called, since it is now safe to call on an already-fully-applied
merge.

Adversarial re-check: does a retry ever double-write? The event and
feedback writes are each gated on a positive existence check
(`_find_pr_event`/`_find_pr_closed_feedback` returning `None`), not on
`draft.status`, so a retry that finds the marker present skips the write
regardless of what `draft.status` says; the draft-file write and the
index upsert are both naturally idempotent (writing the same status
twice, or upserting the same row twice, changes nothing). A three-way
race between two overlapping calls is not possible here: the method is
`@locked`. This also closes the PR body's earlier scope note about
`_handle_closed` sharing the same duplication hazard on a `clear_watch`
failure: `observe_pr_outcome` being idempotent on its own now makes a
second call from any caller, including `_handle_closed`, safe, with no
change to `_handle_closed` itself needed.

### Codex finding 2 (P2): publish-behind dedup matched any open flag for the draft, not the specific merge

`record_publish_behind_draft`'s guard matched a still-open,
slugless `content_drift` flag on `draft_id` and `slug is None` alone. A
draft revised and republished while an earlier publish-behind flag from a
prior cycle was still unresolved (admin has not gotten to it) would have
a second, genuinely different merge (a different `built_version`/
`current_version` pair, since the draft moved on) silently produce
neither a new flag nor a new feedback entry, because the old flag alone
was enough to short-circuit the guard.

Verified live: added
`test_record_publish_behind_draft_dedupes_per_merge_not_per_draft`, which
calls the method twice with two different `built_version`/
`current_version` pairs for the same draft, then a third time repeating
the first pair. Against 3e1e9d6, the second, distinct call produces no
second flag: `assert len(flags) == 2` fails as `1 == 2` right after the
two distinct calls.

**Disposition: fixed.** `ReconcileFlag` now carries `built_version` and
`current_version`, set only by `record_publish_behind_draft` via
`_create_flag_unlocked`. The dedup guard adds both fields to its match,
so only a flag from the *same* merge (identical pair) short-circuits a
retry; a distinct pair for the same draft is not suppressed. A flag
written before these fields existed loads with `built_version=None`,
which never equals a live call's always-set integer, so an old flag from
before this round is left alone rather than mistaken for a match, and a
retry of the same merge still dedupes to one flag
(`test_record_publish_behind_draft_dedupes_per_merge_not_per_draft`'s
third call).

Adversarial re-check: does an old flag on disk (no `built_version` field)
ever get treated as a match for a live call? No: Pydantic loads a missing
field as the model default (`None`), and `None == built_version` is only
true if the live call itself passed `None`, which
`record_publish_behind_draft`'s only caller (`watcher.py`'s
`_handle_merged`) never does (`watch.built_version` is required for the
call to happen at all, per the existing `is not None` guard). Does this
change the meaning of an already-open flag from before this round for an
admin resolving it on the status page? No: `built_version`/
`current_version` are additive fields read only by this dedup check and
never rendered or otherwise consulted.

## make check (Codex round)

`CHRONICLE_REQUIRE_TEST_TOOLS=1 make check` green: lint, format, mypy, the
full unit suite (1035 passed, including the two new tests above), and
prose check.
