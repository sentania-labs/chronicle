"""Filling a published post's public link into its announcements (issue #71).

Pure text handling, kept apart from the store so it can be tested on its own.
The writer cannot know the link while drafting (the url is pinned at first
preview or approve, ADR 022), so an announcement either marks the spot with
`LINK_PLACEHOLDER` or leaves it off; once the publish PR merges, the watcher
asks `fill_links` for the text with the link in place.
"""

from __future__ import annotations

LINK_PLACEHOLDER = "{link}"


def public_link(base_url: str, post_url: str) -> str:
    """The post's absolute URL: the site's `baseURL` joined with the
    site-relative url `convert.post_url` wrote (`/2026/09/slug/`)."""
    return base_url.rstrip("/") + "/" + post_url.lstrip("/")


def fill_links(
    announcements: dict[str, str], link: str, previous_link: str | None = None
) -> dict[str, str]:
    """Each announcement with `link` in it exactly where it belongs.

    An empty announcement stays empty. A `previous_link` (the one an earlier
    fill wrote, before the post's url changed) is replaced by `link`. Then
    every `{link}` becomes `link`; with no placeholder, and the link not
    already present, it is added on its own last line. Running this twice
    with the same link changes nothing the second time.
    """
    filled: dict[str, str] = {}
    for channel, text in announcements.items():
        if not text.strip():
            filled[channel] = text
            continue
        if previous_link and previous_link != link:
            text = text.replace(previous_link, link)
        if LINK_PLACEHOLDER in text:
            text = text.replace(LINK_PLACEHOLDER, link)
        elif link not in text:
            text = text.rstrip() + "\n" + link
        filled[channel] = text
    return filled
