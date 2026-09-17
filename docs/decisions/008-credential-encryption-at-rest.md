# 008: instance key and Fernet for credentials at rest

- **Status:** accepted
- **Date:** 2026-09-16

## Context

Round C2 gives Chronicle three real secrets to hold: the GitHub App's private
key PEM, its OAuth client secret, and its webhook secret (unused this round,
kept for when C3 or later wants it). Spec section 11 says "no credential is
ever written to a draft, a PR, a log, or a bundle in plaintext." ADR 007
already decided consumer tokens need no encryption because they are hashed,
one-way, machine-generated secrets; these three are different; the service
has to read them back to act as the App, so hashing will not do, and they are
human-issued, not something Chronicle mints itself.

## Decision

**A 32-byte instance key at `data/state/instance.key`, generated once with
`os.urandom`, mode 0600, created with `O_CREAT | O_EXCL` so two processes
racing to bootstrap cannot overwrite each other's key.** Every secret is
encrypted with `cryptography.fernet.Fernet`, keyed by
`base64.urlsafe_b64encode(instance_key)`. Fernet over NaCl's secretbox: this
project already depends on `cryptography` for the App JWT's RSA signing, so
Fernet costs no new dependency, and Fernet's format carries its own
timestamp and HMAC, which is exactly the "encrypt this blob, decrypt it
later, notice tampering" shape all three secrets need. Nothing here needs
NaCl's box (public-key) primitives; secretbox would work as well but is not
already in the dependency tree.

**The instance key is never included in a backup bundle.** A bundle that
carried the key would make the encryption pointless: whoever has the bundle
would have everything needed to read it. The bundle format itself is C3 or
later work (spec section 13); this round records the constraint in
`README.md`'s data directory section so it is not lost before the bundle
exists to enforce it.

**`data/state/github-app.json` file format:**

```json
{
  "schema_version": 1,
  "app_id": "123456",
  "slug": "chronicle-a1b2c3d4",
  "client_id": "Iv1.abc123",
  "html_url": "https://github.com/apps/chronicle-a1b2c3d4",
  "client_secret_enc": "<fernet token>",
  "webhook_secret_enc": "<fernet token>",
  "pem_enc": "<fernet token>",
  "created_at": "2026-09-16T10:00:00-05:00",
  "installation_id": "789",
  "owner_repo": "sentania/sentania.github.io",
  "default_branch": "main",
  "last_verified_at": "2026-09-16T10:05:00-05:00",
  "last_error": null,
  "permissions": {"contents": "read", "pull_requests": "read"}
}
```

Everything except the three `_enc` fields is either public (App id, slug,
client id, install target) or Chronicle's own operational bookkeeping
(verification time, last error class). The file is still written mode 0600
as defence in depth, even though only the three fields are actually secret.

**Only `github_app.py` and `github_client.py` decrypt anything.** Content
routes under `/v1` never import either module (spec section 11's "narrow
internal interface"); the admin router calls `GitHubAppStore`'s methods and
never reads `github-app.json` directly.

## Consequences

- Losing `data/state/instance.key` makes every encrypted credential
  permanently unrecoverable; the App has to be re-created through the
  manifest flow. This is the intended failure mode, the same shape as losing
  a LUKS header: the alternative (a recovery path around the key) is a
  recovery path around the encryption.
- Rotating the instance key (never automated, no route exists for it this
  round) means re-encrypting every `_enc` field or re-running the manifest
  flow. Out of scope for C2; noted for whichever round adds key rotation.
- A stolen `github-app.json` alone is useless without `instance.key`; a
  stolen `instance.key` alone is useless without `github-app.json`. An
  attacker needs both files, which is why `instance.key` living outside any
  backup bundle matters: a stolen bundle is not enough on its own.

## Alternatives considered

**NaCl secretbox.** Materially the same guarantee (authenticated symmetric
encryption of a byte string) with a smaller, simpler surface than Fernet's
JSON-adjacent token format. Not chosen because it needs `pynacl` as a new
dependency where Fernet needs none, given `cryptography` is already present
for RSA.

**Encrypting the whole `github-app.json` file instead of three fields.**
Would hide operational fields (installation id, repo, last verified time)
that the admin status page and `/readyz` need to read on every request; the
UI already has to be careful not to render a secret, and field-level
encryption makes that a "not fetched" fact instead of a "must not print
this field" discipline to maintain everywhere the record is read.

**A per-secret random key instead of one instance key.** Would need somewhere
to store those per-secret keys, and that somewhere becomes the actual secret
the instance key already is. No net reduction in what has to be protected.
