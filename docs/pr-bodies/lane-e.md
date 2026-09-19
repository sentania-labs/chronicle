# Lane E: four correctness fixes (issues 41, 28, 23, 25)

**A save can no longer put unapproved text into a publish PR.** Approve now
holds the draft (text and images) until the publish run has opened its PR or
failed, and the run itself refuses to convert any version other than the one
approved. Seen live against `sentania-labs/chronicle-target`: before, the
ghostwriter's post-approval save landed in the PR; after, it is refused with a
409 and the PR carries the approved text. The other three issues are closed
with named tests, listed below.

## Operational summary

- **What changes for someone running it.** A draft in `approved` with a publish
  run queued or building refuses saves, image attach and image detach with
  `409 publish_run_in_progress`. In normal operation the window is the
  publisher's poll interval (default 5 seconds) plus the run. Saving works
  again the moment the PR exists (then `publish_pr_open` applies, as before) or
  the run fails (the draft returns to `in_review`).
- **Blast radius.** The store, convert, publisher and route layers. No template,
  asset or CSS change. No data migration: `Run.approved_version` is a new
  optional field, absent on runs queued before this change, and the publisher
  skips its check for those.
- **Recovery.** Revert the branch. Nothing written by the new code needs to be
  understood by the old code (an extra optional field on a run record is
  ignored on load).
- **Known cost.** See "What the gate costs" under issue 41.

## Issue 41: a save between approve and the publish PR

### Decision: refuse the save (gate), and pin the approved version as a backstop

`Store.save_draft`, `attach_image` and `detach_image` refuse while the draft's
publish run is `queued` or `building`, using the same 409 code
(`publish_run_in_progress`) the re-approve path already used for that exact
window. `approve` stamps `Run.approved_version`, and `publisher._publish`
fails the run with `draft_moved_since_approval` (draft back to `in_review`,
`chronicle` feedback entry, no PR) if the draft it reads is at any other
version.

### Why this shape

1. **It is the rule the code already had, extended over the gap it missed.**
   `save_draft` already refuses while a publish PR is open (`publish_pr_open`).
   The intent was clearly "nothing changes between approval and merge". The
   only hole was the stretch before the PR exists. Closing it is a one-line
   extension of an existing invariant, not a new behaviour to learn.
2. **The approval keeps meaning one thing.** Approval is Scott's act, through
   the `ui` token only. Under the other shape, any consumer token could
   silently revoke it by saving. With the gate, the record can only say
   `approved` when the text is the approved text, so "approved, and nothing
   changed" stays true by construction rather than by convention.
3. **A refusal is loud, a silent revoke is not.** The writer is told at the
   moment it acts (409, names the run and the approved version). The
   alternative leaves the writer believing its save is what will publish.
4. **The pin is still worth having.** The gate lives in the store; the
   publisher reads the draft on its own. The pin means the publisher's safety
   does not depend on every other writer having remembered the gate, and it
   makes a crash-recovered or requeued run safe. It is cheap: one optional int.

### What the other shape would have cost

"A save during a queued or running publish invalidates the approval" (a
transition back to `in_review`) is not a smaller change:

- The run still has to be stopped. There is no cancel path for a queued run
  today, and one that is already `building` cannot be stopped from the store,
  so it needs the pin anyway. That option is the pin plus a cancel mechanism
  plus a new row in `transitions.py`, not instead of them.
- It puts a new transition into the one table, `approved` + `revise`, where a
  save by a ghostwriter changes a status Scott set. `resolve_save` is
  deliberately status-by-table; adding a writer-driven exit from `approved` is a
  policy change the spec does not ask for.
- Pin only (convert exactly the approved version, ignore the newer save) leaves
  the record lying in the other direction: status `approved`, version N+1 on
  disk, a PR carrying N, and a later merge reported as `published_behind_draft`.
  The existing watcher machinery for that case exists to flag a race, not to
  make it routine.

### What the gate costs (be aware of this)

- **An approved draft whose run can never run is frozen for edits.** If no
  GitHub App or test-token repo is configured, `publisher.tick` leaves the run
  `queued` indefinitely ("leaving queued"). While it is queued the draft
  refuses saves and image changes, and it already refused every action
  (`approved` has no exit but re-approve, which is itself blocked while a run is
  queued). Before this change a save was accepted, but it left the draft
  `approved` with the run still queued, so nothing actually became possible.
  There is no admin route to cancel a queued run. Recovery is configuring the
  target, which lets the run drain. A cancel route is a follow-up, not part of
  this branch.
- **The editor shows the refusal after the click, not before.** The Publish
  offer already reads `publish_run_active`; the Save form does not. That is a
  template change (out of this lane) and is listed as a follow-up. The save
  itself is not lost silently: the response is the editor with the refusal
  message, and the editor's local backup is unchanged.

### Live check (before and after)

Setup: `docker compose -p chronicle-lane-e`, api service only, host port 8180,
`CHRONICLE_ALLOW_TEST_TOKEN=1`, `CHRONICLE_GITHUB_TEST_REPO=sentania-labs/chronicle-target`,
the test token piped from `gh auth token` into the compose process (never
written to a file), `CHRONICLE_PUBLISH_POLL_SECONDS=30` to make the
approve-to-PR window wide enough to drive by hand. "Before" is an image built
from the parent of the fix (`git archive` of e6c2c8c, source identical to
origin/main). "After" is an image built from the final branch head. Times are
local (America/Chicago). Both PRs were closed and their branches deleted
afterwards; the target repo has no open PRs and only `main`.

**Before the fix** (PR content shows the unapproved text):

```
readyz: {"ready":true,"checks":[{"name":"data_dir","ok":true,"detail":"/data"},{"name":"git","ok":true,"detail":""},{"name":"index","ok":true,"detail":"schema version 2"},{"name":"github_app","ok":true,"detail":"test token mode"}]}
draft: bf1ea85322cc4d3eb46c114e6aded505
--- save v1 (the text Scott reviews and approves)
status drafting version 1
--- approve (ui token) at 19:25:58
status approved run d16317a5313246c990268955dcfd3c1a
--- ghostwriter saves v2 while the publish run is queued, no PR yet
HTTP 200
{'status': 'approved', 'version_no': '2'}
--- wait for the publish run to open its PR
pr_url: https://github.com/sentania-labs/chronicle-target/pull/10
{'status': 'approved', 'branch': 'post/lane-e-race-before', 'pr_url': 'https://github.com/sentania-labs/chronicle-target/pull/10'}
--- draft record now
status approved version_no 2
--- files changed by the PR, and the post text GitHub holds for it
{"base":"main","head":"post/lane-e-race-before","number":10,"state":"open","title":"Publish: Lane E race before"}
file: content/posts/2026-09-18-lane-e-race-before.md
---
title: Lane E race before
date: '2026-09-18T19:26:12-05:00'
draft: false
url: /2026/09/lane-e-race-before/
---
UNAPPROVED TEXT: nobody previewed or approved this.
```

**After the fix** (save and attach refused, PR content is the approved text):

```
readyz: {"ready":true,"checks":[{"name":"data_dir","ok":true,"detail":"/data"},{"name":"git","ok":true,"detail":""},{"name":"index","ok":true,"detail":"schema version 2"},{"name":"github_app","ok":true,"detail":"test token mode"}]}
draft: 5fca0b4050e4472d8fc8858d1296e006
--- save v1 (the text Scott reviews and approves)
status drafting version 1
--- approve (ui token) at 19:37:56
status approved run 06e38c4540284a66ac610bf5ab7831ac
--- ghostwriter saves v2 while the publish run is queued, no PR yet
HTTP 409
{'error': 'publish_run_in_progress', 'message': 'draft 5fca0b4050e4472d8fc8858d1296e006 was approved at version 1 and its publish run is queued; it cannot be saved until the run has opened its pull request or '}
--- ghostwriter uploads and attaches an image while the run is still queued
attach HTTP 409
{'error': 'publish_run_in_progress', 'message': 'draft 5fca0b4050e4472d8fc8858d1296e006 was approved at version 1 and its publish run is queued; it cannot be given a new image until the run has opened its pull request o'}
--- wait for the publish run to open its PR
pr_url: https://github.com/sentania-labs/chronicle-target/pull/12
{'status': 'approved', 'branch': 'post/lane-e-race-after', 'pr_url': 'https://github.com/sentania-labs/chronicle-target/pull/12'}
--- draft record now
status approved version_no 1
--- files changed by the PR, and the post text GitHub holds for it
{"base":"main","head":"post/lane-e-race-after","number":12,"state":"open","title":"Publish: Lane E race after"}
file: content/posts/2026-09-18-lane-e-race-after.md
---
title: Lane E race after
date: '2026-09-18T19:38:25-05:00'
draft: false
url: /2026/09/lane-e-race-after/
---
APPROVED TEXT: this is what was previewed and approved.
```

## Issue 28: an image directory name is one plain segment

- **Reproduced.** `convert.image_dir_name` returned the last non-empty segment
  as found. Probing what the issue did not: `/a/.` pins `.` (path
  `static/images/./shot.png`) and `/a/b\c` pins `b\c` (a backslash is a
  separator on some platforms and in browsers' URL parsing). Percent-encoded
  `%2e%2e` is a traversal to a browser, so it is covered too.
- **Chose: refuse at save, fall back where a refusal is impossible.**
  `PUT /v1/drafts/{id}` with a `url` whose last segment is `.`, `..`,
  contains a backslash or control character, has leading or trailing
  whitespace, is `.git`, or decodes to one of those, is `422
  frontmatter_url_invalid` (names the url and the reason). Only a url being set
  or changed is judged, so a draft that already carries one (import from main,
  an older save) is never frozen. Where refusing is impossible the pinned
  slug names the directory: an import (warning returned by `create_draft`), a
  submission seed (the `url` key is dropped with a warning, like any key the
  save path would refuse), `_pin_slug`, and `convert.convert` on a draft that
  already pinned a bad `image_dir`.
- **Why refuse rather than always fall back.** A save is where the caller can
  see and fix the value. Falling back silently at preview would pin a
  directory the caller never chose and never hears about, since preview and
  approve return no warnings channel.
- **ADR 015 amended in the same commit** (rule, grandfathering, fallbacks).
- **Regression tests.** `test_convert.py::test_image_dir_name_falls_back_when_the_last_segment_is_not_a_plain_directory_name`,
  `test_convert_does_not_trust_a_pinned_traversal_image_dir`,
  `test_store.py::test_save_refuses_a_url_whose_last_segment_is_not_a_directory_name`,
  `test_api_drafts.py::test_put_with_a_traversal_url_is_422_frontmatter_url_invalid`,
  `test_from_post.py::test_from_post_with_an_unusable_url_segment_falls_back_and_warns`,
  `test_submission_seeding.py::test_a_seeded_url_that_cannot_name_an_image_directory_is_dropped_with_a_warning`.

## Issue 23: submission image ids

- **Reproduced.** `create_submission` stored any string; the detail page's
  `get_image` then 404'd the whole page.
- **Chose: match revise.** `create_submission` (so `POST /v1/submissions`) runs
  the same shape-and-existence check revise uses, now one helper
  (`_require_images`), and answers `422 image_not_found` naming the first bad
  id, before anything is written.
- **Existing bad records.** The detail route no longer calls `get_image` per id.
  It renders the images that exist, names the missing ids in the page notice,
  and hands the edit form only real ids so saving heals the record. A frozen
  (drafted or discarded) submission gets the notice without the promise of a
  save. Follow-up for the template: list the missing ids inline in the images
  section instead of only in the notice (needs a template change, not made).
- **Regression tests.** `tests/test_submission_image_ids.py` (create refused and
  named, first bad id among good ones, POST 422, detail page renders with a
  missing image, healed on save, frozen wording). One existing seeding test
  (`test_an_unknown_image_id_is_a_warning_not_a_failure`) now writes the bad id
  onto the record directly, because create can no longer produce that state;
  its assertions are unchanged.

## Issue 25: seeded material in the changes feed

- **Reproduced.** Draft seeded from a submission with two extra materials:
  `changes?since=0` returns both, `since=1` returns none.
- **Chose: always include.** Feedback with `action == "material"` is returned at
  every `since`, in the same `feedback` list and log order it already had at
  `since=0`, so a client that reads it there needs no change. The ghostwriter
  has no memory between sessions and the material is reference, not review of
  any version, so a version cutoff is the wrong rule for it. Cost: it repeats on
  every call, bounded by the submission's size, written once. The alternative
  (`create_draft` returning it, or a separate key) puts the burden on the client
  to have stored it or to learn a new field.
- **Cutoff for review feedback unchanged** and now pinned by a test.
  Documented in `changes_since` beside the existing cutoff comment, and in spec
  section 6's description of the route.
- **Regression tests.** `test_submission_seeding.py::test_changes_feed_always_carries_the_seeded_reference_material`,
  `test_changes_feed_still_cuts_ordinary_feedback_off_at_since`,
  `test_changes_feed_over_the_wire_keeps_material_at_since_one`.

## Regression tests for issue 41

`test_publisher.py`: `test_save_between_approve_and_publish_run_never_reaches_the_pr`,
`test_save_while_publish_run_is_building_is_refused`,
`test_save_is_allowed_again_once_the_publish_run_has_failed`,
`test_publish_run_refuses_a_draft_that_moved_past_the_approved_version`,
`test_attach_and_detach_are_refused_while_a_publish_run_is_in_flight`,
`test_detach_is_refused_while_a_publish_run_is_in_flight`;
`test_api_drafts.py::test_save_after_approve_is_409_publish_run_in_progress`.
Each was run red against the unfixed code before the fix went in.

## Adversarial review

One non-author reviewer subagent, given only the diff and the one-sentence
scope. It reported eight findings.

| # | Finding | Disposition |
|---|---------|-------------|
| 1 | Image attach/detach change what the run converts without bumping the version, so the save gate and the pin both missed them (confirmed by the reviewer by running it) | **Fixed.** Same gate on `attach_image`, `detach_image` and the editor upload; live-checked. |
| 2 | An imported draft whose main-side `url` is unusable could never be saved again, because the refusal judged the stored value on every save | **Fixed.** Only a new or changed url is judged; ADR 015 amended. |
| 3 | An approved draft whose run can never run (no target configured) is frozen for saves | **Not changed; documented above as a cost.** A save never got such a draft out of `approved`, and there is no cancel route. A cancel route is a follow-up. |
| 4 | Unpublish has the same save race (a save during a queued unpublish moves `published` to `drafting`, and the merge then has no transition) | **Not changed; out of the stated scope (publish).** Note for a separate issue. |
| 5 | `usable_image_dir` and `url_problem` disagreed on whitespace, so the fallback could return a value convert then distrusted; `.git` allowed | **Fixed.** One rule, whitespace and `.git` rejected, a test ties them together. The broader point (the new rule is looser than `SLUG_PATTERN`) is deliberate: an imported directory name has to be whatever main already uses. |
| 6 | The missing-image notice promised a save that a frozen submission has no form for | **Fixed.** Wording depends on whether the submission is editable. |
| 7 | An encoded segment like `my%20post` is judged decoded but used raw, so the published URL 404s | **Not changed; pre-existing** and a different defect (encoding, not traversal). Note for a separate issue. |
| 8 | The editor never states the save restriction before the click; `_offer_state` duplicated the queued/building test | **Duplication fixed** (`Store.active_publish_run`, one definition). **The pre-click message is a template change** and is left as a follow-up. |

The reviewer found nothing in issue 25's change, in lock re-entry, or in the
publisher's read-then-check window.

## Not exercised

- The publisher backstop (`draft_moved_since_approval`) is covered by a unit
  test only. Reaching it live needs a writer that bypasses the store gate, and
  there is none by design.
- Nothing here ran against a real GitHub App installation, only test-token
  mode (ADR 012), which drives the same `GitHubRepoOps` calls.
- Issues 28, 23 and 25 were verified by tests over the store and HTTP
  boundary, not by a live compose run. The editor pages were not rendered in a
  browser (no template changed).
- CI has not run; `make check` passed locally.

## Follow-ups for other issues (not done here)

1. Unpublish has the same save race as publish (finding 4).
2. Admin cancel or requeue for a stuck queued run (finding 3).
3. Template: disable the Save form while `publish_run_active`, and list missing
   submission image ids inline (finding 8, issue 23).
4. A url segment that is percent-encoded is used raw as the directory name
   (finding 7).
5. `Store.get_image` builds a filesystem path from the id it is given without
   the shape check the submission paths now use (`GET /v1/images/{image_id}`
   passes a URL path segment straight in). Not probed for exploitability here;
   worth a look.

## Codex review round

Finding (P1, `store.py`): `active_publish_run` read `last_run`, which goes
through the SQLite index. If approval wrote the run and queue files but died
before the index update, the gate saw no run, and an image attach or detach
(which does not bump `version_no`) got past it and past the publisher's
approved-version backstop.

Changed: `active_publish_run` now scans the queue entries for the draft (a
queue entry lives from `_queue_run` to `finish_run`, so the queue holds only
unfinished runs) and confirms each against its run record, so a finished run
with a leftover entry is not counted. No index read remains.

The re-approve guard in `_act_on_draft_unlocked` had the same hole (it read
`last_run` before this branch), and it now calls `active_publish_run`, so one
fix covers it. Other reads added on this branch: `get_watch` is file-based;
the editor's `last_run` calls only decide which button to show, and the
store-side gate is authoritative, so they stay on the index.

Test: `test_publish_gate_reads_durable_files_when_the_index_lacks_the_run`.
