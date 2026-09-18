"""The one local-time helper every UI template renders stamps through."""

from __future__ import annotations

from datetime import UTC, datetime
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
