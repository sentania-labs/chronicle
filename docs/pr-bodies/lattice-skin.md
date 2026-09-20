Chronicle's UI and Admin now wear Lattice (light and dark), Admin's second look is gone, and this also fixes the last raw UTC value (#27) and the stale editor heading (#36).

## What changes for someone running it

Every page in the UI and Admin looks like the rest of Scott's tools instead of browser defaults. Nothing about data, routes, tokens, the CSP or the lifecycle changes. Blast radius is the two template modules, `style.css`, and three new static files; recovery is reverting the branch, since no record or route depends on it. New in the image: `static/vendor/tokens.css`, `static/vendor/lattice.css`, `static/theme.js`.

## Behaviour addition: a theme control (the only one)

A button at the right edge of the header switches light and dark. The choice is written to the browser's `localStorage` (`chronicle-theme`); with nothing stored, `prefers-color-scheme` decides; light is the default. Nothing is sent to the server. It works on Admin's login and claim pages too.

It is a **synchronous external script in `<head>`** (`/static/theme.js`), ahead of the stylesheets, so the theme is set before first paint and there is no flash. The plan called for an inline script; the CSP is `default-src 'self'`, which would refuse one, and loosening the CSP for a skin is the wrong trade. A blocking same-origin script in `<head>` satisfies both, and the reason is in comments in `ui_chrome.py` and `theme.js` and in ADR 019. The button ships `hidden` and the script reveals it, so with no script there is no dead button.

## What was done

- Vendored the two `v0.1.0` release assets (sha256 verified against the values given, recorded in `THIRD_PARTY.md`, checked by `tests/test_third_party.py`). Lattice has no LICENSE file upstream; the entry says so and invents nothing.
- Both surfaces link tokens, Lattice, (EasyMDE on the editor), then `style.css`. `chronicle/api/ui_chrome.py` is the one place that decides that, plus the header, tabs and notice mapping. Admin's inline `STYLE` block is deleted.
- Lattice components where Chronicle already had the shape: tables, cards, forms, buttons (one primary per screen), badges, banners, header and tabs. Status is a badge with a tone that means something: Published `ok`, Needs revision `warn`, Rejected `bad`, every in-progress status neutral. The internal-only banner is `warn`; notices map ok, error, conflict and warning to `ok`, `bad`, `warn`, `warn`; empty states are the neutral banner.
- `style.css` shrank to what Lattice does not cover and now uses only Lattice tokens. A test fails on a hex or `rgb()` value or a token that does not exist. The EasyMDE toolbar, CodeMirror pane, side-by-side render and status bar are one bordered block in Lattice surfaces, lines and radii, without editing `easymde.min.css`.

## Structural changes (small markup changes the skin needed)

- The nav is `nav.lat-tabs` with `a.lat-tab`, and `page()` in both surfaces takes an `active` value (the tab's href) to mark the current one. Admin's `nav()` and its per-body `{nav()}` calls are gone; Admin's `page()` renders the header, tabs and Log out itself when a page passes `active`. Every page reached with a session does, including the GitHub error pages, "GitHub App connected" and "a digest is already running"; only login and claim, which have no session, render without them.
- The internal-only banner, the tabs and the page `<h1>` now sit in a `.chr-page` container under a header, not directly in `<body>`.
- Tables are wrapped in `.lat-table-scroll` and gain `<thead>`/`<tbody>`; version history in the editor sidebar is now a table (it was a list), with the message as a second row.
- The board's filter fields are grouped in `div.chr-field` so a label sits over its control in one row; the board card's status moved from the meta line into a badge in the title row.
- Submission edit and conflict pages: sections became cards, and the material rows' `<br>` layout became block labels in a grid.
- The editor's sidebar `<details>` and `<section>` gained `.lat-card`; the count is a `.lat-pill`.
- `Save` is a plain button and the editor's one primary is whatever `offers_for` marks (Preview or Publish); a Save that was primary would be a second primary, and the offers are swapped in place after a save while Save is not.
- Existing tests that asserted exact markup were updated to the new classes (board title regex, the Filter button, an offer button, a notice, a textarea attribute, the version-history time cell, the connect page's `<pre>`, and `"<table>" not in` became `"<table" not in` so it still means something). None were skipped, xfailed or deleted.

## Issues

- **#27, closed by this:** the Import page's date column goes through `local_time`. A bare `YYYY-MM-DD` stays its own date on purpose (a date has no instant; shifting it west of UTC would show the day before), with a comment at the call site so nobody "fixes" it into a clock time. A value `local_time` cannot read (`July 4, 2026`) shows the author's own text rather than a dash, and an empty date stays an empty cell. A naive stamp is assumed UTC, the store's convention, and can differ from Hugo's reading by the site's offset (noted at the call site). Tests: a date-only value renders unshifted, a full stamp renders as local clock time, a non-ISO value renders raw, an empty one renders empty. Seen live (shot kept, `after-04-import-*`): `2026-07-27` next to `2026-07-24 12:00 CDT`.
- **#36, closed by this:** the fix is front-end only. The server already renders the new title in the response an in-place save parses; `refreshRegions` in `editor.js` now copies its `<h1>` and `document.title` across as well (a pure `pageTitles` helper, node-tested). Seen live (no screenshot kept): after Save the heading and tab title changed with no reload. Node tests run the real `editor.js` against a stub page and fail if the title or heading update is removed. Nothing under `routes/` or `store.py`.

## Seen working

Docker compose project `chronicle-lane-d` (api `127.0.0.1:8480`, preview `8490`, fresh volumes), real blog digest (346 posts), real Chrome at 1440 px with real clicks and keystrokes, in light and dark. Two separate claims, kept apart on purpose:

- **Covered by a kept screenshot** (`docs/screenshots/lattice-skin/`, before and after pairs plus extras, with its own README): the Posts board (including the empty state, the archive and a Needs revision badge), the editor, a drafted and a `new` submission detail, Import, Admin status, and a version diff. Light and dark for each, except the Needs revision board (light only) and the diff and the states board (dark only).
- **Exercised live, no screenshot kept**: the editor's backup banner, the conflict view (a stale Save), the Tokens page (including the Revoke button), Admin login, and Preview (clicked, builder built it). These are described in the shots' README, not shown by it.
- **No live look on record**: Admin claim, Backup (and restore confirm), Password, the GitHub connect, install and repository pages, the submissions list and the run log. For these the only evidence is markup tests: `tests/test_ui_skin.py` checks stylesheet order and the marked tab on `/admin`, `/admin/tokens`, `/admin/backup` and `/admin/password`, and that the error pages behind a session keep their tabs and Log out. No one has confirmed how they render.

Found by the live look and fixed here (the Revoke one on the Tokens page, which has no kept screenshot): double-spaced diff lines (block span inside a `pre`), a filter row whose select and input were different heights, `Import as post` wrapping, Admin's key and value tables not lining up, and a saturated Revoke button on every token row.

## Chose not to do

- No fix to Lattice's white text on the dark primary button (about 3.6 to 1 on `#3987e5`). It is Lattice's own pairing, not one of the brand book's named ones, and `lattice.css` is vendored unmodified. Tracked upstream as `sentania-labs/lattice#1`.
- Other Lattice pairings measure between about 3.0 and 4.1 to 1 (dark-theme error banners and removed diff lines, `ink-subtle` on the light theme where `.editor-side summary` and the Admin card headings use it, dark links). They come from the design system, Chronicle takes the stylesheet unmodified rather than forking it, and they are named and recorded in ADR 019. Figures are the reviewer's, computed from the token values; Chronicle did not remeasure them.
- No `Cache-Control` on `/static`. Starlette's `StaticFiles` sends only `Last-Modified` and `ETag`, so after an upgrade a browser can hold an old `style.css` for a while (seen once while taking shots). The fix is in `main.py`, outside this branch's surface. Worth an issue.
- No mobile or print layout, no `AGENTS.md` edit (to avoid a conflict with the parallel branch; a one-line pointer to ADR 019 can go in after both merge), no new icons, no colour beyond Lattice's tokens.
- Admin's status page has no primary button: its actions (run digest, run reconciliation) are equals, and Lattice says a screen with two primaries has not decided what it is for.
- The board's PR and preview lines and the run status sentence are plain text, not badges, because existing tests hold their exact wording.
