# Contributing

Thanks for helping. This page is the whole process; there is no separate wiki.

## Run it locally

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker with Compose v2, and
Python 3.12 (uv will fetch it if you do not have it).

```bash
make setup      # sync the uv environment
make check      # lint, types, prose check, unit tests: what CI's checks run
make build      # build the api, builder, and preview Docker images locally
make compose-up # local development only; see docker-compose.yml
```

`make check` predicts CI exactly, because CI calls the same targets. There is
no command in a pull request's workflow that you cannot run here.

## The bar for a pull request

1. **Tests for what you changed.** A behaviour that can be proved without a
   container belongs in `tests/`. Say in the body what you observed.
2. **Reviewed before it opens.** Someone other than the author (a peer, a
   reviewer agent, or a genuinely separate self-review pass) reads the diff
   and tries to break it before the pull request exists. The author's own
   "looks good" does not count.
3. **One round of external review.** Address that one round, fix what is
   valid, reply to what is not, then stop. Do not loop.
4. **CI green, and seen working.** Green is necessary and not sufficient.
   Build the images, run them, hit the endpoint the change claims to affect,
   and put what you saw in the body. A healthy `/healthz` next to a broken
   `/readyz` is the failure this rule exists for.
5. **Tags release.** From a merged `main` commit, `git tag -a vX.Y.Z -m
   vX.Y.Z` and push the tag. No version-bump pull request: the quickstart
   tracks `latest`, and anything deploying Chronicle for real pins a version
   or a digest in its own repository (lab-deployment, not here).

Write the body in operational terms: what changes for someone running it,
what the blast radius is, how to recover if it is wrong.

## House style

- **No em-dashes.** Anywhere: code, comments, docs, commit messages, pull
  request bodies. Use a comma, a colon, parentheses, or a period. `make
  prose-check` is the gate.
- **Filesystem first, git second, internal only.** See
  [AGENTS.md](AGENTS.md) and
  [docs/decisions/001-filesystem-first-internal-git.md](docs/decisions/001-filesystem-first-internal-git.md).
- **Every API path requires a consumer token; Admin is authenticated
  always.** See [AGENTS.md](AGENTS.md).
- **Reconciliation flags, never corrects.** See
  [docs/decisions/005-reconciliation-flags-only.md](docs/decisions/005-reconciliation-flags-only.md).
- **Never commit a secret.** Not in code, fixtures, pull request bodies,
  issues, or screenshots. The example manifests under `examples/k8s/` use
  placeholder values on purpose; never replace them with a real credential
  in this repository.
- **Scope discipline.** Fix the thing the pull request is for. Anything else
  you notice becomes an issue, not extra commits here.

## Where things live

`chronicle/` is the single package: `chronicle/api` (the FastAPI service),
`chronicle/builder` (the Hugo-driven preview builder), `chronicle/preview`
(the static preview server). One `Dockerfile` at the repository root builds
all three as separate targets, with the repository root as the build
context. `ci/` holds the scripts CI and you both run. `tests/` holds this
round's tests; a later round may move to tests alongside each module if the
package grows enough to warrant it.

`coppermind` (github.com/sentania-labs/coppermind, local reference at
`projects/coppermind` in the firstmate workspace) is the reference project
whose conventions this repository follows: the same Python 3.12 and uv
toolchain, the same ruff and mypy configuration, the same ADR format and
numbering, the Makefile-as-single-entry-point pattern, and the same image
conventions (non-root uid, read-only root filesystem, pinned action SHAs in
workflows). Domain code is Chronicle's own; only the shape and tooling are
borrowed.

## Decisions

Numbered records under `docs/decisions/`, never renumbered and never reused.
Add one when a choice would otherwise have to be reconstructed from a chat
log later.
