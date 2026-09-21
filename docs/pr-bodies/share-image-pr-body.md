# Editor: feature image thumbnail, live preview, and shareImage kept in step

## What changes for someone running it

Opening a post in the editor and expanding the Frontmatter panel now shows
a thumbnail of the selected feature image next to the picker, and the same
image at the top of the live markdown preview pane. Choosing "none" hides
both; choosing a different attached image swaps both, live, without a page
reload; uploading a new image and picking it as the feature image shows it
live too, not just after the next reload.

Saving the editor now writes the same value to both `featureImage` and
`shareImage` in a post's frontmatter, clearing both together when the
feature image is cleared. Before this, `shareImage` was a passthrough key
this page never touched, so a post whose share image was set on the
dashboard and then edited here silently drifted. Publishing output is
unchanged in shape: `convert.py` already rewrote both keys to the site path
(`/images/<image_dir>/<filename>`) identically; this only changes what a UI
save writes into the draft before conversion, and a test
(`tests/test_convert.py::test_a_ui_save_writes_shareimage_alongside_featureimage_in_site_path_form`)
pins that both keys still convert correctly together.

No new route, and no filename-keyed route: the thumbnail and the preview
both resolve through the existing `/content/drafts/<id>/images/<sha256>/file`
route, carried in a `data-image-src` attribute the server already escapes
the same way every other value on this page is escaped.

## Blast radius

- `chronicle/api/ui_templates.py`: `_frontmatter_fields` gained a
  `draft_id` parameter and now renders a `data-image-src` attribute on
  every attached-image `<option>` plus one `<img id="featureImageThumb">`.
  Both existing callers (the editor page and the save-conflict page) were
  updated; both now render the thumbnail (see the adversarial review for
  why the second caller has no live JS wiring, which is expected, not a
  gap).
- `chronicle/api/static/editor.js`: a new pure helper (`featureImageSrc`),
  a new `<img>` prepended to the live preview (through the existing
  `sanitize` path, never from body text), a `change` listener on the
  feature image select, and a fix to the image-upload path
  (`addFeatureOption`) so a freshly uploaded feature image behaves the
  same as one already attached.
- `chronicle/api/routes/ui.py`: `_build_frontmatter` writes/clears
  `shareImage` alongside `featureImage`. This is the only change to what
  gets written to a draft's frontmatter; nothing else in the save path
  changed.
- `chronicle/api/static/style.css`: one new rule, `.feature-thumb`.
- No change to `convert.py`, the publish path, reconciliation, or any
  `/v1` route.

## Recovery

Revert the PR. The frontmatter change is additive (a save always writes
`shareImage` equal to `featureImage`, never a new independent value), so a
revert stops writing `shareImage` on future saves but does not need to
undo anything already published; the site-path form Hugo already sees is
unchanged either way.

## What was observed live

Isolated compose stack (`chronicle-lanej`, api on `127.0.0.1:8088`,
preview on `127.0.0.1:8098`), digested from the real blog clone, against
"VCF Operations Can Now See My UniFi Network" (a digested post with
`featureImage` already set). Confirmed with real DOM state, not just
visually: the thumbnail had `complete: true` and a real `naturalWidth`
(1920), and its `src` and the preview pane's first `<img>`'s `src` were
both the same `/content/drafts/<id>/images/<sha256>/file` route. Selecting
`none` (a real dispatched `change` event) hid both; selecting the post's
other attached image swapped both to match. A before shot from
`origin/main` (6f2e90c, this change reverted), same post, same scroll
position, shows no thumbnail element at all and a preview pane that starts
directly at the first paragraph. Screenshots and the full account:
`docs/screenshots/share-image/README.md` (`after-thumbnail-detail.png`,
`none-selected-hides-both.png`, `before-no-thumbnail.png`).

## Adversarial review

Full account in `docs/pr-bodies/share-image-review.md`. Eight angles
(option/filename escaping, the orphan option, the data attribute never
holding a non-route URL, the preview image never sourced from body text,
the sanitize boundary, the conflict page's own caller, a stale `shareImage`
surviving a clear, and convert's output for both keys) were checked and
found already correct by design, each pinned with a test. A ninth,
real finding: a freshly uploaded image selected as the feature image
showed in the dropdown but not in the thumbnail or the preview until the
next page load, because the client-side option the upload path creates
never got the `data-image-src` attribute `featureImageSrc` reads. Fixed
inside this lane (`addFeatureOption` now sets it from the upload
response's own URL) and pinned with a test driving the real upload path
end to end in a headless-Chrome DOM test
(`tests/test_js_dom.py::test_a_freshly_uploaded_feature_image_shows_in_the_thumbnail_without_reload`),
confirmed to fail against the pre-fix code and pass after it.

## Noticed, not fixed (one-line notes, out of this diff)

- The save-conflict page (`ui_templates.conflict_page`) now renders the
  thumbnail too (it shares `_frontmatter_fields` with the editor), but has
  no live JS update on that page's select, since that page has no
  `#editor-app` and no live preview at all; consistent with its existing
  static-reload behaviour, not a new gap.
