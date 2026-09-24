"""Scheduled backups: settings, targets, retention and the loop (issue #68, ADR 024).

The bundle is exactly what `chronicle backup create` writes (ADR 016,
`backup.create_backup`), never with `instance.key`. This module only decides
when to make one, where it goes, and how many to keep:

- **When:** a schedule in America/Chicago wall time (`schedule_slots`), every
  6 or 12 hours, daily, or weekly on Sunday, from a time of day. A missed slot
  (the api was down) runs once at the next check, never once per missed slot.
- **Where:** a local directory (a mounted volume, never under the data
  directory, which would not survive losing it) or an S3 bucket (`s3.py`).
- **How many:** the newest `retention` bundles at the target; older ones
  Chronicle wrote are deleted. Nothing else at the target is ever touched:
  only names matching `BUNDLE_NAME` count.

Settings live in `state/backup-schedule.json` (mode 0600), with the S3 secret
encrypted by the instance key like the GitHub App's credentials. That file is
not in `backup.STATE_MEMBERS`, so no bundle ever carries it. The outcome of
the last run lives in `state/backup-status.json` for `/admin`.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import secrets
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field

from .. import backup as backup_mod
from . import crypto
from .atomic import write_atomic
from .s3 import S3Client, S3Location

log = logging.getLogger("chronicle.api.scheduled_backup")

SETTINGS_FILE_NAME = "backup-schedule.json"
STATUS_FILE_NAME = "backup-status.json"
HEARTBEAT_PATH = ("backup", "heartbeat.json")
SCHEDULE_TZ = ZoneInfo("America/Chicago")
CHECK_SECONDS = 60.0
BUNDLE_NAME = re.compile(r"^chronicle-backup-\d{8}T\d{6}Z\.tar\.gz$")
PROBE_PREFIX = "chronicle-probe-"

# Hours between runs, and how the admin page names each. 24 and 168 run once
# a day or once a week at the time of day; 6 and 12 run at that time and
# every interval after it through the day.
INTERVAL_CHOICES: dict[int, str] = {
    6: "Every 6 hours",
    12: "Every 12 hours",
    24: "Daily",
    168: "Weekly (Sunday)",
}
TIME_OF_DAY = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# One run at a time, whether the loop or an admin's "Run now" started it; a
# restore takes it too, so no run reads the tree mid-swap.
_RUN_LOCK = threading.Lock()
TMP_DIR_PARTS = ("backup-tmp", "scheduled")


@contextmanager
def run_lock() -> Iterator[None]:
    """Wait for any running backup, and hold off new ones, for the block."""
    with _RUN_LOCK:
        yield


class BackupSettings(BaseModel):
    enabled: bool = False
    interval_hours: int = 24
    time_of_day: str = "03:00"
    retention: int = Field(default=14, ge=1, le=365)
    target: str = "local"  # "local" | "s3"
    local_path: str = ""
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_prefix: str = ""
    s3_region: str = "us-east-1"
    s3_access_key_id: str = ""
    # Fernet token under the instance key; never the plaintext, never echoed.
    s3_secret_enc: str = ""
    # When these settings were last saved: a schedule nobody has run yet
    # counts its first slot from here, not from the epoch.
    updated_at: str | None = None


# --- Settings and status on disk -------------------------------------------


def load_settings(state_dir: Path) -> BackupSettings:
    path = state_dir / SETTINGS_FILE_NAME
    try:
        return BackupSettings.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return BackupSettings()


def save_settings(state_dir: Path, settings: BackupSettings) -> None:
    # 0600 like github-app.json: the S3 secret is encrypted, but the rest
    # (endpoint, bucket, access key id) is still nobody else's business.
    write_atomic(
        state_dir / SETTINGS_FILE_NAME, settings.model_dump_json(indent=2) + "\n", mode=0o600
    )


def load_status(state_dir: Path) -> dict[str, Any]:
    try:
        loaded = json.loads((state_dir / STATUS_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _save_status(state_dir: Path, status: dict[str, Any]) -> None:
    write_atomic(state_dir / STATUS_FILE_NAME, json.dumps(status, indent=2, sort_keys=True) + "\n")


def settings_problems(settings: BackupSettings, data_dir: Path) -> list[str]:
    """Why these settings cannot run, or [] when they can."""
    problems: list[str] = []
    if settings.interval_hours not in INTERVAL_CHOICES:
        problems.append(f"interval must be one of {sorted(INTERVAL_CHOICES)} hours")
    if not TIME_OF_DAY.match(settings.time_of_day):
        problems.append("time of day must be HH:MM (24-hour)")
    if settings.target == "local":
        problems.extend(_local_path_problems(settings.local_path, data_dir))
    elif settings.target == "s3":
        if not settings.s3_endpoint.startswith(("https://", "http://")):
            problems.append("S3 endpoint must be an http(s) URL")
        try:
            endpoint = urlsplit(settings.s3_endpoint.strip())
            endpoint.port  # noqa: B018 - raises on a malformed port
        except ValueError:
            return [*problems, "the S3 endpoint is not a valid URL"]
        if endpoint.username or endpoint.password:
            problems.append("put S3 credentials in their own fields, not the endpoint URL")
        if endpoint.query or endpoint.fragment:
            problems.append("the S3 endpoint must not carry a query or fragment")
        if not settings.s3_bucket.strip():
            problems.append("S3 bucket is required")
        if any(part in (".", "..") for part in settings.s3_prefix.split("/")):
            problems.append("the S3 prefix must not contain . or .. segments")
        if not settings.s3_region.strip():
            problems.append("S3 region is required")
        if not settings.s3_access_key_id.strip() or not settings.s3_secret_enc:
            problems.append("S3 access key and secret are both required")
    else:
        problems.append("target must be local or s3")
    return problems


def _local_path_problems(raw: str, data_dir: Path) -> list[str]:
    if not raw.strip():
        return ["a local path is required"]
    path = Path(raw)
    if not path.is_absolute():
        return ["the local path must be absolute"]
    resolved = path.resolve()
    data = data_dir.resolve()
    if resolved == data or data in resolved.parents:
        # A bundle on the data volume does not survive losing that volume,
        # so it is not a backup (issue #68).
        return [f"the local path must not be under the data directory ({data})"]
    return []


# --- Targets ----------------------------------------------------------------


class Target(Protocol):
    def describe(self, name: str) -> str: ...
    def put(self, bundle: Path) -> None: ...
    def names(self) -> list[str]: ...
    def delete(self, name: str) -> None: ...
    def probe(self) -> None: ...
    def close(self) -> None: ...


@dataclass
class LocalTarget:
    directory: Path

    def describe(self, name: str) -> str:
        return str(self.directory / name)

    def put(self, bundle: Path) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        # A copy an earlier run left half-written (a full disk, a restart)
        # would otherwise sit here forever under its own unique name.
        for stale in self.directory.glob(".chronicle-backup-*.tar.gz.partial"):
            stale.unlink(missing_ok=True)
        partial = self.directory / f".{bundle.name}.partial"
        shutil.copyfile(bundle, partial)
        os.replace(partial, self.directory / bundle.name)

    def names(self) -> list[str]:
        return [p.name for p in self.directory.iterdir() if p.is_file()]

    def delete(self, name: str) -> None:
        (self.directory / name).unlink(missing_ok=True)

    def probe(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{PROBE_PREFIX}{secrets.token_hex(6)}.txt"
        path.write_text("chronicle backup target probe\n", encoding="utf-8")
        path.unlink()

    def close(self) -> None:
        return None


class S3Target:
    def __init__(self, client: S3Client, prefix: str):
        self.client = client
        self.prefix = prefix.strip("/")

    def _key(self, name: str) -> str:
        return f"{self.prefix}/{name}" if self.prefix else name

    def describe(self, name: str) -> str:
        return f"s3://{self.client.location.bucket}/{self._key(name)}"

    def put(self, bundle: Path) -> None:
        self.client.put_file(self._key(bundle.name), bundle)

    def names(self) -> list[str]:
        listing_prefix = f"{self.prefix}/" if self.prefix else ""
        return [
            key[len(listing_prefix) :]
            for key in self.client.list_keys(listing_prefix)
            if "/" not in key[len(listing_prefix) :]
        ]

    def delete(self, name: str) -> None:
        self.client.delete(self._key(name))

    def probe(self) -> None:
        key = self._key(f"{PROBE_PREFIX}{secrets.token_hex(6)}.txt")
        self.client.put_bytes(key, b"chronicle backup target probe\n")
        self.client.delete(key)

    def close(self) -> None:
        self.client.close()


def build_target(
    settings: BackupSettings,
    instance_key: bytes,
    transport: httpx.BaseTransport | None = None,
) -> Target:
    if settings.target == "s3":
        location = S3Location(
            endpoint=settings.s3_endpoint.strip(),
            bucket=settings.s3_bucket.strip(),
            region=settings.s3_region.strip(),
            access_key_id=settings.s3_access_key_id.strip(),
            secret_access_key=crypto.decrypt(instance_key, settings.s3_secret_enc),
        )
        return S3Target(S3Client(location, transport=transport), settings.s3_prefix)
    return LocalTarget(Path(settings.local_path))


def prune(target: Target, retention: int) -> list[str]:
    """Delete all but the newest `retention` bundles; return what went.
    Only names matching `BUNDLE_NAME` are counted or touched; their UTC stamp
    sorts in creation order."""
    bundles = sorted(name for name in target.names() if BUNDLE_NAME.match(name))
    doomed = bundles[: max(0, len(bundles) - retention)]
    for name in doomed:
        target.delete(name)
    return doomed


# --- The schedule -----------------------------------------------------------


def schedule_slots(day: dt.date, interval_hours: int, time_of_day: str) -> list[dt.datetime]:
    """Every run time on local `day`, as aware datetimes in SCHEDULE_TZ.

    Wall-clock times, so a daily 03:00 stays 03:00 across a DST change."""
    hour, minute = (int(part) for part in time_of_day.split(":"))
    if interval_hours >= 24:
        step_days = interval_hours // 24
        # Weekly runs on Sunday: date.toordinal() is 7 for a Sunday.
        if step_days > 1 and day.toordinal() % step_days != 0:
            return []
        return [dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=SCHEDULE_TZ)]
    first = dt.datetime(day.year, day.month, day.day, hour, minute)
    out = []
    for step in range(24 // interval_hours):
        naive = first + dt.timedelta(hours=step * interval_hours)
        if naive.date() == day:
            out.append(naive.replace(tzinfo=SCHEDULE_TZ))
    # The same pattern also fills the hours before the time of day.
    for step in range(1, 24 // interval_hours):
        naive = first - dt.timedelta(hours=step * interval_hours)
        if naive.date() == day:
            out.append(naive.replace(tzinfo=SCHEDULE_TZ))
    return sorted(out)


def next_due(since: dt.datetime, interval_hours: int, time_of_day: str) -> dt.datetime:
    """The first scheduled time strictly after `since`."""
    local = since.astimezone(SCHEDULE_TZ)
    for offset in range(-1, 9):
        day = local.date() + dt.timedelta(days=offset)
        for slot in schedule_slots(day, interval_hours, time_of_day):
            if slot > since:
                return slot
    raise ValueError("no scheduled slot within nine days")  # unreachable for valid settings


def _parse(stamp: Any) -> dt.datetime | None:
    if not isinstance(stamp, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def is_due(settings: BackupSettings, status: dict[str, Any], now: dt.datetime) -> bool:
    since = _parse(status.get("last_attempt_at")) or _parse(settings.updated_at)
    if since is None:
        return False
    if since > now:
        # Stamped while the clock ran ahead (then corrected): waiting for the
        # wall clock to catch up would silently skip every run until then.
        return True
    return now >= next_due(since, settings.interval_hours, settings.time_of_day)


def is_overdue(settings: BackupSettings, status: dict[str, Any], now: dt.datetime) -> bool:
    """More than two intervals since the last success (or since the schedule
    was saved, if it has never succeeded): the status page warns."""
    if not settings.enabled:
        return False
    since = _parse(status.get("last_success_at")) or _parse(settings.updated_at)
    if since is None:
        return False
    if since > now:
        return True  # a clock problem worth a look, not a reason to go quiet
    return now - since > dt.timedelta(hours=2 * settings.interval_hours)


# --- Running ----------------------------------------------------------------


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def run_once(
    data_dir: Path,
    state_dir: Path,
    instance_key: bytes,
    *,
    settings: BackupSettings | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any] | None:
    """Make one bundle, deliver it, prune, and record the outcome.

    Returns the recorded status, or None when another run holds the lock.
    Never raises: a failure is recorded with its error for `/admin`."""
    if not _RUN_LOCK.acquire(blocking=False):
        return None
    try:
        return _run_locked(data_dir, state_dir, instance_key, settings, transport)
    finally:
        _RUN_LOCK.release()


def start_run_now(data_dir: Path, state_dir: Path, instance_key: bytes) -> bool:
    """Reserve the run lock now and run in the background; False, and nothing
    started, when a run (or a restore) already holds it. The reservation is
    taken before returning so "started" is never reported for a run that
    would then find the lock taken and quietly do nothing."""
    if not _RUN_LOCK.acquire(blocking=False):
        return False

    def work() -> None:
        try:
            _run_locked(data_dir, state_dir, instance_key, None, None)
        finally:
            _RUN_LOCK.release()

    threading.Thread(target=work, name="chronicle-backup-now", daemon=True).start()
    return True


def _run_locked(
    data_dir: Path,
    state_dir: Path,
    instance_key: bytes,
    settings: BackupSettings | None,
    transport: httpx.BaseTransport | None,
) -> dict[str, Any]:
    settings = settings or load_settings(state_dir)
    status = load_status(state_dir)
    started = _now()
    status["last_attempt_at"] = started.isoformat(timespec="seconds")
    bundle: Path | None = None
    target: Target | None = None
    try:
        problems = settings_problems(settings, data_dir)
        if problems:
            raise ValueError("; ".join(problems))
        # Its own directory, not backup-tmp itself, where a manual
        # download may be streaming a bundle of the same name pattern.
        # Anything left here was abandoned by a run that died (only one
        # runs at a time), so it is swept before the next one starts.
        tmp_dir = state_dir.joinpath(*TMP_DIR_PARTS)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        for stale in tmp_dir.glob("*.tar.gz"):
            stale.unlink(missing_ok=True)
        bundle = backup_mod.create_backup(data_dir, tmp_dir)
        target = build_target(settings, instance_key, transport)
        target.put(bundle)
        pruned = prune(target, settings.retention)
        status.update(
            last_success_at=_now().isoformat(timespec="seconds"),
            last_size_bytes=bundle.stat().st_size,
            last_location=target.describe(bundle.name),
            last_pruned=pruned,
        )
    except Exception as exc:  # recorded, never raised: the loop keeps going
        log.exception("scheduled backup failed")
        status.update(
            last_failure_at=_now().isoformat(timespec="seconds"),
            last_error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if target is not None:
            target.close()
        if bundle is not None:
            bundle.unlink(missing_ok=True)
    _save_status(state_dir, status)
    return status


def test_target(
    settings: BackupSettings,
    data_dir: Path,
    instance_key: bytes,
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """Write and delete a small probe at the target. None on success, else
    why it failed, so a bad credential or mount shows before the first run."""
    problems = settings_problems(settings, data_dir)
    if problems:
        return "; ".join(problems)
    target: Target | None = None
    try:
        target = build_target(settings, instance_key, transport)
        target.probe()
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        if target is not None:
            target.close()


def _write_heartbeat(state_dir: Path, now: dt.datetime) -> None:
    path = state_dir.joinpath(*HEARTBEAT_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"last_run_at": now.isoformat(timespec="seconds"), "interval_seconds": CHECK_SECONDS}
    write_atomic(path, json.dumps(payload) + "\n", mode=0o644)


def tick(data_dir: Path, state_dir: Path, instance_key: bytes, now: dt.datetime) -> bool:
    """Run a backup if one is due. Returns whether one ran."""
    settings = load_settings(state_dir)
    if not settings.enabled or settings_problems(settings, data_dir):
        return False
    if not is_due(settings, load_status(state_dir), now):
        return False
    return run_once(data_dir, state_dir, instance_key, settings=settings) is not None


def run_loop(
    stop_event: threading.Event, data_dir: Path, state_dir: Path, instance_key: bytes
) -> None:
    while not stop_event.is_set():
        now = _now()
        try:
            _write_heartbeat(state_dir, now)
            tick(data_dir, state_dir, instance_key, now)
        except Exception:
            log.exception("backup schedule check failed")
        if stop_event.wait(CHECK_SECONDS):
            break
