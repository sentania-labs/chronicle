# Chronicle: blog publishing service specification, revision 2

**Date:** 2026-09-16
**Author:** Dalinar, from a design conversation with Scott
**Status:** revision 2, Scott's decisions of 2026-09-16 applied (name, UI auth, image ceiling, reconciliation policy).
**Home:** this repository, at `docs/spec/00-spec.md`.
**Provenance:** copied verbatim (this header excepted) from the vault report `2026-09-16-blog-publishing-service-spec.md` on 2026-09-16.

## 1. Purpose

Chronicle carries a piece of writing from an author to the public blog. It
owns everything between the author and the site: raw material intake, drafts,
revisions, preview, publish, unpublish, and the credentials those need. It is
a domain service in the same family as coppermind: filesystem-first, small
API, small UI, nothing you could not walk away from.

It is not an agent. It holds no judgment. Every decision that matters is made
by an author (a person or ghostwriter) or by Scott at a gate the service
exposes.

## 2. Boundaries

**In scope**

- Submissions: raw material bundles from any authorized caller.
- Drafts: markdown with frontmatter, images, versions, per-version author.
- Review actions: revise, request revision, reject, restore.
- Preview: a faithful local Hugo build, served by the service.
- Publish and unpublish: branch plus pull request against the blog repo
  through a GitHub App. Merge on GitHub remains the human gate.
- Reconciliation: main is the source of truth for published posts and for
  the build toolchain.
- Admin: repo target, GitHub App installation, consumer API tokens, status.
- Backup and restore of all service state.

**Out of scope**

- Writing. Ghostwriter and people do that.
- Knowledge retrieval for authors. Ghostwriter reads other posts and the
  submission package; that is a retrieval function on its side.
- The public site's own build and hosting. GitHub Pages does that on public
  runners with no lab secrets.
- Deployment manifests for a specific cluster. The service ships images and
  reference manifests only. lab-deployment owns the private instance.
- Social posts, newsletters, cross-posting.

## 3. Ground truth this replaces

- The blog repo `sentania/sentania.github.io` builds with Hugo extended
  0.164.0 and two theme submodules (blowfish, hugo-clarity) via `hugo.yml`.
- 2026-09-16 (found by Adolin during the C2 live check): that blog repo is
  private, not public.
- `blog-dispatch.yml` in that repo does preview, publish, and unpublish on
  dispatch. It is retired by this service.
- The dashboard's blog services (materialize, dispatch, draft store under
  the vault's content-drafts directory) are retired by this service.
- The vault's `pka-blog-publisher` timer and work clone are retired.
- The `blog.int` preview host is retired once the service's preview is live.

## 4. Domain model

All records are files under the data directory. Git tracks everything except
the image store. An index is derived from the files and rebuildable.

**Submission**

- `id`, `created_at`, `from` (consumer token name), `brief` (one paragraph),
  `materials/` (notes as markdown or text, links, attached image ids),
  `status`: `new`, `claimed`, `drafted`, `discarded`.
- `claimed_by` and `draft_id` once a draft exists.

**Draft**

- `id` (stable, never reused), `slug` (pinned with collision check at first
  preview or publish, then immutable), `title`, frontmatter (Hugo allowlist,
  same field order the dashboard port uses today), `body`.
- `status`: `drafting`, `in_review`, `revision_requested`, `previewed`,
  `approved`, `published`, `unpublished`, `rejected`.
- `source_submission` (optional), `source_post` (set when imported from main).
- `images[]`: manifest of `{image_id, filename, role}` with role `inline`
  or `feature`.
- No claim field: who has a draft open is the editor lock's runtime state
  (ADR 025), never stored on the draft.

**Version**

- One per save. `{draft_id, version_no, author, created_at, base_version,
  message}` plus the content at that version, committed to git.
- `author` is the consumer token name or `editor` for the UI. This is the
  calibration signal: ghostwriter reads the diff between its version and
  the editor's next one. Records written before this name changed still
  say `scott` and are not rewritten; see ADR 014's amendment.

**Feedback**

- Free text attached to a draft by a reviewer with `request_revision` or
  `reject`. Stored beside the version it was written against. Appended to a
  changelog file per draft that git tracks.

**Post**

- One per published post on main: `{slug, path, title, date, sha}`.
  Derived from main by digest and reconciliation. Never edited directly.

**Image**

- Content-addressed by sha256. `{image_id, sha256, filename, bytes, mime}`.
  Stored once, referenced by any number of drafts.

**Run**

- One per preview build or publish action: `{id, draft_id, kind, state,
  started_at, finished_at, log_path, result}` where result carries the
  preview URL, branch, PR URL, or error.

## 5. Lifecycle

```
submission: new -> claimed -> drafted
                          -> discarded
            (new and claimed can also be revised in place, no status change)

draft:      drafting -> in_review -> revision_requested -> drafting
                                  -> rejected -> (restore) -> drafting
                                  -> previewed -> in_review
                                  -> approved -> published
            published -> (revise) -> drafting ... -> published (republish)
            published -> unpublished (PR merged) -> (restore) -> drafting
```

Rules:

- Any author may move a draft to `in_review`. Only Scott (UI) may
  `approve`, `reject`, `request_revision`, or `unpublish`.
- `approved` opens the PR. `published` is set only when the service observes
  the merge on GitHub and re-pulls main.
- `unpublish` opens a PR that deletes the post and its images. `unpublished`
  is set on observed merge.
- Republish reuses the original publish date. First publish stamps today in
  America/Chicago.
- Every transition writes an event. Events are the wake source for a future
  supervisor and the audit trail for people.

## 6. API

Versioned under `/v1`. JSON. Bearer token per consumer, issued in admin.
The UI uses the same API through a session (see section 11).

**Submissions**

- `POST /v1/submissions` create with brief and materials. Images attach by
  `image_id` from a prior upload, or inline as multipart.
- `GET /v1/submissions?status=new`
- `POST /v1/submissions/{id}/claim` sets `claimed` and `claimed_by`.
- `POST /v1/submissions/{id}/discard`
- `PUT /v1/submissions/{id}` revise brief, materials and image ids while the
  submission is `new` or `claimed`; requires `base_version`, 409 with a diff
  if stale, and a `submission_frozen` 409 once it is `drafted` or
  `discarded` (ADR 018).

**Drafts**

- `POST /v1/drafts` create, optionally `from_submission` or `from_post`
  (imports a published post from main into a draft).
- `GET /v1/drafts?status=...`
- `GET /v1/drafts/{id}` returns content, current version number, images,
  status, and `editing` (`{holder, since, expires_at}` while the draft is
  open in the editor, else null).
- `PUT /v1/drafts/{id}` requires `base_version`. Returns 409 with the
  current version and a diff summary if `base_version` is stale. Never
  overwrites silently. Accepts an optional `message`.
- While a draft is open in the editor (a lease renewed every 30 seconds,
  lapsing 2 minutes after the last renewal), a `PUT` by any other identity
  returns 423 `draft_being_edited` with the holder, since, and expiry (ADR
  025). The advisory `claim`/`release` endpoints are gone.
- `GET /v1/drafts/{id}/versions` and `GET /v1/drafts/{id}/versions/{n}`
- `GET /v1/drafts/{id}/changes?since={n}` returns the diffs and feedback
  entries after version `n`, by author. This is ghostwriter's first call
  every session. Reference material seeded from a submission (feedback
  entries with `action` `material`) is included at every `n`, not only
  `n=0`: it is not review of any version, and a resuming writer still needs it.
- `POST /v1/drafts/{id}/actions/{action}` where action is one of `submit`
  (to in_review), `preview`, `approve`, `request_revision` (with feedback),
  `reject` (with feedback), `restore`, `unpublish`. Returns a run id where a
  build or GitHub operation results.
- `GET /v1/drafts/{id}/status` returns status, last run, preview URL,
  branch, PR URL.

**Images**

- `POST /v1/images` multipart upload. Returns `image_id`. Enforces size
  ceiling (default 5 MB), allowed types (png, jpg, webp, gif), and strips
  metadata. Duplicate content returns the existing id.
- `PUT /v1/drafts/{id}/images/{image_id}` attaches with a role.
- `DELETE /v1/drafts/{id}/images/{image_id}` detaches.

**Posts and runs**

- `GET /v1/posts` and `GET /v1/posts/{slug}` from the digest of main.
- `GET /v1/runs/{id}` and `GET /v1/runs/{id}/log`.

**Events**

- `GET /v1/events?since={cursor}` cursor-based, for supervisors and the
  daily briefing.

**Health**

- `GET /healthz` liveness, `GET /readyz` readiness (data dir writable, git
  ok, GitHub App reachable or explicitly not configured).

## 7. UI

Served at the root of the instance's hostname. Three tabs.

- **Content**: submissions queue, drafts by status, the editor (markdown
  with frontmatter fields, image attach, save with conflict handling, the
  editor lock), and the action buttons Scott needs: request revision with
  feedback, reject, approve, unpublish, restore. There is no import tab: digest
  already records every post on main, so a manual import would front
  something that has already happened. Importing a published post into a
  draft (`from_post`) remains a capability of the API and of reconciliation's
  `import_as_draft` resolution, with no UI of its own.
- **Preview**: the static output of the latest preview build per draft,
  served under a preview path by slug, plus the run log for a failed build.
- **Admin**: section 10.

No user login on the content and preview tabs in this revision, by Scott's
decision: the instance is internal-only and the final gate is a merge on
GitHub. Auth is still wired, silently: the UI backend calls the API with its
own internal consumer token (issued at bootstrap, name `ui`), so the API
never has an anonymous path and adding a login later touches only the UI.
Admin is always authenticated.

## 8. Preview builder

- A separate container in the same pod, sharing the data volume and a build
  volume with the api and preview containers.
- Holds a clone of the blog repo at main with submodules, refreshed by
  reconciliation (section 12).
- On a preview run: copy the base site to a scratch tree, write the draft
  as a post using the same conversion as publish, copy referenced images
  into the post's static path, run Hugo with `--baseURL` set to the preview
  path for that slug, write output to the preview volume under the slug,
  record the run.
- Hugo version is read from the blog repo's Pages workflow at reconcile time
  and must match the builder image's installed version. Mismatch is
  surfaced on admin status and marks previews as "toolchain drift"; it does
  not block building.
- Full rebuild per preview. Current site size builds in seconds.

## 9. Publish through the GitHub App

- One App, created through GitHub's manifest flow from the admin bootstrap
  page, so the private key is delivered to the service and never copied by
  hand. Installed on the blog repo only.
- Permissions: contents read and write, pull requests read and write,
  metadata read, actions read (to watch Pages runs). Nothing else.
- Publish: create or reset branch `post/<slug>` from main, write the post
  file and referenced images through the Git data API, open a PR with a
  body built from the draft and run evidence, record the PR URL.
- Unpublish: same shape with deletions.
- Merge watch: poll the PR at a modest interval (default 60 s while a PR is
  open). On merge: delete the branch, re-pull main, rebuild the post record
  from what landed, set the draft's status. The service has no public
  endpoint, so webhooks are not used.
- The lint and normalize pass the vault publisher had runs at publish time,
  not at save time.
- Commits and PRs are authored by the App's bot identity. Scott merges.

## 10. Admin and bootstrap

First run presents a claim page (coppermind pattern): read the one-time
claim code from a restricted bootstrap file, set a password of at least 12
characters. The session lives in a signed cookie. Re-claiming rotates the
signing secret and ends every session.

Bootstrap steps, each resumable:

1. **Connect GitHub**: start the App manifest flow, receive credentials,
   store them encrypted at rest in the state directory.
2. **Choose repo**: list repos the installation can see, pick one, verify
   contents and pull request access with a live call.
3. **Digest**: clone main with submodules, parse `content/posts`, create
   post records, note the Hugo version and theme commits, build the
   preview base.
4. **Import** (optional): restore a backup bundle (section 13). This is how
   the current drafts, published and rejected sets, and Wit's feedback
   files come in, prepared by hand once.

Ongoing admin pages:

- **Repo and App**: current target, installation id, last successful call,
  re-run manifest flow, rotate key.
- **Tokens**: issue a named consumer token (ghostwriter, dashboard,
  lab-admin, briefing), show last use, revoke. Tokens are shown once.
- **Status**: last reconcile, toolchain drift, open PRs, stuck runs,
  submissions waiting, disk use, git health.
- **Backup**: create a bundle now, download it, restore from an upload.

## 11. Auth

- Consumers: bearer tokens, hashed at rest, named, revocable. Every API
  endpoint requires one, reads included. The UI backend is a consumer with
  its own `ui` token; browsers never hold a token. Actions the lifecycle
  reserves for Scott (approve, reject, request revision, unpublish) are
  allowed only to the `ui` token in this revision, which is what "Scott via
  the UI" means until user login exists.
- Admin: password session cookie. Admin routes and the tokens store are
  mounted so the api container's request handlers for content cannot read
  App credentials directly; a narrow internal interface signs GitHub calls.
- No credential is ever written to a draft, a PR, a log, or a bundle in
  plaintext. Bundles contain the encrypted credential store and are useless
  without the instance key, which is not in the bundle.

## 12. Reconciliation

Runs at startup and on a schedule (default hourly), and on every observed
merge.

- Fetch main. If the Hugo version in the Pages workflow or a theme submodule
  commit changed, rebuild the preview base and record toolchain drift if
  the builder image does not match.
- Diff `content/posts` against post records and draft statuses. Every
  mismatch is a flag, never an automatic correction or deletion: a draft
  marked published whose post is missing on main, a post on main with no
  draft marked published, a post removed on main without an unpublish run,
  a slug on main that differs from the draft's pinned slug. Flags appear on
  the admin status page with a one-click resolution (mark as published,
  mark as unpublished, import as draft, ignore) that Scott chooses.
- Verify every published draft's `source_post` still matches main; if main
  moved (Scott edited on GitHub), record a new version authored `github`.
- Report on the admin status page and as events.

## 13. Data, git, backup, restore

- Data directory layout:

```
data/
  repo/            git repo: submissions/, drafts/, versions/, feedback/,
                   posts/, index/  (everything but images)
  images/          content-addressed store, not in git
  preview/         built preview output, disposable
  site/            clone of the blog repo main, disposable
  state/           instance key, encrypted credentials, admin record,
                   bootstrap files (restricted permissions)
```

- Git is internal history only, never pushed. Each save is one commit with
  the author in the commit metadata and the feedback log appended.
- Backup bundle: a tarball of `repo/` (with `.git`), `images/`, and the
  encrypted portion of `state/`. Not `preview/`, not `site/`, not the
  instance key.
- Restore: upload a bundle, validate, replace. Restore of a hand-built
  bundle is also the import path for existing drafts and feedback.
- On total loss without a bundle: published posts are recovered by digest
  from main. Unpublished drafts, submissions, and history are gone. Scott
  accepted this; the bundle is the mitigation.

## 14. Images and containers

Three images from one repo, one Dockerfile stage each. All non-root, read-only
root filesystem, config by environment, no shell in the final stage where
practical, health endpoints, signed images, SBOM, release from a version tag
the way coppermind's CI does it.

- `api`: the service, UI, admin, GitHub client, reconciler, git operations.
- `builder`: Hugo extended pinned to the version the blog builds with, git,
  the build loop. Watches the run queue in the data directory. Rebuilding
  this image is how a Hugo version bump is followed.
- `preview`: static file server over the preview volume, path-prefixed by
  slug, no directory listing.

Volumes: `data` (persistent), `preview` and `site` (may be persistent for
speed but disposable), `state` (persistent, restricted).

Reference manifests in `examples/k8s/`: one Deployment with the three
containers, one PVC for data, one for preview and site, a Secret for the
instance key, an internal Ingress with the root path to api and the preview
path to preview. Clearly marked as a reference, not the lab's deployment.

## 15. Consumers at day 0

- **Ghostwriter**: token. Lists submissions, claims one, creates a draft,
  saves versions with `base_version`, reads `/changes` at the start of each
  session, moves drafts to `in_review`, uploads feature art.
- **Scott**: UI. Reviews, edits, requests revisions, approves, rejects,
  unpublishes.
- **lab-admin** (and any other agent): token. Posts a submission with
  notes and screenshots. Nothing else.
- **Dashboard**: token, read-only. Shows counts and links into the service.
- **Daily briefing**: token, read-only. Reads events and drafts awaiting
  review.

Later, a supervisor (Crucible) consumes `/v1/events` and wakes ghostwriter.
No change to the service is needed for that.

## 16. Migration and decommission

1. Stand up the instance, bootstrap, digest main.
2. Hand-build one restore bundle from the vault's content-drafts (12
   pending, 16 published, 12 rejected, 58 MB of images) and Wit's 16
   feedback files attached to their drafts. Restore it.
3. Issue tokens. Point ghostwriter at the service.
4. Switch the dashboard's Drafts tab to a summary that links to the
   service.
5. Retire, in order: the vault publisher timer and its work clone, the
   dashboard blog services and token, `blog-dispatch.yml` in the blog repo,
   the `blog.int` preview host (lab-admin request).

## 17. Decisions applied in revision 2

- Name: Chronicle.
- UI: no user login now; UI backend holds its own consumer token so the
  API is never anonymous.
- Images: 5 MB ceiling, png/jpg/webp/gif.
- Reconciliation: flags only, with Scott choosing the resolution.
- 2026-09-17, Scott: the image directory is always `static/images/<post
  slug>/`, where "post slug" is the last non-empty path segment of the
  post's `url` (the pinned slug for a new draft with no `url` yet), never
  the dated filename. One rule for imported and new drafts; nothing
  relocates on republish; unpublish deletes that directory; preview and
  publish use the same rule. See ADR 015.

## 18. Not decided here, deliberately

- How ghostwriter retrieves context for writing. Its concern.
- How a future Agent OS interface subsumes the content tab.
- Cluster specifics: registry, ingress class, storage class.
