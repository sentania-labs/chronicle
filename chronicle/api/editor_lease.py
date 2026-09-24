"""The editor lock: a draft open in the editor can't be saved by anyone else (issue #64, ADR 025).

An open edit page holds a lease on its draft for the viewing identity (the
consumer name the domain records: `editor` for the UI): `editor.js` beats
once on load and every `HEARTBEAT_SECONDS` after, under a random per-page
token, through a same-origin POST. Rendering the page alone takes nothing, so
a prefetch or a cross-site GET cannot hold a draft. A lease lapses `LEASE_SECONDS` after its last
heartbeat, so a closed tab, a sleeping laptop or a dropped connection never
leaves a draft stuck; closing the page also sends a best-effort release.

While identity A holds a live lease, a save by any other identity (the UI
save route or `PUT /v1/drafts/{id}`) is refused with 423 naming the holder.
Same-identity saves are not blocked (two tabs): the version check and the
conflict page stay the guard there. Reads, previews, feedback and the
reviewer's actions are never blocked.

Leases are runtime state, not content: held in the api process's memory
(ADR 013, one api process), never versioned, committed or backed up. A
restart forgets them, which is the same as every one lapsing.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .errors import ApiError
from .ui_time import local_time

LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 30


def _utc_now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


@dataclass(frozen=True)
class Lease:
    draft_id: str
    holder: str
    since: dt.datetime
    heartbeat_at: dt.datetime

    @property
    def expires_at(self) -> dt.datetime:
        return self.heartbeat_at + dt.timedelta(seconds=LEASE_SECONDS)

    def as_dict(self) -> dict[str, Any]:
        return {
            "holder": self.holder,
            "since": self.since.isoformat(timespec="seconds"),
            "expires_at": self.expires_at.isoformat(timespec="seconds"),
        }


class EditorLeases:
    """One lease per draft, held by one identity, kept alive by that
    identity's open pages. Each page heartbeats under its own random token,
    so closing one tab (or a navigation's `pagehide` landing after the next
    page's first beat) drops only that page's hold, never a sibling's."""

    def __init__(self, clock: Callable[[], dt.datetime] = _utc_now) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._leases: dict[str, Lease] = {}
        self._pages: dict[str, dict[str, dt.datetime]] = {}

    def _live(self, draft_id: str, now: dt.datetime) -> Lease | None:
        lease = self._leases.get(draft_id)
        if lease is None:
            return None
        pages = self._pages.get(draft_id, {})
        cutoff = now - dt.timedelta(seconds=LEASE_SECONDS)
        for token in [t for t, seen in pages.items() if seen <= cutoff]:
            del pages[token]
        if now >= lease.expires_at or not pages:
            self._leases.pop(draft_id, None)
            self._pages.pop(draft_id, None)
            return None
        return lease

    def current(self, draft_id: str) -> Lease | None:
        with self._lock:
            return self._live(draft_id, self._clock())

    def touch(self, draft_id: str, holder: str, page: str = "") -> Lease:
        """Take or renew `holder`'s lease for one open page; return whichever
        lease is live afterwards. Another identity's live lease is returned
        untouched."""
        with self._lock:
            now = self._clock()
            lease = self._live(draft_id, now)
            if lease is not None and lease.holder != holder:
                return lease
            since = lease.since if lease is not None else now
            fresh = Lease(draft_id, holder, since, now)
            self._leases[draft_id] = fresh
            self._pages.setdefault(draft_id, {})[page] = now
            return fresh

    def release(self, draft_id: str, holder: str, page: str = "") -> None:
        """Drop one page's hold; the lease ends when its last page is gone."""
        with self._lock:
            lease = self._leases.get(draft_id)
            if lease is None or lease.holder != holder:
                return
            pages = self._pages.get(draft_id, {})
            pages.pop(page, None)
            if not pages:
                self._leases.pop(draft_id, None)
                self._pages.pop(draft_id, None)

    def check_save(self, draft_id: str, actor: str) -> None:
        """Refuse a save by `actor` while someone else has the draft open."""
        lease = self.current(draft_id)
        if lease is None or lease.holder == actor:
            return
        raise ApiError(
            423,
            "draft_being_edited",
            f"draft {draft_id} is open in the editor by {lease.holder} since "
            f"{local_time(lease.since.isoformat())}; try again once it is closed",
            **lease.as_dict(),
        )
