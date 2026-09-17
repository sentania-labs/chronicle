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
5. **Tags release.** See "Release procedure" below.

Write the body in operational terms: what changes for someone running it,
what the blast radius is, how to recover if it is wrong.

## Release procedure

From a merged `main` commit, tag it (annotated, not lightweight):

```bash
git tag -a vX.Y.Z -m vX.Y.Z
git push origin vX.Y.Z
```

No version-bump pull request: nothing in the repository carries a version
number outside the tag itself and `CHRONICLE_BUILD_VERSION`, which the
release workflow stamps at build time.

`.github/workflows/ci.yml`'s `release-tag` job refuses a tag that is not
`vMAJOR.MINOR.PATCH`, not annotated, or not reachable from `main` before
anything else runs. On a valid tag, `publish` builds each of the three
images once (`api`, `builder`, `preview`), attaches an SBOM, refuses to
push if `ghcr.io/sentania-labs/chronicle-<target>:vX.Y.Z` already exists
(tags are immutable), pushes, signs with cosign keyless, and verifies the
signature in the same job. `release` then writes the GitHub release from
merged pull request titles since the previous tag. Nothing here runs from
a pull request or a push to `main`; only `publish`, gated on the tag, ever
gets `packages: write` or `id-token: write`.

Verify a published image yourself:

```bash
cosign verify ghcr.io/sentania-labs/chronicle-api:vX.Y.Z \
  --certificate-identity-regexp '^https://github.com/sentania-labs/chronicle/.github/workflows/ci.yml@refs/tags/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

Two traps make hand verification look broken when it is not:

**Verify against the index digest, not a per-architecture child digest.**
The digest a version tag points at, and the one cosign signed, is the OCI
image index digest. Read it from the package versions API rather than
guessing:

```bash
gh api --paginate orgs/sentania-labs/packages/container/chronicle-api/versions \
  --jq '.[] | select(.metadata.container.tags[]? == "vX.Y.Z") | .name'
```

This endpoint pages at 30 the same as the package listing below, so an
older vX.Y.Z can come back empty without `--paginate`.

`docker manifest inspect ghcr.io/sentania-labs/chronicle-api:vX.Y.Z` shows
the index plus its children, including the `linux/amd64` child and an
`unknown/unknown` attestation entry. Verifying against a child digest
instead of the index digest fails, and the failure reads like a missing or
bad signature rather than like the wrong subject: the instinct is to go
recheck the signing step, which is not where the problem is. The signed
v0.1.0 index digests, as a worked example:

- `chronicle-api` `sha256:3cf8e32ad11c4a56b4bc4bc2212383b49e75e4cdf347c33140b5af5338c1efcf`
- `chronicle-builder` `sha256:f51d73a040583c5418e914014e0782cc421f1d410db705f3a7c18df25dc061ff`
- `chronicle-preview` `sha256:337396db33a295fcf00cc6a9600225c8905f67bbf7125dfbd5b93533505f6172`

**Listing the organisation's container packages needs `--paginate`.**

```bash
gh api --paginate "orgs/sentania-labs/packages?package_type=container" \
  --jq '.[].name'
```

GitHub's default page size is 30; the organisation carries more container
packages than that, and a call without `--paginate` silently truncates the
list to the first page. A `chronicle-*` package missing from a truncated
result reads as "the images were never published" when they were
published and signed correctly. A newly published package can also take
time to show up in this aggregate listing at all, paginated or not; when a
package is missing here but the tag-and-digest lookup above resolves
cleanly, trust the direct lookup over the listing.

Package visibility on ghcr is independent of the repository's visibility.
A `ghcr.io/sentania-labs/chronicle-*` package stays private until Scott
flips it by hand on GitHub; nothing in the release workflow changes it.
While a package is private, both `docker manifest inspect` and
`cosign verify` need `docker login ghcr.io` first, with a token carrying
`read:packages`. Once a package is public, neither call needs a login.
Check the package's current visibility before assuming either way.

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
