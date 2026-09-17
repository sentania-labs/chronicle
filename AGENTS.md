# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

## The rules that outrank convenience

- **Filesystem first, git second, internal only.** The data directory is the
  truth; the internal git repository under `data/repo/` is history, never a
  second copy of record, and it is never pushed anywhere. It has no remote.
  Publishing to the public blog repo is a separate action through the GitHub
  App, against a different git history entirely. See
  [docs/decisions/001-filesystem-first-internal-git.md](docs/decisions/001-filesystem-first-internal-git.md).
- **No em-dashes anywhere.** Code, comments, docs, commit messages, PR
  bodies. `make prose-check` (`ci/prose-check.sh`) is the gate; it builds the
  character from its own bytes so the checker does not trip itself.
- **Every API path requires a consumer token.** `/healthz` and `/readyz` are
  the only unauthenticated routes, forever. There is no anonymous path into
  `/v1`, reads included, ever, for any consumer, the UI included. The UI
  backend authenticates with its own `ui` consumer token; browsers never
  hold one. As of round C1 this is enforced code, not just spec intent
  (sections 6 and 11): the whole `/v1` router carries the consumer-token
  dependency, so a new route cannot opt out of it by forgetting. See
  [docs/decisions/004-ui-no-login-consumer-token.md](docs/decisions/004-ui-no-login-consumer-token.md).
  FastAPI's own schema routes (`/docs`, `/redoc`, `/openapi.json`,
  `/docs/oauth2-redirect`) are disabled in `create_app` for the same reason;
  they come back in a later round only if placed behind the consumer-token
  layer, never anonymously.
- **Admin is authenticated always.** Admin's password session is independent
  of the API's consumer tokens and independent of whether the UI ever grows
  user login. Nothing shortcuts Admin's session for convenience. As of round
  C2, `/admin` and `/admin/api` carry their own session dependency
  (`chronicle/api/admin_deps.py`), mounted separately from `build_v1_router()`
  and never imported by a content route; a session cookie authenticates
  nothing under `/v1`, and a bearer token authenticates nothing under
  `/admin`.
- **Reconciliation produces flags only, never automatic correction.** A
  mismatch between main and Chronicle's records is surfaced on the admin
  status page for Scott to resolve; nothing in the reconciler deletes or
  overwrites a record on its own conclusion. Reconciliation does not exist
  yet as of round C1; this is spec intent (section 12) for C4. See
  [docs/decisions/005-reconciliation-flags-only.md](docs/decisions/005-reconciliation-flags-only.md).

The full contributor bar, including the review and release process, is in
[CONTRIBUTING.md](CONTRIBUTING.md). The design spec is in
[docs/spec/00-spec.md](docs/spec/00-spec.md); read it before assuming a
capability exists or is out of scope.

## Layout and commands

`chronicle/` is a single package with three console-script entry points:
`chronicle.api` (the FastAPI app: public contract, UI backend, admin, GitHub
client, reconciler), `chronicle.builder` (the Hugo-driven preview builder),
and `chronicle.preview` (the static preview server). One `pyproject.toml`
covers all three; there is no uv workspace to keep in sync, unlike
coppermind's multi-service layout. One `Dockerfile` at the repository root
builds all three as separate targets (`api`, `builder`, `preview`). A fourth
console script, `chronicle`, is the operator CLI (`reindex`, `token`); it
ships inside the api image rather than an image of its own.

`make` is the only entry point that matters, and CI calls the same targets:
see the [Makefile](Makefile) for the list. `make check` is what CI's `lint`
and `test` jobs run; `make build` builds all three Docker targets locally.

## Sharp edges in the api

- **Git identity is passed per commit, never read from config.** `gitrepo`
  sets `GIT_AUTHOR_*` and `GIT_COMMITTER_*` and pins `GIT_CONFIG_GLOBAL` and
  `GIT_CONFIG_SYSTEM` to `/dev/null` on every call, because the api container
  and CI have no global git config and must never inherit the host's. The
  `ui` token acts as `scott` in everything the domain records (commit author,
  version author, claim holder); that mapping lives in `Consumer`
  (`chronicle/api/deps.py`), so the store receives the acting name already
  resolved and does no translation of its own.
- **A save can change a draft's status.** `PUT /v1/drafts/{id}` on a draft in
  `revision_requested` or `published` moves it to `drafting`. That is not one
  of the API's named actions, so it is modelled as the `revise` action in
  `chronicle/api/transitions.py` and writes an event like any other
  transition. Never decide a status anywhere else: that table is the whole
  lifecycle.
- **The index is a cache; single-record reads must not use it.** Lists and
  cursors come from SQLite, a record's own content always comes from its
  file, and `chronicle reindex` rebuilds every row. See
  [docs/decisions/006-sqlite-derived-index.md](docs/decisions/006-sqlite-derived-index.md).
- **`Store.create_draft` returns `(Draft, warnings)`, not a bare `Draft`.**
  A `from_post` import can drop frontmatter keys the allowlist does not have
  (an existing post on main may predate Chronicle) and can fail to import an
  individual image; both are reported as warnings in the response, never a
  422, because the import itself still succeeded. Every other caller
  (`from_submission`, blank) gets an empty warnings list.
- **`_put_image_unlocked` exists so `from_post` import can reuse image
  ingestion from inside an already-`@locked` method.** `Store`'s lock
  (`threading.Lock`) is not reentrant; calling the public, `@locked`
  `put_image` from within `create_draft` would deadlock. Any future store
  method that needs to call another mutating method internally needs the
  same split, not a nested lock.
- **The GitHub client takes an httpx transport, never the network, in a
  test.** `GitHubClient.__init__`'s `transport` parameter exists solely so
  `httpx.MockTransport` can stand in; every test of the GitHub path (spec's
  requirement) sets it, and production code never does. `AdminServices` also
  accepts a `github_client` override at `.build()` for the same reason.
- **Digest never deletes a post record.** A post on main that a later digest
  no longer finds is left alone; deciding it was actually removed is
  reconciliation's job (ADR 005, arriving C4), not digest's. Digest only
  ever creates or updates.
- **`digest.py` widens `GIT_ALLOW_PROTOCOL` to include `file`.** The repo URL
  digest clones is always Chronicle's own configured source (never attacker
  input), so this costs nothing in practice and is what lets a theme
  submodule pointed at a local path clone in tests the same way a real one
  clones over https in production.
- **`digest.py`'s `GIT_CONFIG_GLOBAL` points at a real config file, not
  `/dev/null`.** Git's dubious-ownership check ignores `GIT_CONFIG_COUNT`
  environment injection for `safe.directory` on purpose (letting an env var
  waive that check would defeat it), so the per-invocation trick
  `_auth_env` uses for an installation token cannot grant this exception;
  only a real config file can. Found live in the C3 real-blog check, where
  `CHRONICLE_DIGEST_REPO_URL` named a clone owned by a different uid than
  the container's.
- **A run's queue entry, lease, and status are three different things, and
  only two of them are guaranteed to agree.** The queue entry
  (`repo/runs/queue/<run_id>.json`) is a request; the lease
  (`state/builder/leases/<run_id>.json`, ADR 011) is a claim; the run
  record's `status` is the durable truth. A builder that crashes can leave
  a run `building` with its lease already gone (its own `tick`'s `finally`
  releases the lease on any exit, including an uncaught exception), not
  just with an expired one, so `runner.recover_expired_leases` checks both:
  every expired lease, and every `building` run with no live lease at all.
- **The pre-swap Hugo output lives under `data/preview/.builds/<run_id>/`,
  never under `data/builder-work/`.** `_atomic_swap`'s rename into
  `data/preview/<slug>/` only stays atomic when the source and destination
  share a filesystem, and `builder-work` is a deliberately separate mount
  from `preview` in both `docker-compose.yml` and `examples/k8s/` (ADR
  011's security-separation argument). Found live the same way: an
  `os.replace` across that mount boundary raises `OSError`, not a silent
  copy.
- **`data/preview/<slug>` is a symlink into `.builds/<run_id>/`, never a
  directory Hugo writes into directly.** `_atomic_swap` replaces it with a
  single rename of a temp symlink, not a remove-then-rename pair, so a
  request resolving `<slug>/` never sees it missing (ADR 011). The old
  build directory a replaced symlink pointed at is removed right after the
  swap; anything that writes under a mutable path in the scratch copy of
  `data/site` (a converted post, an attached image) must `unlink` it first
  because `_copy_site` hard-links wherever it can, and writing in place
  would truncate the same inode `data/site` uses (found live: this is
  exactly what silently corrupted the digest's clone before the round C3
  review caught it).
- **`store.remove_post` is not digest with a delete bolted on.** Digest
  never removes a `Post` record (spec's own rule, enforced in
  `apply_digest`); `remove_post` exists solely for the one caller that
  isn't heuristic, the watcher's confirmed `unpublish` merge, where
  Chronicle caused the removal itself and knows so with certainty.
  Reconciliation's `post_removed_without_unpublish` flag is the heuristic
  version of the same fact for everything else, and it never deletes
  anything; the two must never call each other.
- **A `github`-authored write never implies a status change.**
  `Store.record_github_version` (content drift) and the feedback entry
  `observe_pr_outcome` writes on a closed-without-merge PR both attribute
  to `"github"` regardless of which process actor (the watcher, the
  reconcile loop) observed the fact, and neither touches `draft.status`
  outside what `transitions.WATCH_TRANSITIONS` already decides. Confusing
  the acting process with the record's author would misattribute a change
  Scott made directly on GitHub to a robot.
- **`GitHubRepoOps.get_commit` is not on the spec's literal call list.**
  Resolving `create_tree`'s `base_tree` needs the base commit's tree sha,
  and `get_ref` alone returns only the commit sha; `get_commit` is the one
  call every publish and reconcile run makes to bridge that gap (ADR 012).
- **The UI backend calls `Store` in-process, never over loopback HTTP.**
  `chronicle/api/ui_deps.py:require_ui_consumer` reads
  `data/state/ui_token.txt` fresh on every request and authenticates it
  through the same `TokenStore.authenticate` a bearer header would use, so
  a revoke on `/admin/tokens` disables the whole UI on its very next
  request (ADR 014). A UI route that calls `services.store` without going
  through this dependency first is calling a mutating method with no actor
  at all, which is a type error, not a silent anonymous write, but a
  reviewer should still treat a route that skips it as a blocking finding
  the way `/v1` treats a route that skips `require_consumer`.
- **`ui_templates.py`'s action buttons read `transitions.DRAFT_TRANSITIONS`
  directly, never a second table.** The board and the editor page both
  render exactly the actions a draft's current status allows by iterating
  the same dict `Store.act_on_draft` consults; adding a transition there is
  what makes it show up as a button, nothing in the UI layer needs updating
  to match.
- **The UI's markdown preview pane is client-side only, filled by `ui.js`
  from the visitor's own textarea, never by anything the server renders.**
  Every value a UI template does interpolate goes through `html.escape`
  first, same bar as `admin_templates.py`; `marked.parse`'s output is only
  ever assigned into the DOM the same browser tab's own input produced it
  in, which is why it is not a second XSS surface the way echoing another
  user's content back through it would be.

## Round C4 status

Publish, unpublish, merge watch, and reconciliation are all real. A
publisher thread and a watcher thread run inside the api process alongside
the request handlers (ADR 013, `chronicle/api/background.py`); neither
needs the builder's Hugo toolchain, so neither lives in the builder
container.

- **Publish/unpublish** (`chronicle/api/publisher.py`): claims `publish`
  and `unpublish` queue entries the builder's own claim never looks at,
  builds one commit through the git data API (blob, tree, commit, ref),
  and opens or updates a PR. `GitHubRepoOps` (`chronicle/api/
  github_client.py`) is the narrow interface this and the watcher use;
  `AppRepoOps` is production, `TestRepoOps` is test-only (ADR 012). What a
  successful run wrote lands on `Draft.published` (branch, PR number and
  URL, commit sha, post path, url, date, the image list, and the post
  file's own blob sha), which unpublish and reconciliation's
  `content_drift` check both read back rather than recomputing.
- **Merge watch** (`chronicle/api/watcher.py`): polls `data/repo/watch/`
  (one file per draft with an open Chronicle PR, so a restart resumes),
  backing off from `CHRONICLE_WATCH_POLL_SECONDS` toward
  `CHRONICLE_WATCH_POLL_MAX_SECONDS` while nothing is open. A merge deletes
  the branch, refreshes `data/site/` and post records (`digest_runner.
  refresh_from_target`), and moves the draft to `published` or
  `unpublished` through `transitions.WATCH_TRANSITIONS`; a close without a
  merge moves it to `in_review` with a `github`-authored feedback entry.
- **Reconciliation** (`chronicle/api/reconcile.py`): runs at startup,
  hourly, and right after an observed merge. Computes the five flags in
  spec section 12 and persists them under `data/repo/reconcile/`, never
  correcting anything on its own conclusion (ADR 005); `content_drift` is
  the one exception that writes without an admin's say-so, and it only
  ever adds a `github`-authored version, never changes a status. The admin
  status page lists open flags with the resolutions that apply to each
  type (`reconcile.APPLICABLE_RESOLUTIONS`).
- **Test-token mode** (ADR 012): `CHRONICLE_GITHUB_TEST_TOKEN` plus
  `CHRONICLE_ALLOW_TEST_TOKEN=1` runs the whole publish/watch/reconcile
  path against a real repository named by `CHRONICLE_GITHUB_TEST_REPO`
  with a bearer token instead of a GitHub App installation. Test only:
  never mentioned in `examples/k8s/`. The standing target for this and
  future live checks is the private throwaway repo
  `sentania-labs/chronicle-target` (seeded by Adolin, two fixture posts,
  Hugo 0.164.0, no theme), not the real blog.

The preview build (C3) is unchanged: `chronicle/builder/main.py` still
polls the queue for `preview` runs only.

### Not done, noticed

- `ci/compose-smoke.sh` is not wired into CI: the runners have no fixture
  blog repo or GitHub App to digest against (carried over from C3; still
  C6's problem, not a C4 fix).
- This round assumes a single api replica (ADR 013): the publisher has no
  lease the way the builder does, because nothing today runs more than one
  api process against the same data directory. A second replica needs that
  discipline added, not a redesign.
- Reconciliation's `slug_drift` and `content_drift` flags offer only
  `ignore` as a resolution; nothing here rewrites a draft's pinned slug or
  reconverts it to match what landed on main by hand. That is a deliberate
  scope cut (spec section 12 does not ask for an automated fix, only a
  flag), not an oversight.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
