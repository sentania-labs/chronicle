# Editor rework: live verification screenshots

Real PNG screenshots from headless Chrome (`chrome-devtools-axi`, 1440 px
wide) driven against a running `docker compose` stack, not curl. Taken on
2026-09-18 between 17:49 and 18:05 CDT (America/Chicago).

## How it was produced

- Branch `feat/editor-worth-writing-in`, api image built from the branch,
  compose project `chronicle-lane-c`, fresh volumes, api on
  `127.0.0.1:8380` and preview on `127.0.0.1:8390`.
- The stack digested a two-post fixture blog (Hello World, Second Post), so
  both start as `published` working records. The other records were made
  through the UI's own New post button.
- The image drop was a real `drop` event carrying a real PNG `File`, aimed at
  the editor container at a screen coordinate, so it goes through the same
  handler a dragged file does. The toolbar, the banner, the save indicator and
  the Preview and Publish buttons were used as a visitor would.
- Preview was clicked for real on two records; the builder built both and the
  status moved to Previewed (a published post went through the staged revise
  first).

## Index

- `01-editor-toolbar-side-by-side.png`: the editor with the toolbar (text
  glyphs from Chronicle's own CSS, no FontAwesome, no network) and the
  side-by-side live render. Frontmatter, feedback, images and version
  history are sidebar panels.
- `02-image-dropped-reference-inserted.png`: an image dropped on the body.
  `![rack after](rack-after.png)` landed at the drop point, the live render
  shows the picture (served from the draft's own image URL), and the images
  panel lists it. The Role control is in the same panel.
- `03-save-indicator-saved-sticky-bar.png`: after Save, the indicator reads
  "Saved at 5:57 PM"; the page is scrolled and the status and Save bar has
  stayed at the top.
- `04-backup-banner-offers-restore.png`: unsaved text kept in the browser,
  offered back with Restore or Discard on the next visit. Leaving the page
  without answering keeps the offer (a bug found while taking this shot; see
  `tests/editor.test.mjs`).
- `05-preview-offered-on-published-record.png`: a Published record offers
  Preview (its hint says it moves the post back to Draft first). Republish
  is disabled with "Preview first".
- `06-publish-disabled-until-preview.png`: a Draft with no preview: Preview
  is the primary button, Publish is disabled with "Preview first".
- `07-publish-primary-once-previewed.png`: the same post after a real
  preview build: status Previewed, Publish is now the primary button (its
  hint says it submits for review first), and the last built preview is
  linked.
- `08-posts-board-status-labels.png`: the posts board shows In review, Needs
  revision and Previewed instead of the raw status strings. The filter's
  option values are still the API's own.
