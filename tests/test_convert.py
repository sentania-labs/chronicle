"""convert.py: frontmatter order, filename/url rules, image reference rewriting."""

from __future__ import annotations

from urllib.parse import unquote

import pytest

from chronicle.api import convert
from chronicle.api.models import Draft, DraftImage, now_stamp


def _draft(**overrides: object) -> Draft:
    defaults: dict[str, object] = {
        "id": "d1",
        "created_at": now_stamp(),
        "updated_at": now_stamp(),
        "slug": "my-post",
        "title": "My Post",
        "frontmatter": {"title": "My Post", "date": "2026-08-01T09:00:00-05:00"},
        "body": "hello",
        "images": [],
        "source_post": None,
    }
    defaults.update(overrides)
    return Draft.model_validate(defaults)


def test_frontmatter_is_rendered_in_allowlist_order_with_draft_forced_false() -> None:
    draft = _draft(frontmatter={"tags": ["a"], "title": "My Post", "date": "2026-08-01"})
    converted = convert.convert(draft)
    lines = [line for line in converted.text.splitlines() if ":" in line]
    keys = [line.split(":", 1)[0] for line in lines]
    assert keys.index("title") < keys.index("date") < keys.index("tags")
    assert "draft: false" in converted.text


def test_new_draft_gets_dated_filename_and_dated_url() -> None:
    draft = _draft()
    converted = convert.convert(draft)
    assert converted.post_path == "content/posts/2026-08-01-my-post.md"
    assert converted.url == "/2026/08/my-post/"


def test_new_draft_with_no_date_uses_today() -> None:
    draft = _draft(frontmatter={"title": "My Post"})
    converted = convert.convert(draft)
    assert converted.post_path.startswith("content/posts/")
    assert converted.post_path.endswith("-my-post.md")


def test_imported_draft_keeps_its_original_path_and_url() -> None:
    draft = _draft(
        frontmatter={"title": "My Post", "date": "2024-03-03", "url": "/2024/03/03/my-post/"},
        source_post={"slug": "my-post", "path": "content/posts/my-post.md", "sha": "abc"},
    )
    converted = convert.convert(draft)
    assert converted.post_path == "content/posts/my-post.md"
    assert converted.url == "/2024/03/03/my-post/"


def test_imported_draft_path_outside_posts_dir_is_not_trusted() -> None:
    draft = _draft(
        frontmatter={"title": "My Post", "date": "2024-03-03"},
        source_post={"slug": "x", "path": "../../etc/passwd", "sha": "abc"},
    )
    converted = convert.convert(draft)
    assert converted.post_path == "content/posts/2024-03-03-my-post.md"


def test_digested_record_outside_content_posts_republishes_as_update_not_duplicate() -> None:
    """Issue #18: a site whose real `contentdir` is not `content/posts` (a
    post-like section that moved elsewhere) must still have a digest-created
    record's republish land on the file it already came from, not a new one
    under the hardcoded `content/posts`."""
    draft = _draft(
        frontmatter={"title": "My Post", "date": "2024-03-03", "url": "/2024/03/03/my-post/"},
        source_post={"slug": "my-post", "path": "archive/my-post.md", "sha": "abc"},
    )
    converted = convert.convert(draft, None, "archive")
    assert converted.post_path == "archive/my-post.md"


def test_content_dir_with_trailing_slash_still_matches_source_path() -> None:
    """`hugo config` can report a `contentDir` written with a trailing slash
    in `hugo.toml`; the prefix match must not treat that as a different
    directory and duplicate the post."""
    draft = _draft(
        frontmatter={"title": "My Post", "date": "2024-03-03", "url": "/2024/03/03/my-post/"},
        source_post={"slug": "my-post", "path": "archive/my-post.md", "sha": "abc"},
    )
    converted = convert.convert(draft, None, "archive/")
    assert converted.post_path == "archive/my-post.md"


def test_traversal_outside_a_non_default_content_dir_is_not_trusted() -> None:
    """The `_is_safe_relative` guard must still hold when `content_dir` is a
    derived value rather than the hardcoded `content/posts`."""
    draft = _draft(
        frontmatter={"title": "My Post", "date": "2024-03-03"},
        source_post={"slug": "x", "path": "../../etc/passwd", "sha": "abc"},
    )
    converted = convert.convert(draft, None, "archive")
    assert converted.post_path == "content/posts/2024-03-03-my-post.md"


def test_digested_record_outside_content_posts_with_no_content_dir_still_duplicates() -> None:
    """The pre-fix behaviour, kept as a control: with no `content_dir`
    supplied (today's fallback, no digest state yet) a source path outside
    `content/posts` is still not trusted, and convert falls through to a
    brand new path."""
    draft = _draft(
        frontmatter={"title": "My Post", "date": "2024-03-03", "url": "/2024/03/03/my-post/"},
        source_post={"slug": "my-post", "path": "archive/my-post.md", "sha": "abc"},
    )
    converted = convert.convert(draft)
    assert converted.post_path == "content/posts/2024-03-03-my-post.md"


def test_missing_slug_raises() -> None:
    draft = _draft(slug=None)
    with pytest.raises(convert.ConversionError):
        convert.convert(draft)


def test_missing_title_raises() -> None:
    draft = _draft(frontmatter={"date": "2026-08-01"})
    with pytest.raises(convert.ConversionError):
        convert.convert(draft)


def test_markdown_image_reference_rewritten_by_source_ref() -> None:
    draft = _draft(
        body="see ![alt](content/posts/images/featured.png) here",
        images=[
            DraftImage(
                image_id="img1",
                filename="featured.png",
                role="feature",
                source_ref="content/posts/images/featured.png",
            )
        ],
    )
    converted = convert.convert(draft)
    assert "/images/my-post/featured.png" in converted.text
    assert "content/posts/images/featured.png" not in converted.text


def test_html_img_tag_reference_rewritten() -> None:
    draft = _draft(
        body='<img src="images/inline.png" alt="x">',
        images=[
            DraftImage(
                image_id="img2",
                filename="inline.png",
                role="inline",
                source_ref="images/inline.png",
            )
        ],
    )
    converted = convert.convert(draft)
    assert "/images/my-post/inline.png" in converted.text


def test_frontmatter_image_field_is_rewritten() -> None:
    draft = _draft(
        frontmatter={
            "title": "My Post",
            "date": "2026-08-01",
            "featureImage": "content/posts/images/featured.png",
        },
        images=[
            DraftImage(
                image_id="img1",
                filename="featured.png",
                role="feature",
                source_ref="content/posts/images/featured.png",
            )
        ],
    )
    converted = convert.convert(draft)
    assert "featureImage: /images/my-post/featured.png" in converted.text


def test_freshly_uploaded_image_with_no_source_ref_matches_by_filename() -> None:
    draft = _draft(
        body="![alt](fresh.png)",
        images=[DraftImage(image_id="img3", filename="fresh.png", role="inline", source_ref=None)],
    )
    converted = convert.convert(draft)
    assert "/images/my-post/fresh.png" in converted.text


def test_absolute_and_data_uri_references_are_left_alone() -> None:
    draft = _draft(
        body="![a](https://example.com/x.png) ![b](data:image/png;base64,abc)",
        images=[],
    )
    converted = convert.convert(draft)
    assert "https://example.com/x.png" in converted.text
    assert "data:image/png;base64,abc" in converted.text


def test_image_placements_point_at_static_images_slug() -> None:
    draft = _draft(
        images=[DraftImage(image_id="img4", filename="pic.png", role="inline", source_ref=None)]
    )
    converted = convert.convert(draft)
    assert converted.images[0].site_path == "static/images/my-post/pic.png"
    assert converted.images[0].url == "/images/my-post/pic.png"


def test_static_dir_argument_replaces_the_hardcoded_static_prefix() -> None:
    """ADR 017: `staticdir` (from a site's own `hugo config`) replaces the
    hardcoded `static` prefix when a caller passes one; the public `url` a
    reference rewrites to is unaffected, since Hugo publishes everything
    under its static directory to the site root regardless of its name."""
    draft = _draft(
        images=[DraftImage(image_id="img4", filename="pic.png", role="inline", source_ref=None)]
    )
    converted = convert.convert(draft, "assets")
    assert converted.images[0].site_path == "assets/images/my-post/pic.png"
    assert converted.images[0].url == "/images/my-post/pic.png"


def test_static_dir_defaults_to_static_when_not_passed() -> None:
    draft = _draft(
        images=[DraftImage(image_id="img4", filename="pic.png", role="inline", source_ref=None)]
    )
    converted = convert.convert(draft, None)
    assert converted.images[0].site_path == "static/images/my-post/pic.png"


def test_image_dir_follows_url_not_dated_slug() -> None:
    """ADR 015: the image directory is the URL's last path segment, not
    draft.slug, when the two differ (an imported post whose digest slug
    kept its dated filename stem)."""
    draft = _draft(
        slug="2026-08-01-vcf-operations-can-now-see-my-unifi-network",
        image_dir="vcf-operations-can-now-see-my-unifi-network",
        frontmatter={
            "title": "VCF Operations Can Now See My UniFi Network",
            "url": "/2026/08/vcf-operations-can-now-see-my-unifi-network/",
        },
        images=[DraftImage(image_id="img1", filename="featured.png", role="feature")],
    )
    converted = convert.convert(draft)
    assert converted.images[0].site_path == (
        "static/images/vcf-operations-can-now-see-my-unifi-network/featured.png"
    )


def test_digested_post_body_still_publishes_the_site_path_not_a_chronicle_url() -> None:
    """The editor's live render resolves a site-path reference to Chronicle's
    own serving URL client-side only (editor.js), never in the stored body.
    Nothing here leaks that URL into what convert/publish actually writes for
    the blog PR: a body already carrying the published site path keeps it."""
    draft = _draft(
        slug="2026-08-01-vcf-operations-can-now-see-my-unifi-network",
        image_dir="vcf-operations-can-now-see-my-unifi-network",
        body="![The relationships!](/images/vcf-operations-can-now-see-my-unifi-network/image.png)",
        frontmatter={
            "title": "VCF Operations Can Now See My UniFi Network",
            "url": "/2026/08/vcf-operations-can-now-see-my-unifi-network/",
        },
        images=[
            DraftImage(
                image_id="img1",
                filename="image.png",
                role="inline",
                source_ref="/images/vcf-operations-can-now-see-my-unifi-network/image.png",
            )
        ],
    )
    converted = convert.convert(draft)
    assert "/images/vcf-operations-can-now-see-my-unifi-network/image.png" in converted.text
    assert "/content/drafts/" not in converted.text


def test_image_dir_name_helper() -> None:
    assert (
        convert.image_dir_name("/2026/08/vcf-operations-can-now-see-my-unifi-network/", "fallback")
        == "vcf-operations-can-now-see-my-unifi-network"
    )
    assert convert.image_dir_name(None, "fallback") == "fallback"
    assert convert.image_dir_name("   ", "fallback") == "fallback"


UNUSABLE_URLS = [
    "/a/..",
    "/a/../",
    "..",
    "/a/.",
    "/a/./",
    "/a/b\\c",
    "/a/..\\..",
    "/a/%2e%2e",
    "/a/%2E",
    "/a/b%2Fc",
    "/a/b%5Cc",
    "/a/b\x00c",
    "/a/.git",
    "/a/.GIT",
    "/a/b /",
    # Issue 46: an encoded name is not the string the URL resolves to.
    "/a/my%20post/",
    "/a/100%/",
    "/a/%41",
    "/a/my post/",
    "/a/a?b=1",
    "/a/a#top",
    "/a/a\tb",
]


@pytest.mark.parametrize("url", UNUSABLE_URLS)
def test_image_dir_name_falls_back_when_the_last_segment_is_not_a_plain_directory_name(
    url: str,
) -> None:
    """Issue 28: `..`, `.`, a backslash, and their percent-encoded forms are
    not a directory name; the pinned slug stands in, never a traversal segment."""
    assert convert.image_dir_name(url, "my-post") == "my-post"
    assert convert.url_problem(url) is not None


@pytest.mark.parametrize(
    "url", ["/2026/08/my-post/", "my-post", "/a/v1.2", "/a/index.html", "/a/h\u00e9llo/", "/a/.x"]
)
def test_image_dir_name_keeps_ordinary_segments(url: str) -> None:
    assert convert.url_problem(url) is None
    assert convert.image_dir_name(url, "fallback") == url.strip("/").split("/")[-1]


@pytest.mark.parametrize("url", UNUSABLE_URLS + ["/a/ok/", "/a/v1.2", "/a/.x", None, "/"])
def test_the_derived_image_dir_is_always_one_the_convert_trust_check_accepts(
    url: str | None,
) -> None:
    """`image_dir_name`, `url_problem` and `usable_image_dir` are one rule: the
    fallback can never hand back a value convert would then refuse to trust."""
    assert convert.usable_image_dir(convert.image_dir_name(url, "my-post"))


def test_convert_does_not_trust_a_pinned_traversal_image_dir() -> None:
    """A draft that pinned `..` before the fix: converting must not build
    static/images/../shot.png, so it uses the slug's directory instead."""
    draft = _draft(
        image_dir="..",
        frontmatter={"title": "My Post", "url": "/a/.."},
        images=[DraftImage(image_id="img1", filename="shot.png", role="inline")],
    )
    converted = convert.convert(draft)
    assert converted.images[0].site_path == "static/images/my-post/shot.png"
    assert converted.images[0].url == "/images/my-post/shot.png"


def test_two_attached_images_with_the_same_filename_get_distinct_output_paths() -> None:
    """Defensive fallback: the API rejects this at attach time, but a draft
    written some other way could still carry two images under one filename,
    and they must not collide at static/images/<slug>/<filename> (round C3
    review)."""
    draft = _draft(
        images=[
            DraftImage(image_id="aaaaaaaa1111", filename="pic.png", role="inline"),
            DraftImage(image_id="bbbbbbbb2222", filename="pic.png", role="feature"),
        ]
    )
    converted = convert.convert(draft)
    first, second = converted.images
    assert first.site_path == "static/images/my-post/pic.png"
    assert second.site_path == "static/images/my-post/pic-bbbbbbbb.png"
    assert first.site_path != second.site_path
    assert len({placement.site_path for placement in converted.images}) == 2


# Issue #21: a brand-new post is written into the site's own section
# directory, not the hardcoded `content/posts`. `new_post_dir` is where a file
# is created; `content_dir` stays the content root a source path is matched
# against, and the two are different kinds of value.


def test_new_post_lands_in_the_sites_section_directory() -> None:
    draft = _draft(frontmatter={"title": "My Post", "date": "2024-03-03"})
    converted = convert.convert(draft, None, "content2", "content2/blog")
    assert converted.post_path == "content2/blog/2024-03-03-my-post.md"


def test_new_post_with_no_site_directory_still_lands_in_content_posts() -> None:
    """A Chronicle that never read a site's conventions publishes where it always did."""
    draft = _draft(frontmatter={"title": "My Post", "date": "2024-03-03"})
    assert convert.convert(draft).post_path == "content/posts/2024-03-03-my-post.md"
    assert (
        convert.convert(draft, None, None, None).post_path == "content/posts/2024-03-03-my-post.md"
    )


@pytest.mark.parametrize("unsafe", ["../outside", "/etc", "a/../../b", ""])
def test_an_unsafe_new_post_dir_is_never_a_path(unsafe: str) -> None:
    draft = _draft(frontmatter={"title": "My Post", "date": "2024-03-03"})
    converted = convert.convert(draft, None, "content", unsafe)
    assert converted.post_path == "content/posts/2024-03-03-my-post.md"


def test_new_post_dir_trailing_slash_is_stripped() -> None:
    draft = _draft(frontmatter={"title": "My Post", "date": "2024-03-03"})
    assert (
        convert.convert(draft, None, "content", "content/posts/").post_path
        == "content/posts/2024-03-03-my-post.md"
    )


def test_an_imported_post_still_keeps_its_own_path_over_new_post_dir() -> None:
    draft = _draft(
        frontmatter={"title": "My Post", "date": "2024-03-03", "url": "/2024/03/03/my-post/"},
        source_post={"slug": "my-post", "path": "archive/my-post.md", "sha": "abc"},
    )
    converted = convert.convert(draft, None, "archive", "archive/blog")
    assert converted.post_path == "archive/my-post.md"


# Issue 46: the directory on disk and the image URL written into the body must
# resolve to the same place on the real site. A host decodes a URL before it
# looks the file up, so `static/images/my%20post/` is never reached by
# `/images/my%20post/x.png` (that decodes to `my post`).


def _decoded_url_path(url: str) -> str:
    return unquote(url)


@pytest.mark.parametrize(
    "url",
    ["/2026/08/my-post/", "my-post", "/a/v1.2", "/a/h\u00e9llo/", "/a/.x", "/a/a~b_c-d/", None],
)
def test_the_url_of_an_image_decodes_to_the_path_it_is_written_at(url: str | None) -> None:
    draft = _draft(
        frontmatter={"title": "My Post", "url": url} if url else {"title": "My Post"},
        images=[DraftImage(image_id="img1", filename="shot.png", role="inline")],
    )
    placed = convert.convert(draft).images[0]
    assert _decoded_url_path(placed.url) == "/" + placed.site_path.removeprefix("static/")


@pytest.mark.parametrize("segment", ["my%20post", "100%", "%41bc", "a?b", "a#b", "my post"])
def test_an_unresolvable_segment_never_names_the_image_directory(segment: str) -> None:
    draft = _draft(
        frontmatter={"title": "My Post", "url": f"/2026/09/{segment}/"},
        images=[DraftImage(image_id="img1", filename="shot.png", role="inline")],
        body="![x](shot.png)",
    )
    converted = convert.convert(draft)
    placed = converted.images[0]
    assert placed.site_path == "static/images/my-post/shot.png"
    assert placed.url == "/images/my-post/shot.png"
    assert "(/images/my-post/shot.png)" in converted.text
    assert segment not in placed.site_path and segment not in placed.url


def test_convert_does_not_trust_a_pinned_percent_encoded_image_dir() -> None:
    """A draft that pinned `my%20post` before the refusal: the images were
    written to a directory the URL never reached, so the derived name stands in
    and the body's references land where they resolve."""
    draft = _draft(
        image_dir="my%20post",
        frontmatter={"title": "My Post", "url": "/2026/09/my%20post/"},
        images=[DraftImage(image_id="img1", filename="shot.png", role="inline")],
    )
    placed = convert.convert(draft).images[0]
    assert placed.site_path == "static/images/my-post/shot.png"
    assert placed.url == "/images/my-post/shot.png"
