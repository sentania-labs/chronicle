# In-lane adversarial review: preview post link and slug-pin date

Reviewed the full diff against `origin/main` (`git diff origin/main...HEAD`)
as a reviewer whose job is to break it, per CONTRIBUTING.md's bar 2. Read
cold, without re-deriving the implementation from memory first.

## Findings

### 1. `preview_post_url` let a percent-encoded dot segment survive into the built href (CONFIRMED, fixed)

`url_problem` only judges a url's last path segment (ADR 015); an earlier
segment can carry anything. The first version of `convert.preview_post_url`
filtered the literal strings `.` and `..` out of the path but did nothing
with their percent-encoded forms. The WHATWG URL Standard treats a lone
`%2e` as a single-dot segment and `..`, `.%2e`, `%2e.`, `%2e%2e`
(case-insensitively) as a double-dot one, exactly like the literal forms,
and a browser normalises them on navigation. A frontmatter `url` like
`/%2e%2e/%2e%2e/admin/real-slug/` would have passed `url_problem` (its last
segment, `real-slug`, is fine) and built a preview link whose browser
navigation would land somewhere else entirely on the same host, not inside
`/preview/<slug>/`, when Scott clicked the "primary" link the edit page
renders.

**Fix:** `preview_post_url` now drops both the literal and the
case-insensitive percent-encoded dot-segment forms
(`_SINGLE_DOT_SEGMENTS`, `_DOUBLE_DOT_SEGMENTS` in `convert.py`). Added
`test_preview_post_url_drops_percent_encoded_dot_segments` (parametrized
over `%2e%2e`, `%2E%2E`, `.%2e`, `%2e.`, `%2e`, `%2E`) alongside the
existing literal-`..`/scheme/host tests. Re-reviewed after the fix: the
filter is now defined in terms of the URL Standard's own dot-segment
definition rather than a guess at what a browser normalises, so no further
encoded variant should slip through it.

## Checked and found sound

- **Scheme/host in a hand-set url reaching an href.** `preview_post_url`
  uses `urlsplit(...).path` only, which drops `scheme` and `netloc`
  (including the protocol-relative `//evil.example/x` form) before any
  segment filtering runs. Covered by
  `test_preview_post_url_drops_a_scheme_and_host_in_a_hand_set_url`.
- **Escaping on every rendering surface.** `_post_info`, `_draft_card`,
  `preview_list_page`, `_success_result_html`, and the PR body text all run
  the primary and secondary URLs through `html.escape` (or, for the PR body,
  plain text with no HTML interpretation) before they reach output. No
  surface interpolates either URL unescaped.
- **A run result with `post_url` of the wrong type.** `_success_result_html`
  reads directly from a run's raw JSON `result` dict (not a typed model) and
  isinstance-checks both `post_url` and `preview_url` before using either,
  matching the function's existing style for every other key it reads. The
  route-layer helpers (`routes/drafts.py:_post_url`, `routes/ui.py:_post_url`)
  do the same isinstance check at the boundary, so everything downstream of
  them (`_post_info`, `_draft_card`, `editor_page`) always receives `str |
  None`, never a raw dict value.
- **A date stamped in UTC by mistake.** `store._pin_slug` calls
  `convert.stamp_publish_date()`, which is `publisher.py`'s original
  `datetime.now(tz=PUBLISH_TZ)` (`ZoneInfo("America/Chicago")`) moved into
  `convert.py` verbatim, not reimplemented. `publisher.stamp_publish_date`
  is now an alias for the same function, so the two can never drift apart.
  Covered by `test_stamp_publish_date_is_local_chicago_time_iso_with_seconds`
  and `test_slug_pin_stamps_a_missing_date` (checks the stamped value parses
  with a non-null, non-zero UTC offset).
- **A re-pin path overwriting a hand-set date.** `_pin_slug` only stamps
  `if not draft.frontmatter.get("date")`, and pinning itself is a no-op past
  the first call (`draft.slug` is already set, so the `act_on_draft` call
  sites never call `_pin_slug` again for that draft). Covered by
  `test_slug_pin_never_overwrites_a_hand_set_date` and
  `test_slug_pin_stamps_the_date_once_and_a_later_save_never_moves_it`.
- **An imported post whose url is already set.** `_fill_from_post` (import)
  pins `image_dir` from the existing `url` the same way `_pin_slug` does,
  and an imported post always carries a `date` in its frontmatter already
  (it came from a real post on main), so the new stamping branch in
  `_pin_slug` is a no-op for that path; no test needed beyond the existing
  import coverage, which was unaffected by this diff.
- **The PR body text for unpublish runs.** `_unpublish`'s call into
  `_open_or_update_pr` still passes `None, None` for `preview_url,
  post_url` (previously just `None`), so an unpublish PR body renders no
  Preview line at all, unchanged from before this diff.
- **Backward compatibility for a run recorded before `post_url` existed.**
  Every surface (`routes/drafts.py`, `routes/ui.py`, `ui_templates.py`,
  `publisher._pr_body`) falls back to `preview_url` rather than raising or
  hiding the link, each covered by an explicit fallback test
  (`test_preview_and_status_fall_back_when_the_run_predates_post_url`,
  `test_editor_falls_back_to_the_site_root_when_no_post_url_recorded`,
  `test_run_log_page_falls_back_to_the_site_root_when_no_post_url_recorded`,
  `test_pr_body_falls_back_to_the_site_root_when_the_run_predates_post_url`).
- **An unpinned draft.** `make_draft(..., status="drafting")` leaves
  `draft.slug` unset, so no preview run can exist; both the editor's
  post-info panel and the board card render no link at all
  (`test_an_unpinned_draft_has_no_preview_link_at_all`), matching the
  pre-existing behaviour for a draft with no successful preview.
- **The frontmatter panel after a preview, without a reload trick.** Checked
  whether the `data-refresh`/`refresh=False` distinction in
  `ui_templates.py` matters here. The action buttons (Preview included) are
  plain `<form class="action-form">` posts; `editor.js` only intercepts the
  edit form's own submit (for the AJAX save path) and confirms-before-submit
  on `action-form`, but never calls `preventDefault` on it. The browser
  therefore does a normal navigation to the POST target, which 303-redirects
  to `GET /content/drafts/{id}`, a full server-rendered page load. The
  frontmatter panel's `refresh=False` only matters for the JS partial-swap
  path after a fetch-based Save; a full page load always shows the fresh
  value regardless of that attribute. No code change was needed here; this
  is recorded in the ADR and the PR body rather than left as a silent
  no-op.

## Disposition

One confirmed finding, fixed in this lane before push: the percent-encoded
dot-segment gap in `preview_post_url`. Everything else checked out; no
other changes made as a result of this review.
