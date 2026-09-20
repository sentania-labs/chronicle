"""The one local-time helper every UI template renders stamps through."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from chronicle.api.deps import Services
from chronicle.api.ui_time import local_time, parse_stamp, sort_key

CHICAGO = ZoneInfo("America/Chicago")


def test_utc_stamp_renders_as_chicago_clock_time_with_zone_abbreviation() -> None:
    now = datetime(2026, 9, 18, 20, 0, tzinfo=UTC)
    assert local_time("2026-09-18T19:52:54+00:00", now=now, zone=CHICAGO) == "14:52 CDT"


def test_stamp_from_an_earlier_local_day_carries_its_date() -> None:
    now = datetime(2026, 9, 18, 20, 0, tzinfo=UTC)
    assert local_time("2026-09-17T19:52:54+00:00", now=now, zone=CHICAGO) == "2026-09-17 14:52 CDT"


def test_stamp_that_is_tomorrow_in_utc_but_today_in_chicago_has_no_date() -> None:
    """02:30 UTC on the 18th is 21:30 CDT on the 17th. With the wall clock at
    22:00 CDT on the 17th (03:00 UTC on the 18th) that is still today, and
    the UTC date must not leak through."""
    now = datetime(2026, 9, 18, 3, 0, tzinfo=UTC)
    assert local_time("2026-09-18T02:30:00+00:00", now=now, zone=CHICAGO) == "21:30 CDT"


def test_same_stamp_a_day_later_shows_the_chicago_date_not_the_utc_date() -> None:
    now = datetime(2026, 9, 18, 15, 0, tzinfo=UTC)
    assert local_time("2026-09-18T02:30:00+00:00", now=now, zone=CHICAGO) == "2026-09-17 21:30 CDT"


def test_winter_stamp_uses_standard_time() -> None:
    now = datetime(2026, 9, 18, 15, 0, tzinfo=UTC)
    assert local_time("2026-01-05T18:00:00Z", now=now, zone=CHICAGO) == "2026-01-05 12:00 CST"


@pytest.mark.parametrize("bad", [None, "", "   ", "not a date", "2026-13-45T99:99:99", 12345])
def test_missing_or_garbage_stamp_renders_a_dash_without_raising(bad: object) -> None:
    assert local_time(bad) == "-"


def test_naive_stamp_is_taken_as_utc() -> None:
    parsed = parse_stamp("2026-09-18T19:52:54")
    assert parsed is not None
    assert parsed.utcoffset() is not None
    assert parsed.astimezone(UTC).hour == 19


def test_sort_key_puts_garbage_before_every_real_stamp() -> None:
    stamps = ["2026-09-18T10:00:00+00:00", "garbage", "2026-09-17T23:00:00-05:00", None]
    ordered = sorted(stamps, key=sort_key)
    assert ordered[-2:] == ["2026-09-17T23:00:00-05:00", "2026-09-18T10:00:00+00:00"]
    assert set(ordered[:2]) == {"garbage", None}


def test_timezone_is_overridable_by_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 9, 18, 20, 0, tzinfo=UTC)
    monkeypatch.setenv("CHRONICLE_UI_TIMEZONE", "Europe/London")
    assert local_time("2026-09-18T19:52:54+00:00", now=now) == "20:52 BST"


def test_unknown_timezone_name_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 9, 18, 20, 0, tzinfo=UTC)
    monkeypatch.setenv("CHRONICLE_UI_TIMEZONE", "Mars/Olympus_Mons")
    assert local_time("2026-09-18T19:52:54+00:00", now=now) == "14:52 CDT"


def test_editor_and_board_render_local_time_not_utc(client: TestClient, services: Services) -> None:
    draft, _ = services.store.create_draft("scott")
    draft.updated_at = "2020-01-02T03:04:05+00:00"
    services.store._write_json(services.store._draft_path(draft.id), draft.model_dump(mode="json"))
    services.store.index.upsert_draft(draft)

    board = client.get("/content/drafts")
    assert "2020-01-01 21:04 CST" in board.text
    assert "2020-01-02T03:04:05+00:00" not in board.text

    editor = client.get(f"/content/drafts/{draft.id}")
    assert "+00:00" not in editor.text


def test_out_of_range_stamp_renders_a_dash_instead_of_raising() -> None:
    """Shifting a stamp near year 1 into a zone west of UTC overflows."""
    assert local_time("0001-01-01T00:00:00+00:00", zone=CHICAGO) == "-"
    assert local_time("9999-12-31T23:59:59+00:00", zone=ZoneInfo("Asia/Tokyo")) == "-"


def test_date_only_stamp_renders_its_own_date_not_the_day_before() -> None:
    now = datetime(2026, 9, 18, 20, 0, tzinfo=UTC)
    assert local_time("2026-08-01", now=now, zone=CHICAGO) == "2026-08-01"


def test_naive_datetime_is_still_taken_as_utc() -> None:
    now = datetime(2026, 9, 18, 20, 0, tzinfo=UTC)
    assert local_time("2026-08-01T00:30:00", now=now, zone=CHICAGO) == "2026-07-31 19:30 CDT"


# Wiring: each template call site must render through `local_time`. Every stamp
# below is distinct and far from today, so a call site reverted to the raw ISO
# value fails on both the rendered string and the absent raw stamp.

BOARD_STAMP = "2020-01-02T03:04:05+00:00"
FEEDBACK_STAMP = "2020-02-03T04:05:06+00:00"
VERSION_STAMP = "2020-03-04T05:06:07+00:00"
RUN_STAMP = "2020-04-05T06:07:08+00:00"
CLAIM_STAMP = "2020-05-06T07:08:09+00:00"
BUILT_STAMP = "2020-06-07T08:09:10+00:00"
STARTED_STAMP = "2020-07-08T09:10:11+00:00"
FINISHED_STAMP = "2020-07-08T09:12:13+00:00"


def _assert_local(html: str, raw: str, rendered: str) -> None:
    assert rendered in html
    assert raw not in html
    assert raw.split("+")[0] not in html


def _draft_dict(services: Services) -> dict[str, object]:
    draft, _ = services.store.create_draft("scott")
    dumped: dict[str, object] = draft.model_dump(mode="json")
    return dumped


def test_board_card_renders_updated_as_local_time(services: Services) -> None:
    from chronicle.api import ui_templates as tpl
    from chronicle.api.pagination import paginate

    draft = _draft_dict(services)
    draft["updated_at"] = BOARD_STAMP
    row: dict[str, Any] = {
        "draft": draft,
        "last_author": "scott",
        "run_info": None,
        "flags": [],
        "came_back": False,
    }
    html = tpl.drafts_board_page([row], paginate([], 1), status_filter=None, q="", banner=False)
    _assert_local(html, BOARD_STAMP, "updated: 2020-01-01 21:04 CST")


def test_editor_renders_feedback_versions_run_and_claim_as_local_time(
    services: Services,
) -> None:
    from chronicle.api import ui_templates as tpl

    draft = _draft_dict(services)
    draft["claim"] = {"author": "scott", "since": CLAIM_STAMP}
    versions = [{"version_no": 1, "author": "scott", "created_at": VERSION_STAMP, "message": ""}]
    feedback = [
        {
            "version_no": 1,
            "action": "request_revision",
            "author": "scott",
            "created_at": FEEDBACK_STAMP,
            "text": "tighten it",
        }
    ]
    run = {
        "id": "run1",
        "kind": "preview",
        "status": "succeeded",
        "created_at": RUN_STAMP,
        "started_at": None,
        "finished_at": None,
    }
    html = tpl.editor_page(draft, versions, feedback, run, None, banner=False)
    _assert_local(html, CLAIM_STAMP, "since 2020-05-06 02:08 CDT")
    _assert_local(html, VERSION_STAMP, "<td>2020-03-03 23:06 CST</td>")
    _assert_local(html, FEEDBACK_STAMP, "at 2020-02-02 22:05 CST")
    _assert_local(html, RUN_STAMP, "succeeded at 2020-04-05 01:07 CDT")


def test_run_log_and_preview_list_render_stamps_as_local_time() -> None:
    from chronicle.api import ui_templates as tpl

    run = {
        "id": "run1",
        "kind": "preview",
        "status": "succeeded",
        "started_at": STARTED_STAMP,
        "finished_at": FINISHED_STAMP,
    }
    log = tpl.run_log_page(run, "", banner=False)
    _assert_local(log, STARTED_STAMP, "started: 2020-07-08 04:10 CDT")
    _assert_local(log, FINISHED_STAMP, "finished: 2020-07-08 04:12 CDT")

    rows = [
        {
            "draft_id": "d1",
            "title": "T",
            "preview_url": "/preview/t/",
            "built_at": BUILT_STAMP,
            "wall_seconds": 1.5,
            "toolchain_drift": False,
        }
    ]
    listing = tpl.preview_list_page(rows, banner=False)
    _assert_local(listing, BUILT_STAMP, "<td>2020-06-07 03:09 CDT</td>")


def test_submissions_list_renders_created_as_local_time(services: Services) -> None:
    from chronicle.api import ui_templates as tpl
    from chronicle.api.pagination import paginate

    stamp = "2020-08-09T10:11:12+00:00"
    row: dict[str, Any] = {
        "id": "s1",
        "brief": "a brief",
        "status": "new",
        "from_": "agent",
        "created_at": stamp,
        "image_ids": [],
        "claimed_by": None,
    }
    html = tpl.submissions_list_page(paginate([row], 1), banner=False)
    _assert_local(html, stamp, "<td>2020-08-09 05:11 CDT</td>")
