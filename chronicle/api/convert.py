"""Draft to Hugo post: the one conversion the preview build and publish share.

Spec section 8 says a preview run writes the draft "as a post using the same
conversion as publish", so the conversion lives here, in the api package,
rather than inside the builder: the builder imports it today and the publish
path (C4) imports the same functions, so a preview can never render a post
that publish would write differently.

Nothing in this module touches the filesystem. It answers three questions
about a draft and returns plain strings: what the post file is called, what
its frontmatter and body are, and where each attached image has to land for
the body's references to resolve. The caller writes the files.

The three rules, checked against the real blog's 347 posts:

- **Filename.** An imported draft keeps the file it came from
  (`source_post.path`), because its `url` is already public and the file
  name is part of the archive's shape. A new draft gets
  `<section>/<YYYY-MM-DD>-<slug>.md`, the dated pattern every post on main
  uses, where `<section>` is the directory the site's own posts live in
  (`digest.read_new_post_dir_from_state`, ADR 017's issue 21 amendment: the
  last digest's observed section, `content/posts` on the real blog). With no
  digest state, or a fallback read, it is `content/posts`.
- **`url`.** An imported draft keeps the `url` in its own frontmatter. A new
  draft gets `/<YYYY>/<MM>/<slug>/`, the dominant pattern on main (a
  handful of older posts carry `/<YYYY>/<MM>/<DD>/<slug>/`; Chronicle does
  not reproduce that, and an author who wants it can set `url` by hand,
  which this module then leaves alone).
- **Images.** Every attached image is copied to
  `<static_dir>/images/<image_dir>/<filename>` (`static_dir` is the site's
  own `staticdir`, ADR 017; `static/images` when no caller passes one) and
  every reference to it in the body or in a frontmatter image field is
  rewritten to `/images/<image_dir>/<filename>`, which never carries
  `static_dir`: Hugo publishes everything under its static directory to
  the site root regardless of what that directory is named. `image_dir` is
  the post's own URL slug (the last non-empty path segment of `url`),
  never the dated filename and never `draft.slug` directly when the two
  differ (ADR 015); it is pinned once on the draft (`Draft.image_dir`) at
  the same moment the slug is pinned, so a later save or republish never
  relocates the directory. A reference matches by its recorded
  `source_ref` (what the post itself used before the import) or, for an
  image that has none because it was uploaded fresh, by filename.

`draft: false` is forced on the way out. A preview of a draft that Hugo
skipped as a draft would be an empty page, and publish never wants the flag
either, so it is the one frontmatter value the conversion decides rather
than carries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import yaml

from .models import FRONTMATTER_ALLOWLIST, Draft

POSTS_DIR = "content/posts"
STATIC_IMAGES_DIR = "static/images"

# Spec section 5: "First publish stamps today in America/Chicago", regardless
# of what timezone the container itself runs in. ADR 022: the slug pin
# (`store.Store._pin_slug`) stamps a missing date with the same clock, since
# the pin is the moment the filename and url are derived from it, not first
# preview or publish alone.
PUBLISH_TZ = ZoneInfo("America/Chicago")


def stamp_publish_date() -> str:
    return datetime.now(tz=PUBLISH_TZ).isoformat(timespec="seconds")


# The two ways a post reaches an image, the same pair `store.py` scans for on
# a `from_post` import: markdown `![alt](path)` and a bare `<img src="...">`.
_MARKDOWN_IMAGE_REF = re.compile(r"(!\[[^\]]*\]\(\s*)([^)\s]+)")
_HTML_IMG_REF = re.compile(r"""(<img\b[^>]*\bsrc=["'])([^"']+)""", re.IGNORECASE)

# Frontmatter keys whose value is a single image reference.
IMAGE_FRONTMATTER_KEYS = ("featureImage", "shareImage")


class ConversionError(Exception):
    """The draft cannot be rendered as a post file at all."""


@dataclass(frozen=True)
class ImagePlacement:
    """One attached image and the path its references now point at."""

    image_id: str
    filename: str
    role: str
    # Site-relative path under the scratch tree, e.g.
    # `static/images/my-slug/featured.png`.
    site_path: str
    # The URL the body and frontmatter now use, e.g. `/images/my-slug/featured.png`.
    url: str


@dataclass(frozen=True)
class ConvertedPost:
    """Everything the caller needs to write, and nothing about how."""

    slug: str
    # Site-relative path of the post file, e.g. `content/posts/2026-08-01-x.md`.
    post_path: str
    # The full file: frontmatter fence, frontmatter, fence, body.
    text: str
    # The post's own permalink, site-relative, e.g. `/2026/08/x/`.
    url: str
    images: list[ImagePlacement]


def post_date(frontmatter: dict[str, Any]) -> date:
    """The date the filename and a generated `url` are built from.

    A post on main carries an ISO timestamp with an offset; a draft that has
    never set one gets today in the service's own local zone, which is the
    same rule spec section 5 gives for the publish date stamp.
    """
    raw = frontmatter.get("date")
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        try:
            return datetime.fromisoformat(text).date()
        except ValueError:
            try:
                return date.fromisoformat(text[:10])
            except ValueError:
                pass
    return datetime.now().astimezone().date()


def post_filename(draft: Draft, slug: str) -> str:
    # ADR 017: Hugo does not govern content filenames at all, so this stays
    # an observed convention read off the real posts, never something a
    # site's own `hugo config` could confirm or replace.
    return f"{post_date(draft.frontmatter).isoformat()}-{slug}.md"


def post_path(
    draft: Draft,
    slug: str,
    content_dir: str | None = None,
    new_post_dir: str | None = None,
) -> str:
    """Where the post file goes, site-relative.

    An import keeps the path it came from, but only when that path is inside
    `content_dir` and names no parent directory: `source_post` is data from
    a digest of main, and a path that escaped the content directory would let
    a build write outside the scratch tree. `content_dir` is the site's own
    `contentdir` (ADR 017, `hugo config`'s answer, e.g. `"content"`), read by
    the caller from the last digest's conventions
    (`digest.read_content_dir_from_state`); `None` (the default, and every
    call site before this parameter existed) keeps the pre-ADR-017 hardcoded
    `content/posts` (issue #18: a site whose archive lives outside
    `content/posts` used to fall through here and land a duplicate file
    instead of updating the one already on main). A trailing slash on
    `content_dir` is stripped before the prefix match: `hugo config` can
    report a `contentDir` written with one in `hugo.toml`, and the match
    below is a plain string prefix, not a path join, so an unstripped
    slash would make every digest-created record on that site fail to
    match its own directory and duplicate on every republish, exactly
    what this parameter exists to stop.

    A brand-new draft (no `source_post`) is written into `new_post_dir`
    (issue #21): the section directory the site's own posts live in, which
    the caller reads from the last digest (`digest.read_new_post_dir_from_state`).
    It is a different kind of value from `content_dir`: that one is the
    content ROOT (`content`) and is only ever a prefix to match, while this
    is where a file is created, so it names the section (`content/posts`)
    and is never derived from `content_dir` here. `None` (a caller that has
    no digest state, and every call site before this parameter existed) keeps
    `POSTS_DIR`, and so does a value that is not a safe relative directory:
    the state file is data from a digest of main, not something a path may
    be built from unchecked.
    """
    base = (content_dir or POSTS_DIR).rstrip("/")
    source = (draft.source_post or {}).get("path")
    if source and _is_safe_relative(source) and source.startswith(f"{base}/"):
        return source
    target = new_post_dir.rstrip("/") if new_post_dir else ""
    if not target or not _is_safe_relative(target):
        target = POSTS_DIR
    return f"{target}/{post_filename(draft, slug)}"


def post_url(draft: Draft, slug: str) -> str:
    existing = draft.frontmatter.get("url")
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    # ADR 017: this `/YYYY/MM/slug/` default stays convention, not derived.
    # Scott's own site has `permalinks: null` in its Hugo config, so there
    # is nothing there to read this from; it is an observed pattern from
    # the real posts, and an author who wants something else sets `url`
    # by hand, which this function then leaves alone.
    stamp = post_date(draft.frontmatter)
    return f"/{stamp.year:04d}/{stamp.month:02d}/{slug}/"


# A browser normalises a URL path's dot segments before it ever requests
# anything, and the WHATWG URL Standard treats a lone percent-encoded "%2e"
# as a single-dot segment and "..", ".%2e", "%2e.", "%2e%2e" (case-insensitive)
# as a double-dot one, the same as their literal forms. Filtering only the
# literal "." and ".." here would let a crafted `%2e%2e` segment survive into
# the built href and have the *browser* collapse it back to ".." on
# navigation, walking the link out of `/preview/<slug>/` to another path on
# the same host (found in review: `url_problem` only judges a url's last
# segment, so an earlier one can carry this).
_SINGLE_DOT_SEGMENTS = frozenset({".", "%2e"})
_DOUBLE_DOT_SEGMENTS = frozenset({"..", ".%2e", "%2e.", "%2e%2e"})


def preview_post_url(preview_base: str, post_url_value: str) -> str:
    """The post's own URL inside the preview site, from the preview site's
    root (`preview_base`, e.g. `https://x/preview/<slug>/`) and the post's
    own `url` (this module's `post_url` output, already written into the
    converted frontmatter).

    `post_url_value` is untrusted past what `url_problem` catches: that check
    only judges a url's last path segment (ADR 015), so a hand-set
    frontmatter `url` can still carry a scheme, a host, or a dot segment
    (literal or percent-encoded) anywhere else in it. Only the path is ever
    used, and every dot segment is dropped, so a crafted url can never make
    the built link leave the preview site.
    """
    path = urlsplit(post_url_value.strip()).path
    segments = [
        part
        for part in path.split("/")
        if part
        and part.lower() not in _SINGLE_DOT_SEGMENTS
        and part.lower() not in _DOUBLE_DOT_SEGMENTS
    ]
    base = preview_base.rstrip("/")
    return f"{base}/{'/'.join(segments)}/" if segments else f"{base}/"


def _last_segment(url: str) -> str | None:
    segments = [part for part in url.strip().split("/") if part]
    return segments[-1] if segments else None


def _segment_problem(segment: str) -> str | None:
    """Why `segment` cannot be one directory name under static/images/, or None.

    The directory on disk and the image URL written into the body are the
    same string (`image_site_path` and `image_url` both take the name as it
    is), and a browser or static host decodes a URL before it looks the file
    up. So a name is usable only if decoding it, and reading it as a URL, are
    both no-ops: a percent sign is refused outright (`my%20post` would be a
    directory literally named `my%20post` that the URL `/images/my%20post/`
    never reaches, since that decodes to `my post`; `%2e%2e` is `..` by the
    same decoding), and so are the characters that end or split a URL path
    (`?`, `#`) and whitespace (a reference in a body stops at it). A
    backslash is a separator on some platforms and in browsers' URL parsing,
    so it is never part of a name either. ADR 015, amended 2026-09-19 (issue 46).
    """
    if segment != segment.strip():
        return f"{segment!r} has leading or trailing whitespace"
    if segment in (".", ".."):
        return f"{segment!r} is a relative path segment, not a directory name"
    if segment.lower() == ".git":
        return f"{segment!r} is a git metadata name that a tree cannot contain"
    if "/" in segment or "\\" in segment:
        return f"{segment!r} contains a path separator"
    if any(ord(char) < 32 or ord(char) == 127 for char in segment):
        return f"{segment!r} contains a control character"
    if "%" in segment:
        return (
            f"{segment!r} contains a percent sign; the directory on disk and the image"
            " URL written for it must be the same string, so an encoded name is not accepted"
        )
    if any(char.isspace() for char in segment):
        return f"{segment!r} contains whitespace, which an image reference in a body cannot carry"
    if "?" in segment or "#" in segment:
        return f"{segment!r} contains a character that ends a URL path"
    return None


def usable_image_dir(name: str | None) -> bool:
    """True when `name` is a single plain directory name (ADR 015)."""
    return bool(name) and _segment_problem(name or "") is None


def url_problem(url: str | None) -> str | None:
    """Why a post's `url` cannot name its image directory, or None.

    A blank url, or one with no segments (`/`), is not a problem: there is no
    segment to misuse and the pinned slug names the directory. This is what
    `Store.save_draft` refuses with 422 `frontmatter_url_invalid`.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    segment = _last_segment(url)
    return None if segment is None else _segment_problem(segment)


def image_dir_name(url: str | None, fallback_slug: str) -> str:
    """ADR 015: the URL's last non-empty path segment, or the pinned slug.

    `url` is a post's own frontmatter value (an import keeps the one the
    real post already used; a new draft may not have one yet), so this is
    the one place both `_fill_from_post` and `_pin_slug` in `store.py` call
    to agree on the same directory name a draft is going to keep for life.
    A last segment that is not a plain directory name (`..`, `.`, a
    backslash, a percent sign, whitespace, `?` or `#`: see `_segment_problem`)
    yields the fallback slug instead;
    a save refuses such a url up front (`url_problem`), so this only fires
    for a draft saved before that, or an import from main.
    """
    if isinstance(url, str) and url.strip():
        segment = _last_segment(url)
        if segment is not None and _segment_problem(segment) is None:
            return segment
    return fallback_slug


def image_site_path(
    image_dir: str, filename: str, static_images_dir: str = STATIC_IMAGES_DIR
) -> str:
    return f"{static_images_dir}/{image_dir}/{PurePosixPath(filename).name}"


def image_url(image_dir: str, filename: str) -> str:
    return f"/images/{image_dir}/{PurePosixPath(filename).name}"


def _is_safe_relative(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(parts) and not path.startswith("/") and ".." not in parts


def _disambiguated_filename(filename: str, image_id: str) -> str:
    """`filename` with a short slice of `image_id` inserted before the suffix.

    `image_id` is the image's own content sha256, so two images that reach
    here with the same filename are guaranteed different content: this is
    the only thing that can tell their output paths apart.
    """
    path = PurePosixPath(filename)
    return f"{path.stem}-{image_id[:8]}{path.suffix}"


def placements(
    draft: Draft, image_dir: str, static_images_dir: str = STATIC_IMAGES_DIR
) -> list[ImagePlacement]:
    """Where each attached image lands, deduplicating a repeated filename.

    `attach_image` (`chronicle/api/store.py`) already rejects attaching a
    second image under a filename the draft already has, so this is a
    defensive fallback, not the primary guard: a draft written before that
    check existed, or by anything that writes `Draft.images` directly, could
    still carry two entries with the same filename and different content. A
    plain second occurrence would collide with the first at
    `<static_images_dir>/<image_dir>/<filename>` and silently overwrite it
    (round C3 review); a repeated name here gets its image id worked into the
    output path instead.

    `static_images_dir` is site-relative (`static/images` by default, ADR
    017's `staticdir` plus the `images` subdirectory when a caller passes
    it); the public `url` a reference is rewritten to never carries this
    prefix, since Hugo publishes everything under its static directory to
    the site root regardless of what that directory is named.
    """
    seen: dict[str, int] = {}
    placed: list[ImagePlacement] = []
    for image in draft.images:
        name = PurePosixPath(image.filename).name
        seen[name] = seen.get(name, 0) + 1
        output_name = name if seen[name] == 1 else _disambiguated_filename(name, image.image_id)
        placed.append(
            ImagePlacement(
                image_id=image.image_id,
                filename=name,
                role=image.role,
                site_path=image_site_path(image_dir, output_name, static_images_dir),
                url=image_url(image_dir, output_name),
            )
        )
    return placed


def _rewrite_map(
    draft: Draft, placed: list[ImagePlacement]
) -> tuple[dict[str, str], dict[str, str]]:
    """(by source_ref, by filename): the two ways a reference is recognised."""
    by_ref: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for image, placement in zip(draft.images, placed, strict=True):
        if image.source_ref:
            by_ref[image.source_ref.strip()] = placement.url
        by_name[placement.filename] = placement.url
    return by_ref, by_name


def rewrite_reference(ref: str, by_ref: dict[str, str], by_name: dict[str, str]) -> str:
    """One reference, rewritten to its preview URL, or left exactly as it was.

    An absolute http(s) reference is never touched: it points at something
    the site does not own. Everything else is matched first on the whole
    reference (the `source_ref` an import recorded, query string and fragment
    included) and then on its filename, which is what catches an image
    uploaded fresh to a draft that has no source reference at all.
    """
    candidate = ref.strip()
    if candidate.startswith(("http://", "https://", "//", "data:", "mailto:")):
        return ref
    if candidate in by_ref:
        return by_ref[candidate]
    bare = candidate.split("#", 1)[0].split("?", 1)[0]
    if bare in by_ref:
        return by_ref[bare]
    name = PurePosixPath(bare).name
    if name in by_name:
        return by_name[name]
    return ref


def rewrite_body(body: str, by_ref: dict[str, str], by_name: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        return match.group(1) + rewrite_reference(match.group(2), by_ref, by_name)

    rewritten = _MARKDOWN_IMAGE_REF.sub(replace, body)
    return _HTML_IMG_REF.sub(replace, rewritten)


def render_frontmatter(frontmatter: dict[str, Any]) -> str:
    """YAML frontmatter in allowlist order, one key per line.

    Order is the allowlist's order (ADR 007), not insertion order and not
    alphabetical, so two saves of the same draft, or a preview and the
    publish that follows it, produce a byte-identical file. Values go through
    the YAML dumper rather than an f-string so a title with a colon, a date
    that must stay a string, or a tag list all survive the round trip.
    """
    lines: list[str] = []
    for key in FRONTMATTER_ALLOWLIST:
        if key not in frontmatter:
            continue
        dumped = yaml.safe_dump(
            {key: frontmatter[key]}, sort_keys=False, default_flow_style=False, allow_unicode=True
        )
        lines.append(dumped.rstrip("\n"))
    # A key outside the allowlist cannot reach a saved draft (`check_frontmatter`
    # rejects it) and is dropped by a `from_post` import with a warning, so
    # anything left here is a record written by an older build: drop it rather
    # than hand Hugo a field the site does not know.
    return "\n".join(lines)


def convert(
    draft: Draft,
    static_dir: str | None = None,
    content_dir: str | None = None,
    new_post_dir: str | None = None,
) -> ConvertedPost:
    """The draft as a Hugo post file, plus where its images have to land.

    `static_dir` is the site's own `staticdir` (ADR 017, `hugo config`'s
    answer, e.g. `"static"`), read by the caller from the last digest's
    conventions (`digest.read_static_dir_from_state`) or supplied directly
    when it already has a fresh `HugoConventions`; `None` (the default, and
    every call site before this parameter existed) keeps the pre-ADR-017
    hardcoded `static/images`. `content_dir` is the same shape for the
    site's own `contentdir` (`digest.read_content_dir_from_state`), passed
    through to `post_path` so a digest-created record's source path is
    matched against the site's real content directory rather than a
    hardcoded `content/posts` (issue #18); `None` keeps the same
    pre-ADR-017 fallback. `new_post_dir` is where a brand-new post is
    written (`digest.read_new_post_dir_from_state`, issue #21); `None` keeps
    `content/posts`.
    """
    slug = draft.slug
    if not slug:
        raise ConversionError(
            f"draft {draft.id} has no pinned slug; a slug is pinned at the first preview or publish"
        )
    if not draft.frontmatter.get("title"):
        raise ConversionError(f"draft {draft.id} has no title in its frontmatter")

    # `draft.image_dir` is pinned once, at the same moment the slug is
    # (ADR 015); this fallback only fires for a draft written before that
    # field existed, or a test that builds a Draft by hand.
    # A pinned value that is not a plain directory name (a draft that pinned
    # `..` before issue 28's fix) is never trusted: nothing valid was ever
    # written under it, so the derived name stands in.
    image_dir = (
        draft.image_dir
        if draft.image_dir and usable_image_dir(draft.image_dir)
        else image_dir_name(draft.frontmatter.get("url"), slug)
    )
    static_images_dir = f"{static_dir}/images" if static_dir else STATIC_IMAGES_DIR
    placed = placements(draft, image_dir, static_images_dir)
    by_ref, by_name = _rewrite_map(draft, placed)

    frontmatter = dict(draft.frontmatter)
    frontmatter["draft"] = False
    frontmatter["url"] = post_url(draft, slug)
    # A draft normally has its date stamped at the slug pin (ADR 022) or at
    # first publish (`store.record_publish_result`), both before this ever
    # runs; this only fires for a draft built by hand (a test) or written
    # before either stamp existed. `post_date` is what `url` above and
    # `post_filename` already use, so this never disagrees with them.
    if not frontmatter.get("date"):
        frontmatter["date"] = post_date(draft.frontmatter).isoformat()
    for key in IMAGE_FRONTMATTER_KEYS:
        value = frontmatter.get(key)
        if isinstance(value, str) and value.strip():
            frontmatter[key] = rewrite_reference(value, by_ref, by_name)

    body = rewrite_body(draft.body, by_ref, by_name)
    text = f"---\n{render_frontmatter(frontmatter)}\n---\n{body}"
    if not text.endswith("\n"):
        text += "\n"

    return ConvertedPost(
        slug=slug,
        post_path=post_path(draft, slug, content_dir, new_post_dir),
        text=text,
        url=frontmatter["url"],
        images=placed,
    )
