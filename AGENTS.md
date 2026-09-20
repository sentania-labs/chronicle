# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

## The rules that outrank convenience

- **Filesystem first, git second, internal only.** The data directory is the
  truth; the internal git repository under `data/repo/` is history, never a
  second copy of record, and it is never pushed anywhere. It has no remote.
  Publishing to the public blog repo is a separate action through the GitHub
  App, against a different git history entirely. See
  [docs/decisions/001-filesystem-first-internal-git.md](docs/decisions/001-filesystem-first-internal-git.md).
- **No em-dashes anywhere.** Code, comments, docs, commit messages, PR
  bodies. `make prose-check` (`ci/prose-check.sh`) is the gate; it builds the
  character from its own bytes so the checker does not trip itself.
- **Every API path requires a consumer token.** `/healthz` and `/readyz` are
  the only unauthenticated routes, forever. There is no anonymous path into
  `/v1`, reads included, ever, for any consumer, the UI included. The UI
  backend authenticates with its own `ui` consumer token; browsers never
  hold one. As of round C1 this is enforced code, not just spec intent
  (sections 6 and 11): the whole `/v1` router carries the consumer-token
  dependency, so a new route cannot opt out of it by forgetting. See
  [docs/decisions/004-ui-no-login-consumer-token.md](docs/decisions/004-ui-no-login-consumer-token.md).
  FastAPI's own schema routes (`/docs`, `/redoc`, `/openapi.json`,
  `/docs/oauth2-redirect`) are disabled in `create_app` for the same reason;
  they come back in a later round only if placed behind the consumer-token
  layer, never anonymously.
- **Admin is authenticated always.** Admin's password session is independent
  of the API's consumer tokens and independent of whether the UI ever grows
  user login. Nothing shortcuts Admin's session for convenience. As of round
  C2, `/admin` and `/admin/api` carry their own session dependency
  (`chronicle/api/admin_deps.py`), mounted separately from `build_v1_router()`
  and never imported by a content route; a session cookie authenticates
  nothing under `/v1`, and a bearer token authenticates nothing under
  `/admin`.
- **Reconciliation produces flags only, never automatic correction.** A
  mismatch between main and Chronicle's records is surfaced on the admin
  status page for Scott to resolve; nothing in the reconciler deletes or
  overwrites a record on its own conclusion. Reconciliation does not exist
  yet as of round C1; this is spec intent (section 12) for C4. See
  [docs/decisions/005-reconciliation-flags-only.md](docs/decisions/005-reconciliation-flags-only.md).

The full contributor bar, including the review and release process, is in
[CONTRIBUTING.md](CONTRIBUTING.md). The design spec is in
[docs/spec/00-spec.md](docs/spec/00-spec.md); read it before assuming a
capability exists or is out of scope.

## Layout and commands

`chronicle/` is a single package with three console-script entry points:
`chronicle.api` (the FastAPI app: public contract, UI backend, admin, GitHub
client, reconciler), `chronicle.builder` (the Hugo-driven preview builder),
and `chronicle.preview` (the static preview server). One `pyproject.toml`
covers all three; there is no uv workspace to keep in sync, unlike
coppermind's multi-service layout. One `Dockerfile` at the repository root
builds all three as separate targets (`api`, `builder`, `preview`). A fourth
console script, `chronicle`, is the operator CLI (`reindex`, `token`); it
ships inside the api image rather than an image of its own.

`make` is the only entry point that matters, and CI calls the same targets:
see the [Makefile](Makefile) for the list. `make check` is what CI's `lint`
and `test` jobs run; `make build` builds all three Docker targets locally.

## Sharp edges in the api

- **Git identity is passed per commit, never read from config.** `gitrepo`
  sets `GIT_AUTHOR_*` and `GIT_COMMITTER_*` and pins `GIT_CONFIG_GLOBAL` and
  `GIT_CONFIG_SYSTEM` to `/dev/null` on every call, because the api container
  and CI have no global git config and must never inherit the host's. The
  `ui` token acts as `editor` in everything the domain records (commit
  author, version author, claim holder); that mapping lives in `Consumer`
  (`chronicle/api/deps.py`), so the store receives the acting name already
  resolved and does no translation of its own. Records written before this
  name changed still say `scott` and are not rewritten; see ADR 014's
  amendment.
- **A save can change a draft's status.** `PUT /v1/drafts/{id}` on a draft in
  `revision_requested` or `published` moves it to `drafting`. That is not one
  of the API's named actions, so it is modelled as the `revise` action in
  `chronicle/api/transitions.py` and writes an event like any other
  transition. Never decide a status anywhere else: that table is the whole
  lifecycle.
- **The index is a cache; single-record reads must not use it.** Lists and
  cursors come from SQLite, a record's own content always comes from its
  file, and `chronicle reindex` rebuilds every row. See
  [docs/decisions/006-sqlite-derived-index.md](docs/decisions/006-sqlite-derived-index.md).
- **`Store.create_draft` returns `(Draft, warnings)`, not a bare `Draft`.**
  A `from_post` import can drop frontmatter keys the allowlist does not have
  (an existing post on main may predate Chronicle) and can fail to import an
  individual image; both are reported as warnings in the response, never a
  422, because the import itself still succeeded. `from_submission` reports
  the same way (a dropped frontmatter key, unparseable frontmatter, an image
  id not in the store, a filename collision); a blank draft, or a submission
  with no materials, gets an empty list.
- **`from_submission` seeds the draft; the other materials never touch the
  body.** `_seed_draft_from_submission` takes the first material that looks
  like a post (frontmatter block or leading heading) as the body, else the
  first with any text; every other material becomes a `material` feedback
  entry authored by the creating consumer. It runs inside the create lock,
  so it uses `_append_feedback` and `get_image`, never a `@locked` method.
  See [docs/decisions/018-mutable-submissions-and-seeded-drafts.md](docs/decisions/018-mutable-submissions-and-seeded-drafts.md).
- **A submission is version 1 until revised, and only `new` or `claimed`
  ones can be.** `Submission.version_no` defaults to 1 so records written
  before versioning load unchanged; `Store.revise_submission` writes
  `submissions/<id>/versions/<n>.json` (backfilling version 1 for a legacy
  record in the same commit) and a `submission.revise` event. The frozen
  states are decided in `transitions.resolve_submission_revise`, not in the
  route. The index has no version column on purpose (ADR 006).
- **`_put_image_unlocked` exists so `from_post` import can reuse image
  ingestion from inside an already-`@locked` method.** `Store`'s lock
  (`threading.Lock`) is not reentrant; calling the public, `@locked`
  `put_image` from within `create_draft` would deadlock. Any future store
  method that needs to call another mutating method internally needs the
  same split, not a nested lock.
- **The GitHub client takes an httpx transport, never the network, in a
  test.** `GitHubClient.__init__`'s `transport` parameter exists solely so
  `httpx.MockTransport` can stand in; every test of the GitHub path (spec's
  requirement) sets it, and production code never does. `AdminServices` also
  accepts a `github_client` override at `.build()` for the same reason.
- **A run nobody claims fails itself, and the gate covers both run kinds.**
  `Store.active_publish_run` counts `publish` and `unpublish` runs, so a save,
  attach or detach is refused while either is queued or building.
  `publisher.expire_unclaimed_runs` (called by `run_loop` after `tick`, never
  from inside it, because `tick` returns early when no target is configured)
  fails a run still `queued` past `CHRONICLE_PUBLISH_QUEUE_TIMEOUT_SECONDS`
  through `finish_run`, which is what unfreezes the draft. See
  [docs/decisions/020-queued-publish-runs-time-out.md](docs/decisions/020-queued-publish-runs-time-out.md).
- **Digest never deletes a post record.** A post on main that a later digest
  no longer finds is left alone; deciding it was actually removed is
  reconciliation's job (ADR 005, arriving C4), not digest's. Digest only
  ever creates or updates.
- **`digest.py` widens `GIT_ALLOW_PROTOCOL` to include `file`.** The repo URL
  digest clones is always Chronicle's own configured source (never attacker
  input), so this costs nothing in practice and is what lets a theme
  submodule pointed at a local path clone in tests the same way a real one
  clones over https in production.
- **`digest.py`'s `GIT_CONFIG_GLOBAL` points at a real config file, not
  `/dev/null`.** Git's dubious-ownership check ignores `GIT_CONFIG_COUNT`
  environment injection for `safe.directory` on purpose (letting an env var
  waive that check would defeat it), so the per-invocation trick
  `_auth_env` uses for an installation token cannot grant this exception;
  only a real config file can. Found live in the C3 real-blog check, where
  `CHRONICLE_DIGEST_REPO_URL` named a clone owned by a different uid than
  the container's.
- **A run's queue entry, lease, and status are three different things, and
  only two of them are guaranteed to agree.** The queue entry
  (`repo/runs/queue/<run_id>.json`) is a request; the lease
  (`state/builder/leases/<run_id>.json`, ADR 011) is a claim; the run
  record's `status` is the durable truth. A builder that crashes can leave
  a run `building` with its lease already gone (its own `tick`'s `finally`
  releases the lease on any exit, including an uncaught exception), not
  just with an expired one, so `runner.recover_expired_leases` checks both:
  every expired lease, and every `building` run with no live lease at all.
- **The pre-swap Hugo output lives under `data/preview/.builds/<run_id>/`,
  never under `data/builder-work/`.** `_atomic_swap`'s rename into
  `data/preview/<slug>/` only stays atomic when the source and destination
  share a filesystem, and `builder-work` is a deliberately separate mount
  from `preview` in both `docker-compose.yml` and `examples/k8s/` (ADR
  011's security-separation argument). Found live the same way: an
  `os.replace` across that mount boundary raises `OSError`, not a silent
  copy.
- **`data/preview/<slug>` is a symlink into `.builds/<run_id>/`, never a
  directory Hugo writes into directly.** `_atomic_swap` replaces it with a
  single rename of a temp symlink, not a remove-then-rename pair, so a
  request resolving `<slug>/` never sees it missing (ADR 011). The old
  build directory a replaced symlink pointed at is removed right after the
  swap; anything that writes under a mutable path in the scratch copy of
  `data/site` (a converted post, an attached image) must `unlink` it first
  because `_copy_site` hard-links wherever it can, and writing in place
  would truncate the same inode `data/site` uses (found live: this is
  exactly what silently corrupted the digest's clone before the round C3
  review caught it).
- **`store.remove_post` is not digest with a delete bolted on.** Digest
  never removes a `Post` record (spec's own rule, enforced in
  `apply_digest`); `remove_post` exists solely for the one caller that
  isn't heuristic, the watcher's confirmed `unpublish` merge, where
  Chronicle caused the removal itself and knows so with certainty.
  Reconciliation's `post_removed_without_unpublish` flag is the heuristic
  version of the same fact for everything else, and it never deletes
  anything; the two must never call each other.
- **A `github`-authored write never implies a status change.**
  `Store.record_github_version` (content drift) and the feedback entry
  `observe_pr_outcome` writes on a closed-without-merge PR both attribute
  to `"github"` regardless of which process actor (the watcher, the
  reconcile loop) observed the fact, and neither touches `draft.status`
  outside what `transitions.WATCH_TRANSITIONS` already decides. Confusing
  the acting process with the record's author would misattribute a change
  Scott made directly on GitHub to a robot.
- **`GitHubRepoOps.get_commit` is not on the spec's literal call list.**
  Resolving `create_tree`'s `base_tree` needs the base commit's tree sha,
  and `get_ref` alone returns only the commit sha; `get_commit` is the one
  call every publish and reconcile run makes to bridge that gap (ADR 012).
- **The UI backend calls `Store` in-process, never over loopback HTTP.**
  `chronicle/api/ui_deps.py:require_ui_consumer` reads
  `data/state/ui_token.txt` fresh on every request and authenticates it
  through the same `TokenStore.authenticate` a bearer header would use, so
  a revoke on `/admin/tokens` disables the whole UI on its very next
  request (ADR 014). A UI route that calls `services.store` without going
  through this dependency first is calling a mutating method with no actor
  at all, which is a type error, not a silent anonymous write, but a
  reviewer should still treat a route that skips it as a blocking finding
  the way `/v1` treats a route that skips `require_consumer`.
- **`ui_templates.py`'s action buttons read `transitions.DRAFT_TRANSITIONS`
  directly, never a second table.** The board and the editor page both
  render exactly the actions a draft's current status allows by iterating
  the same dict `Store.act_on_draft` consults; adding a transition there is
  what makes it show up as a button, nothing in the UI layer needs updating
  to match.
- **The editor's live render is client-side only, filled by `editor.js` from
  EasyMDE's own text, never by anything the server renders.** Every value a
  UI template does interpolate goes through `html.escape` first, same bar as
  `admin_templates.py`. A round C5 review found that a draft's body is not
  trusted input even here: a different consumer token can `PUT` a body
  containing an event-handler payload, and Scott's own browser is what later
  renders it when he opens the editor. `editor.js`'s `previewRender` runs
  `marked.parse`'s output through `ui.js`'s `sanitize` (strips script-bearing
  tags, `on*` attributes, and `javascript:` URLs) before EasyMDE assigns it;
  do not remove that step, and do not add a second `innerHTML` assignment of
  parsed markdown anywhere without the same treatment. Both `ui.js` and
  `editor.js` keep their pure helpers outside a `document` guard so
  `tests/*.test.mjs` can `require` them (run by `tests/test_js.py`). EasyMDE
  runs with `autoDownloadFontAwesome` and `spellChecker` off (its bundle names
  CDN URLs, and CSP would block them); toolbar glyphs are text in
  `style.css`, so a new toolbar button needs a glyph rule there.
- **The editor's Preview and Publish offers are staged, and the table is
  still the only decider.** `ui_actions.offers_for` renders an offer available
  only if `transitions.plan_action` finds a path, and `Store.act_on_draft_staged`
  (editor buttons only, never `/v1`) runs that path's steps under one lock:
  Preview on a `published` post revises it first, Publish on a `previewed` one
  submits it first. The lifecycle stays in the table, but the action route also
  holds a click to the offer the page rendered for it
  (`ui_actions.staged_refusal`, same `_offer_state` inputs): a disabled offer,
  or an absent one for a staged click, is a 409, so "Preview first" and an open
  unpublish PR are enforced where the page states them, not only on the button.
  That check is the `guard` `act_on_draft_staged` runs under the store lock,
  never a check before the call: a save landing in between would otherwise be
  published unpreviewed. A preview counts only
  while `built_version` equals the draft's version, so a save makes it stale
  again. The draft status shown to a human goes through
  `ui_status.status_label`; API values and filter query values stay raw.
- **The image upload route answers JSON to `Accept: application/json`.**
  `editor.js` gets the stored filename (a dedup can keep an earlier upload's
  name), the markdown to insert, and a per-draft image URL for the live render.
  The UI names uploads with `images.safe_upload_filename` (a stem with nothing
  ASCII left keeps its extension and takes a short content hash), so the
  reference the editor inserts is one `convert.py` resolves; the `/v1` route
  still stores the name it is given. `Store.put_and_attach_image` does the
  upload and the attach as one step, removes what it created if the attach is
  refused, and refuses an inline reference to an already-stored image whose name
  is not `images.is_plain_filename` (a dedup can land on a `/v1` or imported
  name with spaces, which `convert.py`'s reference match cannot carry).
- **`ui_deps.check_same_origin` treats an `Origin` header that does not
  resolve to this host as cross-origin, including the literal string
  `"null"` a sandboxed iframe sends.** A round C5 review found that
  `urlsplit("null").netloc` is empty, which used to fall through the same
  branch as "no header sent at all" and let a cross-origin sandboxed iframe
  submit every state-changing UI form. Only the genuine absence of both
  `Origin` and `Referer` is allowed through; a present-but-unparsable value
  never is.
- **The image directory is pinned once and stored, never recomputed from
  `draft.slug`.** `Draft.image_dir` (ADR 015) is the last non-empty path
  segment of `frontmatter["url"]` when set, the pinned slug otherwise; it
  is set at `_fill_from_post` (import) or `_pin_slug` (a new draft's first
  preview or approve) and read as-is by `convert.convert`. A real post's
  digest slug (`digest.slug_for`) can carry a dated filename stem
  (`2026-08-01-my-post`) that is never what `static/images/` on main is
  named; `_import_post_images`'s fallback filesystem sweep looks in
  `static/images/<image_dir>/`, not `static/images/<slug>/`, for exactly
  this reason (found live in the C6 three-post diff: the wrong directory
  silently dropped every unreferenced image on a dated-filename post).
- **`chronicle/backup.py`'s restore swap keeps the displaced tree until
  reindex succeeds, and `_safe_member` runs before any extraction.** A tar
  member with an absolute path, a `..` segment, or a symlink is refused
  before `tarfile.extractall` ever touches disk; the manifest's checksums
  are then verified against the extracted staging tree before anything
  under the data directory is renamed. See ADR 016.
- **The editor's action buttons and the 409 conflict view both know about
  more than `transitions.DRAFT_TRANSITIONS` alone.** `Store.act_on_draft`
  separately refuses a re-approve while a publish PR is already open
  (409 `publish_pr_open`), which the transition table itself cannot see;
  `_action_buttons` takes a `publish_pr_open` flag so that button does not
  render only to 409 on click, and `draft_save`'s 409 handler checks
  `exc.code == "stale_base_version"` before treating a 409 as the
  conflict-view case, because `publish_pr_open` is also a 409 with no
  meaningful diff to show. A round C5 review found both gaps live.
- **Digest lands a published working record directly, and "Drafts" reads
  "Posts" in the UI.** As of ADR 017, `Store.apply_digest`'s
  `_create_published_drafts_from_digest` reuses `_fill_from_post` to create
  a `Draft` at `published`, with a `published` dict populated from what
  digest itself observed, for any post on main no existing draft already
  tracks by slug or by file path; a record already tracking that slug or
  path (drafting, published, anything) is left alone, which is what makes
  a second digest of the same post, and the `post_on_main_without_published_draft`
  reconcile flag, both stay quiet. This method must never call a `@locked`
  `Store` method (`list_drafts`, `create_draft`, `resolve_flag`, and so on):
  it always runs from inside `apply_digest`'s own lock, and the store's
  lock is not reentrant, so a `list_drafts()` call here deadlocks the
  process rather than raising (found live running this round's own test
  suite). The "Drafts" to "Posts" rename this ADR also makes is UI copy
  only (`ui_templates.py`, `admin_templates.py`); `data/repo/drafts/`, the
  `Draft` model, and every route path are unchanged.
- **`convert.convert`'s `static_dir` argument is read from the last
  digest, never from a fresh `hugo config` call.** ADR 017's `staticdir`
  wiring added an optional second argument (site-relative, `static` when
  omitted); `publisher.py`, `builder/runner.py`, and `cli.py`'s
  `convert-dry-run` each call `digest.read_static_dir_from_state(store.
  data_dir)` right before converting, which reads `data/state/toolchain.
  json`'s `conventions.staticdir` (falling back the same way digest itself
  does if no digest has run yet or the file does not parse) rather than
  invoking Hugo again for a value the last digest already derived. A brand
  new post's directory is read the same way (`digest.read_new_post_dir_from_state`,
  `new_post_dir`): the section the last digest saw most posts in, never built
  from `contentdir` (a root) or `mainsections` (page types), and exactly
  `content/posts` when nothing was read (ADR 017, issue 21 amendment).

## Round C4 status

Publish, unpublish, merge watch, and reconciliation are all real. A
publisher thread and a watcher thread run inside the api process alongside
the request handlers (ADR 013, `chronicle/api/background.py`); neither
invokes Hugo at all, so neither needs the builder's Hugo toolchain to run
a build. Digest, which the reconciler calls, is a different story: as of
ADR 017 it runs `hugo config` (never a build) against the digested working
tree to derive Chronicle's content, image, and taxonomy conventions, so the
api image now installs the same pinned Hugo the builder stage does. The
builder remains the only place a Hugo *build* runs.

- **Publish/unpublish** (`chronicle/api/publisher.py`): claims `publish`
  and `unpublish` queue entries the builder's own claim never looks at,
  builds one commit through the git data API (blob, tree, commit, ref),
  and opens or updates a PR. `GitHubRepoOps` (`chronicle/api/
  github_client.py`) is the narrow interface this and the watcher use;
  `AppRepoOps` is production, `TestRepoOps` is test-only (ADR 012). What a
  successful run wrote lands on `Draft.published` (branch, PR number and
  URL, commit sha, post path, url, date, the image list, and the post
  file's own blob sha), which unpublish and reconciliation's
  `content_drift` check both read back rather than recomputing.
- **Merge watch** (`chronicle/api/watcher.py`): polls `data/repo/watch/`
  (one file per draft with an open Chronicle PR, so a restart resumes),
  backing off from `CHRONICLE_WATCH_POLL_SECONDS` toward
  `CHRONICLE_WATCH_POLL_MAX_SECONDS` while nothing is open. A merge deletes
  the branch, refreshes `data/site/` and post records (`digest_runner.
  refresh_from_target`), and moves the draft to `published` or
  `unpublished` through `transitions.WATCH_TRANSITIONS`; a close without a
  merge moves it to `in_review` with a `github`-authored feedback entry.
- **Reconciliation** (`chronicle/api/reconcile.py`): runs at startup,
  hourly, and right after an observed merge. Computes the five flags in
  spec section 12 and persists them under `data/repo/reconcile/`, never
  correcting anything on its own conclusion (ADR 005); `content_drift` is
  the one exception that writes without an admin's say-so, and it only
  ever adds a `github`-authored version, never changes a status. The admin
  status page lists open flags with the resolutions that apply to each
  type (`reconcile.APPLICABLE_RESOLUTIONS`).
- **Test-token mode** (ADR 012): `CHRONICLE_GITHUB_TEST_TOKEN` plus
  `CHRONICLE_ALLOW_TEST_TOKEN=1` runs the whole publish/watch/reconcile
  path against a real repository named by `CHRONICLE_GITHUB_TEST_REPO`
  with a bearer token instead of a GitHub App installation. Test only:
  never mentioned in `examples/k8s/`. The standing target for this and
  future live checks is the private throwaway repo
  `sentania-labs/chronicle-target` (seeded by Adolin, two fixture posts,
  Hugo 0.164.0, no theme), not the real blog.

The preview build (C3) is unchanged: `chronicle/builder/main.py` still
polls the queue for `preview` runs only.

## Round C6 status

Image directory rule (ADR 015), backup and restore (ADR 016, `chronicle
backup create|restore`, `/admin/backup`), a release pipeline gated to `v*`
tags (SBOM, cosign keyless sign and verify, immutable ghcr.io tags),
`compose-smoke` wired into CI on pull requests (it needed no GitHub App or
network access; the C3/C4 note below was stale), a Content-Security-Policy
header on every response, and pagination on the three UI listing pages.
See `docs/pr-bodies/c6.md` for the live-check evidence and the adversarial
review's findings and disposition.

### Not done, noticed

- This round assumes a single api replica (ADR 013): the publisher has no
  lease the way the builder does, because nothing today runs more than one
  api process against the same data directory. A second replica needs that
  discipline added, not a redesign.
- Reconciliation's `slug_drift` and `content_drift` flags offer only
  `ignore` as a resolution; nothing here rewrites a draft's pinned slug or
  reconverts it to match what landed on main by hand. That is a deliberate
  scope cut (spec section 12 does not ask for an automated fix, only a
  flag), not an oversight.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
