# 025: the editor lock replaces the advisory claim

- **Status:** accepted
- **Date:** 2026-09-24

## Context

A draft's claim (`POST /v1/drafts/{id}/claim`, the edit page's Claim button)
recorded a name and a time, and no save path ever checked it. It looked like
a lock and was not one (issue #64). The collision that matters is the editor
working in the UI while the ghostwriter saves the same draft through the API.

## Decision

- **Lease.** Opening a draft's edit page takes a lease for the viewing
  identity: the consumer name the domain records (`editor` for the UI). The
  page renews it every 30 seconds; it lapses 2 minutes after the last
  renewal, so a closed tab, a sleeping laptop or a dropped connection never
  leaves a draft stuck. Closing the page sends a best-effort release.
- **Enforcement.** While identity A holds a live lease, a save by any other
  identity, through the UI save route or `PUT /v1/drafts/{id}`, is refused
  with 423 `draft_being_edited`, naming the holder, since and expiry.
  Same-identity saves (two tabs) are not blocked; the version check and the
  conflict page stay the guard there. Reads, previews, feedback and the
  reviewer's actions are never blocked.
- **Runtime state.** Leases live in the api process's memory
  (`editor_lease.EditorLeases` on `Services`), one api process per ADR 013.
  They are not versioned, committed or backed up; a restart forgets them,
  which is the same as every one lapsing.
- **UI.** The Claim and Release buttons and the claim text are gone. When
  another identity holds the lease, the edit page opens read-only with a
  "Being edited by X since HH:MM" banner and a disabled Save, and reloads
  itself when the heartbeat reports the lease has come to it. The board marks
  a draft with a live lease "editing: X".
- **API.** `GET /v1/drafts/{id}` carries `editing: {holder, since,
  expires_at}` or null. `POST /v1/drafts/{id}/claim` and `/release` are
  removed outright (Scott's call on the issue), not kept as no-op aliases.
- `Draft.claim` stays in the model only so records that carry one still load;
  nothing writes or reads it.

## Consequences

- The ghostwriter's blog-drafting skill must stop calling the claim
  endpoints and handle a 423 on save (wait and retry for a bounded time, or
  report back). Tracked in the ghostwriter repo.
- A second api replica would need the leases moved out of process memory.
