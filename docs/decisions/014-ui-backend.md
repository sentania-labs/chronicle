# 014: the UI backend calls the store in-process, authenticated as the ui token

- **Status:** accepted
- **Date:** 2026-09-17

## Context

C5 adds the content and preview tabs: server-rendered pages served by the api
process at `/`, with no login of their own (spec section 11, ADR 004). Every
domain write still has to look, to the store and to `git log`, exactly like a
call any other consumer token could have made, authored `scott` per the `ui`
token's existing meaning (`chronicle/api/tokens.py:commit_author`). Two ways
to get there:

- **Loopback HTTP.** The UI route handler builds a real `Authorization:
  Bearer <token>` request to `http://127.0.0.1:8080/v1/...` and lets
  `require_consumer` authenticate it exactly like an external caller.
- **In-process.** The UI route handler calls `Store` methods directly,
  constructing the same `Consumer` object `require_consumer` would have
  returned, without a second HTTP round trip.

## Decision

In-process, through a new dependency (`ui_deps.require_ui_consumer`) that
reads `data/state/ui_token.txt` fresh on every request, calls
`TokenStore.authenticate` on that plaintext exactly the way a bearer header
would be checked, and only then returns `Consumer(token_name=UI_TOKEN_NAME)`.

In operational terms:

- **What breaks if the token rotates.** Nothing, either way, if "rotates"
  means "a new value is minted and the old one still works during a
  changeover" (`ensure_ui_token` never does that; it only re-mints when the
  old record is unreadable). But the sharper case is a revoke: an admin
  pulling `ui` from `/admin/tokens` as an emergency brake. Loopback would
  need its own fresh read of the file before every call anyway to see that,
  so this is not a loopback-only cost; the reason to call out "reads the
  file at request time" here is that in-process is not exempt from it, and
  it would be a live bug (a UI that silently keeps acting as `scott` after
  its own credential was revoked) to skip the read and hardcode
  `Consumer(token_name=UI_TOKEN_NAME)` from a cached value. Every mutating
  UI route therefore pays one `TokenStore.authenticate` call, the same cost
  `require_consumer` already pays for a real bearer call.
- **What an extra hop costs.** A loopback call is a second HTTP request per
  page action: a new connection or a pooled one, a second pass through
  FastAPI's own routing and validation, and a second failure mode (the
  process not yet listening on its own port during startup, a body-size
  middleware measuring the same payload twice). None of that buys isolation
  here, because it is the same process listening to itself; the "boundary"
  a network hop usually enforces does not exist when both sides are the
  same address space. In-process pays exactly one call into `Store`, the
  same call the `/v1` route handler itself makes.
- **What auditing sees as the actor.** Identical either way: `Consumer.name`
  resolves `ui` to `scott` (`tokens.commit_author`), so every version,
  event, and git commit a UI action produces is authored `scott` whether
  the call crossed a socket or not. Choosing in-process does not weaken
  this; the same `Consumer` type and the same `commit_author` mapping
  produce it.

Given the audit outcome is identical and the loopback hop only adds latency,
failure modes, and a second copy of body-size and validation logic to keep
in sync with `/v1`'s own, in-process is the one with a real advantage: it
cannot itself become unreachable while the rest of the api is serving `/v1`
fine, and a crash in the UI's own request handling cannot look like a
network-level 502 from the api to itself.

`require_ui_consumer` deliberately mirrors `require_consumer`'s shape
(`chronicle/api/deps.py`) rather than reusing it directly: the former reads
its bearer value from a file, the latter from a header, and collapsing them
into one function would mean threading a "where does the token come from"
branch through code whose whole point is that a `/v1` route never has to
ask that question.

## No session, no vendored heavyweight framework

Content and preview carry no cookie and the browser never holds the `ui`
token (AGENTS.md, this round's hard rule): a visible banner
(`CHRONICLE_UI_BANNER`, on by default) says so on every page, because
"internal-only and unauthenticated" is an operational fact Scott needs
looking at the page, not just reading a README.

No htmx or similar was pulled in. The UI's own interactivity is one
textarea-to-preview-pane wire-up (`chronicle/api/static/ui.js`, hand-written,
under twenty lines) driven by a vendored markdown renderer
(`marked` v12.0.2, MIT, `chronicle/api/static/vendor/marked.min.js`,
licence noted in `THIRD_PARTY.md` and in the file's own header comment).
Every other interaction on these pages is a plain HTML form post and a
redirect, the same shape `admin_templates.py` already uses; introducing a
client-side framework to manage state a server-rendered page and a redirect
already manage would be new surface with no problem behind it.

## CSRF, given no session exists to steal

A cookie-based CSRF token would protect a credential the browser holds; here
the browser holds nothing; the risk is a third-party page on the same
internal network POSTing to Chronicle's forms in a visitor's browser tab,
not session theft. `ui_deps.check_same_origin` compares `Origin` (falling
back to `Referer`) against the request's own host on every state-changing
UI route and refuses a mismatch with 403. This is cheap, adds no credential
the banner would then have to explain, and closes the one CSRF-shaped hole
that exists on a surface with no login: a cross-origin form or script
submitting here on a visitor's behalf.

## Consequences

- A UI route handler that forgets `require_ui_consumer` calls `Store`
  directly with no actor at all, which is a type error (every mutating
  `Store` method takes an `actor: str`), not a silent anonymous write; there
  is no equivalent of `/v1`'s router-wide dependency to lean on here because
  the UI's own read routes (the drafts board, a submission's detail page)
  legitimately need no actor at all. Reviewers should treat this the way
  `/v1`'s own bar treats a route that skips `require_consumer`: a blocking
  finding, not a style note.
- Revoking `ui` on `/admin/tokens` now doubles as a kill switch for the
  whole UI, not just for whatever external process happened to hold the
  bearer token before C5. That is a feature (AGENTS.md's "no manual infra
  changes" rule needs exactly one lever to pull when disabling the UI
  outright), not a side effect to work around.
- The UI backend never needs its own health check separate from the api's:
  it lives in the same process, behind the same `/healthz` and `/readyz`.

## Amendment, 2026-09-19: the ui token now authors `editor`, not `scott`

The decision text above is left as written; the mapping it describes has
changed. `UI_COMMIT_AUTHOR` (`chronicle/api/tokens.py`) now resolves the
`ui` token to `editor`, a role name rather than a person, so every version,
event, and git commit a UI action produces from this date forward is
authored `editor`. Everything the decision text above says about in-process
authentication, the actor `require_ui_consumer` resolves, and revocation as
a kill switch still holds; only the resulting name changed. History is
mixed: records written before this change still say `scott` and are not
rewritten. See `docs/notes/lane-f-part-b.md`.
