# Four status words, and the came-back-from-review fix: live verification

Real PNG screenshots from headless Chrome (`chrome-devtools-axi`, default
viewport) driven against a running `docker compose` stack, not curl. Taken
on 2026-09-20 between 08:39 and 08:45 CDT (America/Chicago).

## How it was produced

- Branch `feat/ui-four-words` at commit `cb3c2ca`, api/builder/preview images
  built from this worktree, compose project `chronicle-lane-f`, fresh named
  volumes, api on `127.0.0.1:8180` and preview on `127.0.0.1:8190`. The
  compose file was the repository's own with only the host ports and
  `CHRONICLE_EXTERNAL_URL` overridden (no GitHub App and no test-token mode
  configured, so a publish run has nowhere to go and stays queued forever,
  used deliberately for item 05 below).
- The stack digested the real blog clone (`/home/scott/claude/cloudsandunicorns`,
  copied into the api container, `chronicle digest` run against it): 346
  posts created and published as working records, Hugo 0.164.0, two theme
  submodules. That is the "Published archive (346)" seen in every board shot.
- Six more records were made through `/v1` with a throwaway consumer token
  (`seed`) for everything any consumer may do, and the `ui` token (read from
  the running container's `/data/state/ui_token.txt`) for the four
  UI-only actions (`approve`, `request_revision`, `reject`, and the save
  that revises a draft): a plain `drafting` post, an `in_review` post, a
  `rejected` post, an `approved` post with its publish run queued and never
  claimed, and the two came-back-from-review posts described below. A
  seventh record ("Feedback with several lines") exists solely to show a
  multi-line feedback entry.
- The browser held no token in its address bar; every page here was loaded
  as a plain URL, matching the UI's own contract that a browser never holds
  a consumer token (the api authenticates the UI backend's own `ui` token
  server-side).

## Index

- `01-posts-board-four-words-light.png` / `-dark.png`: the Posts board with
  the archive expanded. Every status badge on the page reads one of the
  four words: DRAFT, IN REVIEW, PUBLISHED, REJECTED. "Publish run in
  progress" shows IN REVIEW with an APPROVED detail badge beside it (a
  fact, not a fifth status word); "Sent back by reviewer" and "Resubmitted
  after revision" show DRAFT with a CAME BACK FROM REVIEW detail badge.
  346 published records are real digested posts.
- `02a-came-back-badge-before-preview.png`: "Sent back by reviewer",
  isolated by search, in `revision_requested`: DRAFT with CAME BACK FROM
  REVIEW, no preview link yet.
- `02b-came-back-badge-after-preview.png`: the same draft after it was
  revised (moved to `drafting`) and successfully previewed (moved to
  `previewed`), never resubmitted. Still DRAFT, still CAME BACK FROM
  REVIEW, now also PREVIEW BUILT with a working preview link. This is the
  change this branch made: a preview alone used to erase the signal.
- `03-resubmitted-previewed-no-badge.png`: "Resubmitted after revision",
  isolated by search: `revision_requested`, then revised, resubmitted
  (`in_review`), and previewed again (`previewed`). DRAFT and PREVIEW BUILT
  show; CAME BACK FROM REVIEW does not, because the resubmit's event is
  newer than the old revision request. This is the false positive an
  independent reviewer caught (finding A) and it stays fixed.
- `04-nav-no-import-tab.png`: the top navigation reads Submissions, Posts,
  Preview. No Import link. `GET` and `POST /content/import` both 404
  (checked with curl, not pictured: `404` for both).
- `05-editor-save-disabled-publish-run-active.png`: the editor for
  "Publish run in progress" (`approved`, with a `publish` run queued and
  never claimed, since this stack has no GitHub App or test-token repo
  configured). The Save button is disabled and reads "A publish run is in
  progress, so saving is refused until it finishes." right next to it.
  Preview is also disabled ("Already approved").
- `06-feedback-multiline-preserved.png`: a `request_revision` feedback
  entry with three visual lines from a single CRLF-and-blank-line string
  (`First line...\r\nSecond line...\r\n\r\nThird paragraph...`), rendered
  as three `<br>` breaks, no blank paragraph collapsed and no raw `\r`
  visible.
- `07-status-filter-dropdown-open.png`: the status filter opened, showing
  exactly five options: all, Draft, In review, Published, Rejected.
- `07-status-filter-draft-applied.png`: the same board with Draft selected
  and submitted. In flight drops from 6 to 3 (the two came-back drafts and
  the plain draft), the archive shows 0, and the URL carries
  `status=drafting,revision_requested,previewed,unpublished`, proving the
  filter actually narrows the board rather than just relabelling it.

## The CRLF save (not a screenshot)

A real browser `textarea`'s `value` and a `FormData` built from it are both
already newline-normalized to `\n` by the DOM itself (confirmed live: after
`textarea.value = 'a\r\nb'`, `new FormData(form).get('body')` contains no
`\r\n` at all). So a genuine keystroke or paste into this editor's
CodeMirror pane cannot carry a literal CRLF to the server; only a client
that builds the POST body itself can. To test the server's own normalisation
regardless of which client sent it, this session used the browser's `fetch`,
in the open editor tab, with real session cookies, against the exact URL
the Save button posts to (`.../content/drafts/<id>/save`), with a body
string containing real `\r\n` line endings built by hand:

```
fetch(form.action, { method: 'POST', credentials: 'same-origin',
  body: new URLSearchParams({ base_version: '1', title: '...',
    body: '# A plain draft\r\n\r\nStill being written, now with CRLF from the browser.\r\nThird line here.\r\n',
    ... }) })
```

Response: `200`. Reading the stored version back through `/v1`:

```
$ curl -s $BASE/drafts/$D1 -H "Authorization: Bearer $SEED" | python3 -c "..."
version_no: 2
'# A plain draft\n\nStill being written, now with CRLF from the browser.\nThird line here.\n'
```

No `\r` anywhere in the stored body. The version diff between v1 and v2:

```
$ curl -s "$BASE/drafts/$D1/changes?since=1" -H "Authorization: Bearer $SEED" | python3 -m json.tool
"diff": "--- v1\n+++ v2\n@@ -3,4 +3,5 @@\n ---\n # A plain draft\n \n-Still being written.\n+Still being written, now with CRLF from the browser.\n+Third line here.\n"
```

One line removed, two added; the heading, the blank line, and the frontmatter
delimiter are untouched context, not every line removed and re-added. This
is the base-was-already-LF case (`_crlf_to_lf` normalising a CRLF post
against an LF base), covered by
`test_editor_save_stores_lf_when_posting_over_an_lf_base`.

## What the live look showed, and what was fixed because of it

Nothing broken was found in this lane's files. Every behaviour described in
`docs/notes/lane-f-part-a.md` and `docs/notes/lane-f-part-b.md` (including
the second fix round's event-ordering fix) matched what the browser showed:
the four words, the detail badges, the Import tab's removal, the locked
Save with its reason, the multi-line feedback rendering, the de-duplicated
filter, and the CRLF normalisation all behaved exactly as documented.

Checked for tokens, credentials and internal addresses before saving: none
are visible in any image (the address bar is not captured by these
screenshots, and no page renders a token, consistent with
`tests/test_ui.py::test_ui_token_never_appears_in_any_rendered_page`).
