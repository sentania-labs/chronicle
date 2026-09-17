"""Claiming one run at a time, and surviving a builder that dies mid-build.

A queue entry under `data/repo/runs/queue/` says a run wants building. It does
not say who is building it, and it cannot: it is written by the api and
tracked by git, so it is a request, not a claim. The claim is a separate
file, a lease, under `data/state/builder/leases/<run_id>.json`:

```json
{"run_id": "...", "builder_id": "chronicle-builder-1", "claimed_at": "...",
 "expires_at": "...", "renewed_at": "..."}
```

Leases are deliberately outside `repo/`: a claim is runtime coordination
between processes, not history worth committing, and committing one would put
two writers into the same git index for no gain. They are also outside
`preview/`, which the preview container serves.

Two rules make this safe with more than one builder, which is not the shape
today but is the shape the spec leaves open:

- **One at a time.** A claim is `open(O_CREAT|O_EXCL)`, so exactly one
  process creates the file. A builder that loses the race moves on.
- **A crash is recoverable.** The winner writes an expiry and renews it while
  it builds. A lease whose expiry has passed is stale: another builder takes
  it over under `flock`, which is what keeps two builders from both deciding
  a stale lease is theirs. `flock` is held only for the read-decide-write of
  the lease file itself, never for the build.
"""

from __future__ import annotations

import fcntl
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ..api.models import now_stamp


@dataclass(frozen=True)
class Lease:
    run_id: str
    builder_id: str
    claimed_at: str
    expires_at: str
    renewed_at: str

    def expired(self, at: datetime | None = None) -> bool:
        moment = at or datetime.now().astimezone()
        try:
            return datetime.fromisoformat(self.expires_at) <= moment
        except ValueError:
            # An unparsable expiry is treated as expired rather than as
            # eternal: a lease nobody can read must not block the queue
            # forever.
            return True


def _expiry(seconds: float) -> str:
    return (datetime.now().astimezone() + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _payload(lease: Lease) -> str:
    return json.dumps(lease.__dict__, indent=2, sort_keys=True) + "\n"


def _read(path: Path) -> Lease | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return Lease(**loaded)
    except TypeError:
        return None


class LeaseDirectory:
    def __init__(self, directory: Path, lease_seconds: float) -> None:
        self.directory = directory
        self.lease_seconds = lease_seconds
        self.directory.mkdir(parents=True, exist_ok=True)

    def path(self, run_id: str) -> Path:
        return self.directory / f"{run_id}.json"

    def read(self, run_id: str) -> Lease | None:
        return _read(self.path(run_id))

    def held(self, run_id: str) -> bool:
        """True when a live lease exists, whoever holds it."""
        lease = self.read(run_id)
        return lease is not None and not lease.expired()

    def claim(self, run_id: str, builder_id: str) -> Lease | None:
        """Take the run, or return None because someone else has it.

        The fast path is an exclusive create. The slow path exists only for a
        lease that is already there: it is taken over if, and only if, it has
        expired, decided while holding `flock` on the lease file so two
        builders cannot both conclude that it was theirs to take.
        """
        stamp = now_stamp()
        lease = Lease(
            run_id=run_id,
            builder_id=builder_id,
            claimed_at=stamp,
            expires_at=_expiry(self.lease_seconds),
            renewed_at=stamp,
        )
        path = self.path(run_id)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return self._take_over_if_stale(path, lease)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(_payload(lease))
        return lease

    def _take_over_if_stale(self, path: Path, lease: Lease) -> Lease | None:
        try:
            handle = path.open("r+", encoding="utf-8")
        except OSError:
            return None
        with handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                # Another builder is mid-decision on this same lease.
                return None
            try:
                existing = _read(path)
                if existing is not None and not existing.expired():
                    return None
                handle.seek(0)
                handle.truncate()
                handle.write(_payload(lease))
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return lease

    def renew(self, lease: Lease) -> Lease:
        """Push the expiry out while a build is still running.

        Written in place with a temp file and a rename so a reader never sees
        half a lease. A lease taken over by someone else is not clawed back:
        the renewal is skipped and the caller keeps building, which is safe
        because the atomic swap of the output directory is the last step.
        """
        current = self.read(lease.run_id)
        if current is not None and current.builder_id != lease.builder_id:
            return lease
        renewed = Lease(
            run_id=lease.run_id,
            builder_id=lease.builder_id,
            claimed_at=lease.claimed_at,
            expires_at=_expiry(self.lease_seconds),
            renewed_at=now_stamp(),
        )
        path = self.path(lease.run_id)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(_payload(renewed), encoding="utf-8")
        os.replace(temp, path)
        return renewed

    def release(self, run_id: str) -> None:
        self.path(run_id).unlink(missing_ok=True)

    def expired_leases(self) -> list[Lease]:
        return [
            lease
            for lease in (_read(path) for path in sorted(self.directory.glob("*.json")))
            if lease is not None and lease.expired()
        ]
