# Announcements panel: verification screenshots

Real PNG screenshots from headless Chrome (`chrome-devtools-axi`, 1280 px
wide) driven against a running `docker compose` stack, not curl. Taken on
2026-09-21 around 00:05-00:10 (America/Chicago).

## How it was produced

- Branch stack: `feat/announcements`, compose project `chronicle-lanek`,
  clean volumes, api on `127.0.0.1:8089`, preview on `127.0.0.1:8099` (a
  `docker-compose.lanek.override.yml`, not committed, used `ports:
  !override` on both services so the stack carried only those two host
  bindings, not the default `8080`/`8090` on top of them).
- A token was minted inside the stack (`docker compose exec api chronicle
  token issue lanek`), used only to call the API from this shell, never
  printed to a file or committed.
- Created a plain draft through `POST /v1/drafts`, then `PUT
  /v1/drafts/{id}` with a title, a one-line body, and all three
  announcement channels filled in.
- Baseline (before) stack: `origin/main` (af14f39), built as a standalone
  container in a throwaway git worktree at `/tmp/chronicle-before-af14f39`
  (no compose project, so no port or volume collision with the branch
  stack), on `127.0.0.1:8087`. The same draft, title, and body were
  created there the same way, with no `announcements` key at all (the
  field does not exist on that branch, confirmed by the raw JSON response
  from its own `PUT`). Screenshotted, then removed entirely (container,
  image, worktree).
- Opened the edit page for the branch-stack draft, expanded the
  "Announcements" panel, confirmed all three textareas showed the text
  exactly as saved. Clicked the "X" channel's Copy button and confirmed
  live: the button's adjacent status span read "Copied" in the
  accessibility tree at the moment of the screenshot (its DOM
  `textContent` clears itself after 4 seconds, so timing mattered).
  Edited the "X" textarea's text, clicked the page's own Save button, and
  confirmed the version history counter moved from 1 to 2. Confirmed
  through `GET /v1/drafts/{id}` (not just the rendered page) that the
  edited text came back and `version_no` was `2`.
- Opened the same edit page on the before stack and expanded its sidebar:
  the panel order there is Frontmatter, Feedback, Images, with no
  "Announcements" disclosure at all.

## Index

- `after-panel-filled.png`: the branch stack's editor, Announcements panel
  expanded, showing all three channels (X, Bluesky, LinkedIn) with the
  text saved through the API.
- `after-copy-confirmed.png`: the same panel after clicking the "X"
  channel's Copy button, showing the "Copied" confirmation next to the
  button, and the same textarea after being edited live in the browser
  and saved (status moved from DRAFT to IN REVIEW from an earlier Submit
  click made while locating the real Save button; the version history
  counter, not shown in the crop, moved from 1 to 2 as a result of the
  edit-and-save step, confirmed separately over `GET`).
- `before-no-panel.png`: the same post, same scroll position, from
  `origin/main` (af14f39, this change reverted). The sidebar shows
  Frontmatter, Feedback, and Images, with no Announcements panel to
  expand.

## Noticed, not fixed (out of scope for this pass)

- Nothing else was noticed live-checking this feature; see
  `docs/pr-bodies/announcements-review.md` for the one finding from the
  written adversarial review pass (the conflict page's reload form, fixed
  in this branch) and everything else that was checked and held.

Checked for tokens, credentials, and internal addresses before saving:
none are visible in any image (the captures show only the post's own
title, body, and the announcement text used for this check, all of which
is placeholder text written for this verification).
