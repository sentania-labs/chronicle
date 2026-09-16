# 003: in-house preview builder instead of blog-repo CI

- **Status:** accepted
- **Date:** 2026-09-16

## Context

Ground truth before Chronicle (spec section 3) used `blog-dispatch.yml` in
the blog repo itself to do preview, publish, and unpublish on dispatch, plus
a `blog.int` preview host. Both are retired by this service. A draft needs a
faithful preview of what it will look like once published, served
somewhere an author can look at it, before anything is proposed as a pull
request.

## Decision

Chronicle ships its own preview builder: a separate container in the same
pod (spec section 8), holding a clone of the blog repo at main with
submodules, refreshed by reconciliation. A preview run copies the base site
to a scratch tree, writes the draft as a post using the same conversion
publish uses, copies referenced images into the post's static path, runs
Hugo with `--baseURL` set to the preview path for that slug, and writes
output to the preview volume under the slug. Hugo extended is pinned in the
builder image at the version the blog repo's own Pages workflow uses
(`HUGO_VERSION`, default 0.164.0 as of this round); a version bump is
followed by rebuilding this image, and a mismatch against what reconciliation
observes in the blog repo is surfaced as toolchain drift rather than a
build failure.

## Consequences

- Preview no longer depends on GitHub Actions minutes, a dispatch workflow
  in the blog repo, or the `blog.int` host; all three are retired per spec
  section 16.
- The service now owns keeping its Hugo version current, which is a
  maintenance cost the blog repo's own CI previously carried implicitly by
  building on every push. Toolchain drift reporting (spec section 12) is the
  mitigation: it surfaces the mismatch on the admin status page rather than
  building silently wrong output.
- A preview build runs entirely inside the instance, with no dependency on
  GitHub availability, at the cost of the instance needing enough resources
  to run a full Hugo build itself. Current site size builds in seconds
  (spec section 8), so this is not yet a real cost.

## Alternatives considered

Reusing `blog-dispatch.yml` and dispatching a workflow run per preview was
rejected: it ties every preview to GitHub Actions availability and minutes,
requires the blog repo to know about draft-specific build inputs it has no
other reason to know about, and keeps the retired `blog.int` host alive as a
serving target.
