# 027: browser sign-in through OIDC, and the two anonymous routes it needs

- **Status:** accepted
- **Date:** 2026-10-08
- **Amends:** [004](004-ui-no-login-consumer-token.md) (the content UI's
  "no login" and the list of anonymous routes)

## Context

ADR 004 put the content and preview tabs behind no login at all: the
instance was internal-only, the `ui` token was the one gate, and everything
the UI wrote was authored by a role (`editor`), not a person. Issue 81 asks
for the UI and Admin to know who is at the keyboard, through the lab's own
identity provider, so the records can say who did what and so a person who
leaves a group loses access without anyone editing Chronicle. Its
implementation spec splits the work in three; this ADR covers piece 1,
browser sign-in, and leaves personal API tokens (piece 2) and service
account tokens (piece 3) to later decisions that build on the identity and
roles defined here.

Two of the project's standing rules shape the design. AGENTS.md says
`/healthz` and `/readyz` are the only unauthenticated routes, forever, and
ADR 004 is the record behind it. An OIDC relying party cannot obey that
rule as written: the browser has to be sent to the provider from somewhere
before a session exists, and the provider has to send it back to somewhere
before a session exists. The second rule is that the admin password and
claim-code flows are the break-glass path and must keep working when the
provider is down or misconfigured.

The first attempt (PR 82, closed) wired all three pieces at once and was
found by review to have the state its tables needed never created, the PKCE
verifier thrown away, the id token call wrong, the session secret read from
an attribute that did not exist, the UI gate never wired, the secret written
outside the data directory, anonymous routes added with no decision record,
and provider error text written into a page unescaped. This design is a
fresh one, written against the spec, and each of those is a thing it is
built not to do.

## Decision

### Exactly two anonymous routes, and why

`GET /auth/oidc/start` and `GET /auth/oidc/callback` need no credential.
They are the only two routes added without one, and the reason is the
nature of the flow, not convenience: `start` mints a fresh `state`, `nonce`
and PKCE verifier, seals them in a short-lived cookie and redirects to the
provider; `callback` is where the provider returns the browser with a code.
`callback` compares `state` before anything else, a provider error included:
a response without this attempt's state is refused without ending the
attempt, so a cross-site link to the callback cannot cancel a sign-in.
Both run before a session can exist because they are what produces one.
Neither reads or writes any record under the data directory, neither takes
a bearer token, and both answer 404 in the usual error envelope unless OIDC
is configured, so an instance that has not turned sign-in on has gained no
reachable surface at all. They are mounted in `main.py` beside the UI
router, never inside `build_v1_router()` and never inside the UI router's
own session gate.

Everything else keeps the rule. There is still no anonymous path into
`/v1`; a session cookie authenticates nothing there, and a bearer token
authenticates nothing under `/admin` or the UI. `POST /auth/oidc/logout`
requires a session (it ends one) and the same-origin check every UI form
carries.

### The library

Authlib (`authlib>=1.8`) is the OIDC library: the PKCE challenge, the
authorization request and token request encoders, client authentication at
the token endpoint, and `CodeIDToken`'s claim validation (`iss`, `aud`,
`exp`, `iat`, `nonce`, `azp`, `at_hash`) are its, not hand-written. The id
token's signature is verified with joserfc, which is Authlib's own JOSE
engine and a hard dependency of it; Authlib 1.8 verifies id tokens this
exact way itself and marks its older `authlib.jose` module deprecated, so
`chronicle/api/oidc.py` imports joserfc directly and `pyproject.toml`
declares it, the same rule the existing pydantic entry follows. The HTTP
calls (discovery, keys, the code exchange, userinfo) go through httpx with
a transport a test can replace, the shape the GitHub client already has;
no test touches a network. Only asymmetric signing algorithms are accepted:
an HMAC-signed id token would be signed with the client secret, and anyone
holding that could mint a sign-in.

### What a deployer provides

Environment variables, like every other setting, all under
`CHRONICLE_OIDC_*`: the issuer, the client id, the path of the client secret
file, the redirect URI (default: the external URL plus
`/auth/oidc/callback`), the scopes (default `openid profile email`), the
groups claim name (default `groups`), and the group-to-role mapping. The
client secret is read from a file that must resolve under the data
directory (default `state/oidc-client-secret`), fresh on every exchange so
a rotated file is live without a restart; it is never an environment
value. The mapping is `group=role` pairs (or a JSON object) and the only
roles are `admin` and `editor`; no group name appears anywhere in the code
or a default. Setting any of these without the required ones, naming an
unknown role, mapping no group at all, or pointing the secret file outside
the data directory refuses to start the api, the way a stray test token
does (ADR 012): starting anyway would leave the UI open while the operator
believes it is behind sign-in, or lock everyone out at the callback.

### The session

A successful sign-in sets one cookie, `chronicle_session`: `HttpOnly`,
`SameSite=Lax`, `Secure` under https, twelve hours, sealed with Fernet
under `state/oidc-session.key` (32 random bytes, 0600, created the way
`instance.key` is, never in a backup). It carries the person's issuer,
subject, display name and roles; nothing is stored server-side per
session. The sign-in attempt itself rides a second, ten-minute cookie
scoped to `/auth/oidc`, holding the state, nonce and PKCE verifier, sealed
the same way and tagged so the two kinds can never be swapped for each
other. Deleting the key file and restarting ends every session at once.

### Identity, roles, and what the records say

A person is their issuer plus subject. The name the records carry is the
provider's `preferred_username`, else the email, else the subject (and
never `ui` or `editor`, the ui token's own names: a subject equal to one
is written as `oidc:ui` or `oidc:editor`), because Scott reads
`git log` and a UUID there helps nobody; the log line that admits every
sign-in writes the identity beside that name and the roles granted, so the
name is always traceable. The roles are whatever the person's groups map
to at that moment, recomputed at every sign-in and never cached across
one. A person whose groups map to nothing is refused with a 403 and gets
no session of any kind.

The UI router carries `ui_deps.require_ui_session` as a router-wide
dependency, the shape `/v1` uses for its consumer token, so a new route
cannot forget it: with OIDC unset it is a no-op and the surface is exactly
ADR 014's; with OIDC set a browser navigation without a session is sent to
`/auth/oidc/start` with the page it wanted as `next` (a path on this host
only, never an absolute URL), and anything else gets a 401. The credential
into the store is still the `ui` token (`require_ui_consumer`, ADR 014);
what changes is that the `Consumer` it returns now carries the signed-in
name as `actor`, so every version, event, feedback entry, editor lease and
git commit a UI action produces is authored by the person. `/admin`
accepts a session whose roles include `admin`, checked before the password
session, and admin actions that record an actor (flag resolution) record
the person's name; the password session, the claim code, `/admin/login`
and `/admin/logout` are unchanged and `/admin/login` offers both doors. The
"internal-only and unauthenticated" banner is not shown once OIDC is
configured, because it is then not true.

Unchanged by all of this: `/v1` consumer tokens, the `ui` token and its
revoke-as-kill-switch, the GitHub App flow, and every record format.

## Consequences

- AGENTS.md's rule reads "the only unauthenticated routes are `/healthz`,
  `/readyz`, and the two ADR 027 names". Any further anonymous route still
  needs an ADR of its own first.
- Errors are readable and never carry the provider's words into the page: a
  provider that is down gives a 502 page that names the problem and links
  to the admin password login; a refused code exchange, a token that does
  not verify, an expired attempt and a mismatched state each say so in the
  flow's own wording. The provider's `error_description` and token-endpoint
  messages go to the log, where the operator reads them.
- Revocation is coarse: delete `state/oidc-session.key` and restart, or
  wait twelve hours. There is no per-person sign-out from the server side
  and no refresh of roles inside a session; a person removed from a group
  keeps their current session until it expires. Piece 2 or 3 may want finer
  control and should add it then, not here.
- Sign-out ends the Chronicle session only; the browser's session at the
  provider is the person's own. RP-initiated logout at the provider is not
  done.
- The display name and consumer token names share one namespace in the
  records: a token issued as `scott` and a person whose username is `scott`
  author alike. The admin issuing tokens avoids the clash; the log line
  carrying the identity resolves it after the fact.
- Pieces 2 and 3 have what they need to build on: `oidc_session.Principal`
  (issuer, subject, name, roles), `settings.OIDC_ROLES`, and
  `Consumer.actor`. Nothing for them is built here.

## Alternatives considered

- **An authenticating reverse proxy** (oauth2-proxy and the like) in front
  of the whole instance. Keeps Chronicle's code untouched, but the records
  would still say `editor`, `/admin` would still take only the password,
  and a group change would not change a role; the issue asks for identity
  in the records, not just a wall in front of them.
- **Authlib's Starlette integration** with Starlette's session middleware.
  Would have put a server-wide session cookie on every response, including
  `/v1` and the health routes, and its httpx client module logs a
  deprecation warning on import under the httpx this project pins. The
  protocol pieces alone give the same guarantees without either.
- **PyJWT, already a dependency, for the whole flow.** It verifies a JWT
  and nothing else; PKCE, the request encodings, client authentication and
  the OIDC claim rules would all have been written here, which is exactly
  what the first attempt got wrong.
- **Server-side sessions** (a table or a file per session). Would allow
  per-person revocation, but adds a store, a sweep, and a backup question
  for a surface with a handful of users; a sealed cookie and a twelve-hour
  lifetime are enough for piece 1.
