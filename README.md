# Chronicle

Chronicle carries a piece of writing from an author to the public blog. It
owns everything between the author and the site: raw material intake,
drafts, revisions, preview, publish, unpublish, and the credentials those
need. It is a domain service in the coppermind family: filesystem-first,
small API, small UI, nothing you could not walk away from. It is not an
agent and holds no judgment; every decision that matters is made by an
author or by Scott at a gate the service exposes. The full design is in
[docs/spec/00-spec.md](docs/spec/00-spec.md).

This round (C0) bootstraps the repository: the toolchain, the three images,
the reference manifests, the ADRs, and a real `/healthz` and `/readyz` on the
api image. No domain routes (submissions, drafts, publish) exist yet.

## The three images

One repository, one `Dockerfile`, three build targets, per spec section 14:

- **api**: the service, UI backend, admin, GitHub client, and reconciler.
  Round C0 ships a real `GET /healthz` (liveness) and `GET /readyz`
  (readiness: the data directory is writable, git is available, and the
  GitHub App is honestly reported "not configured" until C2 bootstraps it).
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

`docker-compose.yml` is for local development only. It is not how Chronicle
is deployed: reference deployment manifests are in [examples/k8s/](examples/k8s/),
and lab-deployment owns the private instance for real.

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
