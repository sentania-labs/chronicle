Closes #60

## What changes for someone running it

The post-merge watcher no longer gets stuck retrying a branch delete that
already happened. GitHub answers a delete of a ref that is already gone
with a 422 and the message "Reference does not exist", not a 404, and the
watcher's delete call did not know that; every retry hit the same 422 and
gave up before it got to the step that actually flips the draft to
published or unpublished.

The stuck live draft (702ddce9d44b4fe8bba0c10297776927, blog PR #58,
merged 4:14 PM CDT Sept 22) recovers with no manual change on the first
watcher tick after this deploys. The watch file for that draft on disk is
untouched by this change; what changes is only how the watcher's code
reacts to it. Walking the next tick through the fixed code: `get_pull`
still reports the PR merged, `delete_ref` against the already-gone branch
now returns success instead of raising (the new 422 handling), the site
refresh runs (issue 57's stale-lock fix is already deployed, so this step
that used to fail now succeeds), and the draft's status is still
`approved` (this merge was never actually recorded, since the loop always
died before reaching that step), so the rest of the handler runs exactly
as it would for a fresh merge: the draft flips to `published` and the
watch clears.

## Why this needed more than the delete_ref fix

Fixing only the 422 meant `_handle_merged`'s later steps could now be
reached again after a delete that used to fail out. That surfaced a
second, unrelated problem the blind review caught: retrying the whole
handler after a partial success used the draft's *current* status to
decide what a merge means, and that status can already reflect an earlier,
successful run. A publish watch's own table entry
(`approved` to `published`) and an unpublish watch's own entry
(`published` to `unpublished`) share the same event name ("merged"),
so a retry that found the draft already `published` from its own first,
mostly-successful attempt could reapply the *unpublish* watch's entry and
flip a freshly published draft straight back to `unpublished`. The fix
now checks what a merged watch of its own kind is always supposed to
produce, once, before deciding whether to call `observe_pr_outcome` or
`record_publish_behind_draft` again. Full detail and how each finding was
verified (a failing test against the pre-fix code, then the fix) is in
[watcher-missing-ref-review.md](watcher-missing-ref-review.md).

## Blast radius

The api's watcher and GitHub client path (`chronicle/api/watcher.py`,
`chronicle/api/github_client.py`), plus `chronicle/api/store.py`
(`observe_pr_outcome`, `record_publish_behind_draft`, and
`_create_flag_unlocked`, keeping each idempotent against a retry) and
three new optional fields on existing models
(`FeedbackEntry.pr_number`, `Event.pr_number`,
`ReconcileFlag.built_version`/`current_version`) that a record written
before this PR simply loads as `None`. No route, no schema migration, no
change to what an admin or a consumer token can do. The publisher and
reconciler are untouched.

## Recovery if this is wrong

Revert the PR. The watcher goes back to its current behavior: it will
still get stuck the same way on a branch that is already gone, but
nothing here changes what is on disk or what state a draft can reach that
the pre-fix code could not also reach, so a revert is a plain behavioral
rollback with no data cleanup needed.

## Review

Blind adversarial review pass (a genuinely separate read of the diff,
trying to break it) found three real gaps in the first draft of this fix
and one out-of-scope note; every valid finding was fixed and re-verified
with a test that fails against the pre-fix code and passes after. Full
writeup: [watcher-missing-ref-review.md](watcher-missing-ref-review.md).

## What I saw working

`make check` green: lint, format check, mypy, prose check, and the full
unit test suite (1031 tests) all pass, including the four new tests that
fail against the pre-fix code and pass after
(`tests/test_watcher.py`, `tests/test_github_repo_ops.py`). This round did
not build or run the containers; the change is confined to code the unit
suite already exercises against a fake and a real `httpx.MockTransport`
GitHub client, not anything that needs a running service to observe.

## Scope note

A sibling lane (issue #61) is touching `chronicle/api/convert.py`,
`chronicle/api/routes/drafts.py`, `chronicle/api/routes/ui.py`, and
`chronicle/api/ui_templates.py` in parallel. None of those files were
touched here.

## Codex round

A Codex review of this open PR found two more real gaps, both fixed on
this branch: `observe_pr_outcome` could leave SQLite permanently behind
the draft file after a retry (a durable per-PR marker on the event and
feedback entry now makes it idempotent end to end, and this also closes
the `_handle_closed` note below), and `record_publish_behind_draft`
deduplicated on any open flag for the draft rather than the specific
merge (the flag now carries the `built_version`/`current_version` pair it
was raised for, and the guard matches on that pair). Full detail,
including each fix's own adversarial re-check, is in
[watcher-missing-ref-review.md](watcher-missing-ref-review.md)'s "Codex
round" section.

`_handle_closed` (the close-without-merge path) was noticed during the
first review pass to have the same shape of retry gap as one of the
findings above (a duplicate feedback entry and event on a retry after
`clear_watch` fails). Making `observe_pr_outcome` idempotent end to end in
the Codex round closes this too, since a second call from any caller,
`_handle_closed` included, is now always safe; no separate change to
`_handle_closed` was needed.
