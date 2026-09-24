# The authoring flow, in detail

This is the mechanics behind preview, publish, unpublish, merge watch, and
reconciliation: what each one actually does, and the sharp edges around
them. For running an instance (data directory, tokens, images, admin
bootstrap, backup), see [docs/operations.md](operations.md). The API surface
and the UI are also covered here.

## Preview

`POST /v1/drafts/{id}/actions/preview` pins the draft's slug (if it has
none yet) and queues a run of kind `preview`. The builder container polls
`data/repo/runs/queue/` (default every 5 seconds,
`CHRONICLE_BUILDER_POLL_SECONDS`), claims at most one run at a time with a
lease under `data/state/builder/leases/<run_id>.json` (ADR 011 has the
exact format and the crash-recovery argument), and for a claimed run:

1. Copies `data/site/` (the digest's clone, submodules included) into a
   scratch tree under `data/builder-work/scratch/<run_id>/`.
2. Converts the draft to a post file with `chronicle/api/convert.py`
   (frontmatter in ADR 007's allowlist order, `draft: false` forced, the
   filename and `url` rules below), the same module publish reuses, and
   copies its attached images into `static/images/<image_dir>/`.
3. Runs `hugo --source <scratch> --destination data/preview/.builds/<run_id>
   --baseURL <CHRONICLE_EXTERNAL_URL>/preview/<slug>/ --minify --gc`,
   capturing output as the run's log.
4. On success, atomically points the symlink `data/preview/<slug>` at the
   build's output directory (a single rename, never a remove-then-rename
   pair, so a request never sees a missing `<slug>/`; ADR 011)
   (`GET /v1/drafts/{id}/status` and the narrower
   `GET /v1/drafts/{id}/preview` then carry `preview_url`) and moves the
   draft to `previewed`. On failure, the previous preview tree, if any, is
   left untouched, and the draft's status does not change.

**Filename and `url` rules:** a draft imported with `from_post` keeps the
file and `url` it came from; a new draft gets
`content/posts/<YYYY-MM-DD>-<slug>.md` and `url: /<YYYY>/<MM>/<slug>/`.

The builder writes a heartbeat to `data/state/builder/heartbeat.json` every
poll tick (builder id, last loop time, its own `hugo version`, and the
preview queue depth); the admin status page shows it alongside toolchain
drift (the builder's actual Hugo version against what the last digest found
in the blog repo's own build workflow, per run and per heartbeat; drift
never blocks a build).

## Publish and unpublish

`POST /v1/drafts/{id}/actions/approve` (from `in_review`) and
`POST /v1/drafts/{id}/actions/unpublish` (from `published`) each queue a
run of kind `publish` or `unpublish`. A publisher thread inside the api
process (ADR 013; it needs no Hugo toolchain, so it does not live in the
builder container) claims one at a time:

1. Create or hard-reset the branch `post/<slug>` at the default branch's
   current head, through the GitHub App's git data API.
2. Convert the draft with the same `chronicle/api/convert.py` the builder
   uses for preview, run the lint and normalize pass
   (`chronicle/api/lint.py`: auto-fixes a relative image reference missing
   its leading slash and warns, never blocks, on a malformed markdown
   link), and write the post file plus its images as one commit.
3. Open a pull request to the default branch (body: title, description or
   summary, the preview URL, the run id, the image list, and a
   `chronicle: publish` or `chronicle: unpublish` marker line), or, if one
   is already open for that branch, update its body instead of opening a
   second one. Idempotent by construction: re-running the action for the
   same draft always resets the branch and reuses the same PR.
4. Record the branch, PR number, PR URL, and commit sha on the run and on
   `Draft.published`. First publish stamps `date` as now in
   America/Chicago; republish keeps the original date and sets `lastmod`
   to now.

The draft's status does not change here: `approved` and `published` stay
apart until the merge is actually observed (below). Unpublish deletes
exactly the post file and images the last successful publish recorded, not
whatever the draft's current state happens to compute to.

## Merge watch

A watcher thread, alongside the publisher in the same process, polls every
PR Chronicle has open (`data/repo/watch/<draft_id>.json`, one file per
draft, so a restart resumes exactly where it left off) at
`CHRONICLE_WATCH_POLL_SECONDS` (default 60s), backing off toward
`CHRONICLE_WATCH_POLL_MAX_SECONDS` (default 900s) while nothing is open.

- **Merged:** delete the remote branch, refresh `data/site/` and the
  affected `Post` record, and move the draft to `published` or
  `unpublished`. Reconciliation also runs immediately after.
- **Closed without merging:** move the draft back to `in_review` with a
  `github`-authored feedback entry saying the PR was closed.

## Reconciliation

Runs at startup, hourly (`CHRONICLE_RECONCILE_INTERVAL_SECONDS`, default
3600s), and right after every observed merge. Fetches the default branch,
re-parses posts and the build toolchain, and computes five flags, never
correcting anything on its own conclusion
([docs/decisions/005-reconciliation-flags-only.md](decisions/005-reconciliation-flags-only.md)):

| Flag | What it means |
| --- | --- |
| `draft_published_missing_on_main` | a draft says `published` but its post is gone from main |
| `post_on_main_without_published_draft` | main has a post with no draft tracking it as published |
| `post_removed_without_unpublish` | a post Chronicle knew about vanished from main with no unpublish run |
| `slug_drift` | the post's file is still there, but its slug on main no longer matches the draft's pinned slug |
| `content_drift` | a published post's content on main differs from what Chronicle last published |

`content_drift` also records a new draft version authored `github` with
the content actually on main, so Chronicle's own copy never silently goes
stale; it still never changes the draft's status. The admin status page
lists every open flag with the resolutions that apply to its type: `mark
published`, `mark unpublished`, `import as draft`, `ignore`. Each
resolution is one admin-authored event, and a status change it implies
still goes through the same transition table every other action uses.

## Image directory

Every attached image lands at `static/images/<image_dir>/<filename>` in
the published post, where `image_dir` is the post's own URL slug (the last
non-empty path segment of `url`), never the dated filename and never
`draft.slug` when the two differ (ADR 015). It is pinned once, at the same
moment the slug is, and stored on `Draft.image_dir`; nothing relocates it
on a later save or republish. Preview and publish share the same
conversion (`chronicle/api/convert.py`), so a preview's image paths are
always what publish would write. `chronicle convert-dry-run <draft_id>`
prints the post path, every image destination, and the rewritten
references without touching GitHub, which is how this rule is checked
against a real blog clone without a live publish.

## Test-token mode

Test only, never used in a real deployment: setting
`CHRONICLE_GITHUB_TEST_TOKEN` and `CHRONICLE_ALLOW_TEST_TOKEN=1` runs
publish, watch, and reconcile against `CHRONICLE_GITHUB_TEST_REPO`
(`owner/name`) with that bearer token directly, no GitHub App installation
required ([docs/decisions/012-test-token-github-mode.md](decisions/012-test-token-github-mode.md)).
The token alone is refused: the api will not start unless both variables
are set, naming both in the error. The admin status page and `/readyz`
both report `github_app: test token mode` so it is never mistaken for a
verified App. `examples/k8s/` never mentions either variable.

This mode acts on whatever repository `CHRONICLE_GITHUB_TEST_REPO` names;
it is meant to point at a throwaway repository rather than a live blog, but
nothing in the code special-cases or protects a real blog beyond that
choice of target. Point it at a real blog's repository and it will act on
that repository.

## The API

Everything under `/v1` requires `Authorization: Bearer <token>`, reads
included. `/healthz` and `/readyz` are the only anonymous routes, forever.

| Path | What it does |
| --- | --- |
| `POST /v1/submissions`, `GET /v1/submissions?status=` | raw material in, triage out |
| `POST /v1/submissions/{id}/claim`, `/discard` | submission lifecycle |
| `PUT /v1/submissions/{id}` | replace brief, materials, image ids (all required, image ids must exist) while `new` or `claimed`; requires `base_version`, 409 with a diff if stale, 409 `submission_frozen` after that |
| `POST /v1/drafts` (`blank`, `from_submission`, `from_post`) | new draft; `from_submission` seeds body, title, frontmatter and images from the submission's materials (ADR 018); `from_post` imports a published post from main |
| `GET /v1/drafts?status=`, `GET /v1/drafts/{id}` | content, version, images, status, `editing` (who has it open in the editor) |
| `PUT /v1/drafts/{id}` | save; requires `base_version`, 409 with a diff if stale |
| (editor lock, ADR 025) | a `PUT` by another identity while the draft is open in the editor returns 423 |
| `GET /v1/drafts/{id}/versions`, `/versions/{n}`, `/changes?since=` | history and diffs |
| `POST /v1/drafts/{id}/actions/{action}` | submit, preview, approve, request_revision, reject, restore, unpublish |
| `GET /v1/drafts/{id}/status` | status, last run, preview URL, branch, PR URL |
| `POST /v1/images`, `PUT`/`DELETE /v1/drafts/{id}/images/{image_id}` | upload, attach, detach |
| `GET /v1/posts`, `/posts/{slug}`, `/runs/{id}`, `/runs/{id}/log` | read paths |
| `GET /v1/events?since={cursor}` | cursor-based event log |

`approve`, `request_revision`, `reject`, `restore`, and `unpublish` are
reserved for the `ui` token (spec section 11, ADR 004); any other token
gets a 403 naming the action. `preview`, `approve`, and `unpublish` return
a `run_id` for the build or GitHub operation behind the action.

The editor labels each reserved action for the account that holds the `ui`
token, because the UI always calls through that token and can therefore
always reach the button. The label is not a permissions check on the
click itself: nothing blocks a click on it. That label bakes a name into
the rendered page rather than describing the token generically, which is
tracked as issue #12.

## The UI

Served by the api process at `/`, with no login of its own (spec section
11, ADR 004, ADR 014): `/admin` keeps its password session, and the two
never share a dependency. The UI backend authenticates its own calls into
the store the same way any other consumer would, reading
`data/state/ui_token.txt` fresh on every request rather than caching it, so
a revoke on `/admin/tokens` disables the UI immediately. Every version,
event, and commit the UI produces is authored by the account that holds the
`ui` token, same as any other call it makes.

Because there is no login, a banner on every content and preview page says
so: "This Chronicle instance is internal-only and unauthenticated. Anyone
who can reach it on the network can create, edit, and act on content." It
is on by default; `CHRONICLE_UI_BANNER=0` turns it off for an operator who
has already put the whole thing behind their own auth proxy.

Every timestamp the content and preview pages show is local clock time,
`America/Chicago` by default (`CHRONICLE_UI_TIMEZONE` overrides it with any
tz database name): `14:52 CDT` for today, `2026-09-17 14:52 CDT` for another
day. The API's JSON and the records on disk keep their ISO stamps unchanged.

- **Submissions** (`/content/submissions`): the intake queue, with a detail
  page per submission (materials, attached images, an edit form for the brief
  and materials while it is new or claimed, "create draft from it",
  "discard").
- **Posts board** (`/content/drafts`): a "New post" button (POST
  `/content/drafts/new`, a blank draft straight into the editor), then work
  in flight (every status but `published`, newest first, never paged) above
  a collapsed "Published archive" (newest first, 50 per page, each section
  with its own count). Filterable by status and searchable by title or slug
  (`q`, case-insensitive substring); a search, a `published` filter, or a
  page past the first opens the archive. One card per post (title, slug,
  last author, updated, an "editing: X" marker while open in the editor, open PR link, preview link, open
  reconciliation flags).
- **Editor** (`/content/drafts/{id}`): frontmatter fields, a body textarea
  with a client-side live markdown preview pane (vendored `marked`, see
  [THIRD_PARTY.md](../THIRD_PARTY.md)), image upload and detach, a claim
  indicator, version history with a unified diff between any two versions,
  the feedback log, and one action button per transition the draft's
  current status allows (`chronicle/api/transitions.py` is the only table
  consulted; the UI never hand-codes a second one).
- **Conflict view:** a save with a stale `base_version` never overwrites.
  It renders a 409 page instead: the server's diff summary, the current
  version reloaded into the save form (ready to reapply on top of), and
  the visitor's own attempted title and body in a second, read-only pane
  to copy from by hand.
- **Preview** (`/content/previews`): every draft with a built preview, its
  build time, wall seconds, and toolchain-drift flag, plus a rebuild
  button; a run log page (`/runs/{run_id}`) for any run. Deliberately not
  `/preview` itself: `examples/k8s/ingress.yaml` routes that whole prefix
  to the static preview container, so a UI route living there would be
  unreachable through the instance hostname. `/preview/<slug>/...` stays
  the static build output's own prefix (unchanged, Hugo bakes it into
  every built page's links); only the UI's list and rebuild controls
  moved.

No CDN and no network fetch at page load: `marked.min.js` and the
hand-written `style.css`/`ui.js` are all served from this same process.
