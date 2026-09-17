# 011: builder scratch directory and lease format

- **Status:** accepted
- **Date:** 2026-09-16

## Context

Spec section 8 leaves two things open: where the builder's scratch tree
lives, and how a builder claims a queued run without two builders (or a
crashed one) both thinking they own it.

## Decision

**Scratch directory.** `data/builder-work/` (`CHRONICLE_BUILDER_WORK_DIR`
overrides it), holding `scratch/<run_id>` (the copied site plus the
converted post and its images), `cache/` (Hugo's `--cacheDir`), and
`resources/` (`HUGO_RESOURCEDIR`). It sits under `data/`, not
`data/preview/`, on purpose: the preview container mounts only the preview
volume, and a scratch tree carrying a full site clone and Hugo's resource
cache has no reason to be reachable through that container even by
accident. It shares the data volume the builder already writes to, so no
new volume is needed, and cleanup (`shutil.rmtree` after every run, success
or failure) never touches anything the preview server serves.

**One exception: the Hugo build output itself.** Hugo writes each run's
build to `data/preview/.tmp/<run_id>`, not under `builder-work/`, found
live in the C3 real-blog check: `Path.replace` (`os.replace`) only stays
atomic when the source and destination share a filesystem, and
`builder-work` is a deliberately separate mount from `preview` in both
compose and the k8s reference. Putting the pre-swap output on the
`builder-work` side made the final `_atomic_swap` into `data/preview/
<slug>/` cross a mount boundary, which `os.replace` refuses outright
("Invalid cross-device link") rather than silently falling back to a copy.
`data/preview/.tmp/<run_id>` costs nothing extra in exposure: it is already
the run's finished, public-bound output at the moment it lands there, the
same content `<slug>/` is about to become, just not yet visible under a
slug the api's status route would ever hand out.

**Lease format.** `data/state/builder/leases/<run_id>.json`:

```json
{"run_id": "...", "builder_id": "...", "claimed_at": "...",
 "expires_at": "...", "renewed_at": "..."}
```

Outside `data/repo/` deliberately (AGENTS.md's filesystem-first rule is
about the durable record; a lease is runtime coordination between
processes, not history worth a git commit) and outside `data/preview/` for
the same reason the scratch directory is. Claiming is `open(O_CREAT |
O_EXCL)`, so exactly one process ever creates a given lease file; a lease
past its `expires_at` is stale and is taken over under `flock` on the lease
file itself, which is what stops two builders from both deciding the same
stale lease is theirs. Full mechanics and the crash-recovery argument are
in `chronicle/builder/leases.py`'s module docstring, not repeated here.

## Consequences

- A run whose builder crashes mid-build shows `building` in its own record
  until the lease expires (`CHRONICLE_BUILDER_LEASE_SECONDS`, default 900s,
  comfortably longer than the 600s default build timeout so a legitimately
  slow build is never mistaken for a crash). Until then, the admin status
  page shows a run in flight with no builder actually working on it; this
  is the same kind of honest-but-stale state ADR 005 already accepts for
  reconciliation, not a bug to route around here.
- `builder-work/` counts against the same disk budget as everything else
  under `data/`. Full rebuild every run means it never grows unbounded
  across runs, only within one run's lifetime, and every exit path in
  `runner.build_one` removes both `scratch/<run_id>` and `output/<run_id>`
  before returning.
