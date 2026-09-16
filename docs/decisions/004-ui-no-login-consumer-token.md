# 004: UI without user login but with a UI consumer token

- **Status:** accepted
- **Date:** 2026-09-16

## Context

The content and preview tabs (spec section 7) are where Scott reviews
drafts, requests revisions, approves, and publishes. The API requires a
bearer token per consumer for every endpoint, reads included (spec section
6 and 11). The UI needs to call that same API. Scott's decision recorded in
spec section 17: no user login on the content and preview tabs in this
revision, because the instance is internal-only and the real gate is the
merge on GitHub, not a login screen in front of it.

## Decision

The UI backend holds its own consumer token, name `ui`, issued at bootstrap,
and calls the API with it like any other consumer. Browsers never hold a
token; there is no anonymous path into the API at any point, login or no
login. Actions the lifecycle reserves for Scott (approve, reject, request
revision, unpublish) are allowed only to the `ui` token in this revision,
which is what "Scott via the UI" means until user login exists (spec
section 11). Admin is a separate surface and is always authenticated by a
password session, independent of this decision (spec section 10).

## Consequences

- Adding user login later touches only the UI: a new auth layer in front of
  a backend that already talks to the API exactly like a logged-in session
  would, with no change to the API's authentication model.
- Anyone who can reach the UI's network path can act as Scott today, since
  the `ui` token is the only gate on content actions. This is accepted
  because the instance is internal-only (spec section 7) and is the reason
  admin still requires its own authenticated session regardless.
- Once consumer tokens exist (no route but `/healthz` and `/readyz` is
  implemented as of round C0), the API's authorization model only has to
  distinguish "the `ui` token" from every other consumer for the
  Scott-reserved actions, so introducing real per-person authorization later
  is a matter of minting distinguishable tokens or sessions, not
  restructuring the permission checks.

## Alternatives considered

Requiring a login on the content and preview tabs now was rejected by
Scott's explicit decision (spec section 17): the merge gate on GitHub is the
actual control, and a login screen on an internal-only instance would add
friction without adding a real boundary at this stage.
