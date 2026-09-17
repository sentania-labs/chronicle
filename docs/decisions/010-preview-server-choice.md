# 010: preview server stays a small Python static server

- **Status:** accepted
- **Date:** 2026-09-16

## Context

C0 shipped a placeholder preview server (`http.server`, directory listing
refused) because it was cheap and already real enough to exercise the image
and volume plumbing. C3 has to make it serve one preview per slug at
`/preview/<slug>/...`, refuse anything that would escape the preview root
(a `..`, its percent-encoded form, or a symlink pointing outside it), and
keep running non-root with a read-only root filesystem. Three shapes were on
the table: nginx-unprivileged, Caddy, or keeping the Python server and
making it correct.

## Decision

Keep the Python server. `chronicle/preview/main.py` now resolves every
request against the preview root and rejects it unless the resolved path
(symlinks included) is still inside that root, serves `index.html` for a
directory and a 404 for one without it, and answers `/healthz` directly.

## Consequences, in operational terms

- **Image size and surface.** No new base image or package: the preview
  target still builds from the same `python:3.12-slim` stage as `api` and
  `builder`, so there is one fewer base image to patch and scan. nginx or
  Caddy would each add their own binary and its own CVE stream to watch.
- **Read-only rootfs, for free.** nginx needs a writable `/var/cache/nginx`,
  `/var/run`, and a client-body temp path even for pure static serving,
  which under a read-only root filesystem means tmpfs mounts and a config
  that points at them. Caddy's default state directory has the same shape.
  The Python server writes nothing at runtime; the existing non-root,
  read-only setup needed no new mounts.
- **The config surface is Python, not a DSL.** Path containment (the actual
  risk here: traversal and symlink escape) is forty lines of code this
  round's tests exercise directly (`tests/test_preview.py`), not an nginx
  `location` block relying on `alias`/`root` interaction and
  `disable_symlinks` being remembered on every future edit. A mistake in an
  nginx config only surfaces by curling it by hand; a mistake here fails a
  test in CI.
- **Cost paid later, not now.** nginx or Caddy would outperform this under
  real concurrent load and TLS termination is not this server's job either
  way (that sits in front of it in the lab). At today's traffic (one admin
  clicking preview links) this is not a real cost, and revisiting it if
  that changes means swapping one container image, not the request-handling
  contract `/preview/<slug>/...` already establishes.

## Alternatives considered

nginx-unprivileged was the strongest alternative: broad familiarity,
battle-tested traversal handling. It was set aside because it adds a
package to track and a set of writable paths to carve out of a read-only
rootfs, for a containment guarantee the Python server now provides directly
and testably. Caddy was set aside for the same reason plus a less familiar
config format for whoever next touches this.
