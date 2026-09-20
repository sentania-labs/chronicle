# Lattice skin: live verification screenshots

Real PNG screenshots from headless Chrome (`chrome-devtools-axi`, viewport 1440
by 900) driven against a running `docker compose` stack, not curl. The "before"
shots were taken on 2026-09-18 at about 7:20 PM CDT (America/Chicago) from the
unstyled UI; the "after" shots the same evening between 7:49 PM and 7:51 PM CDT.

## What is and is not shown

Shots kept here: the Posts board, the editor, the submission detail (drafted
and `new`), Import, Admin status, and a version diff. Nothing else is
pictured.

Exercised live during the same session, but with no shot kept: the editor's
backup banner, the conflict view (a stale Save), the Tokens page, Admin login,
and Preview. Where this README describes one of those, it is describing what
was seen at the time, not something the shots below show.

No live look is on record for: Admin claim, Backup (and restore confirm), Password, the
GitHub connect, install and repository pages, the submissions list and the run
log. Only markup tests cover those.

## How it was produced

- Branch `feat/lattice-skin`, api image built from the branch, compose project
  `chronicle-lane-d`, fresh named volumes (`chronicle-lane-d_data` and
  `chronicle-lane-d_preview`, created by that first `up`), api on
  `127.0.0.1:8480` and preview on `127.0.0.1:8490`. The compose file was the
  repository's own with only the two host ports and `CHRONICLE_EXTERNAL_URL`
  changed.
- The BEFORE set was taken first, from the same stack built at the branch point
  (origin/main behaviour: the only commit ahead of it at that moment vendored
  two CSS files nothing linked yet). The AFTER set is the same stack, same data,
  with only the api image rebuilt from the finished branch. Same width, same
  records, so each pair compares.
- The stack digested the real blog clone (`/home/scott/claude/cloudsandunicorns`,
  346 posts, two theme submodules), so every post starts as a `published` working
  record. The Import page needs posts no record tracks yet, and a digest now
  tracks all of them (ADR 017), so the 10 newest posts (two of them with
  date-only frontmatter) had their working-record folders removed from the
  throwaway volume and the index rebuilt with `chronicle reindex`. That is a
  local fixture step, not something the UI does.
- Submissions were posted with a throwaway consumer token. Two were turned into
  posts through the UI's own buttons, one was saved with a title and body, one
  was submitted for review. Preview was clicked for real and the builder built
  it. The theme was changed with real clicks on the header's theme control; the
  browser's `localStorage` carried the choice between pages.
- The editor was driven with real keystrokes and clicks (a title change, a Save,
  typing in the CodeMirror pane, a stale Save for the conflict view). The Admin
  shots are logged in through the login form.

## Index

Before and after pairs (same page, same width; only the theme differs in the
dark shots):

- `before-01-posts-board.png` and `after-01-posts-board-light.png` /
  `after-01-posts-board-dark.png`: the Posts board. Status is a badge next to
  the title, `New post` is the one primary, the filter is one row of Lattice
  controls, the archive is folded.
- `before-02-editor.png` and `after-02-editor-light.png` /
  `after-02-editor-dark.png`: the editor. The EasyMDE toolbar, the CodeMirror
  pane, the side-by-side render and the status bar are one bordered block in
  Lattice surfaces, lines and radii; Publish is the single primary; the sticky
  bar sits just under the header; the sidebar is Lattice cards. The images panel
  lists `rack-after.png` in the after shots only, uploaded between the two sets.
- `before-03-submission-detail.png` and `after-03-submission-detail-light.png` /
  `after-03-submission-detail-dark.png`: a drafted submission (no actions left).
- `before-04-import.png` and `after-04-import-light.png` /
  `after-04-import-dark.png`: the Import page. The date column is local clock
  time for a full stamp (`2026-07-24 12:00 CDT`, a July stamp in CDT; the
  `2026-01-05 21:15 CST` row is in winter time) and a date-only value stays its
  own date (`2026-07-27`, `2026-07-06`). That column was the last raw UTC value
  (#27). The before shot is the raw column.
- `before-05-admin-status.png` and `after-05-admin-status-light.png` /
  `after-05-admin-status-dark.png`: the Admin status page. The before shot is
  the first screen only; the after shots are the whole page.

Extra shots with no before pair:

- `after-03b-submission-new-with-actions-light.png` / `-dark.png`: a `new`
  submission, full page: Create post is the one primary, Discard is the one
  danger, the edit form's material rows are recessed groups inside the card.
- `after-06-posts-board-states-and-archive-dark.png`: the board searched for
  `aria`: the neutral empty state for "in flight" and the archive open, each
  Published post carrying the `ok` badge.
- `after-07-posts-board-needs-revision-light.png`: the board with a post in
  Needs revision, the `warn` badge; Previewed and Draft stay neutral.
- `after-08-version-diff-dark.png`: a version diff. Added lines are `ok`,
  removed lines `bad`, hunk headers the accent's soft ground. Lines are single
  spaced now (they were double spaced by a block-level span inside a `pre`).

## What the live look showed, and what was fixed because of it

Each item below says whether a shot of it is kept.

- The diff page's coloured lines were double spaced; fixed in `style.css`
  (shot kept: `after-08`).
- The filter row's select and input were different heights, so their labels
  sat at different heights; fixed with one height for the row (shot kept: the
  board).
- `Import as post` wrapped onto two lines; buttons no longer wrap (shot kept:
  `after-04`).
- Admin's Status tables did not line their values up from card to card; key and
  value tables now use a fixed first column (shot kept: `after-05`).
- Revoke in the tokens table was a saturated danger button on every row; it is
  a plain button now (Lattice keeps danger for a destructive action on its own,
  and Restore now and Discard still are). Seen live on the Tokens page, no shot
  kept.
- After an in-place Save the page heading and tab title follow the new title
  (#36); seen live, the heading changed with no reload. No shot kept.

## Not fixed here, and why

- White text on the dark theme's primary button (`accent`, #3987e5) is about
  3.6 to 1. That pairing is Lattice's own (`.lat-btn--primary` sets the text to
  `#ffffff`), the brand book does not list it among the non-negotiable pairings,
  and `lattice.css` is vendored and unmodified, so it is reported upstream
  rather than patched here.
