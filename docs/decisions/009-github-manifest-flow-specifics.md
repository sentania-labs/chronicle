# 009: GitHub App manifest flow specifics

- **Status:** accepted
- **Date:** 2026-09-16

## Context

ADR 002 already decided Chronicle authenticates as a GitHub App created
through the manifest flow rather than a hand-registered OAuth App or a
personal access token. That ADR did not settle the details a working
implementation needs: what the manifest actually contains, how the
CSRF-equivalent `state` value is carried across a redirect Chronicle's own
server never sees the middle of, whether the target is a user account or an
organization, and how the installation id (a second, separate step from
creating the App) gets captured.

## Decision

**The manifest omits `hook_attributes` entirely.** Spec section 9 says
merge detection is polling, not webhooks, because the service has no public
endpoint; a `webhook_secret` still comes back from GitHub's conversion
response regardless (GitHub always mints one), so it is stored encrypted
alongside the other two secrets for whichever later round turns webhooks on,
but nothing generates or sends one today.

**`public` is always `false`.** This App exists to act on one private
instance's blog repo; there is no scenario where a second party installs it.

**`default_permissions`, exactly:** `contents: write`, `pull_requests:
write`, `metadata: read`, `actions: read`. No `single_file`: Chronicle writes
whole post files and their images through the Git data API (spec section 9),
never a single tracked file, so `single_file` scope would add restriction
with no matching use.

**`redirect_url` is `{CHRONICLE_EXTERNAL_URL}/admin/github/callback`.**
`CHRONICLE_EXTERNAL_URL` is a new required-in-practice setting (defaults to
`http://localhost:8080` for local development, where the manifest flow is
untestable against real GitHub anyway since GitHub must be able to redirect
a browser back to it): a from-scratch instance cannot build a correct
redirect target from a request's own `Host` header, because the browser
completing the manifest flow is Scott's, not a request Chronicle's server
ever handles directly. Recorded once in an env var rather than derived,
because the alternative is asking an unauthenticated request to assert its
own external hostname, which is exactly the kind of thing a reverse proxy in
front of Chronicle should not be trusted to tell it.

**State handling: an unsigned random nonce in a short-lived, path-scoped,
`HttpOnly` cookie, compared against the query parameter with
`secrets.compare_digest`.** `GET /admin/github/connect` (a session-gated
page, so only an authenticated admin ever reaches it) mints
`secrets.token_urlsafe(24)`, embeds it in the manifest submission's `state`
query parameter, and sets it as a cookie scoped to `/admin/github` with a
600-second `Max-Age`. The callback reads the same cookie and refuses the
request outright if it is missing or does not match. This does not need to
be HMAC-signed the way the session cookie does: its only job is proving the
callback belongs to the same browser that started the connect flow, a
single round trip through a party (the admin's own browser) already
trusted, not authenticating anything on its own.

**User account vs. organization target is the same manifest, a different
GitHub URL.** `https://github.com/settings/apps/new?state=...` targets the
signed-in user's own account; `https://github.com/organizations/{org}/settings/apps/new?state=...`
targets an organization. The admin page offers both as two form actions
against the same manifest JSON; Chronicle never needs to know which one was
used until the callback, since the conversion response is identical either
way.

**Installation id capture is a distinct manual step, not inferred.** GitHub
does not return an installation id from the manifest conversion: it is
created only when the admin (or an org owner) installs the App, a second
browser action GitHub does not redirect back from in any form the manifest
flow specifies. `GET /admin/github/install` mints an App JWT and calls `GET
/app/installations` to list what the App's own credentials can already see,
letting the admin pick one; a text field also accepts a pasted id directly,
for the case where the App was installed by someone else or `list
installations` is slow to reflect a brand-new install.

## Consequences

- `CHRONICLE_EXTERNAL_URL` has to be set correctly before starting the
  connect flow, or the manifest's `redirect_url` sends GitHub's redirect
  somewhere Chronicle is not listening; README's runbook calls this out as
  the one setting that must be right before step one.
- Losing the state cookie (a browser that clears cookies mid-flow, or a
  second browser completing the redirect) means starting over from `/admin/github/connect`;
  there is no recovery path, which is intentional, since accepting a
  callback with no matching state is exactly the CSRF this check exists to
  refuse.
- A webhook secret sits encrypted at rest, doing nothing, until some later
  round wires up a webhook receiver. That round has to add the receiving
  endpoint and decide it is worth accepting the public exposure the current
  polling design deliberately avoids (ADR 002); this ADR does not decide
  that trade for them.

## Alternatives considered

**Deriving `CHRONICLE_EXTERNAL_URL` from the request.** Rejected: the
request that needs the URL (building the manifest) is not the request GitHub
redirects to, and even if it were, trusting a `Host` or `X-Forwarded-Host`
header for something as consequential as where a GitHub App's callback
lands is the wrong default for a service that otherwise trusts nothing an
unauthenticated request says about itself.

**Signing the state value like the session cookie.** Would add nothing: the
threat this defends against is a callback request that did not originate
from the browser that started the connect flow, which an unpredictable
per-flow nonce already defeats. Signing would matter if the value carried
information to trust later; it does not outlive the single round trip.
