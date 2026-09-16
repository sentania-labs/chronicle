# 001: filesystem-first store with internal-only git

- **Status:** accepted
- **Date:** 2026-09-16

## Context

Chronicle is a domain service in the coppermind family (spec section 1):
filesystem-first, small API, small UI, nothing an operator could not walk
away from. Section 4 defines submissions, drafts, versions, feedback, posts,
images, and runs as files under a data directory, and section 13 lays out
that layout: `repo/` for everything but images, `images/` content-addressed
and outside git, `preview/` and `site/` disposable, `state/` restricted.

The domain model needs a real history: every draft version, every feedback
entry, every author's edit. That history has to survive a restart, support a
diff between two authors' versions (the ghostwriter calibration signal in
section 4), and never leave the instance.

## Decision

The data directory is the source of truth. Git tracks `repo/` (everything
except the image store) as internal history only. Every save is one commit,
authored by the consumer token name or `scott` for the UI, with the feedback
log appended in the same commit. This git repository is never pushed
anywhere; it has no remote. Publishing to the public blog repo is a
completely separate action through the GitHub App (see 002), against a
different git history entirely.

Images are content-addressed by sha256 and stored outside git, since binary
blobs do not diff usefully and git history is not the mechanism protecting
them; the backup bundle (section 13) is.

## Consequences

- A draft's edit history is `git log` on `repo/drafts/<id>`, free of any
  extra bookkeeping.
- Restore is straightforward: replace `repo/` (with its `.git`) and
  `images/` from a backup bundle, no PostgreSQL or external store to
  reconcile against.
- Total loss without a bundle recovers only what reconciliation can rebuild
  from the public blog's main branch (published posts); everything else is
  gone. Scott accepted this trade in the spec (section 13); the bundle is
  the mitigation, not a guarantee.
- No push credential for this git repository exists anywhere in the
  service, which removes an entire class of accidental-exposure risk: there
  is nothing to leak that would let internal drafts reach a public remote.

## Alternatives considered

A database of record (PostgreSQL, as coppermind uses for its derived state)
was rejected as the source of truth for the same reason coppermind rejects
it: every row would need to be the durable copy of something a person wrote,
which contradicts filesystem-first. A database mirror rebuildable from
`repo/` remains open for a later round if query performance demands it, the
same way coppermind's PostgreSQL mirrors its notes filesystem.
