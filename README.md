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
`/v1` and all behind a consumer token. This round (C2) adds the admin front
door (claim, sessions, tokens page, status page), a GitHub App connected
through the manifest flow, and the digest of main that turns published posts
into `Post` records and `from_post` imports. The preview build and the
GitHub publish path itself (opening a PR, watching for a merge) arrive in
C3 through C5; where an action would trigger one of those, it still records
a queued run and stops.

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
    runs/queue/<run_id>.json     consumed by the builder in C3
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
  preview/                       built preview output, disposable
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
  default `0.164.0`), plus git for the blog repo clone. Round C0 ships a
  placeholder entry point that logs its startup and exits; the run queue
  watcher and Hugo build loop arrive with the preview builder.
- **preview**: a static file server over the preview volume with directory
  listing disabled. This one is real in round C0, not a placeholder; the
  per-slug path prefix arrives with the preview builder.

```bash
docker build --target api .
docker build --target builder .
docker build --target preview .
```

All three run non-root (uid 1000) and are intended to run with a read-only
root filesystem. `api` needs a writable volume at `CHRONICLE_DATA_DIR`;
`builder` needs the same plus a writable Hugo cache under its home
directory; `preview` needs a writable volume at `CHRONICLE_PREVIEW_DIR`.

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
   both exercise it without credentials.

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
