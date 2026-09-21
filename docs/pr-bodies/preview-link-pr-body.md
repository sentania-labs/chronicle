# Preview post link, and the date pinned with the slug

## TLDR

A preview run's link always pointed at the preview site's own root (the
theme's home page), never the post. Combined with an undated draft's
build-time date not being written back anywhere, that made a fresh
preview look like nothing had built: the home page's "Recent" list sorts by
date and a post whose date recomputes on every build never sits still on
it. This branch adds `post_url` beside `preview_url` on a preview run's
result, makes the post's own page the primary link everywhere a preview
link renders, and stamps a draft's date at the same moment its slug is
pinned (first preview or approve) rather than only at first publish.
`make check` is green (1008 tests). An adversarial review of the full diff
found one real issue, fixed on this branch with a regression test: a
percent-encoded `..` segment in a hand-set frontmatter `url` could have
walked a rendered preview link off the preview site. Confirmed live against
a fresh stack: the edit page's primary link opens the post itself
(confirmed by title and URL), the Frontmatter panel shows the stamped date
immediately after the preview action with no reload trick, and both
`GET /v1/drafts/{id}/preview` and `/status` carry `post_url`.

## What changed

- **`convert.preview_post_url(preview_base, post_url_value)`**: builds the
  post's full preview link from the preview site's root and
  `convert.post_url`'s own output, so the date path is never derived a
  second time anywhere. Treats the url as untrusted past what
  `convert.url_problem` already checks (that only judges a url's last path
  segment, ADR 015): drops the scheme, host, and every dot segment
  (literal and percent-encoded) before building the link, so a hand-set
  frontmatter `url` can never make a rendered preview link leave the
  preview site.
- **The builder's run result gains `post_url`** beside the existing
  `preview_url` (`chronicle/builder/runner.py`). A run recorded before this
  field existed carries no `post_url`; every surface below falls back to
  `preview_url` instead of breaking.
- **Every surface that shows a preview link makes the post the primary
  link, the site root secondary**: the edit page's post-info panel, the
  drafts board card, the preview list page, a run's own result rendering,
  `GET /v1/drafts/{id}/preview`, `GET /v1/drafts/{id}/status`, and the
  publish PR body (`Preview: <post url>`, site root on the line after). An
  unpublish PR still carries no preview link, unchanged from before this
  branch. An unpinned draft (no slug, so no preview run has ever
  succeeded) still shows no preview link at all.
- **The date is pinned with the slug, not only at first publish**
  (`store.Store._pin_slug`, ADR 022): stamps `frontmatter["date"]` when
  missing, in the same ISO-in-America/Chicago form `record_publish_result`'s
  own stamp uses. The pin is the moment the filename and default `url` are
  both derived from the date (ADR 015's same reasoning for `image_dir`), so
  stamping there keeps a preview and a later publish from ever disagreeing
  about either. Never overwrites a date already present (hand-set,
  imported, or already pinned); pinning itself only runs once per draft.
  The stamping function moved from `publisher.py` into `convert.py`
  (`publisher.stamp_publish_date` is now an alias for the same function) so
  `store.py` can call it without `store.py` and `publisher.py` importing
  each other.
- **`convert.convert` also stamps a date when the frontmatter it is handed
  has none**, using the same `post_date` calculation the filename and `url`
  already use. This only fires for a draft built by hand (a test) or
  written before either the pin or the publish stamp existed; every real
  call site stamps earlier.
- Docs: one AGENTS.md sharp-edge line, ADR 022
  (`docs/decisions/022-preview-post-link-and-slug-pin-date.md`).

## Blast radius

- **Link targets**: every existing `preview_url` consumer keeps working
  unchanged (the field, its meaning, and its value are untouched); what
  changes is that a new `post_url` field now exists beside it and most UI
  surfaces prefer it when present. Reverting this PR removes the field and
  the surfaces go back to linking the site root, with no data loss (nothing
  here deletes or migrates a stored value).
- **One new run-result key** (`post_url`, alongside `preview_url` on a
  preview run's `result` dict). `Run.result` is a plain `dict[str, Any]`;
  no schema migration needed.
- **A date stamped at pin time** where previously it was stamped only at
  first publish (or not at all, for a preview-only draft). This is
  additive: a draft that already carries a date is untouched, and a
  stamped-but-never-published draft's date is harmless, visible in the
  Frontmatter panel and editable there like any other frontmatter field.

## Recovery

Revert the PR. Any date this branch stamped on a draft in the meantime
stays on that draft (harmless, and editable in the Frontmatter panel same
as any hand-set value); nothing here writes to a published post or to
main.

## Live check

Isolated stack, compose project `chronicle-lanel`, api on `127.0.0.1:8091`,
preview on `127.0.0.1:8092`, both port-only and `CHRONICLE_EXTERNAL_URL`
overrides via an uncommitted `docker-compose.lanel.override.yml` (the
override had to set `CHRONICLE_EXTERNAL_URL` on the `builder` service too,
not just `api`, since a preview's URLs come from the builder's own copy of
that setting; noted in the screenshots README as a possible future doc
clarification, not fixed in this diff). Digested the read-only blog clone
from the vault scratchpad: 347 records, matching the known C6 baseline.
Created a draft with a stack-minted token (never printed to a file or
committed), no date set, ran the preview action, and confirmed live in
headless Chrome:

- The preview run's result carried both `preview_url` and `post_url`;
  `GET /v1/drafts/{id}/preview` returned the same two keys.
- The edit page's post-info panel showed "Last built preview" (the post's
  own URL) as the primary link, with a secondary "(site)" link beside it,
  immediately after the preview action, no reload trick needed (the action
  button is a plain form post that 303-redirects to a full page load).
- The Frontmatter panel's Date field showed the stamped value
  (`2026-09-21T01:17:14-05:00`, local Chicago time) in that same load.
- Clicking the primary link landed on the post itself (`document.title`
  and `<h1>` both read the post's title, `location.href` matched
  `post_url`), not the theme's home page.
- The preview site's root (what the old, pre-fix link landed on) showed
  only the theme's home page and its already-published "Recent" list, no
  sign of the new draft.

Screenshots: `docs/screenshots/preview-link/` (`after-edit-page-links.png`,
`after-post-page.png`, `before-home-page-root-link.png`, README with the
full narrative). Stack torn down after: no `chronicle-lanel` containers,
volumes, or images left, and the throwaway compose override was removed
rather than committed.

## Adversarial review

Full diff against `origin/main` reviewed cold (CONTRIBUTING.md bar 2),
findings and disposition recorded in
`docs/pr-bodies/preview-link-review.md`. One confirmed finding, fixed
before push: `preview_post_url`'s first version filtered only the literal
`.`/`..` path segments out of a hand-set frontmatter `url`, missing that
the WHATWG URL Standard (and every browser) also treats the percent-encoded
forms (`%2e`, `%2e%2e`, `.%2e`, `%2e.`, case-insensitively) as dot segments
and normalises them on navigation. Since `url_problem` only judges a url's
last path segment (ADR 015), an earlier segment carrying `%2e%2e` would
have passed validation and built a preview link whose *browser*
normalization walked it off `/preview/<slug>/` onto another path on the
same host. Fixed by filtering both forms; regression test added
(`test_preview_post_url_drops_percent_encoded_dot_segments`). Everything
else checked (scheme/host stripping, escaping on every render surface, run
result typing, UTC-vs-local stamping, re-pin overwrite, imported-post urls,
unpublish PR body, backward compatibility for pre-existing runs, unpinned
drafts, and the frontmatter panel's refresh behavior after a preview) came
back sound; see the review file for the detail on each.
