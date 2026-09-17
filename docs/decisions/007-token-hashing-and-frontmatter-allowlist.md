# 007: sha256 for token hashing, and the frontmatter allowlist

- **Status:** accepted
- **Date:** 2026-09-16

## Context

Two things the spec requires but deliberately leaves open, both operational
rather than architectural, and both needed before the first `/v1` route
exists.

Section 11 says consumer tokens are "hashed at rest" without naming the
algorithm. Section 4 says a draft's frontmatter is a "Hugo allowlist, same
field order the dashboard port uses today" without listing the keys, and an
allowlist with no list is not enforceable.

## Decision

**Tokens are hashed with a plain sha256 of the token bytes, stored as hex.**
The token itself is `secrets.token_urlsafe(32)`: 256 bits of CSPRNG output.
A slow KDF (argon2, bcrypt, scrypt) exists to make guessing low-entropy human
input expensive, and there is no low-entropy input here to guess. Against a
256-bit random secret, a slow hash buys nothing an attacker would ever finish
paying for, while costing every single authenticated request the KDF's work
factor. Tokens are compared with `hmac.compare_digest`, stored only as their
digest, and the plaintext is returned exactly once at issue: printed by
`chronicle token issue`, or written to `data/state/ui_token.txt` at mode 0600
for the UI backend's bootstrap token. Nothing logs a plaintext token.

**The frontmatter allowlist is exactly these keys, in this order:** `title`,
`date`, `lastmod`, `draft`, `description`, `tags`, `categories`, `series`,
`slug`, `featureImage`. Every key is optional except `title`, which is
required to save or submit, because it is what a slug is derived from at
pinning time. Any other key in submitted frontmatter is a 422 naming the
offending keys, never a silent drop.

## Consequences

- A stolen `tokens.json` yields digests, not tokens, and the response is the
  same either way: revoke every name in it and issue new ones. There is no
  scenario in which a work factor on the hash would have changed that
  response.
- If tokens ever become something a person types or chooses, this decision
  has to be revisited, because the premise (high-entropy random input) is
  what makes it correct.
- Authors get a precise error instead of a post that silently lost a field
  between the draft and the published site. The cost is that a new Hugo
  field is a code change here, which is the intended gate: the publish path
  writes only fields Chronicle knows how to render.
- The allowlist order is also the render order for the diff view and, later,
  for the published file, so two versions of a post do not diff on field
  order alone.

## Amendment, 2026-09-16: the allowlist against the real blog

Adolin ran `from_post` against a read-only clone of the actual blog repo
(347 posts). The allowlist above was written against the dashboard port's
field list from memory, not against what the real posts contain, and it
missed keys every real post carries: `author` (346 of 347), `type` (all
347), and `url` (all 347). It also lacked `summary` and `shareImage`, both
present in the dashboard port's own field set.

Losing `url` on import is the one that matters operationally: `url` is the
permalink Hugo has already been serving for that post, and it is not
derived from anything else Chronicle stores (not `slug`, which can differ
from the URL segment on an older post). A republish that dropped it would
silently break every existing inbound link to that post, with no error at
publish time to catch it.

**The allowlist is now these keys, in this order:** `title`, `author`,
`type`, `date`, `lastmod`, `draft`, `url`, `slug`, `description`, `summary`,
`categories`, `tags`, `series`, `featureImage`, `shareImage`. `title`
remains the only required key. `excerpt`, seen on 2 of the 347 posts, stays
off the list: too rare to be a dashboard field, and `from_post` already
reports a dropped key as a warning rather than failing the import.

## Alternatives considered

**argon2id for tokens.** The default reflex, and wrong here for the reason
above: it is a defence against dictionary attack on human-chosen secrets,
paid per request. Revisit only if tokens stop being machine-generated.

**HMAC with a server-side pepper.** Would add value against an attacker with
the database but not the state directory's key material. Chronicle has no
such split: `tokens.json` and any pepper would sit in the same 0700
directory, so the pepper would protect nothing it does not already share a
fate with.

**No frontmatter allowlist, pass through whatever the author sends.** Would
let an author set `draft: false`, `aliases`, or a layout override that
Chronicle never reasons about and the publish path never validates. Rejected:
the service owns the conversion to a post file, and what it does not
recognise it must refuse rather than forward.
