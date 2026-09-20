# Editor live preview images: verification screenshots

Real PNG screenshots from headless Chrome (`chrome-devtools-axi`, 1280 px
wide) driven against a running `docker compose` stack, not curl. Taken on
2026-09-20 around 14:20 (America/Chicago).

## How it was produced

- Branch stack: `feat/editor-preview-images`, compose project
  `chronicle-lanei`, clean volumes, api on `127.0.0.1:8087`, preview on
  `127.0.0.1:8097` (a `docker-compose.lanei.override.yml`, not committed,
  used `ports: !override` on both services so the stack carried only those
  two host bindings, not the default `8080`/`8090` on top of them).
- The blog clone at
  `/tmp/claude-1004/-home-scott-agents-vault/bf2ceedd-bf7c-411f-82e3-472fd9c738c1/scratchpad/blog`
  was copied into the api container with `docker compose cp` and digested
  with `CHRONICLE_DIGEST_REPO_URL` pointed at the in-container path (see
  the top of `docker-compose.yml`). Result: 347 records created, 347
  published as working records, Hugo 0.164.0, 2 theme submodules.
- Baseline (before) stack: `origin/main` (545ab9d), built in a throwaway
  git worktree and run as a single standalone container (no compose
  project, so no port or volume collision with the branch stack), digested
  from the same clone, screenshotted, then removed entirely (container,
  volume, image, worktree).
- Opened the Posts board, found "VCF Operations Can Now See My UniFi
  Network" (a digested, already-published record), opened its editor. The
  editor already defaults to side by side (`editor.js` calls
  `toggleSideBySide()` on load), so no click was needed to reach the
  preview pane.
- Confirmed live, not just visually: `document.querySelectorAll(".editor-preview
  img")` on the branch stack returned both attached inline images with
  `complete: true` and a real `naturalWidth` (1293 and 3840), each `src`
  resolved to `/content/drafts/<id>/images/<sha256>/file`. A direct `curl`
  of that URL returned `200`, `content-type: image/png`,
  `x-content-type-options: nosniff`, `content-disposition: inline`, and
  `cache-control: private, max-age=31536000, immutable`.

## Index

- `editor-preview-images-rendered.png`: the branch stack's editor, side by
  side, scrolled to the two inline images. The left pane is the raw
  markdown (`![The relationships!](/images/vcf-operations-can-now-see-my-unifi-network/image.png)`
  and the matching reference for `protect-camera-object.png`, both site-path
  references written by a digest, not by Chronicle); the right pane shows
  both images rendered: a small network-relationship diagram and a UniFi
  Protect camera screenshot.
- `editor-preview-images-before-broken.png`: the same post's editor and the
  same scroll position, from `origin/main` (545ab9d, this fix reverted).
  The preview pane shows the two references as broken-image icons and their
  alt text ("The relationships!", "The Front Door UniFi Camera object,
  discovered and monitored through the same UniFi Controller pack") instead
  of the pictures, because `lookupImageSrc` on main only resolves a bare
  filename, never the `/images/<image_dir>/<filename>` form a digested
  post's body actually carries.

## Noticed, not fixed (out of scope for this pass)

- The editor is still unstyled default-browser controls in places (noted
  already in `docs/screenshots/posts-board/README.md`); this pass did not
  touch layout or style.
- A reference using `./image.png`, a nested path, or a filename carrying a
  query string or fragment still renders broken in the preview; the fix
  here covers exactly the two forms a digested post's body and Chronicle's
  own bare-filename references use, not every conceivable relative path
  shape.

Checked for tokens, credentials and internal addresses before saving: none
are visible in either image (the captures show only the post's own title,
body, frontmatter panel state, and image filenames).
