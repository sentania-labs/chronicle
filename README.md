# Chronicle

Chronicle carries a piece of writing from an author to the public blog. It
owns everything between the author and the site: raw material intake,
drafts, revisions, preview, publish, unpublish, and the credentials those
need. It is a domain service in the coppermind family: filesystem-first,
small API, small UI, nothing you could not walk away from. It is not an
agent and holds no judgment; every decision that matters is made by an
author or by Scott at a gate the service exposes. The full design is in
[docs/spec/00-spec.md](docs/spec/00-spec.md).

C1 shipped the store and the public API: submissions, drafts with versions
and conflict-detecting saves, feedback, images, runs, and events, all under
`/v1` and all behind a consumer token. C2 added the admin front door (claim,
sessions, tokens page, status page), a GitHub App connected through the
manifest flow, and the digest of main that turns published posts into
`Post` records and `from_post` imports. C3 made preview real: the builder
container watches the run queue, converts a draft to a Hugo post, and
builds it; the preview container serves the result at
`/preview/<slug>/...`. This round (C4) makes publish, unpublish, merge
watch, and reconciliation real: `approve` and `unpublish` open a pull
request against the blog repo through the GitHub App, a watcher polls it
until it merges or closes, and reconciliation flags (never corrects) a
mismatch between main and Chronicle's own records.

## Preview

`POST /v1/drafts/{id}/actions/preview` pins the draft's slug (if it has
none yet) and queues a run of kind `preview`. The builder container polls
`data/repo/runs/queue/` (default every 5 seconds, `CHRONICLE_BUILDER_POLL_SECONDS`),
claims at most one run at a time with a lease under
`data/state/builder/leases/<run_id>.json` (ADR 011 has the exact format and
the crash-recovery argument), and for a claimed run:

1. Copies `data/site/` (the C2 digest clone, submodules included) into a
   scratch tree under `data/builder-work/scratch/<run_id>/`.
2. Converts the draft to a post file with `chronicle/api/convert.py`
   (frontmatter in ADR 007's allowlist order, `draft: false` forced, the
   filename and `url` rules below), the same module publish will reuse in
   C4, and copies its attached images into `static/images/<slug>/`.
3. Runs `hugo --source <scratch> --destination data/preview/.builds/<run_id>
   --baseURL <CHRONICLE_EXTERNAL_URL>/preview/<slug>/ --minify --gc`,
   capturing output as the run's log.
4. On success, atomically points the symlink `data/preview/<slug>` at the
   build's output directory (a single rename, never a remove-then-rename
   pair, so a request never sees a missing `<slug>/`; ADR 011)
   (`GET /v1/drafts/{id}/status` and the narrower `GET /v1/drafts/{id}/preview`
   then carry `preview_url`) and moves the draft to `previewed`. On
   failure, the previous preview tree, if any, is left untouched, and the
   draft's status does not change.

**Filename and `url` rules**, checked against the real blog's 347 posts:
a draft imported with `from_post` keeps the file and `url` it came from; a
new draft gets `content/posts/<YYYY-MM-DD>-<slug>.md` and `url:
/<YYYY>/<MM>/<slug>/`, the dominant pattern on main.

The builder writes a heartbeat to `data/state/builder/heartbeat.json` every
poll tick (builder id, last loop time, its own `hugo version`, and the
preview queue depth); the admin status page shows it alongside toolchain
drift (the builder's actual Hugo version against what the last digest found
in the blog repo's own Pages workflow, per run and per heartbeat; drift
never blocks a build).

A full rebuild of the real blog (347 posts, two theme submodules) measured
2.6 seconds wall time end to end in the C3 pull request's live check
against a local clone; see that PR for the full run record and evidence.

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
   (`chronicle/api/lint.py`, ported from an earlier vault publisher: it
   auto-fixes a relative image reference missing its leading slash and
   warns, never blocks, on a malformed markdown link), and write the post
   file plus its images as one commit.
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
  `unpublished`. Reconciliation also runs immediately after (spec section
  12's third trigger).
- **Closed without merging:** move the draft back to `in_review` with a
  `github`-authored feedback entry saying the PR was closed.

## Reconciliation

Runs at startup, hourly (`CHRONICLE_RECONCILE_INTERVAL_SECONDS`, default
3600s), and right after every observed merge. Fetches the default branch,
re-parses posts and the build toolchain, and computes five flags, never
correcting anything on its own conclusion (`docs/decisions/
005-reconciliation-flags-only.md`):

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

## Test-token mode

Test only, never used in a real deployment: setting
`CHRONICLE_GITHUB_TEST_TOKEN` and `CHRONICLE_ALLOW_TEST_TOKEN=1` runs
publish, watch, and reconcile against `CHRONICLE_GITHUB_TEST_REPO`
(`owner/name`) with that bearer token directly, no GitHub App installation
required (`docs/decisions/012-test-token-github-mode.md`). The token alone
is refused: the api will not start unless both variables are set, naming
both in the error. The admin status page and `/readyz` both report
`github_app: test token mode` so it is never mistaken for a verified App.
`examples/k8s/` never mentions either variable.

`sentania-labs/chronicle-target` is the standing target for this mode: a
private throwaway repository seeded with a two-post Hugo site in the real
blog's shape (dated filenames, `url`, `type`, `author`, images,
`.github/workflows/hugo.yml` pinned to Hugo 0.164.0, default branch
`main`, no theme). Every round's live check that needs a real publish,
merge, or reconcile cycle runs against it with a personal access token in
place of an installation token; it is not the real blog, and nothing here
ever touches `sentania/sentania.github.io`.

## The API

Everything under `/v1` requires `Authorization: Bearer <token>`, reads
included. `/healthz` and `/readyz` are the only anonymous routes, forever.

| Path | What it does |
| --- | --- |
| `POST /v1/submissions`, `GET /v1/submissions?status=` | raw material in, triage out |
| `POST /v1/submissions/{id}/claim`, `/discard` | submission lifecycle |
| `POST /v1/drafts` (`blank`, `from_submission`, `from_post`) | new draft, including an import of a published post from main |
| `GET /v1/drafts?status=`, `GET /v1/drafts/{id}` | content, version, images, status, claim |
| `PUT /v1/drafts/{id}` | save; requires `base_version`, 409 with a diff if stale |
| `POST /v1/drafts/{id}/claim`, `/release` | advisory claim, surfaced but never blocking |
| `GET /v1/drafts/{id}/versions`, `/versions/{n}`, `/changes?since=` | history and diffs |
| `POST /v1/drafts/{id}/actions/{action}` | submit, preview, approve, request_revision, reject, restore, unpublish |
| `GET /v1/drafts/{id}/status` | status, last run, preview URL, branch, PR URL |
| `POST /v1/images`, `PUT`/`DELETE /v1/drafts/{id}/images/{image_id}` | upload, attach, detach |
| `GET /v1/posts`, `/posts/{slug}`, `/runs/{id}`, `/runs/{id}/log` | read paths |
| `GET /v1/events?since={cursor}` | cursor-based event log |

`approve`, `request_revision`, `reject`, `restore`, and `unpublish` are
reserved for the `ui` token (spec section 11, ADR 004); any other token gets
a 403 naming the action. `preview`, `approve`, and `unpublish` return a
`run_id` for the build or GitHub operation a later round will perform.

## The UI

C5 adds a content and preview UI, served by the api process at `/`, with no
login of its own (spec section 11, ADR 004, ADR 014): `/admin` keeps its
password session exactly as before, and the two never share a dependency.
The UI backend authenticates its own calls into the store the same way any
other consumer would, reading `data/state/ui_token.txt` fresh on every
request rather than caching it, so a revoke on `/admin/tokens` disables the
UI immediately (ADR 014's "in-process, not loopback" reasoning covers why).
Every version, event, and commit the UI produces is authored `scott`, same
as any other call the `ui` token makes.

Because there is no login, a banner on every content and preview page says
so: "This Chronicle instance is internal-only and unauthenticated. Anyone
who can reach it on the network can create, edit, and act on content." It is
on by default; `CHRONICLE_UI_BANNER=0` turns it off for an operator who has
already put the whole thing behind their own auth proxy.

- **Submissions** (`/content/submissions`): the intake queue, with a detail
  page per submission (materials, attached images, "create draft from it",
  "discard").
- **Drafts board** (`/content/drafts`): filterable by status, one card per
  draft (title, slug, last author, updated, claim holder, open PR link,
  preview link, open reconciliation flags).
- **Import** (`/content/import`): a searchable list of `/v1/posts`, each row
  a one-click `from_post` import.
- **Editor** (`/content/drafts/{id}`): frontmatter fields, a body textarea
  with a client-side live markdown preview pane (vendored `marked`, see
  [THIRD_PARTY.md](THIRD_PARTY.md)), image upload and detach, a claim
  indicator, version history with a unified diff between any two versions,
  the feedback log, and one action button per transition the draft's
  current status allows (`chronicle/api/transitions.py` is the only table
  consulted; the UI never hand-codes a second one). Reserved actions
  (`approve`, `request_revision`, `reject`, `restore`, `unpublish`) are
  labelled "Scott only" because the UI holds the `ui` token and can always
  reach them, not because anything blocks a click.
- **Conflict view:** a save with a stale `base_version` never overwrites.
  It renders a 409 page instead: the server's diff summary, the current
  version reloaded into the save form (ready to reapply on top of), and the
  visitor's own attempted title and body in a second, read-only pane to
  copy from by hand.
- **Preview** (`/content/previews`): every draft with a built preview, its
  build time, wall seconds, and toolchain-drift flag, plus a rebuild button;
  a run log page (`/runs/{run_id}`) for any run. Deliberately not `/preview`
  itself: `examples/k8s/ingress.yaml` routes that whole prefix to the static
  preview container on port 8090, so a UI route living there would be
  unreachable through the instance hostname. `/preview/<slug>/...` stays
  the static build output's own prefix (unchanged, Hugo bakes it into every
  built page's links); only the UI's list and rebuild controls moved.

No CDN and no network fetch at page load: `marked.min.js` and the
hand-written `style.css`/`ui.js` are all served from this same process.

The C5 live check's evidence (rendered HTML fragments; Playwright was
unavailable that session, so these stand in for screenshots) is under
[docs/screenshots/c5/](docs/screenshots/c5/).

## Tokens

```bash
uv run chronicle --data-dir /data token issue ghostwriter   # prints the token once
uv run chronicle --data-dir /data token list                # names and use, never secrets
uv run chronicle --data-dir /data token revoke ghostwriter
```

Tokens are hashed at rest (ADR 007) and shown exactly once. The UI backend's
own token is minted on first start and written to
`data/state/ui_token.txt`, mode 0600, never logged. That file is the only
plaintext token on disk; treat it as a credential.

## The data directory

`CHRONICLE_DATA_DIR` names it. The filesystem is the record, git is the
history, and the index is a cache (ADRs 001 and 006).

```
data/
  repo/                          internal git repository, no remote, never pushed
    submissions/<id>.json
    drafts/<id>/draft.json       current draft
    drafts/<id>/versions/<n>.json
    feedback/<draft_id>.jsonl    the feedback record the API reads, one entry per line
    feedback/<draft_id>.md       the same entries rendered for a human, never parsed back
    posts/<slug>.json
    runs/<id>.json
    runs/queue/<run_id>.json     consumed by the builder (preview) or the publisher (publish/unpublish)
    runs/logs/<run_id>.log       captured build output, read by GET /v1/runs/{id}/log
    watch/<draft_id>.json        one open Chronicle PR the watcher is polling (ADR 013)
    reconcile/<flag_id>.json     one reconciliation flag (spec section 12, ADR 005)
    events/log.jsonl             append-only, one object per line, monotonic seq
    index/chronicle.db           derived SQLite, gitignored, rebuildable
  images/<sha[:2]>/<sha>.<ext>   content-addressed, sha256 is the image id
  state/                         0700, every file inside 0600 except toolchain.json
    tokens.json, ui_token.txt      consumer tokens (ADR 007)
    claim-code                     one-time admin claim code; deleted once claimed
    admin.json                     argon2 password hash and session signing secret
    instance.key                   32 random bytes; encrypts github-app.json; never backed up
    github-app.json                GitHub App record, secrets encrypted at rest (ADR 008)
    digest-status.json             last digest's counts and timestamps, not secret
    toolchain.json                 last digest's Hugo version and theme commits, 0644
    builder/heartbeat.json         builder id, last loop time, hugo version, queue depth
    builder/leases/<run_id>.json   one builder's claim on one run (ADR 011)
    publisher/heartbeat.json       last publish/unpublish poll loop time, queue depth
    watcher/heartbeat.json         last watch poll loop time, PRs watched, current interval
    reconcile/heartbeat.json       last reconciliation run time, current interval
  builder-work/                  the builder's scratch tree and Hugo caches, disposable
    scratch/<run_id>/, cache/, resources/
  preview/                       built preview output, disposable
    <slug>/                      symlink to the live build under .builds/
    .builds/<run_id>/            one build's output, live once a symlink points at it
  site/                          clone of the blog repo main, disposable
```

`instance.key` is never included in a backup bundle (the bundle format
itself arrives in a later round): a bundle that carried it would make
`github-app.json`'s encryption pointless. See ADR 008.

A queue entry is `{"run_id", "draft_id", "kind", "enqueued_at"}`, where
`kind` is `preview`, `publish`, or `unpublish`.

Every write under `repo/` is one git commit authored by the acting token's
name, or `scott` for the `ui` token. `chronicle reindex` rebuilds
`index/chronicle.db` from the files; nothing is lost if it is deleted.

## The three images

One repository, one `Dockerfile`, three build targets, per spec section 14:

- **api**: the service, UI backend, admin, GitHub client, and reconciler.
  It ships `/v1`, plus `GET /healthz` (liveness) and `GET /readyz`
  (readiness: the data directory is writable, git is available, the derived
  index opens at the expected schema version, and the GitHub App is honestly
  reported "not configured" until C2 bootstraps it).
- **builder**: Hugo extended (pinned by the `HUGO_VERSION` build arg,
  default `0.164.0`), plus git for the blog repo clone. Polls the run
  queue, builds `preview` runs, and writes a heartbeat (see "Preview"
  above).
- **preview**: a small Python static file server (ADR 010) over the
  preview volume, serving `/preview/<slug>/...`, no directory listing, a
  `GET /healthz`.

```bash
docker build --target api .
docker build --target builder .
docker build --target preview .
```

All three run non-root (uid 1000) and are intended to run with a read-only
root filesystem. `api` needs a writable volume at `CHRONICLE_DATA_DIR`;
`builder` needs the same plus a writable home directory (Hugo's own cache
lives under `data/builder-work/`, not the home directory, but `uv`'s
installed packages still expect one); `preview` needs a writable volume at
`CHRONICLE_PREVIEW_DIR`.

## Local end to end: `make compose-up`

```bash
CHRONICLE_DIGEST_REPO_URL=/path/to/a/local/clone docker compose up -d --build
```

Then, against `localhost:8080` (api) and `localhost:8090` (preview): claim
the instance, run the digest (button on `/admin`, or `POST /admin/digest`
with the session cookie), `POST /v1/drafts` with `from_post` naming a slug
the digest found, `POST /v1/drafts/{id}/actions/preview` with the `ui`
token, poll `GET /v1/runs/{run_id}` until `succeeded`, then open
`http://localhost:8090<preview_url>` in a browser. `docker-compose.yml` has
the full comment on why `CHRONICLE_EXTERNAL_URL` there points at the
preview port and not the api's.

## Run it locally

```bash
uv sync            # install the dev environment
make check         # lint, types, prose check, unit tests
make build         # build all three Docker images locally
make compose-up     # local development only; see docker-compose.yml
```

To run the api against a throwaway data directory:

```bash
export CHRONICLE_DATA_DIR=$(mktemp -d)
uv run chronicle-api                             # serves on :8080
uv run chronicle token issue ghostwriter         # reads CHRONICLE_DATA_DIR
curl -s localhost:8080/readyz
curl -s localhost:8080/v1/drafts -H "Authorization: Bearer $TOKEN"
```

`docker-compose.yml` is for local development only. It is not how Chronicle
is deployed: reference deployment manifests are in [examples/k8s/](examples/k8s/),
and lab-deployment owns the private instance for real.

## Admin bootstrap

`CHRONICLE_EXTERNAL_URL` has to be set before the first claim if you plan to
connect a real GitHub App: it is the base URL GitHub redirects back to after
the manifest flow (`{CHRONICLE_EXTERNAL_URL}/admin/github/callback`), and it
cannot be guessed from an incoming request because the request that needs it
(building the manifest) is not the request GitHub redirects to (ADR 009).
It defaults to `http://localhost:8080`, which is fine for exercising
everything except the two steps that happen in your own browser against
real GitHub.

The click-by-click order, once for a fresh instance:

1. **Claim.** `GET /admin` shows the claim page until claimed. Read the
   one-time code from `data/state/claim-code` (api logs only that a code
   exists and where), pick a password of at least 12 characters, submit.
   The code file is deleted the moment this succeeds.
2. **Log in.** `GET /admin/login`, the password from step 1. Sets a signed,
   `HttpOnly`, 12-hour session cookie.
3. **Connect GitHub** (in your browser, on github.com). `/admin/github/connect`
   renders a form carrying the App manifest; submitting it opens GitHub's own
   "create a GitHub App" page, pre-filled, targeting either your personal
   account or an organization. This is the first of the two steps that
   happen on GitHub, not Chronicle: your browser talks to GitHub directly,
   and GitHub redirects back to `/admin/github/callback` with a one-time
   code once the App exists.
4. **Install the App** (in your browser, on github.com). The callback page
   links to `{app html_url}/installations/new`; installing there and picking
   the blog repo is the second step that happens on GitHub. Come back to
   `/admin/github/install` afterward.
5. **Choose the installation.** `/admin/github/install` lists what the
   App's own credentials can already see and lets you pick one, or paste an
   installation id directly.
6. **Choose the repo.** `/admin/github/repo` lists the repositories that
   installation can reach; picking one makes a live call to verify contents
   and pull-request access and records the result.
7. **Digest.** The "Run digest now" button on `/admin` (or `chronicle
   digest`) clones or fetches the chosen repo's default branch into
   `data/site/` and writes post records. With no GitHub App configured yet,
   setting `CHRONICLE_DIGEST_REPO_URL` to a public https URL runs the same
   digest anonymously, which is how this round's real-world check and CI
   both exercise it without credentials. `CHRONICLE_DIGEST_REPO_URL` also
   accepts a local filesystem path or a `file://` URL to a clone you already
   have on disk, which is the way to test digest against a private repo
   without ever putting a GitHub token in Chronicle's environment.

Only steps 3 and 4 happen on GitHub's own pages in your browser; every other
step is a Chronicle admin page. `/admin/tokens` issues and revokes named
consumer tokens at any point after claiming; `ui` is reserved. `/admin`
itself is the status page: last digest, post count, toolchain drift, App and
repo connection state, submissions and drafts by status, disk use, and git
health.

## Where the spec lives

The design specification is [docs/spec/00-spec.md](docs/spec/00-spec.md),
copied verbatim from the vault report that authored it. Decisions that would
otherwise have to be reconstructed from a chat log are recorded as ADRs
under [docs/decisions/](docs/decisions/).

## Reference project

[coppermind](https://github.com/sentania-labs/coppermind) (local reference
at `projects/coppermind` in the firstmate workspace) is the reference
project whose conventions this repository follows: Python 3.12 with uv,
hatchling, the same ruff and mypy configuration, the same ADR format and
numbering, the Makefile-as-single-entry-point pattern where CI calls make
targets, and the same image conventions (non-root uid, read-only root
filesystem, pinned action SHAs in workflows). Chronicle's domain code is its
own; only coppermind's shape and tooling are borrowed. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the full contributor bar and
[AGENTS.md](AGENTS.md) for the rules that outrank convenience.
