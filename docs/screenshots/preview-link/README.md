# Preview post link and pinned date: verification screenshots

Real PNG screenshots from headless Chrome (`chrome-devtools-axi`, 1280 px
wide) driven against a running `docker compose` stack, not curl. Taken on
2026-09-21 around 01:18 (America/Chicago).

## How it was produced

- Branch stack: `feat/preview-post-link`, compose project `chronicle-lanel`,
  clean volumes, api on `127.0.0.1:8091`, preview on `127.0.0.1:8092` (a
  `docker-compose.lanel.override.yml`, not committed, used `ports:
  !override` on the `api` and `preview` services so the stack carried only
  those two host bindings, not the default `8080`/`8090` on top of them; it
  also set `CHRONICLE_EXTERNAL_URL: http://localhost:8092` on both the
  `api` and the `builder` services, since a preview's `preview_url` and
  `post_url` come from the builder's own setting, not the api's).
- The blog clone at
  `/tmp/claude-1004/-home-scott-agents-vault/bf2ceedd-bf7c-411f-82e3-472fd9c738c1/scratchpad/blog`
  was copied into the api container with `docker compose cp` and digested
  with `CHRONICLE_DIGEST_REPO_URL` pointed at the in-container path (see the
  top of `docker-compose.yml`). Result: 347 records created, 347 published
  as working records, Hugo 0.164.0, 2 theme submodules, matching the C6
  baseline exactly.
- A stack-minted consumer token (`chronicle token issue`, run inside the api
  container; never printed into a file or committed) created a draft titled
  "Lane L Preview Link Check" with a body and no `date`, then ran the
  `preview` action through the API.
- The preview run succeeded (`GET /v1/runs/{id}` polled to `status:
  "succeeded"`); its `result` carried both
  `"post_url": "http://localhost:8092/preview/lane-l-preview-link-check/2026/09/lane-l-preview-link-check/"`
  and `"preview_url": "http://localhost:8092/preview/lane-l-preview-link-check/"`.
  `GET /v1/drafts/{id}/preview` returned the same two keys.
- Opened the edit page in headless Chrome. The post-info panel's "Last
  built preview" link (the primary one) pointed at the `post_url` above; a
  secondary "(site)" link beside it pointed at the `preview_url` root. The
  Frontmatter panel's Date field already showed `2026-09-21T01:17:14-05:00`,
  the value `store._pin_slug` stamped when the preview action pinned the
  slug (ADR 022), no reload trick needed: the action button is a plain form
  post that 303-redirects to a full page load of the same route.
- Clicked the primary link. `document.title` and the page's `<h1>` both
  read "Lane L Preview Link Check" (confirmed with `chrome-devtools-axi
  eval`), and `location.href` was the `post_url` above: the click landed on
  the post itself, not the theme's home page.
- For the before shot, opened the preview site's root
  (`http://127.0.0.1:8092/preview/lane-l-preview-link-check/`) directly:
  this is exactly where the old, pre-fix `preview_url` link would have sent
  a click. `document.title` there is "Clouds and Unicorns", the theme's own
  home page, and its "Recent" list carries only already-published posts
  (dated July and August 2026); the new draft is nowhere on it. A separate
  full second stack from `origin/main` was judged too heavy for this
  confirmation (the builder image and a full digest would have to be
  rebuilt a second time just to reach the same page), so the before shot is
  this lane's own stack's root link, taken after the fix's builder change
  was already live but before the API/UI link-selection change would have
  mattered: the root page a click on the old `preview_url` reaches is
  identical either way, since `preview_url` itself is unchanged by this PR.

## Index

- `after-edit-page-links.png`: the branch stack's edit page for the new
  draft. The post-info panel shows "Last built preview" (the post's own
  URL) as the primary link with "(site)" as a secondary link beside it, and
  the Frontmatter panel's Date field shows the stamped value, all without a
  manual reload after the preview action.
- `after-post-page.png`: the page the primary link opens: the post itself,
  titled "Lane L Preview Link Check", dated September 21, 2026, with its
  own body text, not the theme's home page.
- `before-home-page-root-link.png`: the preview site's root, what the old
  `preview_url` link (still the secondary link today) lands on: the
  theme's home page and its "Recent" list of already-published posts, with
  no sign of the new draft.

## Noticed, not fixed (out of scope for this pass)

- The compose override needed `CHRONICLE_EXTERNAL_URL` set on both `api`
  and `builder` to get a consistent link; the committed `docker-compose.yml`
  comment only calls out the api/preview relationship, not that the builder
  carries its own copy of the same setting. Worth a doc line in a future
  pass, not part of this diff's scope.

Checked for tokens, credentials and internal addresses before saving: none
are visible in any image (the captures show only the post's own title,
body, frontmatter panel state, and the theme's own published post titles).
