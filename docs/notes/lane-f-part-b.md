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
  too. Tests: a CRLF post that changes one line stores LF and its version diff
  is exactly one removed and one added line; a stale-save conflict page carries
  no `\r`. Both fail without the fix. Only the body is normalised, as on the
  submission form. Consequence to know: a post imported from main with real
  CRLF endings will have every line ending rewritten on its first UI save.
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
