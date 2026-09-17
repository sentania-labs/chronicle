# 012: test-token mode for the GitHub client

- **Status:** accepted
- **Date:** 2026-09-16

## Context

C4 adds publish, merge watch, and reconciliation, all of which drive the
GitHub client interface (`chronicle/api/github_client.py`). No GitHub App
exists yet: Scott creates one at instance bootstrap (README's admin
bootstrap section), which means this round's own live check, and CI's
future coverage of the publish path, have no App to mint installation
tokens from. Something has to exercise the same interface against a real
repository without one.

## Decision

The publish, watch, and reconcile call surface (git data: get ref, create
ref, update ref, delete ref, get commit; contents: get file; pulls: create,
get, list open by head, update body) is factored into one narrow interface,
`GitHubRepoOps`, in `chronicle/api/github_client.py`. It has exactly two
implementations:

- `AppRepoOps`, production: mints an installation token through the
  existing App JWT and installation-token flow (C2's `GitHubClient`,
  `GitHubAppStore`), the same way `digest_runner.py` and
  `routes/admin.py` already do, and caches it the same way.
- `TestRepoOps`, test-only: wraps a bearer token handed to it directly, no
  minting, no App record.

`TestRepoOps` is wired up only when both `CHRONICLE_GITHUB_TEST_TOKEN` and
`CHRONICLE_ALLOW_TEST_TOKEN=1` are set. The token alone is refused: the api
raises at startup, naming both variables, rather than silently running in a
weaker mode because one flag was set and not the other. This mirrors the
shape of a footgun, not a convenience: a token env var alone must never be
enough to grant production-shaped access. The repo it acts against comes
from `CHRONICLE_GITHUB_TEST_REPO=owner/name`, independent of whatever
GitHub App repo (if any) is configured, so this mode never needs a real App
to exist.

`git data` here includes `get_commit`, one call past the literal list in
the spec conversation: resolving `base_tree` for `create_tree` needs the
base commit's tree sha, and `get_ref` alone returns only the commit sha, not
its tree. `get_commit` is the one extra call that makes the documented
sequence (ref, blobs, tree, commit, ref update) actually work.

The admin status page and `/readyz` report `github_app: test token mode`
whenever this mode is active, so it is never mistaken for a verified App
installation in a log or a screenshot. `examples/k8s/` never mentions either
environment variable: it is the reference manifests for a real deployment,
and a real deployment always has an App by the time it publishes anything.

## Consequences

- The C4 live check (this round's pull request) and any future CI coverage
  of publish/watch/reconcile can run against a real GitHub repository
  (`sentania-labs/chronicle-target`, the standing throwaway target Adolin
  seeded) with a personal access token (`gh auth token`) standing in for an
  installation token, with no App to create first.
- A future round wiring up webhooks or App-only endpoints (checks, App
  installation management) has to extend `GitHubRepoOps` deliberately;
  `TestRepoOps` only ever implements what this interface declares, so it
  cannot silently drift ahead of the production path.
- Anyone who sets `CHRONICLE_GITHUB_TEST_TOKEN` in a real deployment without
  also setting the allow flag gets a clear refusal instead of a service that
  looks like it is running against the App when it is not.
