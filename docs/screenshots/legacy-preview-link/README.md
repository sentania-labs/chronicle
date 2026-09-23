# Legacy preview run's post link: verification screenshots

Real PNG screenshots from headless Chrome (`chrome-devtools-axi`, 1280 px
wide) driven against a running `docker compose` stack, not curl. Taken on
2026-09-22 around 21:09 (America/Chicago).

## How it was produced

- Branch stack: `fix/legacy-preview-post-url`, compose project
  `chronicle-lane61`, clean volumes, api on `127.0.0.1:8093`, preview on
  `127.0.0.1:8094` (a `docker-compose.lane61.override.yml`, not committed,
  used `ports: !override` on both services and pointed
  `CHRONICLE_EXTERNAL_URL` at `http://localhost:8094`, the same shape as
  `docs/screenshots/share-image/README.md`).
- The blog clone at
  `/tmp/claude-1004/-home-scott-agents-vault/1912db70-fbc9-4e82-a9c6-f33eba286214/scratchpad/blog`
  was copied into the api container with `docker compose cp` and digested
  with `CHRONICLE_DIGEST_REPO_URL` pointed at the in-container path. Result:
  348 records created, 348 published as working records, Hugo 0.164.0, 2
  theme submodules.
- Minted a fresh consumer token with `chronicle token issue lane61-agent`
  inside the api container, never written to a file or committed.
- Created a draft ("Legacy Preview Link Check") through `/v1/drafts`, gave
  it a title and body, and ran the `preview` action. It succeeded with a
  fresh `post_url` in its result
  (`.../preview/legacy-preview-link-check/2026/09/legacy-preview-link-check/`)
  and a pinned frontmatter date (`2026-09-22T21:07:59-05:00`), confirming
  the fix does nothing to a normal, current run.
- Seeded a legacy run to match what issue 61 describes: edited the run's
  stored result JSON directly inside the container's data volume
  (`/data/repo/runs/<run_id>.json`) to remove `post_url`, and removed the
  draft's frontmatter `date` (`/data/repo/drafts/<draft_id>/draft.json`),
  so the run and draft looked exactly like a pre-v0.3.3 pin: a slug, a
  `preview_url`, and no `post_url` or `date` anywhere. Confirmed the
  stored run result had no `post_url` before opening any page.
- Opened the edit page in headless Chrome: "Last built preview" pointed at
  `http://localhost:8094/preview/legacy-preview-link-check/2026/09/legacy-preview-link-check/`,
  with `(site)` linking the root separately, derived from the run's own
  `started_at` (`2026-09-23T02:08:00+00:00` UTC, i.e. 21:08 CDT) converted
  to `PUBLISH_TZ` rather than the raw UTC date. Clicked it: landed on
  `http://localhost:8094/preview/legacy-preview-link-check/2026/09/legacy-preview-link-check/`
  with `document.title` "Legacy Preview Link Check · Clouds and Unicorns"
  and `<h1>` "Legacy Preview Link Check", the post itself, not the site's
  home page. `/content/previews` showed and linked to the same derived
  post URL.
- Confirmed via `curl`: `GET /v1/drafts/{id}/preview` returned
  `post_url` ending in `/legacy-preview-link-check/2026/09/legacy-preview-link-check/`.
  Re-read the stored run JSON from the container afterward: still no
  `post_url` key in `result`, confirming the derivation never writes
  anything back.
- Tore the stack down with `docker compose -p chronicle-lane61 down -v`
  once the checks above were done; no `chronicle-lane61` containers or
  volumes were left behind.

## Index

- `edit-page-derived-link.png`: the editor page for the seeded legacy run's
  draft. The sidebar's "Last built preview" link is the derived post URL
  (`.../2026/09/legacy-preview-link-check/`), with a separate `(site)` link
  to the preview root, even though the stored run result carries only
  `preview_url`.
- `post-page.png`: the page that link lands on, showing the post's own
  title, date (September 22, 2026), and body, not the site's home page.

Checked for tokens, credentials and internal addresses before saving: none
are visible in either image (the captures show only the seeded draft's own
title, body, and the theme's own real-blog content around it).
