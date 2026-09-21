# 022: a preview links to the post itself, and its date is pinned with the slug

- **Status:** accepted
- **Date:** 2026-09-21

## Context

A preview run's result only ever carried `preview_url`, the preview site's
own root (`f"{settings.external_url}/preview/{slug}/"`), which renders the
theme's home page, not the post. The post's own page lives at
`/preview/<slug>/<post url>/`, where `<post url>` is whatever
`convert.post_url` wrote into the converted frontmatter (the `/YYYY/MM/slug/`
convention, ADR 017, or a hand-set `url`). Nothing in the UI, the API, or the
publish PR body ever linked there, so opening a preview meant landing on the
home page and hunting for the post.

That hunt failed outright for a draft with no `date`: `convert.post_date`
falls back to today at build time for both the filename and the `url`, but
that fallback only exists to make the build succeed, not to pin anything.
Nothing wrote the computed date back onto the draft before this ADR, so it
was recomputed on every build. The theme's home page sorts by date, and a
post whose only date is "whatever today happened to be at build time" is not
a stable sort key; three drafts previewed on different days could each
render a slightly different date on every subsequent rebuild, which is a
symptom distinct from, but compounding, the missing link itself.

## Decision

**The post's own preview URL.** `convert.preview_post_url(preview_base,
post_url_value)` builds the full link from the preview site's root and the
post's own `url` (`convert.post_url`'s output): no second derivation of the
date path anywhere. It treats `post_url_value` as untrusted past what
`convert.url_problem` already catches (that check only judges a url's last
path segment, ADR 015): only the path component is used, and `.`/`..`
segments are dropped, so a hand-set frontmatter `url` carrying a scheme, a
host, or a `..` segment can never make the built link leave the preview
site.

The builder's run result gains `post_url` beside the existing `preview_url`.
Every surface that shows a preview link makes the post URL the primary link
and keeps the site root as a secondary "site" link: the edit page's post-info
panel, the drafts board card, the preview list page and a run's own result
rendering, `GET /v1/drafts/{id}/preview` and `GET /v1/drafts/{id}/status`,
and the publish PR body. A run recorded before this field existed carries no
`post_url`; every one of those surfaces falls back to `preview_url` rather
than breaking or hiding the link. An unpinned draft (no slug, so no preview
run has ever succeeded) still shows no preview link at all, exactly as
today.

**The date is pinned with the slug, not stamped only at first publish.**
`store.Store._pin_slug`, the one place ADR 015 already fixes as the moment
`image_dir` is pinned (a new draft's first preview or approve, or an
import's `_fill_from_post`), now also stamps `frontmatter["date"]` when it
is missing, using the same ISO-in-America/Chicago form
`record_published`'s own stamp uses (`convert.stamp_publish_date`, moved
there from `publisher.py` so `store.py` can call it without an import cycle;
`publisher.stamp_publish_date` stays as a re-export so nothing else moves).

This is the pin moment, not first preview alone, because the filename
(`convert.post_filename`) and the default `url` (`convert.post_url`) are
both derived from this date: a date that drifted between preview and
publish would move the post's own url out from under it, the same hazard
ADR 015 named for `image_dir`. Stamping at the pin makes a preview and a
later publish of the same draft agree on both without either recomputing
anything.

The stamp only fills a blank: a draft that already carries a `date` (hand-set,
imported, or pinned once already) is never overwritten, and pinning is
itself a no-op past the first call (`draft.slug` is already set, so
`_pin_slug` never runs again for that draft). `record_published`'s own
stamping is unchanged beyond now agreeing with an earlier stamp when one
already exists.

## Consequences

- Opening a preview link now lands on the post, not the home page.
- A brand-new draft's date is visible in the Frontmatter panel as soon as
  the first preview or approve pins the slug, not only after a publish.
- `convert.py` gains the publish-date clock (`PUBLISH_TZ`,
  `stamp_publish_date`) that `publisher.py` used to own alone; `store.py`
  already imported `convert`, so this adds no new import edge.
- A percent-encoded, scheme-carrying, or `..`-bearing frontmatter `url` still
  cannot make `preview_post_url` produce a link outside the preview site,
  the same posture ADR 015 already takes for `image_dir`.
