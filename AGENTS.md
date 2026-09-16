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
  user login. Nothing shortcuts Admin's session for convenience. Admin does
  not exist yet as of round C1; this is spec intent (section 10) for C2.
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

## Round C1 status

The store and `/v1` are real: submissions, drafts with versions and
conflict-detecting saves, feedback, images, runs, and events, every route
behind a consumer token. Builder and preview are untouched C0 process shells.
The actions that will one day build or publish (`preview`, `approve`,
`unpublish`) only write a run record and a queue entry under
`repo/runs/queue/`. No Hugo build, no GitHub call, no reconciler, no admin or
UI HTML; those arrive in C2 through C5 per the spec.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
