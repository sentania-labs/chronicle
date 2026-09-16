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
  hold one. This is spec intent (section 6 and 11) to build against starting
  in C1, not existing code: as of round C0 no `/v1` routes exist yet to
  enforce it on. See
  [docs/decisions/004-ui-no-login-consumer-token.md](docs/decisions/004-ui-no-login-consumer-token.md).
- **Admin is authenticated always.** Admin's password session is independent
  of the API's consumer tokens and independent of whether the UI ever grows
  user login. Nothing shortcuts Admin's session for convenience. Admin does
  not exist yet as of round C0; this is spec intent (section 10) for C2.
- **Reconciliation produces flags only, never automatic correction.** A
  mismatch between main and Chronicle's records is surfaced on the admin
  status page for Scott to resolve; nothing in the reconciler deletes or
  overwrites a record on its own conclusion. Reconciliation does not exist
  yet as of round C0; this is spec intent (section 12) for C4. See
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
builds all three as separate targets (`api`, `builder`, `preview`).

`make` is the only entry point that matters, and CI calls the same targets:
see the [Makefile](Makefile) for the list. `make check` is what CI's `lint`
and `test` jobs run; `make build` builds all three Docker targets locally.

## Round C0 status

This round ships the repository skeleton, the spec, the ADRs, and the api
image's real `/healthz` and `/readyz`. The builder and preview entry points
are process shells: builder logs and exits (no run queue exists yet, no blog
repo clone); preview is a real static file server with directory listing
disabled, but without the per-slug path prefix the full preview builder
needs. No domain routes (submissions, drafts, publish) exist yet; those
arrive in C1 onward per the spec.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
