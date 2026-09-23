Closes #61

## What changes for someone running it

A preview run recorded before v0.3.3 (#56) added `post_url` still had none
stored, so the Preview tab, the edit page, the board card, `GET
/v1/drafts/{id}/status`/`preview`, and the publish PR body all sent people
to the preview site's root instead of the post. Five slugs on the live
instance's `/content/previews` still did this, including an approved draft
that can no longer be rebuilt to pick up a fresh `post_url` the normal way.

Every one of those surfaces now derives the post's own link at read time
instead, through one function (`convert.resolve_run_post_url`): given a
run and its draft, a stored `post_url` still wins outright; otherwise a
pinned slug plus the draft's own date rebuilds the same link a fresh build
would, and when the draft has no date either (pinned before ADR 022), the
run's own build timestamp, read in its own recorded offset (never converted
to America/Chicago, since a pre-v0.3.3 build's date came from the builder
process's own local zone, which the timestamp's offset already records),
stands in for it. Nothing is rewritten: not the run, not the draft. A draft still
has no preview link at all until its first successful preview run, exactly
as before.

## Blast radius

Link targets only. No stored data changes: no run, draft, post, or index
record is written to differently than before this branch. The function is
pure and read-only; every call site that used to read `result.get(
"post_url")` directly now calls it instead, but every route, template, and
response shape is otherwise unchanged.

## Recovery

Revert the PR. Every surface goes back to falling back to the preview
site's root for a run with no stored `post_url`, exactly the v0.3.3
behavior; nothing needs to be undone in the data directory, since nothing
here writes to it.

## What I observed live

Isolated compose stack (`chronicle-lane61`, api on 127.0.0.1:8093, preview
on 127.0.0.1:8094), digested from the read-only blog clone (348 records).
Created a draft, ran preview, confirmed a fresh run gets a real `post_url`
untouched by this change. Then seeded a legacy run by hand in the
container's data volume (removed `post_url` from the stored result and
`date` from the draft's frontmatter, matching a pre-v0.3.3 pin exactly) and
confirmed, in headless Chrome:

- The edit page's "Last built preview" link is the derived post link, with
  the site root as a separate secondary link, and clicking it lands on the
  post itself (title and body confirmed in the rendered page, not the
  theme's home page).
- `/content/previews` shows and links to the same derived post URL.
- `GET /v1/drafts/{id}/preview` returns a `post_url` ending in the post's
  own path.
- The stored run result still carries no `post_url` after all of the
  above: nothing here writes anything back.

Screenshots: `docs/screenshots/legacy-preview-link/edit-page-derived-link.png`,
`docs/screenshots/legacy-preview-link/post-page.png`; see that directory's
README for the full narrative.

Stack torn down afterward with `-p chronicle-lane61 down -v`; no
containers or volumes left behind.

## Review findings and disposition

In-lane adversarial review (`docs/pr-bodies/legacy-preview-link-review.md`)
checked a hand-set frontmatter `url` reaching an href, escaping, a run
timestamp read as UTC by mistake, a malformed timestamp, a draft deleted
after the run, a draft whose slug changed, extra store reads on the board
page per card, and the PR body. No findings required a code change: the
derivation reuses the existing `preview_post_url` hardening for a hand-set
url, every render site already escapes its output, reading the stamp in
its own recorded offset is what reproduces a pre-v0.3.3 build's date
(covered by a test with a build stamped just past a UTC month boundary,
still the prior month in America/Chicago), a
malformed timestamp is caught and falls back cleanly, every board/editor
call site already holds the draft object it needs with no new reads, and
the run-log route is the only new read, once per page view. The tests that
asserted the old "falls back to root" behavior were rewritten to assert
the new derive-first behavior, since that is exactly what issue 61 changes,
and a genuine-fallback test was added at each layer for when derivation
still cannot produce a link.

## Not exercised

- The publish PR body's derived-link path was exercised at the unit level
  (`tests/test_publisher.py`) but not against a live GitHub App or the
  test-token repo; publish itself is unrelated to this change and untouched
  by it.
- Anything else noticed while reading this code is out of scope for this
  issue; nothing was found worth a one-line note here.
