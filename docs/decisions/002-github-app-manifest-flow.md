# 002: GitHub App via the manifest flow instead of a PAT

- **Status:** accepted
- **Date:** 2026-09-16

## Context

Chronicle publishes and unpublishes posts by opening and closing pull
requests against the blog repo (spec section 9), and needs to watch for
their merge. A personal access token could do this, but it would be tied to
a person's GitHub identity, would need to be generated and pasted in by
hand, and would carry whatever scope that person's account has rather than
the narrow set Chronicle actually needs.

## Decision

Chronicle authenticates to GitHub as a GitHub App, created through GitHub's
manifest flow from the admin bootstrap page (spec section 10, step 1), so
the private key is delivered directly to the service and never copied by
hand. The App is installed on the blog repo only, with permissions limited
to contents read and write, pull requests read and write, metadata read,
and actions read (to watch Pages runs). Nothing else. Commits and PRs are
authored by the App's bot identity; Scott merges on GitHub, which remains
the human gate.

The service has no public endpoint, so webhooks are not used; merge
detection is a poll at a modest interval (default 60 seconds while a PR is
open), per spec section 9.

## Consequences

- Credentials are scoped to exactly what publish and unpublish need, not to
  a person's account, and are never typed or pasted by an operator.
- Revoking access means uninstalling the App or rotating its key from
  admin, not rotating a person's PAT and updating every place that used it.
- Losing the manifest flow's callback (for example, no public endpoint
  reachable during setup) is a bootstrap-time constraint the admin flow has
  to handle; this is a cost paid once at bootstrap in exchange for the
  scoping benefit for the life of the instance.
- No webhook infrastructure exists or is needed, at the cost of publish and
  unpublish state lagging up to the poll interval behind an actual GitHub
  merge.

## Alternatives considered

A personal access token was rejected for the reasons above. A GitHub App
created by hand through the developer settings UI (rather than the manifest
flow) was rejected because it still requires an operator to copy a private
key into the service manually, which the manifest flow avoids entirely.
