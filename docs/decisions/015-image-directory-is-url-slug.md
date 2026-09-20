# 015: the image directory is always the URL's slug, never the dated filename

- **Status:** accepted
- **Date:** 2026-09-17

## Context

`convert.py` placed every attached image at `static/images/<draft.slug>/`.
For a new draft this is fine: `draft.slug` is the pinned slug, and a new
draft's `url` (when Hugo generates one) is built from that same slug, so
the two always agree.

It breaks on import. `digest.slug_for` derives a post's digest slug from an
explicit frontmatter `slug`, or from the post file's own name: for a
page-bundle post that is the parent directory name, but for the dominant
pattern on the real blog, `content/posts/<YYYY-MM-DD>-<slug>.md`, it is the
whole filename stem, dated prefix included. The real blog's own
`static/images/<slug>/` directories are never dated; they are named from
the post's `url` frontmatter (`/2026/08/vcf-operations-can-now-see-my-unifi-network/`
gives `vcf-operations-can-now-see-my-unifi-network`), which Hugo's own
routing follows and Chronicle's digest never recomputes. An import that
placed images at `static/images/2026-08-01-vcf-operations-can-now-see-my-unifi-network/`
would publish a post whose body references point nowhere the real site's
own images live, and a republish would not touch the images already
sitting at the URL-derived path either: two directories for one post,
neither matching what a hand check of `static/images/` on main expects.

## Decision

The image directory is always `static/images/<post slug>/`, where "post
slug" means the last non-empty path segment of the post's `url` when the
draft has one, and the pinned slug otherwise (a brand-new draft with no
`url` yet, where slug and url-to-be already agree). One rule, no separate
case for imported versus new drafts.

The segment must be a plain directory name. `.`, `..`, anything containing
a backslash or a control character, leading or trailing whitespace, `.git`,
and the percent-encoded forms of the separators and dots (`%2e%2e`, `%5c`)
are not: `static/images/../shot.png` is one level above
the images directory, and a browser resolves an encoded `..` the same way.
Amended 2026-09-18 (issue 28): `PUT /v1/drafts/{id}` refuses a `url` whose
last segment fails this with 422 `frontmatter_url_invalid`, so the problem
is named at the save that caused it rather than at a later preview or
publish. Only a `url` being set or changed is judged: a draft's own current
`url` is accepted again, because the editor re-sends the stored value with
every save and an import keeps whatever main has, so refusing it would leave
a draft that can never be saved. Where a refusal is impossible or too late, the pinned slug names
the directory instead: an import from main (the post already exists, so the
`url` is kept as found and `create_draft` returns a warning), a submission
seed (the `url` key is dropped with a warning, like any other key the save
path would refuse), a draft that saved such a `url` before the refusal
existed (`_pin_slug` falls back), and a draft that already pinned a bad
`image_dir` (`convert.convert` ignores it and derives the name again).
`convert.usable_image_dir` and `convert.url_problem` are the one definition.

Amended 2026-09-19 (issue 46): a percent-encoded segment is refused, not
decoded. The directory on disk and the image URL rewritten into the body are
built from the same string, but a browser or static host decodes a URL before
it looks the file up, so a segment like `my%20post` produced a directory
literally named `my%20post` that `/images/my%20post/shot.png` never reaches
(it decodes to `my post`): every image on such a post 404ed. The requirement
is that the directory and the URL resolve to the same place, so a name is
usable only when decoding it, and reading it as a URL, are both no-ops.
`_segment_problem` now refuses any `%` (which also covers `%2e%2e` and `%5c`,
so it no longer decodes to judge them), any whitespace (a reference in a body
stops at it, so `my post` cannot be written into a body either), and `?` and
`#` (they end a URL path, so `a?b` names the directory `a`). The refusal is
the same mechanism as issue 28: 422 `frontmatter_url_invalid` at the save that
sets `url`, the pinned slug wherever a refusal is impossible or too late, and
a `url` the draft already carries is never refused again. A draft that pinned
such a directory earlier is treated like one that pinned `..`: `convert`
ignores the pin and derives the name again, so its references now point at a
directory that resolves; files already written under the old name are left
where they are. Percent-decoding instead (`my%20post` to `my post` on disk)
was rejected: it would put a space in a filesystem path, need every place
that writes the URL (body, frontmatter image keys, the recorded image list,
unpublish's delete set, reconciliation) to encode it again, and turn `%2f`
and `%00` into decoded characters that then need judging. Non-ASCII letters
are still accepted as they are: a literal `é` in the URL and on disk
decode to the same name.

This is pinned once, at the same moment a slug is pinned (`_fill_from_post`
for an import, `_pin_slug` for a new draft's first preview or approve), and
stored on `Draft.image_dir` rather than recomputed from `url` on every
convert: a later save changing `url` by hand (frontmatter allows it) must
never relocate a directory publish or preview has already written images
into. `convert.convert` reads `draft.image_dir` directly; it only falls
back to deriving one live for a draft written before this field existed.

Collision check: pinning `image_dir` for a new draft refuses (409
`image_dir_collision`) when `static/images/<image_dir>/` already exists on
main (checked against the digest's own site clone) or is already pinned by
a different draft (checked against the index's `pinned_image_dirs`). An
import never hits this check: it is reproducing a directory that is
already that post's own, not claiming a new one.

Unpublish's delete set needs no separate directory-delete step: every
image `convert.placements` ever wrote for a draft is already recorded on
`draft.published["images"]` with its full `site_path` under `image_dir`,
and deleting each of those blobs through the git data API leaves the
directory with nothing in it, which git does not track as an entry to
begin with.

## Consequences

- Preview and publish share `convert.convert`, so this is one code path,
  not two rules that could drift.
- An import's three-post live check (C6 PR) diffs the resulting
  `static/images/<image_dir>/` paths and rewritten body references against
  the real blog's clone, byte for byte, with zero differences expected.
- `docs/spec/00-spec.md` section 17 carries a dated line recording this as
  Scott's decision; nothing else in the spec changes.
