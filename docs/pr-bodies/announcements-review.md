# Blind adversarial review: announcements (ADR 021)

Reviewed the full diff against origin/main (`git diff origin/main...HEAD`) as a
reviewer whose job is to break it, after the branch's own `make check` was
already green. Scenarios tried: an unknown channel key, a non-string value, a
huge string, announcement text containing HTML or quotes on the edit page, a
PUT omitting the field wiping data, a stale base_version, the convert output,
a backup restored from pre-field data, and the copy button on a page with no
clipboard API.

## Findings

### 1. The conflict page's reload form wiped announcements on resubmit (confirmed, fixed)

`routes/ui.py`'s `draft_save` always calls `_build_announcements(form)` and
treats the result as a full replacement: a channel whose `announcement_*`
field is absent from the submitted form is absent from the saved mapping.
`ui_templates.conflict_page`'s reload form (the "Current version, reapply
from here" form show after a 409) rendered the frontmatter fields and the
body, but no announcement fields at all. Resubmitting that form exactly as
rendered, the normal recovery path after a conflict, would silently clear
whatever the draft's announcements held, with no notice and no way to tell
from the page that it happened.

Fixed by adding `_announcement_hidden_fields`, which renders the current
draft's announcements as hidden inputs in the reload form
(`ui_templates.py`), so a straight resubmit preserves them. A regression
test, `test_the_conflict_page_reload_form_carries_the_current_announcements`,
covers the 409 pane still showing the attempted text and the resubmit
preserving what the draft actually had.

Re-reviewed after the fix: the reload form now carries the draft's
`announcements` the same way it already carried `frontmatter`, and the fix
does not touch the API PUT path's own keep-when-omitted behaviour, which was
correct before and after.

## Not findings (checked, no change)

- **Unknown channel key / non-string value**: `store.check_announcements`
  refuses both with a 422 in the shared `ApiError` envelope
  (`announcement_channel_not_allowed`, `announcement_wrong_type`), covered by
  `tests/test_announcements.py`.
- **Huge string**: no per-channel limit exists on purpose (ADR 021); the
  API's global body-size guard (`chronicle/api/main.py`) is the only ceiling,
  the same one the draft body lives under. Tested with a 20000-character
  value.
- **HTML or quotes in announcement text on the edit page**: `ui_templates.
  _announcement_fields` runs every value through `html.escape` before
  interpolating it into the textarea; `test_announcement_text_is_escaped_not_
  rendered` posts a payload with a closing `</textarea>`, an `onerror`
  attribute, an ampersand, and a quote, and asserts none of it survives
  unescaped.
- **A PUT omitting `announcements` wiping data**: `Store.save_draft` treats
  `None` as keep, a mapping (`{}` included) as replace. Tested for a second
  PUT that only changes the body.
- **Stale base_version**: refused as a 409 before the announcements check
  runs, and the draft's announcements are unchanged; also tested with an
  invalid announcement in the same stale request, to confirm the conflict
  is what gets reported.
- **Convert output**: `test_convert_output_is_byte_identical_with_and_
  without_announcements` builds the same draft with and without the field
  and asserts identical text, images, post path, and URL, plus that no
  announcement value appears in the converted text.
- **A backup restored from pre-field data**: `test_a_backup_made_before_
  this_field_restores_with_an_empty_mapping` writes a draft.json with no
  `announcements` key, backs it up, restores into a fresh data directory, and
  asserts the restored draft has `announcements == {}`.
- **The copy button with no clipboard API**: `editor.js`'s click handler
  checks for `navigator.clipboard` and `writeText` before calling either, and
  reports the same "not available" message on that path or on a rejected
  promise or a thrown exception. `copyStateText` is unit tested for both
  outcomes.

## Checked, not further tested, with reason

- **`editor.js`'s `readFields`/`writeFields` picking up the three
  announcement fields for the local-backup and Restore flow.** These
  textareas join the form by the standard HTML `form="edit-form"` attribute,
  the same mechanism the frontmatter panel's fields already use; a form's
  `.elements` collection includes any element associated by that attribute
  regardless of where in the document it sits, so no code change was needed
  in `readFields`/`writeFields` themselves. `tests/editor.test.mjs`'s fake
  DOM harness (`loadEditorPage`) stubs `form.elements` as a plain array with
  no such association logic; a test built against that stub would verify the
  stub, not the real browser behaviour. The server-rendered-HTML test
  (`test_the_editor_renders_three_named_textareas_joined_to_the_edit_form`)
  is what actually holds this contract: it asserts `form="edit-form"` is
  present on all three announcement fields, which is the one condition a
  real browser needs to include them in the form's submission and in
  `readFields`'s loop.
