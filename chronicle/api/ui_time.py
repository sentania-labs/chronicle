"""Local clock time for anything the UI shows a human.

The API and the on-disk records keep ISO stamps unchanged; this is render
layer only. One function, `local_time`, is what every UI template calls, so
the zone rule lives in exactly one place. The zone is read from
`CHRONICLE_UI_TIMEZONE` on each call (default America/Chicago) rather than
threaded through every template function; a name the system's tz database does
not know falls back to the default instead of failing a page render.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, tzinfo
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .settings import DEFAULT_UI_TIMEZONE, UI_TIMEZONE_ENV


@lru_cache(maxsize=8)
def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return ZoneInfo(DEFAULT_UI_TIMEZONE)


def ui_zone() -> ZoneInfo:
    name = os.environ.get(UI_TIMEZONE_ENV, "").strip() or DEFAULT_UI_TIMEZONE
    return _zone(name)


def parse_stamp(stamp: object) -> datetime | None:
    """An aware datetime for an ISO stamp, or None if it is missing or
    unparsable. A naive stamp is taken as UTC, the store's own convention
    for anything it did not stamp with an offset."""
    if not isinstance(stamp, str) or not stamp.strip():
        return None
    try:
        parsed = datetime.fromisoformat(stamp.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def sort_key(stamp: object) -> datetime:
    """A total-order key over stamps that may be missing or garbage: those
    sort as the oldest possible moment rather than raising."""
    return parse_stamp(stamp) or datetime.min.replace(tzinfo=UTC)


def local_time(stamp: object, *, now: datetime | None = None, zone: tzinfo | None = None) -> str:
    """`14:52 CDT` for a stamp on today's date in the zone, and
    `2026-09-17 14:52 CDT` for any other day. A missing or unparsable stamp
    renders as `-`. `now` and `zone` exist for tests."""
    parsed = parse_stamp(stamp)
    if parsed is None:
        return "-"
    tz = zone or ui_zone()
    local = parsed.astimezone(tz)
    today = (now or datetime.now(UTC)).astimezone(tz).date()
    clock = local.strftime("%H:%M %Z")
    if local.date() == today:
        return clock
    return f"{local.date().isoformat()} {clock}"
