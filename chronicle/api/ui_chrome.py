"""The page chrome the UI and Admin share: stylesheets, header, tabs, notices.

Both surfaces wear Lattice (`static/vendor/tokens.css` and `lattice.css`, then
Chronicle's own `style.css` for what Lattice does not cover). This module is the
one place that decides the stylesheet order, the head script that picks the
theme, the header with its theme control, the tab strip, and how a notice kind
maps onto a Lattice banner, so the two `page()` functions cannot drift into two
looks. Render layer only: nothing here reads or changes a record.

Every value interpolated below has already been through `html.escape` by the
caller, or is a constant in this file.
"""

from __future__ import annotations

from html import escape

# Read the theme before the first paint, or the page flashes light and then
# flips. This has to be a synchronous script in <head>, ahead of the
# stylesheets, and it has to be an external file rather than inline: the
# Content-Security-Policy is `default-src 'self'` (main.py), so an inline
# script would be refused, and a `defer` or `async` one would run after the
# first paint. A same-origin blocking script in <head> is the one form that
# satisfies both. It is tiny, and it applies a stored choice, falling back to
# `prefers-color-scheme`.
THEME_SCRIPT = '<script src="/static/theme.js"></script>'

STYLESHEETS = (
    '<link rel="stylesheet" href="/static/vendor/tokens.css">'
    '<link rel="stylesheet" href="/static/vendor/lattice.css">'
)
# The one Chronicle stylesheet, last, so it wins over Lattice where it must.
OWN_STYLESHEET = '<link rel="stylesheet" href="/static/style.css">'

VIEWPORT = '<meta name="viewport" content="width=device-width, initial-scale=1">'

# A notice's kind is the word the route already passes ("ok", "error",
# "conflict", "warning"); this maps it to the Lattice banner that carries the
# same state. An unknown kind is a plain neutral banner rather than an error.
NOTICE_TONES = {"ok": "ok", "error": "bad", "conflict": "warn", "warning": "warn"}


def head(*, extra_stylesheets: str = "", scripts: str = "") -> str:
    """The <head> assets in the documented order: theme script, tokens, Lattice,
    any vendored sheet a page needs (EasyMDE), then `style.css` on top."""
    return f"{VIEWPORT}{THEME_SCRIPT}{STYLESHEETS}{extra_stylesheets}{OWN_STYLESHEET}{scripts}"


def banner_class(tone: str = "") -> str:
    return f"lat-banner lat-banner--{tone}" if tone else "lat-banner"


def badge(text: str, tone: str = "", *, attrs: str = "") -> str:
    """A Lattice badge holding plain `text` (escaped here). `attrs` is extra
    attributes for the tag, such as an id, and is trusted markup."""
    cls = f"lat-badge lat-badge--{tone}" if tone else "lat-badge"
    return f'<span class="{cls}"{attrs}>{escape(text)}</span>'


def notice(message: str, kind: str = "error") -> str:
    """A notice paragraph. `notice` and the kind stay as classes because
    `editor.js` reads the first `.notice` off a page it fetched."""
    tone = NOTICE_TONES.get(kind, "")
    return f'<p class="notice {escape(kind)} {banner_class(tone)}">{escape(message)}</p>'


def theme_toggle() -> str:
    """Hidden until `theme.js` runs: without script there is nothing for it to
    do, and a dead button is worse than none."""
    return (
        '<button type="button" id="theme-toggle" class="lat-btn lat-btn--ghost chr-theme" '
        "data-theme-toggle hidden>Theme</button>"
    )


def header(brand: str, *, trailing: str = "") -> str:
    """The one sticky header. `trailing` is markup for the right edge, beside
    the theme control (Admin's log out button)."""
    return (
        '<header class="lat-header">'
        f'<strong class="chr-brand">{escape(brand)}</strong>'
        f'<span class="chr-header-end">{trailing}{theme_toggle()}</span>'
        "</header>"
    )


def tabs(links: tuple[tuple[str, str], ...], active: str | None, *, label: str) -> str:
    """The tab strip. `active` is the href of the current tab, marked `.is-on`
    (and `aria-current`); an href that matches none marks none."""
    items = []
    for href, text in links:
        on = href == active
        current = ' aria-current="page"' if on else ""
        items.append(
            f'<a class="lat-tab{" is-on" if on else ""}" href="{escape(href)}"{current}>'
            f"{escape(text)}</a>"
        )
    return f'<nav class="lat-tabs" aria-label="{escape(label)}">{"".join(items)}</nav>'
