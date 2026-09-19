"""The Import page's date column goes through `local_time` like every other stamp (#27).

A bare `YYYY-MM-DD` frontmatter date has no instant, so it stays its own date;
a full stamp is shown as local clock time. Both stamps are far from today so the
rendered string does not depend on when the test runs.
"""

from __future__ import annotations

from typing import Any

from chronicle.api import ui_templates as tpl
from chronicle.api.pagination import paginate


def _import_html(*dates: str) -> str:
    posts: list[dict[str, Any]] = [
        {"slug": f"post-{i}", "title": f"Post {i}", "date": date} for i, date in enumerate(dates)
    ]
    return tpl.import_page(
        paginate(posts, 1), "", banner=False, posts_total=len(posts), untracked_total=len(posts)
    )


def test_a_date_only_value_renders_unshifted() -> None:
    html = _import_html("2026-07-27")
    assert '<td class="lat-num">2026-07-27</td>' in html


def test_a_full_stamp_renders_as_local_clock_time() -> None:
    # 03:30 UTC on 2020-07-08 is 22:30 CDT the evening before, in Chicago.
    html = _import_html("2020-07-08T03:30:00+00:00")
    assert '<td class="lat-num">2020-07-07 22:30 CDT</td>' in html
    assert "2020-07-08T03:30:00" not in html


def test_a_space_separated_utc_stamp_from_a_real_blog_is_converted() -> None:
    html = _import_html("2005-06-10 17:37:40+00:00")
    assert '<td class="lat-num">2005-06-10 12:37 CDT</td>' in html
