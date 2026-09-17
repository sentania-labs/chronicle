# Running an instance

This is the reference for standing up and operating a Chronicle instance:
the data directory, tokens, the three container images, admin bootstrap,
and where backups fit. For how content actually moves through the service
(preview, publish, merge watch, reconciliation, image paths), see
[docs/authoring-flow.md](authoring-flow.md).

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

`instance.key` is never included in a backup bundle: a bundle that carried
it would make `github-app.json`'s encryption pointless. See ADR 008 and
"Backup and restore" below.

Every write under `repo/` is one git commit authored by the acting token's
name, or `scott` for the `ui` token (that mapping is the `Consumer` your own
token resolves to, not something the store guesses). `chronicle reindex`
rebuilds `index/chronicle.db` from the files; nothing is lost if it is
deleted.

## Tokens

```bash
uv run chronicle --data-dir /data token issue ghostwriter   # prints the token once
uv run chronicle --data-dir /data token list                # names and use, never secrets
uv run chronicle --data-dir /data token revoke ghostwriter
```

Tokens are hashed at rest (ADR 007) and shown exactly once. The UI backend's
own token is minted on first start and written to `data/state/ui_token.txt`,
mode 0600, never logged. That file is the only plaintext token on disk;
treat it as a credential.

`approve`, `request_revision`, `reject`, `restore`, and `unpublish` are
reserved for the `ui` token (spec section 11, ADR 004); any other token gets
a 403 naming the action.

## The three images

One repository, one `Dockerfile`, three build targets, per spec section 14:

- **api**: the service, UI backend, admin, GitHub client, and reconciler.
  It ships `/v1`, plus `GET /healthz` (liveness) and `GET /readyz`
  (readiness: the data directory is writable, git is available, the derived
  index opens at the expected schema version, and the GitHub App is honestly
  reported "not configured" until it is bootstrapped).
- **builder**: Hugo extended (pinned by the `HUGO_VERSION` build arg,
  default `0.164.0`), plus git for the blog repo clone. Polls the run
  queue, builds `preview` runs, and writes a heartbeat.
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

`docker-compose.yml` is for local development only. Reference deployment
manifests, closer to how a real instance runs, are in
[examples/k8s/](../examples/k8s/).

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
   one-time code from `data/state/claim-code` (the api logs only that a
   code exists and where), pick a password of at least 12 characters,
   submit. The code file is deleted the moment this succeeds.
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
   digest anonymously, which is how CI exercises it without credentials.
   `CHRONICLE_DIGEST_REPO_URL` also accepts a local filesystem path or a
   `file://` URL to a clone you already have on disk, which is the way to
   test digest against a private repo without ever putting a GitHub token
   in Chronicle's environment.

Only steps 3 and 4 happen on GitHub's own pages in your browser; every other
step is a Chronicle admin page. `/admin/tokens` issues and revokes named
consumer tokens at any point after claiming; `ui` is reserved. `/admin`
itself is the status page: last digest, post count, toolchain drift, App and
repo connection state, submissions and drafts by status, disk use, and git
health.

## Backup and restore

`chronicle backup create [--out path]` writes a checksummed, gzip tarball
(`chronicle-backup-<UTC stamp>.tar.gz`): `repo/` with its git history,
`images/`, and the encrypted portion of `state/` (`tokens.json`,
`admin.json`, `github-app.json`, `ui_disabled` if present). Never
`instance.key`, `preview/`, `site/`, `builder-work/`, `claim-code`, or
`ui_token.txt`. `manifest.json` at the root carries record counts and a
sha256 for every other member.

`chronicle backup restore <bundle> --yes` validates the manifest, refuses
an unknown `schema_version`, verifies every member's checksum (refusing on
any mismatch, missing, or extra member), then swaps `repo/`, `images/`, and
the state files into place, keeping the displaced tree until `chronicle
reindex` against the new one succeeds. Run it against a stopped instance
(api and builder both, ADR 016) since the builder holds its own long-lived
connection to the index that does not notice the swap; the one exception is
`/admin/backup`'s upload path, which restores from within the running
instance after you type `restore` to confirm, and then needs a manual
builder restart for the same reason. Either path needs a `chronicle digest`
run afterward before a preview or publish will work, since `site/` is not
part of the bundle.

See [docs/backup.md](backup.md) for the exact file layout and JSON schema,
precise enough to hand-build an import bundle without reading the code, and
ADR 016 for the restore swap's reasoning.
