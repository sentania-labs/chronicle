# 005: reconciliation produces flags only, never automatic correction

- **Status:** accepted
- **Date:** 2026-09-16

## Context

Main is the source of truth for published posts and for the build
toolchain (spec section 2), but Chronicle's own records (draft status, post
records, pinned slugs) can drift from it: a merge Chronicle never observed,
a post edited or removed directly on GitHub, a slug changed by hand. Section
12 lists the mismatches reconciliation must detect: a draft marked published
whose post is missing on main, a post on main with no draft marked
published, a post removed on main without an unpublish run, a slug on main
that differs from the draft's pinned slug.

## Decision

Reconciliation runs at startup, on a schedule (default hourly), and on every
observed merge, and it only ever flags a mismatch. It never applies an
automatic correction or deletion. Every flag appears on the admin status
page with a one-click resolution (mark as published, mark as unpublished,
import as draft, ignore) that Scott chooses (spec section 12, and Scott's
decision in section 17). Every published draft's `source_post` is verified
against main separately; if main moved because Scott edited on GitHub
directly, reconciliation records a new version authored `github` rather than
silently overwriting the draft's history.

## Consequences

- No reconciliation run can delete a draft, a post record, or an image on
  its own conclusion, which bounds the blast radius of a reconciliation bug
  to a wrong flag rather than lost data.
- Every drift condition accumulates as a visible flag until Scott resolves
  it, which means the admin status page has to be checked, not just green
  CI or a healthy `/readyz`. This is the operational cost of the safety
  guarantee, and is out of scope for round C0.
- A future automatic resolution for a specific, well-understood flag would
  be a new decision superseding this one, not an incremental change to the
  reconciler.

## Alternatives considered

Automatic correction (for example, silently marking a draft published
whenever its post appears on main) was rejected because it can act on a
false signal, such as a coincidentally matching slug from an unrelated post,
in a way a person overseeing the flag would catch immediately. The record
of what reconciliation found and what Scott chose to do about it is also
the audit trail this service is built to keep (spec section 5).
