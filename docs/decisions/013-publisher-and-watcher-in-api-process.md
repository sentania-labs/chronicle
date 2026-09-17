# 013: the publisher and merge watcher run in the api process

- **Status:** accepted
- **Date:** 2026-09-16

## Context

C4 needs something to consume `publish` and `unpublish` runs from the queue
and drive them through the GitHub App's git data and pulls API, and
something to poll the PRs it opens until they merge or close. The builder
container (C3) already polls the same queue directory for `preview` runs
and already has its own container, so putting this work there instead of
the api is the visible alternative.

## Decision

Both run inside the api process: a publisher thread that claims `publish`
and `unpublish` queue entries (the builder's own `claim_next` only ever
looks at `kind="preview"`, so the two never compete for the same entry),
and a watcher thread that polls `data/repo/watch/` for open Chronicle PRs.

Reasons, in order of how much they'd cost to get wrong:

- **The GitHub App credentials already live here, decrypted.** `github-app.json`
  is encrypted at rest under the instance key (ADR 008), and only the api
  process ever holds that key in memory. The builder image has no reason to
  read it, and giving it one means a second process now needs the
  decryption path, the installation-token cache, and the same care about
  never logging a secret that `github_client.py` already has to have.
- **Publish and unpublish need no Hugo toolchain.** The builder container
  exists because Hugo does: it is pinned by `HUGO_VERSION`, it needs git for
  submodules, and a full site copy per run. A publish run writes one post
  file, a handful of images, and calls the git data API; none of that
  touches Hugo. Putting it in the builder would mean shipping Hugo into a
  container that never runs it for this purpose, or splitting the builder
  image in two, either of which is a deployment change with no upside.
- **The watcher and the publisher share the same read of `data/repo/`.**
  Both need to know which drafts have an open Chronicle PR, and the
  publisher is what creates that record in the first place
  (`data/repo/watch/<draft_id>.json`). Splitting them across two containers
  would mean the same store, opened by two processes, coordinating through
  the file the publisher just wrote, for no reason the builder's own
  api-vs-builder split already has (that split exists because the builder
  needs a different container image, not because it needs a different
  store).
- **Reconciliation already has to run somewhere with the store and the
  GitHub client both in hand**, and spec section 12 has it running "at
  startup, hourly, and on every observed merge": the third of those is a
  direct call from the watcher's own merge handler, which only works
  cleanly if they are in the same process.

This assumes a single api replica, the same assumption `AdminServices`
already makes (an in-memory installation-token cache with no cross-replica
invalidation, C2). A second api replica would mean two publisher threads
racing the same queue; nothing in this round adds locking for that case
because nothing in the deployment story calls for more than one replica
yet. If that changes, the publisher and watcher need the same
one-claim-at-a-time discipline the builder's lease already has
(`chronicle/builder/leases.py`), not a redesign, since queue entries and
watch files live under the same `data/repo/` every process already shares.

## Consequences

- One more background thread pair in the api process, alongside none
  today (C1 through C3 are all request-driven). Both are daemon threads
  started at `_bootstrap` time, same as `AdminServices` construction, and
  both stop when the process does; a crash mid-publish leaves a run
  `building` exactly the way a crashed builder leaves a preview run
  `building`, and the next api start's publisher recovers it the same way
  `recover_expired_leases` does for the builder (no separate lease
  directory: the publisher is the only writer of `publish`/`unpublish`
  queue entries' claim state, since there is only one of it).
- The api's own readiness and liveness no longer describe only request
  handling: a publisher or watcher thread that dies silently would leave
  runs stuck `queued` forever with `/readyz` still green. The status page's
  existing heartbeat pattern (`builder_heartbeat`) is extended with
  `publisher_heartbeat` and `watcher_heartbeat` for the same reason C3 added
  one for the builder: a stuck loop should be visible without reading logs.
- The builder image gains no new responsibility and no new secret exposure;
  it stays exactly what ADR 011 already describes.
