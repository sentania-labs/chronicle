# Feature image thumbnail and preview: verification screenshots

Real PNG screenshots from headless Chrome (`chrome-devtools-axi`, 1280 px
wide) driven against a running `docker compose` stack, not curl. Taken on
2026-09-20 around 22:30 (America/Chicago).

## How it was produced

- Branch stack: `feat/edit-page-share-image`, compose project
  `chronicle-lanej`, clean volumes, api on `127.0.0.1:8088`, preview on
  `127.0.0.1:8098` (a `docker-compose.lanej.override.yml`, not committed,
  used `ports: !override` on both services so the stack carried only those
  two host bindings, not the default `8080`/`8090` on top of them).
- The blog clone at
  `/tmp/claude-1004/-home-scott-agents-vault/bf2ceedd-bf7c-411f-82e3-472fd9c738c1/scratchpad/blog`
  was copied into the api container with `docker compose cp` and digested
  with `CHRONICLE_DIGEST_REPO_URL` pointed at the in-container path (see
  the top of `docker-compose.yml`). Result: 347 records created, 347
  published as working records, Hugo 0.164.0, 2 theme submodules.
- Baseline (before) stack: `origin/main` (6f2e90c), built in a throwaway
  git worktree and run as a single standalone container (no compose
  project, so no port or volume collision with the branch stack), digested
  from the same clone, screenshotted, then removed entirely (container,
  image, worktree).
- Opened the Posts board, searched for "UniFi Network", opened "VCF
  Operations Can Now See My UniFi Network" (a digested, already-published
  record with a `featureImage` set), expanded the Frontmatter panel.
- Confirmed live, not just visually: on the branch stack,
  `document.getElementById("featureImageThumb")` had `complete: true` and
  `naturalWidth: 1920`, and its `src` was
  `http://127.0.0.1:8088/content/drafts/fb7046473b3144b6b78f05c3c7291db4/images/00261f95b358a5d134c599c7d8b197b249a9b30b9e24e558bbb69068f02bd83b/file`,
  the same `/content/drafts/<id>/images/<sha256>/file` route. The preview
  pane's first child was an `<img>` with that same `src`. Changing the
  select's value to `"none"` (dispatching a real `change` event) set the
  thumbnail's `hidden` to `true` and removed the image from the top of the
  preview; changing it to the post's other attached image (`image.png`)
  updated both the thumbnail and the preview to that image's URL, matching
  each other. On `origin/main` in the same panel,
  `document.getElementById("featureImageThumb")` is `null`: no element to
  even query.

## Index

- `after-thumbnail-detail.png`: the branch stack's editor, side by side,
  scrolled to the Frontmatter panel. The preview pane (center) shows the
  post's `featured.png` at the top, above the rendered body; the sidebar
  (right) shows the same image as a thumbnail directly under the "Feature
  image" select, which reads `featured.png`.
- `none-selected-hides-both.png`: the same post and scroll position, after
  setting the select to `none`. The preview pane's rendered body starts
  directly with the post's own inline diagram (no feature image above it),
  and the Frontmatter panel shows no thumbnail.
- `before-no-thumbnail.png`: the same post, same scroll position, from
  `origin/main` (6f2e90c, this change reverted). The Feature image select
  still reads `featured.png` (frontmatter untouched), but there is no
  thumbnail under it, and the preview pane's body starts directly at the
  first paragraph with nothing shown for the feature image.

## Noticed, not fixed (out of scope for this pass)

- The conflict page's own frontmatter form (`ui_templates.conflict_page`)
  now renders the same thumbnail server-side (it shares `_frontmatter_fields`
  with the editor), but has no `editor.js` wiring: a visitor changing that
  page's select before resubmitting will not see the thumbnail update live.
  That page has no live markdown preview at all, so this is consistent with
  its existing static-reload behaviour, not a new gap.
- The editor is still unstyled default-browser controls in places (noted
  already in `docs/screenshots/posts-board/README.md`); this pass added one
  small `.feature-thumb` rule and did not otherwise touch layout or style.

Checked for tokens, credentials and internal addresses before saving: none
are visible in any image (the captures show only the post's own title,
body, frontmatter panel state, and image filenames).
