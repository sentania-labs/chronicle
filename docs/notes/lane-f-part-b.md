# Lane F, part B: Import tab removed, five UI backlog fixes

Branch `feat/ui-four-words`, on top of part A. No PR opened. `store.py`,
`convert.py`, `publisher.py`, `images.py` and `ci.yml` were not touched.

## 1. Import tab removed (closes #20)

Gone: the nav link, `GET` and `POST /content/import`, `_untracked_posts`,
`_import_listing`, `import_page`, `IMPORT_TAB`, `_frontmatter_date`, and the
tests that covered them (`tests/test_ui_import_date.py` deleted; the import
section of `tests/test_ui.py` deleted). A new test asserts both routes 404 and
the nav no longer links to it.

Kept, untouched: `from_post` in the store and on `POST /v1/drafts`,
`_fill_from_post`, `resolve_flag`'s `import_as_draft`, `tests/test_from_post.py`.

One test I did not simply delete: the one proving a draft's creation warnings
(a dropped frontmatter key) show on the editor once and not on reload. It used
the import route only as its way in. It now goes through the submission's
"Create post" route, which still uses the same flash.

Docs: the spec's Content tab paragraph and Scott's-role line no longer list
import, and say `from_post` remains an API and reconciliation capability with
no UI. `docs/authoring-flow.md` lost its Import entry. ADR 017 still mentions
`Import as draft` (the reconciliation resolution, which is real) and was left
alone.

## 2. Backlog fixes

- **#22 CRLF.** `draft_save` now runs the body through the same `_crlf_to_lf`
  the submission edit form uses. The conflict view gets the normalised body
  too. Tests: posting a CRLF body over an LF-stored base stores LF and diffs
  as exactly one removed and one added line; a stale-save conflict page
  carries no `\r`. A separate test covers the other case directly: posting
  over a base that was itself stored with CRLF (an import, or any writer
  other than this route) still normalises the posted body to LF, so the
  diff comes out as every line removed and re-added, not one. Three tests,
  all fail without the fix. Only the body is normalised, as on the
  submission form. Consequence to know: a post imported from main with real
  CRLF endings will have every line ending rewritten on its first UI save.
  Fixing that needs normalising on read or on import, which lives in
  store.py; not done here.
- **#45 first half.** The Save button is now in a `data-refresh` region
  (`#save-control`), disabled with a readable reason beside it while a publish
  run is queued or building. The instruction named the run case; the store
  refuses on an open publish PR the same way, so I applied the same lock there
  (the page already had a PR notice, the button just stayed live). `editor.js`
  looks the button up by id each time instead of holding it (the swap replaces
  the element), reads `data-locked`, and Ctrl+S refuses with the reason instead
  of firing a doomed POST. Python and node tests cover it.
- **#45 second half.** A submission's Images list now shows a missing id in
  place: the escaped id, a `Missing` warn badge, "not in the image store". The
  heading count stays the number actually shown. The notice above the page is
  kept (it says saving removes them). Escaping tested with a script-tag id.
- **#26.** New `_multiline`: escape first, then turn newlines into `<br>`
  (CRLF and lone CR count as one break). Used on the feedback log. The same bug
  was on a submission's material text, in my file, so it is fixed there too.
  Test: a note with CRLF, blank line, `<b>`, `<script>` and `&` renders three
  `<br>` and no live tags.
- **#35 caching.** See below.

## Caching approach for #35: revalidate always

`/static` is now `RevalidatingStaticFiles` (`chronicle/api/main.py`), sending
`Cache-Control: no-cache` on every asset response, 304 included. A browser may
keep a copy but must ask before each use; Starlette already sends an ETag, so
an unchanged file costs a small 304 and a changed one is fetched. It cannot
serve a stale file across a deploy.

I chose this over immutable-with-fingerprint because fingerprinting needs a
hash in every template that names an asset plus a build step, and one missed
reference reproduces the exact failure. For an internal tool with a dozen small
files and one editor, revalidating costs nothing. The reasoning is in a comment
above the class.

Residual risk, stated in that comment: the ETag is mtime plus size. A rebuild
that changed a file's bytes but kept its size and mtime would revalidate as
unchanged. A fresh checkout does not do that. If it ever matters, the fix is a
content-hash ETag, not a longer cache.

A missing file's 404 is raised out to the app's error handler and carries no
`Cache-Control`; it also carries no validator, so nothing can reuse it. Tests:
header present on four assets, on a 304, sent once, and absent from `/healthz`.

## 3. ADR 019

Appended a dated amendment (2026-09-19). It covers the `warn` tone moving off
the status badge onto `Came back from review`, and, since it was equally stale,
the consequence line saying static assets carry no `Cache-Control`. The
decision text is unchanged.

## Where the instructions were wrong or incomplete

- The instruction said `_offer_state` in `routes/ui.py` computes
  `publish_run_active` and "the other buttons use it". True, but Save was not
  one of them and also ignored `publish_pr_open`, which the store refuses on
  equally. Handled both.
- #45's "not offered" for Save could not be a plain omission: the button lives
  in the sticky editor bar outside the swappable regions and `editor.js`
  captured the element once, so it had to become a refreshed region and the JS
  had to stop caching the element.
- The Import tab's test for the warnings flash was not import-specific; see
  section 1.

## Broken in the other lane's files

Nothing found. Nothing was fixed there.

## Fix round (2026-09-19/20)

An independent reviewer went over this branch at `62d3a02` and filed seven
findings, lettered A through G. Commits `ca4fbce` through `2596ea9`, plus the
formatting-only `7fcc248`, are the response. `store.py`, `convert.py`,
`publisher.py`, `images.py` and `ci.yml` were still not touched.

- **A: "Came back from review" reappeared after a resubmit.** The reviewer's
  scenario: `revision_requested`, a preview (to `drafting`), a resubmit (to
  `in_review`), then a further preview success lands on `previewed`. Nothing
  in the feedback log orders that resubmit against the old request (`submit`
  and `approve` write no feedback entry), so the badge was reading a stale
  verdict. Fixed in `ca4fbce`: `came_back_from_review`
  (`chronicle/api/ui_status.py`) now checks the log only while the status is
  `drafting`, never `previewed`. What the badge catches now: a draft sitting
  in `revision_requested`, or sitting in `drafting` with `request_revision`
  as the newest feedback verdict, in both cases unless it has a `published`
  record (still read as answered, per the existing published-record
  exception). What it no longer catches: a draft that reached `previewed`
  straight out of `revision_requested` on its first preview, with no
  resubmit in between. That draft's badge now disappears one step earlier
  than "resubmitted", the moment the preview succeeds, because `previewed`
  cannot be told apart from the already-answered case with the log alone.
  This is a known, accepted gap, not a new bug: closing it needs the store to
  record something the log can order a resubmit against, which is out of
  this lane's files. `chronicle/api/ui_status.py`'s docstring now states
  this directly (it no longer claims `previewed` coverage anywhere), and
  `tests/test_ui_status.py` covers both the still-caught case and the
  no-longer-caught one.
- **B: the UI author is `editor`, docs still said `scott`.** `tokens.py`
  already had `UI_COMMIT_AUTHOR = "editor"`; the prose describing the wire
  contract had not caught up. Fixed in `c08d2ce`: `AGENTS.md`,
  `docs/spec/00-spec.md`, `docs/decisions/014-ui-backend.md`,
  `docs/decisions/001-filesystem-first-internal-git.md`, and
  `chronicle/api/routes/ui.py`'s module docstring now all say `editor`, each
  noting that records written before the rename still say `scott` and are
  not rewritten. The two ADRs got a dated amendment rather than a rewrite of
  the original decision text, matching how ADR 019 was already handled in
  part A.
  Not fixed, reporting instead: the reviewer's related nit that token names
  are not validated or reserved (`chronicle/api/tokens.py`), so a consumer
  token literally named `editor` would author indistinguishably from the UI.
  The old value `scott` was a name nobody would pick for a token by
  accident; `editor` is a word an operator naming a new agent token could
  plausibly choose. Fixing it means deciding a reserved-name policy for
  token creation, which is a `tokens.py` concern outside this lane's file
  list. Reported here, not fixed, and not filed as an issue by this round.
- **C: the CRLF test proved the wrong thing; a CRLF-stored post is
  rewritten wholesale on first UI save.** The existing test seeded an LF
  base and posted CRLF over it, which only proves browser CRLF gets
  normalised; it never tested a base that was itself stored with CRLF. Fixed
  in `4d5d083`: the test was renamed to say what it actually covers
  (`test_editor_save_stores_lf_when_posting_over_an_lf_base`), and a new
  test, `test_editor_save_over_a_crlf_stored_base_rewrites_every_line`, adds
  coverage for the CRLF-stored-base case the note already disclosed but no
  test proved.
  Not fixed, reporting instead: a post imported from main with real CRLF
  line endings, or written by any caller other than this UI route, still
  has every line rewritten on its first save through this editor, because
  only `draft_save`'s `_crlf_to_lf` normalises, on the way in, and nothing
  normalises what is already on disk. The resulting version diff shows every
  line removed and re-added instead of the one line that actually changed,
  and (unverified here) the publish PR built from that version would carry
  the same full-file diff on the blog side. The real fix is normalising on
  read or on import, both of which live in `store.py`, outside this lane's
  file list. This is the first of the two items this fix round is reporting
  rather than fixing because the file that would need to change belongs to
  another lane.
- **D: `?status=drafting,drafting` rendered every card twice.**
  `parse_status_filter` concatenated per-status lists with no
  de-duplication; a hand-typed URL with a repeated status doubled its cards.
  Fixed in `dc31908`: it now builds the list through a dict to de-duplicate
  while preserving order. `tests/test_ui_status.py` covers a repeated value
  and a repeated value mixed with a distinct one.
- **E: locked Save stays locked until a reload; no live browser check.**
  Deliberately not fixed. If a publish PR merges or closes in another tab,
  or a publish run finishes, the editor has no polling and no push channel,
  so `#save-control` only refreshes on the next save, upload, or staged
  action in that same tab; until then the button can show a stale
  "in progress" lock a few seconds after the run has actually finished. The
  reviewer flagged this as a nit, not a should-fix, and closing it for real
  means adding a polling loop or a push mechanism to `editor.js`, which is a
  design decision (poll interval, what triggers a re-render, added load on
  every open editor tab) beyond what a fix round should decide on its own.
  Left as-is; a candidate for its own pass if Scott wants it.
- **F: `setSaveBusy`'s lock-check guard had no test.** Reverting
  `if (button && saveLockReason() === null)` to `if (button)` in
  `chronicle/api/static/editor.js`'s `setSaveBusy` left `tests/test_js.py`
  green: the guard existed in code (a locked Save button already stays
  disabled after a save response) but nothing proved it. `editor.js` itself
  was not touched, because the behaviour was already correct. Fixed in
  `62f9f50`: a new test in `tests/editor.test.mjs`
  ("a save response that swaps in a locked Save button is not re-enabled
  afterwards (#45)") drives a save whose response swaps in an already-locked
  `#save-control` and asserts the resulting Save button stays disabled; it
  goes red against the reviewer's mutation and green against the real code.
- **G: dead `chr-missing-image` CSS hook.** `ui_templates.py` emitted
  `class="chr-missing-image"` on a submission's missing-image row with no
  matching CSS rule and no JS reading it. Fixed in `8c44c50` (class dropped)
  and reflowed in `7fcc248` (the row's line wrapping, which the class
  removal left ragged, put back to one line). `tests/test_ui_submission_edit.py`
  updated to match the row's new markup.

Both items reported above, not fixed, need a file this lane does not own:
the CRLF-stored-base rewrite needs normalisation in `store.py` (finding C),
and the token-name collision needs a reserved-name policy in
`chronicle/api/tokens.py` (finding B's nit). Neither is in this lane's file
list; both are left for whichever lane owns those files next.

## Second fix round (2026-09-20): the previewed badge, closed exactly

Finding A's fix round accepted a known gap rather than closing #37 for real:
a draft previewed straight out of `revision_requested`, never resubmitted,
lost the badge one step earlier than before, because the feedback log has
no entry for `submit` or `approve` and so cannot order a resubmit against an
old request. `models.py`'s `Event` (`draft_id`, `from_status`, `to_status`,
`seq`, `ts`) can: `store._append_event` writes one for every status change,
`submit` and `approve` included, so the event stream orders a resubmit
against a request exactly where the feedback log could not.

- **What changed.** `chronicle/api/index.py` gets one new read-only method,
  `Index.revision_answer_seqs(draft_ids)`: for each draft id, the newest
  event seq with `to_status == "revision_requested"`, and the newest event
  seq with `to_status` in `("in_review", "approved", "published",
  "rejected", "unpublished")`, meaning the draft was moved back out of the
  author's hands. One SQL query, `GROUP BY` the JSON `draft_id` field,
  covering every id the caller asks for at once. Nothing else in
  `index.py` changed: no schema, no new table, no write path, so the
  other lane touching `store.py` cannot conflict with this file.
  `ui_status.came_back_from_review` now takes `request_seq` and
  `answered_seq` instead of a feedback-action list: `revision_requested` is
  still true unconditionally (reaching that status this round always is the
  fact), `drafting` and `previewed` are true exactly when `request_seq` is
  newer than `answered_seq` (or there is no answer yet), and every other
  status is false without looking at either seq. `routes/ui.py` computes
  the seqs (through `services.store.index`, never touching `store.py`) and
  passes the resulting bool into both `_board_row` and the editor page;
  `ui_templates.py`'s `editor_page` now takes `came_back` as a plain
  argument instead of computing it itself, so there is exactly one place
  that reads the event stream for this fact, not two.
- **Both required scenarios, and all the old ones.** request_revision,
  revise, preview succeeds (status `previewed`, never resubmitted): the
  request event outranks nothing, badge shows. Same start, then resubmit
  (to `in_review`) and preview succeeds again (status `previewed` a second
  time): the resubmit's event is newer than the request's, badge does not
  show, even though the status is identical to the first case. Both are
  covered by name in `tests/test_ui_status.py`
  (`test_came_back_shows_once_previewed_straight_out_of_revision_requested`,
  `test_came_back_does_not_show_after_a_full_resubmit_round_trip`), driven
  through the real routes and a real `finish_run`, not `make_draft`'s
  status override. `revision_requested` showing, `drafting` after a revise
  showing, and a reject settling it are unchanged (the existing tests for
  those still pass); a resubmit to plain `in_review` was already false
  because that status is not one of the three this check even looks at.
- **The published-record gap is closed too, not just reported.** The first
  fix round's note said this needed a per-draft event lookup living in
  `store.py`. It does not: `Index.revision_answer_seqs` reads the events
  `store.py` already writes, from `index.py`, which this lane does own.
  A request that predates a publish is now ordered against it exactly (the
  submit that had to happen before that publish is always the older event),
  so the `published` parameter is gone from `came_back_from_review`
  entirely rather than kept as a forced-False special case.
- **Cost on a board page.** One call to `revision_answer_seqs` per page
  load in `drafts_board`, covering every active draft plus the visible
  archive page's ids in a single query, not one query per card. The query
  itself is a full scan of the `events` table (there is no index on a JSON
  field, and adding one would be a schema change this lane was told to
  avoid), but it is one scan for the whole page, not a scan repeated per
  row. The editor's single-draft page makes the same call for just its one
  id, which is the "one indexed query per row" shape for a page with
  exactly one row.
- **The docstring.** `ui_status.came_back_from_review`'s docstring is
  rewritten to describe the event-ordering check as it now stands; the
  parts about `previewed` never claiming the fact, and a published record
  being unsettleable, are deleted rather than layered under new text, since
  neither is true of the code anymore.

## Live check (2026-09-20)

Ran the branch (`cb3c2ca`) in a real browser against a fresh-volume compose
stack (`chronicle-lane-f`, api on 8180, preview on 8190), digested against
the real blog clone (346 posts) and seeded with seven records through `/v1`
to hit every state this branch touches. Screenshots and the full method are
in `docs/screenshots/four-words/README.md`; this section is what that
session found, not a repeat of it.

- **All eight items were seen working, exactly as documented.** The board
  shows only the four words at real scale (346 published archive plus six
  in-flight records covering Draft, In review, Published, Rejected); the
  came-back badge survives a preview taken straight out of
  `revision_requested` and disappears only after a real resubmit round
  trip, matching the second fix round's fix exactly; the Import tab is
  gone from the nav and both its routes 404; the editor's Save is disabled
  with "A publish run is in progress, so saving is refused until it
  finishes." while an `approved` draft's publish run sits queued; a
  three-line CRLF feedback entry renders as three breaks with no raw `\r`;
  the filter dropdown lists exactly five options and actually narrows the
  board (6 in flight down to 3, URL carrying the de-duplicated status
  list).
- **The publish-run-active state was produced without touching GitHub.**
  This stack has no GitHub App installed and no `CHRONICLE_GITHUB_TEST_TOKEN`
  set, so `publisher.build_repo_target` returns `None` and `tick` leaves a
  claimed run queued forever (logged, not an error). That gave a stable,
  reproducible "publish run active" state for the editor screenshot with
  no risk to the real blog or the standing `chronicle-target` throwaway
  repo.
- **A genuine browser textarea cannot carry a literal CRLF to the server.**
  Both the DOM `textarea.value` getter and `new FormData(form)` normalise
  `\r\n` to `\n` before any JavaScript in this editor ever sees it
  (confirmed live in the open tab). The CRLF save proof in the screenshots
  README therefore posts to the editor's own save URL with a hand-built
  body carrying real `\r\n` bytes via the browser's `fetch`, in the same
  tab, with the same session cookies, rather than through a simulated
  keystroke. This is not a gap in the fix: it demonstrates the server
  normalises regardless of what put the CRLF on the wire, which is the
  actual threat model (a non-browser client, or a different browser
  engine, could still send one).
- **Nothing broken found.** Every behaviour matched what parts A and B
  documented, including the second fix round's event-ordering change. No
  code changes were made in this session.
- **Not exercised.** A publish run that actually completes (would need a
  GitHub App or the standing `sentania-labs/chronicle-target` test-token
  target; out of scope for a UI-only live check) and the unpublish/merge
  watch path (same reason, and not part of this branch's changes).
