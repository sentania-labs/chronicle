"""Lattice on both surfaces: stylesheet order, no second look, theme, tabs, tones.

The skin is checked here as markup and as files, because the rendered result is
checked live (docs/screenshots/lattice-skin/). What can be held without a
browser is held: the documented stylesheet order, that Admin lost its inline
block, that the theme script is a same-origin blocking script the
Content-Security-Policy will run, that the current tab is marked, that every
status has a tone, and that `style.css` uses Lattice tokens rather than hex.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from chronicle.api import admin_templates, ui_templates
from chronicle.api.models import DRAFT_STATUSES
from chronicle.api.pagination import paginate
from chronicle.api.ui_status import STATUS_TONES, status_tone

STATIC = Path(__file__).parent.parent / "chronicle" / "api" / "static"
TOKENS = ("/static/vendor/tokens.css", "/static/vendor/lattice.css", "/static/style.css")


def _head(html: str) -> str:
    return html.split("</head>")[0]


def _assert_lattice_order(html: str) -> None:
    head = _head(html)
    positions = [head.index(f'href="{href}"') for href in TOKENS]
    assert positions == sorted(positions), "tokens, then lattice, then style.css"
    # The theme script must run before anything paints, so it precedes them all.
    theme = head.index('<script src="/static/theme.js"></script>')
    assert theme < positions[0]
    assert "<style" not in html, "no inline stylesheet: one look, not two"
    # The CSP is `default-src 'self'`: an inline script would be refused.
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)


def _board() -> str:
    return ui_templates.drafts_board_page(
        [], paginate([], 1), status_filter=None, q="", banner=True
    )


def test_ui_pages_link_the_stylesheets_in_the_documented_order() -> None:
    _assert_lattice_order(_board())
    _assert_lattice_order(ui_templates.submissions_list_page(paginate([], 1), banner=False))


def test_the_editor_keeps_easymde_between_lattice_and_style_css() -> None:
    head = _head(ui_templates.page("t", "", banner=False, editor=True))
    order = [
        head.index(f'href="{href}"')
        for href in (
            "/static/vendor/lattice.css",
            "/static/vendor/easymde.min.css",
            "/static/style.css",
        )
    ]
    assert order == sorted(order)


def test_admin_pages_wear_the_same_stylesheets_and_have_no_inline_block(
    admin_client: TestClient,
) -> None:
    for path in ("/admin", "/admin/tokens", "/admin/backup", "/admin/password"):
        response = admin_client.get(path)
        assert response.status_code == 200, path
        _assert_lattice_order(response.text)
    _assert_lattice_order(admin_templates.login_page())
    assert not hasattr(admin_templates, "STYLE")


def test_the_current_tab_is_marked_and_only_that_one() -> None:
    html = _board()
    assert html.count("is-on") == 1
    assert '<a class="lat-tab is-on" href="/content/drafts" aria-current="page">Posts</a>' in html
    imp = ui_templates.page("t", "", banner=False, active=ui_templates.IMPORT_TAB)
    assert 'class="lat-tab is-on" href="/content/import"' in imp
    assert "is-on" not in ui_templates.page("t", "", banner=False)


def test_admin_marks_its_current_tab_and_puts_log_out_in_the_header(
    admin_client: TestClient,
) -> None:
    html = admin_client.get("/admin/tokens").text
    assert 'class="lat-tab is-on" href="/admin/tokens" aria-current="page"' in html
    assert html.count("is-on") == 1
    assert html.index("Log out") < html.index('class="lat-tabs"')
    assert "Log out" not in admin_templates.login_page()


def test_the_theme_control_ships_hidden_until_the_script_can_use_it() -> None:
    html = _board()
    assert re.search(r'<button[^>]*id="theme-toggle"[^>]*\bhidden\b', html)


def test_the_theme_script_is_served_to_the_browser(client: TestClient) -> None:
    response = client.get("/static/theme.js")
    assert response.status_code == 200
    assert "data-theme" in response.text
    assert "chronicle-theme" in response.text


def test_the_internal_only_banner_is_a_warning_and_a_notice_keeps_its_hook() -> None:
    html = ui_templates.page("t", "", banner=True, notice="nope", notice_kind="error")
    assert 'class="banner lat-banner lat-banner--warn"' in html
    assert 'class="notice error lat-banner lat-banner--bad"' in html
    conflict = ui_templates.page("t", "", banner=False, notice="x", notice_kind="conflict")
    assert 'class="notice conflict lat-banner lat-banner--warn"' in conflict


def test_every_status_has_a_tone_and_only_meaningful_ones_carry_colour() -> None:
    assert set(STATUS_TONES) == set(DRAFT_STATUSES)
    assert set(STATUS_TONES.values()) <= {"", "ok", "warn", "bad"}
    assert status_tone("published") == "ok"
    assert status_tone("revision_requested") == "warn"
    assert status_tone("rejected") == "bad"
    # In-progress statuses stay neutral: colour would be decoration.
    for neutral in ("drafting", "in_review", "previewed", "approved", "unpublished"):
        assert status_tone(neutral) == ""
    assert status_tone("not-a-status") == ""


def test_style_css_uses_lattice_tokens_and_no_hardcoded_colour() -> None:
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", code), "a hex colour breaks when the theme flips"
    assert not re.search(r"\brgba?\(", code)
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", (STATIC / "vendor" / "tokens.css").read_text()))
    defined |= set(re.findall(r"(--[a-z0-9-]+)\s*:", code))
    used = set(re.findall(r"var\((--[a-z0-9-]+)\)", code))
    assert used <= defined, f"style.css uses a token that does not exist: {sorted(used - defined)}"
