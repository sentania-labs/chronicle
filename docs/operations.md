# Running an instance

This is the reference for standing up and operating a Chronicle instance:
the data directory, tokens, the three container images, admin bootstrap,
and where backups fit. For how content actually moves through the service
(preview, publish, merge watch, reconciliation, image paths), see
[docs/authoring-flow.md](authoring-flow.md).

## The data directory

`CHRONICLE_DATA_DIR` names it. The filesystem is the record, git is the
history, and the index is a cache (ADRs 001 and 006).

```
data/
  repo/                          internal git repository, no remote, never pushed
    submissions/<id>.json
    submissions/<id>/versions/<n>.json   one per revision of a submission (ADR 018)
    drafts/<id>/draft.json       current draft
    drafts/<id>/versions/<n>.json
    feedback/<draft_id>.jsonl    the feedback record the API reads, one entry per line
    feedback/<draft_id>.md       the same entries rendered for a human, never parsed back
    posts/<slug>.json
    runs/<id>.json
    runs/queue/<run_id>.json     consumed by the builder (preview) or the publisher (publish/unpublish)
    runs/logs/<run_id>.log       captured build output, read by GET /v1/runs/{id}/log
    watch/<draft_id>.json        one open Chronicle PR the watcher is polling (ADR 013)
    reconcile/<flag_id>.json     one reconciliation flag (spec section 12, ADR 005)
    events/log.jsonl             append-only, one object per line, monotonic seq
    index/chronicle.db           derived SQLite, gitignored, rebuildable
  images/<sha[:2]>/<sha>.<ext>   content-addressed, sha256 is the image id
  state/                         0700, every file inside 0600 except toolchain.json
    tokens.json, ui_token.txt      consumer tokens (ADR 007)
    claim-code                     one-time admin claim code; deleted once claimed
    admin.json                     argon2 password hash and session signing secret
    instance.key                   32 random bytes; encrypts github-app.json; never backed up
    oidc-client-secret             the OIDC client secret, mounted by the deployer (ADR 027); never backed up
    oidc-session.key               32 random bytes sealing browser sessions (ADR 027); never backed up
    github-app.json                GitHub App record, secrets encrypted at rest (ADR 008)
    digest-status.json             last digest's counts and timestamps, not secret
    toolchain.json                 last digest's Hugo version and theme commits, 0644
    builder/heartbeat.json         builder id, last loop time, hugo version, queue depth
    builder/leases/<run_id>.json   one builder's claim on one run (ADR 011)
    publisher/heartbeat.json       last publish/unpublish poll loop time, queue depth
    watcher/heartbeat.json         last watch poll loop time, PRs watched, current interval
    reconcile/heartbeat.json       last reconciliation run time, current interval
  builder-work/                  the builder's scratch tree and Hugo caches, disposable
    scratch/<run_id>/, cache/, resources/
  preview/                       built preview output, disposable
    <slug>/                      symlink to the live build under .builds/
    .builds/<run_id>/            one build's output, live once a symlink points at it
  site/                          clone of the blog repo main, disposable
```

`instance.key` is never included in a backup bundle: a bundle that carried
it would make `github-app.json`'s encryption pointless. See ADR 008 and
"Backup and restore" below.

Every write under `repo/` is one git commit authored by the acting token's
name, except the `ui` token's writes, which are committed under a fixed
author name the UI sets. `chronicle reindex` rebuilds
`index/chronicle.db` from the files; nothing is lost if it is deleted.

## Tokens

```bash
uv run chronicle --data-dir /data token issue ghostwriter   # prints the token once
uv run chronicle --data-dir /data token list                # names and use, never secrets
uv run chronicle --data-dir /data token revoke ghostwriter
```

Tokens are hashed at rest (ADR 007) and shown exactly once. The UI backend's
own token is minted on first start and written to `data/state/ui_token.txt`,
mode 0600, never logged. That file is the only plaintext token on disk;
treat it as a credential.

`approve`, `request_revision`, `reject`, `restore`, and `unpublish` are
reserved for the `ui` token (spec section 11, ADR 004); any other token gets
a 403 naming the action.

## The three images

One repository, one `Dockerfile`, three build targets, per spec section 14:

- **api**: the service, UI backend, admin, GitHub client, and reconciler.
  It ships `/v1`, plus `GET /healthz` (liveness) and `GET /readyz`
  (readiness: the data directory is writable, git is available, the derived
  index opens at the expected schema version, and the GitHub App is honestly
  reported "not configured" until it is bootstrapped).
- **builder**: Hugo extended (pinned by the `HUGO_VERSION` build arg,
  default `0.164.0`), plus git for the blog repo clone. Polls the run
  queue, builds `preview` runs, and writes a heartbeat.
- **preview**: a small Python static file server (ADR 010) over the
  preview volume, serving `/preview/<slug>/...`, no directory listing, a
  `GET /healthz`.

```bash
docker build --target api .
docker build --target builder .
docker build --target preview .
```

All three run non-root (uid 1000) and are intended to run with a read-only
root filesystem. `api` needs a writable volume at `CHRONICLE_DATA_DIR`;
`builder` needs the same plus a writable home directory (Hugo's own cache
lives under `data/builder-work/`, not the home directory, but `uv`'s
installed packages still expect one); `preview` needs a writable volume at
`CHRONICLE_PREVIEW_DIR`.

`docker-compose.yml` is for local development only. Reference deployment
manifests, closer to how a real instance runs, are in
[examples/k8s/](../examples/k8s/).

## Admin bootstrap

`CHRONICLE_EXTERNAL_URL` has to be set before the first claim if you plan to
connect a real GitHub App: it is the base URL GitHub redirects back to after
the manifest flow (`{CHRONICLE_EXTERNAL_URL}/admin/github/callback`), and it
cannot be guessed from an incoming request because the request that needs it
(building the manifest) is not the request GitHub redirects to (ADR 009).
It defaults to `http://localhost:8080`, which is fine for exercising
everything except the two steps that happen in your own browser against
real GitHub.

The click-by-click order, once for a fresh instance:

1. **Claim.** `GET /admin` shows the claim page until claimed. Read the
   one-time code from `data/state/claim-code` (the api logs only that a
   code exists and where), pick a password of at least 12 characters,
   submit. The code file is deleted the moment this succeeds.
2. **Log in.** `GET /admin/login`, the password from step 1. Sets a signed,
   `HttpOnly`, 12-hour session cookie.
3. **Connect GitHub** (in your browser, on github.com). `/admin/github/connect`
   renders a form carrying the App manifest; submitting it opens GitHub's own
   "create a GitHub App" page, pre-filled, targeting either your personal
   account or an organization. This is the first of the two steps that
   happen on GitHub, not Chronicle: your browser talks to GitHub directly,
   and GitHub redirects back to `/admin/github/callback` with a one-time
   code once the App exists.
4. **Install the App** (in your browser, on github.com). The callback page
   links to `{app html_url}/installations/new`; installing there and picking
   the blog repo is the second step that happens on GitHub. Come back to
   `/admin/github/install` afterward.
5. **Choose the installation.** `/admin/github/install` lists what the
   App's own credentials can already see and lets you pick one, or paste an
   installation id directly.
6. **Choose the repo.** `/admin/github/repo` lists the repositories that
   installation can reach; picking one makes a live call to verify contents
   and pull-request access and records the result.
7. **Digest.** The "Run digest now" button on `/admin` (or `chronicle
   digest`) clones or fetches the chosen repo's default branch into
   `data/site/` and writes post records. With no GitHub App configured yet,
   setting `CHRONICLE_DIGEST_REPO_URL` to a public https URL runs the same
   digest anonymously, which is how CI exercises it without credentials.
   `CHRONICLE_DIGEST_REPO_URL` also accepts a local filesystem path or a
   `file://` URL to a clone you already have on disk, which is the way to
   test digest against a private repo without ever putting a GitHub token
   in Chronicle's environment.

Only steps 3 and 4 happen on GitHub's own pages in your browser; every other
step is a Chronicle admin page. `/admin/tokens` issues and revokes named
consumer tokens at any point after claiming; `ui` and `editor` are reserved,
since `editor` is the identity the `ui` token's writes carry. `/admin`
itself is the status page: last digest, post count, toolchain drift, App and
repo connection state, submissions and drafts by status, disk use, and git
health.

## Browser sign-in (OIDC)

Off unless configured, and then all-or-nothing (ADR 027). With nothing set,
the content and preview pages need no login and show the "internal-only and
unauthenticated" banner, exactly as before. With the settings below set,
every content and preview page needs a session from your identity provider,
`/admin` accepts a session whose groups grant the `admin` role, and every
version, event, feedback entry and git commit the UI writes is authored by
the signed-in person instead of `editor`. The admin password (and the claim
code before it) keep working throughout as the break-glass path, and the
`/admin/login` page offers both. Nothing changes for `/v1` consumer tokens,
the `ui` token, or the GitHub App: a browser session authenticates nothing
under `/v1`.

The settings, all `CHRONICLE_OIDC_*`. Setting any of them without the three
required ones makes the api refuse to start, with the reason in its log.

| Variable | Required | Meaning |
| --- | --- | --- |
| `CHRONICLE_OIDC_ISSUER` | yes | The provider's issuer URL, exactly as its discovery document states it (trailing slash included). Discovery is read from `<issuer>/.well-known/openid-configuration`. |
| `CHRONICLE_OIDC_CLIENT_ID` | yes | The client id the provider registered for Chronicle. |
| `CHRONICLE_OIDC_GROUP_ROLES` | yes | Group-to-role mapping: `group=role` pairs separated by commas, or a JSON object (`{"Group, With Commas": "admin"}`). The roles are `admin` (opens `/admin` and the content UI) and `editor` (the content UI). Group names are matched exactly. A person in no mapped group is refused and gets no session. |
| `CHRONICLE_OIDC_CLIENT_SECRET_FILE` | no | Path of the file holding the client secret. Default `state/oidc-client-secret`, relative to the data directory; an absolute path must still resolve under it. The secret is never an environment value. The file is read on every sign-in, so rotating it needs no restart. |
| `CHRONICLE_OIDC_REDIRECT_URI` | no | The callback the provider sends the browser back to. Default `${CHRONICLE_EXTERNAL_URL}/auth/oidc/callback`; register the same value at the provider. |
| `CHRONICLE_OIDC_SCOPES` | no | Space or comma separated. Default `openid profile email`; `openid` is always included. |
| `CHRONICLE_OIDC_GROUPS_CLAIM` | no | The claim carrying the person's groups, read from the id token, else from userinfo. Default `groups`. |

Two anonymous routes exist for the flow and nothing else: `GET
/auth/oidc/start` (sends the browser to the provider) and `GET
/auth/oidc/callback` (where it comes back). Both answer 404 until OIDC is
configured. The session is a twelve-hour `HttpOnly` cookie sealed with
`state/oidc-session.key`; roles are whatever the person's groups map to at
the moment they sign in, re-read at every sign-in and never refreshed inside
a session. To end every session at once, delete `state/oidc-session.key` and
restart the api; everyone signs in again.

A person is identified by issuer plus subject. The name the records show is
the provider's `preferred_username` (else the email, else the subject); the
api logs `oidc: sign-in allowed: issuer=... subject=... as 'name' with roles
[...]` at every sign-in, so a name in `git log` is always traceable to the
account behind it. Keep consumer token names and people's usernames apart:
a token issued as `scott` and a person named `scott` author alike.

### Example: Authentik

Any standards-compliant provider works the same way; Authentik is the one
spelled out here.

1. In Authentik, create an **OAuth2/OpenID Provider**: client type
   *Confidential*, redirect URI `https://chronicle.example.internal/auth/oidc/callback`,
   signing key any RSA or EC key (not HS256), and the default scope mappings
   `openid`, `profile`, `email`. Authentik's `profile` scope already carries
   the `groups` claim with the person's group names. Note the client id and
   client secret, and the provider's issuer URL from its overview page
   (`https://auth.example.internal/application/o/chronicle/`, trailing slash
   included).
2. Create an **Application** bound to that provider, and bind the groups that
   should reach Chronicle (say `chronicle-admins` and `chronicle-editors`) so
   nobody outside them even reaches the consent screen.
3. Put the client secret in a file under the data directory, mode 0600. In
   Kubernetes, mount a Secret as that one file:

   ```yaml
   # In the api container of examples/k8s/deployment.yaml
   env:
     - name: CHRONICLE_OIDC_ISSUER
       value: https://auth.example.internal/application/o/chronicle/
     - name: CHRONICLE_OIDC_CLIENT_ID
       value: REPLACE_ME_CLIENT_ID
     - name: CHRONICLE_OIDC_GROUP_ROLES
       value: chronicle-admins=admin,chronicle-editors=editor
   volumeMounts:
     - name: oidc-client-secret
       mountPath: /data/state/oidc-client-secret
       subPath: client-secret
       readOnly: true
   volumes:
     - name: oidc-client-secret
       secret:
         secretName: chronicle-oidc-client-secret   # key: client-secret
         defaultMode: 0400
   ```

   With Compose, write the file into the data volume
   (`docker compose exec api sh -c 'umask 077; cat > /data/state/oidc-client-secret'`)
   and set the three variables on the `api` service.
4. Restart the api. The log says `oidc: client secret file ... does not
   exist` if the mount is wrong, and the api refuses to start outright on a
   mapping or path mistake. Open the instance: `/content/drafts` now sends
   you to Authentik and back; `/admin/login` shows "Sign in with your
   account" above the password form.

If the provider is unreachable, the sign-in start page says so and links to
`/admin/login`, where the password still works; nothing else in the api
depends on the provider being up. If a mapping change locks everyone out of
the content UI, fix `CHRONICLE_OIDC_GROUP_ROLES` and restart; to fall back
to the pre-sign-in behaviour entirely, unset every `CHRONICLE_OIDC_*`
variable and restart (the UI is then open to the network again, banner and
all, so do that only where ADR 014's assumptions still hold).

## Backup and restore

`chronicle backup create [--out path]` writes a checksummed, gzip tarball
(`chronicle-backup-<UTC stamp>.tar.gz`): `repo/` with its git history,
`images/`, and the encrypted portion of `state/` (`tokens.json`,
`admin.json`, `github-app.json`, `ui_disabled` if present). Never
`instance.key`, `preview/`, `site/`, `builder-work/`, `claim-code`, or
`ui_token.txt`. `manifest.json` at the root carries record counts and a
sha256 for every other member.

`chronicle backup restore <bundle> --yes` validates the manifest, refuses
an unknown `schema_version`, verifies every member's checksum (refusing on
any mismatch, missing, or extra member), then swaps `repo/`, `images/`, and
the state files into place, keeping the displaced tree until `chronicle
reindex` against the new one succeeds. Run it against a stopped instance
(api and builder both, ADR 016) since the builder holds its own long-lived
connection to the index that does not notice the swap; the one exception is
`/admin/backup`'s upload path, which restores from within the running
instance after you type `restore` to confirm, and then needs a manual
builder restart for the same reason. Either path needs a `chronicle digest`
run afterward before a preview or publish will work, since `site/` is not
part of the bundle.

See [docs/backup.md](backup.md) for the exact file layout and JSON schema,
precise enough to hand-build an import bundle without reading the code, and
ADR 016 for the restore swap's reasoning.
