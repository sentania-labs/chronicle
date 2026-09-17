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
  `content/posts/<YYYY-MM-DD>-<slug>.md`, the pattern every post on main
  uses.
- **`url`.** An imported draft keeps the `url` in its own frontmatter. A new
  draft gets `/<YYYY>/<MM>/<slug>/`, the dominant pattern on main (a
  handful of older posts carry `/<YYYY>/<MM>/<DD>/<slug>/`; Chronicle does
  not reproduce that, and an author who wants it can set `url` by hand,
  which this module then leaves alone).
- **Images.** Every attached image is copied to
  `static/images/<image_dir>/<filename>` and every reference to it in the
  body or in a frontmatter image field is rewritten to
  `/images/<image_dir>/<filename>`. `image_dir` is the post's own URL slug
  (the last non-empty path segment of `url`), never the dated filename and
  never `draft.slug` directly when the two differ (ADR 015); it is pinned
  once on the draft (`Draft.image_dir`) at the same moment the slug is
  pinned, so a later save or republish never relocates the directory. A
  reference matches by its recorded `source_ref` (what the post itself used
  before the import) or, for an image that has none because it was uploaded
  fresh, by filename.

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

import yaml

from .models import FRONTMATTER_ALLOWLIST, Draft

POSTS_DIR = "content/posts"
STATIC_IMAGES_DIR = "static/images"

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
    return f"{post_date(draft.frontmatter).isoformat()}-{slug}.md"


def post_path(draft: Draft, slug: str) -> str:
    """Where the post file goes, site-relative.

    An import keeps the path it came from, but only when that path is inside
    `content/posts` and names no parent directory: `source_post` is data from
    a digest of main, and a path that escaped the content directory would let
    a build write outside the scratch tree.
    """
    source = (draft.source_post or {}).get("path")
    if source and _is_safe_relative(source) and source.startswith(f"{POSTS_DIR}/"):
        return source
    return f"{POSTS_DIR}/{post_filename(draft, slug)}"


def post_url(draft: Draft, slug: str) -> str:
    existing = draft.frontmatter.get("url")
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    stamp = post_date(draft.frontmatter)
    return f"/{stamp.year:04d}/{stamp.month:02d}/{slug}/"


def image_dir_name(url: str | None, fallback_slug: str) -> str:
    """ADR 015: the URL's last non-empty path segment, or the pinned slug.

    `url` is a post's own frontmatter value (an import keeps the one the
    real post already used; a new draft may not have one yet), so this is
    the one place both `_fill_from_post` and `_pin_slug` in `store.py` call
    to agree on the same directory name a draft is going to keep for life.
    """
    if isinstance(url, str) and url.strip():
        segments = [part for part in url.strip().split("/") if part]
        if segments:
            return segments[-1]
    return fallback_slug


def image_site_path(image_dir: str, filename: str) -> str:
    return f"{STATIC_IMAGES_DIR}/{image_dir}/{PurePosixPath(filename).name}"


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


def placements(draft: Draft, image_dir: str) -> list[ImagePlacement]:
    """Where each attached image lands, deduplicating a repeated filename.

    `attach_image` (`chronicle/api/store.py`) already rejects attaching a
    second image under a filename the draft already has, so this is a
    defensive fallback, not the primary guard: a draft written before that
    check existed, or by anything that writes `Draft.images` directly, could
    still carry two entries with the same filename and different content. A
    plain second occurrence would collide with the first at
    `static/images/<image_dir>/<filename>` and silently overwrite it (round
    C3 review); a repeated name here gets its image id worked into the
    output path instead.
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
                site_path=image_site_path(image_dir, output_name),
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


def convert(draft: Draft) -> ConvertedPost:
    """The draft as a Hugo post file, plus where its images have to land."""
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
    image_dir = draft.image_dir or image_dir_name(draft.frontmatter.get("url"), slug)
    placed = placements(draft, image_dir)
    by_ref, by_name = _rewrite_map(draft, placed)

    frontmatter = dict(draft.frontmatter)
    frontmatter["draft"] = False
    frontmatter["url"] = post_url(draft, slug)
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
        post_path=post_path(draft, slug),
        text=text,
        url=frontmatter["url"],
        images=placed,
    )
