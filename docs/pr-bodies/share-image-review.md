# Adversarial review: edit page feature image thumbnail, preview, shareImage

Blind pass against the full diff of `feat/edit-page-share-image` versus
`origin/main` (`git diff origin/main...HEAD`), read as a reviewer whose job
is to break it. Findings below; every one that was real was fixed inside
this lane before this file was written up, and the fix was re-reviewed.

## Findings

1. **A filename or an option value carrying a quote or an angle bracket
   could break out of the `<option>` or the `data-image-src` attribute.**
   `_frontmatter_fields` already escapes the option's `value` and text with
   `html.escape`, and the new `data-image-src` attribute goes through the
   same `escape(image_url(...))` call. Verified with a filename of
   `weird"><script>.png` (`tests/test_ui.py::test_feature_image_option_data_attribute_escapes_dangerous_filenames`):
   the rendered page contains no literal `<script>` and the attribute is
   correctly escaped. **Disposition: no defect found (the existing escape
   discipline already covers the new attribute); a regression test was
   added to pin it.**

2. **The orphan option (a stored `featureImage` no attached image
   represents) could render a `data-image-src` pointing nowhere, or the
   thumbnail could show stale content for it.** Confirmed the orphan
   branch never emits the attribute (there is no `image_id` to build a
   route from), and `thumb_src` is only ever set inside the `ranked`
   branch, so it stays empty for an orphan. `featureImageSrc` in `editor.js`
   returns `null` for an option with no `dataset.imageSrc`, which both
   `updateFeatureThumb` and `featureImageHtml` treat as "hide it, render
   nothing." **Disposition: correct as designed; tests added
   (`test_feature_image_thumbnail_is_hidden_for_an_orphan_feature_image`,
   the two `featureImageSrc` orphan cases in `tests/editor.test.mjs`).**

3. **The feature image element in the live preview could end up sourced
   from the draft body instead of the server-rendered data attribute.**
   Traced `renderMarkdown`: `featureImageHtml()` reads only
   `document.getElementById("featureImage")`'s selected option's
   `dataset.imageSrc`, never the textarea or `marked.parse`'s output. The
   body's own sanitize pass (`lookupImageSrc`) is unchanged and still runs
   separately. **Disposition: correct; the live-check step (below) also
   confirmed the preview's first child img's `src` never changes when the
   body text changes without a feature image selection change.**

4. **The feature image element could skip the sanitize boundary
   `AGENTS.md` requires for anything rendered into the preview pane.**
   `featureImageHtml()` builds the `<img>` string and passes it through
   `sanitize()` before returning it, the same function the body's markdown
   goes through, so `isSafeUrl` and the same tag/attribute stripping apply
   to it. It is a second call to `sanitize()`, not a second `innerHTML`
   assignment: the combined string (`featureImageHtml() + body`) is still
   handed to EasyMDE's own single `previewRender` assignment.
   **Disposition: correct as designed.**

5. **The submission edit page (in fact, the conflict page's own
   frontmatter form, `ui_templates.conflict_page`, the second caller of
   `_frontmatter_fields`) could break because it never had a `draft_id`
   available, or could silently drop the thumbnail.** `conflict_page`
   already computes `draft["id"]` for its own `<form action=...>`; passing
   the same value into `_frontmatter_fields` was a one-line change, and
   that page now renders the thumbnail too. It has no `editor.js` wiring
   (no `#editor-app` on that page), so a visitor changing the select there
   before resubmitting will not see the thumbnail update live; this is
   consistent with that page's existing static-reload behaviour (nothing
   else on it is live either) and is noted in
   `docs/screenshots/share-image/README.md`. **Disposition: both callers
   render the thumbnail; the second caller's lack of live JS update is
   accepted, not a defect, and is called out explicitly below and in the
   PR body.**

6. **A save could leave a stale `shareImage` behind a cleared
   `featureImage`.** `_build_frontmatter` pops both keys together when the
   form's `featureImage` is empty. Verified with a draft that had
   `shareImage` set independently of `featureImage` before the save
   (`test_save_with_an_empty_feature_image_removes_a_pre_existing_shareimage`):
   both keys are gone afterward. **Disposition: no defect found; test
   added.**

7. **Publish output could grow, drop, or mis-rewrite one of the two keys.**
   `convert.py`'s `IMAGE_FRONTMATTER_KEYS` already iterated `featureImage`
   and `shareImage` identically before this change; nothing in this diff
   touches `convert.py`. Added
   `tests/test_convert.py::test_a_ui_save_writes_shareimage_alongside_featureimage_in_site_path_form`
   to pin that a draft carrying both keys (the shape a UI save now always
   produces) converts both to the site-path form. **Disposition: no defect
   found; test added as the dispatch required.**

8. **A select `change` event might not actually re-render the side-by-side
   preview**, since EasyMDE's side-by-side rendering function is wired to
   CodeMirror's own `"update"` event, not to a generic DOM `"change"`.
   `editor.js`'s change handler calls `editor.codemirror.refresh()`, which
   forces CodeMirror to redraw and fires `"update"`, invoking
   `sideBySideRenderingFunction` (and therefore `renderMarkdown`) again.
   This is read from EasyMDE's own vendored source
   (`t.on("update", t.sideBySideRenderingFunction)` set up when side by
   side is toggled on, which the editor does by default on load), not
   assumed. **Disposition: confirmed live in the browser check below,
   not just by reading the vendored source:** dispatching a real `change`
   event on the select updated both the thumbnail and the preview pane's
   image to the newly selected attachment, and updated both back to
   nothing when set to `none`.

## Live check confirms the above

Against the isolated `chronicle-lanej` compose stack, digested from the real
blog clone, on "VCF Operations Can Now See My UniFi Network" (a digested
post with `featureImage` set): the thumbnail had `complete: true` and a real
`naturalWidth` (1920), its `src` and the preview pane's first `<img>` `src`
were both the exact same `/content/drafts/<id>/images/<sha256>/file` route,
selecting `none` hid both, and selecting the post's other attached image
swapped both to match. See `docs/screenshots/share-image/README.md` for the
full account and the screenshots themselves.

9. **A freshly uploaded image selected as the feature image would not show
   in the thumbnail or the preview until the next full page load.** The
   frontmatter panel deliberately opts out of the after-save/after-upload
   `data-refresh` swap (`ui_templates._panel`'s own `refresh=False`, so a
   visitor's in-progress typing in that panel is never clobbered), so
   `editor.js`'s `addFeatureOption` builds the new `<option>` client-side
   instead of getting a fresh one from the server. It set the option's
   `value` and text, selected it, but never gave it a `data-image-src`, the
   one attribute `featureImageSrc` actually reads: the select would show
   the right filename, but the thumbnail stayed hidden and the preview
   showed nothing, contradicting what the dropdown itself said.
   **Disposition: real defect, found in this review, fixed inside this
   lane.** `addFeatureOption` now takes the upload response's own `url`
   (already returned by the API and already used to track this upload's
   markdown-insertion case) and sets it as `data-image-src` on the new
   option, then calls the same `updateFeatureThumb` and
   `editor.codemirror.refresh()` the select's own `change` handler uses, so
   selecting a freshly uploaded feature image behaves identically to
   selecting one that was already attached when the page loaded.
   `tests/test_js_dom.py::test_a_freshly_uploaded_feature_image_shows_in_the_thumbnail_without_reload`
   drives the real upload path (a mocked `fetch`, a real
   `DataTransfer`-backed file input, the actual `#image-form` submit
   handler) end to end in a full `editor.js` load, and was confirmed to
   fail against the pre-fix code (`assert out["optionHasSrc"] is True`
   failed) before the fix and pass after it.

## Not filed as issues

Finding 9 was fixed inside this lane, not filed as a separate issue, since
the dispatch scope covers exactly this interaction (the thumbnail and the
image upload path are both part of "Requested outcome" item 1). Every other
finding was already correct by design. No new issue was filed as a result
of this review.
