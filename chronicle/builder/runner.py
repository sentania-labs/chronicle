"""One poll loop tick: recover, claim, build, record.

Split out of `main.py` so a test can drive a tick directly against a real
`Store` and a temporary lease directory without a subprocess or a socket.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..api import convert
from ..api.models import Draft, Run, now_stamp
from ..api.store import Store
from . import hugo
from .leases import LeaseDirectory
from .settings import BuilderSettings

log = logging.getLogger("chronicle.builder")

TOOLCHAIN_FILE_NAME = "toolchain.json"
IGNORE_GIT = shutil.ignore_patterns(".git")


class BuildFailed(Exception):
    def __init__(self, error_class: str, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class


@dataclass(frozen=True)
class Heartbeat:
    builder_id: str
    last_loop_at: str
    hugo_version: str
    queue_depth: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "builder_id": self.builder_id,
            "last_loop_at": self.last_loop_at,
            "hugo_version": self.hugo_version,
            "queue_depth": self.queue_depth,
        }


def site_hugo_version(data_dir: Path) -> str:
    path = data_dir / "state" / TOOLCHAIN_FILE_NAME
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unknown"
    version = loaded.get("hugo_version")
    return str(version) if version else "unknown"


def write_heartbeat(settings: BuilderSettings, store: Store, hugo_version: str) -> None:
    heartbeat = Heartbeat(
        builder_id=settings.builder_id,
        last_loop_at=now_stamp(),
        hugo_version=hugo_version,
        queue_depth=store.queue_depth("preview"),
    )
    path = settings.heartbeat_path
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    payload = json.dumps(heartbeat.as_dict(), indent=2, sort_keys=True) + "\n"
    temp.write_text(payload, encoding="utf-8")
    temp.replace(path)


def recover_expired_leases(store: Store, leases: LeaseDirectory, actor: str) -> int:
    """Put back on the queue any run a crashed builder left mid-flight.

    Runs at every loop tick, not only at startup, so a lease that expires
    while this builder is busy with something else is still recovered on the
    next pass rather than waiting for a restart.

    Two cases, not one: a lease past its `expires_at` is the expected way a
    hung build gets noticed, but `tick`'s own `finally` releases a run's
    lease the moment `build_one` returns control to it at all, including on
    an exception neither `build_one` nor anything it calls caught (found
    live in the C3 real-blog check: an unhandled `OSError` from a bad
    `os.replace` left a run `building` with its lease already gone, both
    before this second case existed). A run still marked `building` with no
    live lease at all, expired or missing, was not being built by anyone the
    moment this loop looked, full stop.
    """
    recovered = 0
    for lease in leases.expired_leases():
        run = store.get_run(lease.run_id)
        if run.status == "building":
            store.requeue_run(lease.run_id, actor, f"lease held by {lease.builder_id} expired")
            recovered += 1
        leases.release(lease.run_id)
    for run in store.runs_in_flight("preview"):
        if not leases.held(run.id):
            store.requeue_run(run.id, actor, "building with no live lease")
            recovered += 1
    return recovered


def claim_next(store: Store, leases: LeaseDirectory, builder_id: str) -> Run | None:
    """The one queued preview run this builder now owns, or None.

    Walks the queue oldest first and takes the first entry whose lease is
    free; a run already leased by someone else, or one whose run record has
    moved on (raced by another claim in the same instant), is skipped rather
    than retried.
    """
    for entry in store.queued_entries(kind="preview"):
        run_id = str(entry["run_id"])
        run = store.get_run(run_id)
        if run.status != "queued":
            continue
        if leases.claim(run_id, builder_id) is not None:
            return run
    return None


def _copy_site(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, ignore=IGNORE_GIT, copy_function=_link_or_copy)


def _link_or_copy(src: str, dst: str) -> None:
    try:
        Path(dst).hardlink_to(src)
    except OSError:
        shutil.copy2(src, dst)


def _write_post_and_images(store: Store, draft: Draft, scratch: Path) -> convert.ConvertedPost:
    converted = convert.convert(draft)
    post_path = scratch / converted.post_path
    post_path.parent.mkdir(parents=True, exist_ok=True)
    post_path.write_text(converted.text, encoding="utf-8")
    for placement in converted.images:
        source = store.image_blob(placement.image_id)
        target = scratch / placement.site_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return converted


def _atomic_swap(built: Path, destination: Path) -> None:
    """Rename the finished build over the slug's live preview directory.

    `built` and `destination` share the preview volume's filesystem, so this
    `os.replace` (via `Path.replace`) is one atomic rename: there is no
    instant where the slug directory is half-old, half-new, or missing.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    stale = None
    if destination.exists():
        stale = destination.with_name(f".{destination.name}.stale-{now_stamp().replace(':', '')}")
        destination.replace(stale)
    built.replace(destination)
    if stale is not None:
        shutil.rmtree(stale, ignore_errors=True)


def output_path(store: Store, run_id: str) -> Path:
    """Where a run's Hugo build lands before its atomic swap.

    Under `store.preview_dir`, not `settings.work_dir`: see ADR 011. A
    rename across a mount boundary is not atomic, and `builder-work` is a
    deliberately separate mount from `preview` in both compose and the k8s
    reference.
    """
    return store.preview_dir / ".tmp" / run_id


def build_one(store: Store, settings: BuilderSettings, run: Run) -> None:
    """Claimed, still `queued` in the record: build it, then record the outcome.

    `store.start_run` is the first write, so a run visibly moves to
    `building` before any Hugo process starts; every exit path below ends in
    exactly one call to `store.finish_run`, succeeded or failed.
    """
    draft = store.get_draft(run.draft_id)
    installed = hugo.installed_version(settings.hugo_bin)
    site_version = site_hugo_version(store.data_dir)
    drift = site_version != "unknown" and installed != "unknown" and site_version != installed
    store.start_run(run.id, settings.builder_id, installed, drift)

    started = time.monotonic()
    log_path = store.log_path_for(run.id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    scratch = settings.scratch_dir / run.id
    output = None
    try:
        converted = _write_post_and_images(store, draft, _prepare_scratch(store, scratch))
        output = output_path(store, run.id)
        if output.exists():
            shutil.rmtree(output)
        base_url = f"{settings.external_url}/preview/{converted.slug}/"
        result = hugo.build(
            settings.hugo_bin,
            scratch,
            output,
            base_url,
            settings.cache_dir,
            settings.resource_dir,
            settings.build_timeout_seconds,
        )
        log_path.write_text(result.output, encoding="utf-8")
        wall_time = time.monotonic() - started
        if result.returncode != 0:
            error_class = "timeout" if result.timed_out else "hugo_build_failed"
            store.finish_run(
                run.id,
                settings.builder_id,
                succeeded=False,
                result={
                    "wall_time_seconds": round(wall_time, 3),
                    "error_class": error_class,
                },
            )
            return
        destination = store.preview_dir / converted.slug
        _atomic_swap(output, destination)
        preview_url = f"{settings.external_url}/preview/{converted.slug}/"
        store.finish_run(
            run.id,
            settings.builder_id,
            succeeded=True,
            result={
                "preview_url": preview_url,
                "slug": converted.slug,
                "wall_time_seconds": round(wall_time, 3),
            },
        )
    except convert.ConversionError as exc:
        wall_time = time.monotonic() - started
        log_path.write_text(f"conversion failed: {exc}\n", encoding="utf-8")
        store.finish_run(
            run.id,
            settings.builder_id,
            succeeded=False,
            result={"wall_time_seconds": round(wall_time, 3), "error_class": "conversion_failed"},
        )
    except OSError as exc:
        # A filesystem-level failure (an unexpected cross-device rename, a
        # permission error, disk full) is still this one run's failure, not
        # a reason to crash the whole poll loop and leave the run `building`
        # with no one holding its lease (recover_expired_leases's second
        # case exists for exactly the crash this except clause now prevents).
        wall_time = time.monotonic() - started
        log_path.write_text(f"builder error: {exc}\n", encoding="utf-8")
        store.finish_run(
            run.id,
            settings.builder_id,
            succeeded=False,
            result={"wall_time_seconds": round(wall_time, 3), "error_class": "builder_error"},
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        if output is not None:
            shutil.rmtree(output, ignore_errors=True)


def _prepare_scratch(store: Store, scratch: Path) -> Path:
    _copy_site(store.site_dir, scratch)
    return scratch


def tick(store: Store, settings: BuilderSettings, leases: LeaseDirectory) -> bool:
    """One loop iteration: recover, claim at most one run, build it if claimed.

    Returns whether a run was claimed, purely so `main.run` can log something
    more useful than "polled" on every idle tick.
    """
    recover_expired_leases(store, leases, settings.builder_id)
    installed = hugo.installed_version(settings.hugo_bin)
    write_heartbeat(settings, store, installed)
    run = claim_next(store, leases, settings.builder_id)
    if run is None:
        return False
    try:
        build_one(store, settings, run)
    finally:
        leases.release(run.id)
    return True
