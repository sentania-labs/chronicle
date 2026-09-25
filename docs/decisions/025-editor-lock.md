# 025: the editor lock replaces the advisory claim

- **Status:** accepted
- **Date:** 2026-09-24

## Context

A draft's claim (`POST /v1/drafts/{id}/claim`, the edit page's Claim button)
recorded a name and a time, and no save path ever checked it. It looked like
a lock and was not one (issue #64). The collision that matters is the editor
working in the UI while the ghostwriter saves the same draft through the API.

## Decision

- **Lease.** An open edit page holds a lease for the viewing identity: the
  consumer name the domain records (`editor` for the UI). A top-level,
  same-origin load of the page takes it at render, under a page token the
  server mints, so the editor is protected before (or without) script; the
  browser's Fetch Metadata headers keep an `<img>` or link on another site,
  a prefetch, and the editor's own background refetch from taking one.
  `editor.js` then beats once on load and every 30 seconds under the same
  token, through a same-origin POST. The lease lives while any
  of its pages has beaten in the last 2 minutes, so a closed tab, a sleeping
  laptop or a dropped connection never leaves a draft stuck. Closing a page
  sends a best-effort release of that page's hold only, so a second tab, or
  the next page of a navigation, keeps the lock.
- **Enforcement.** While identity A holds a live lease, a save by any other
  identity, through the UI save route, `PUT /v1/drafts/{id}`, or `/v1` image
  attach and detach (which can change the body the editor has open), is
  refused with 423 `draft_being_edited`, naming the holder, since and expiry.
  The check and the write are one step (`EditorLeases.writing`): a lease
  cannot be granted between an admitted save's check and its write.
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
- `Draft.claim` stays in the model only so records that carry one still
  load; a stored value is dropped on load, so `GET` always shows
  `"claim": null` and no response names a holder nobody can release.
- **Who it actually protects.** Only the UI takes leases, and the UI is always
  `editor`, so in practice the lock stops API consumers (the ghostwriter)
  while the editor has a draft open. It is not UI-against-UI locking: two
  tabs are the same identity, guarded by the version check. The read-only
  banner exists for a future non-editor lease holder.

## Consequences

- The ghostwriter's blog-drafting skill must stop calling the claim
  endpoints and handle a 423 on save (wait and retry for a bounded time, or
  report back). Tracked in the ghostwriter repo.
- A second api replica would need the leases moved out of process memory.
