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
