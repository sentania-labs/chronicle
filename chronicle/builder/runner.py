"""One poll loop tick: recover, claim, build, record.

Split out of `main.py` so a test can drive a tick directly against a real
`Store` and a temporary lease directory without a subprocess or a socket.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..api import convert
from ..api import digest as digest_mod
from ..api.models import Draft, Run, now_stamp
from ..api.store import Store, text_fingerprint
from . import hugo
from .leases import Lease, LeaseDirectory
from .settings import BuilderSettings

log = logging.getLogger("chronicle.builder")

TOOLCHAIN_FILE_NAME = "toolchain.json"
IGNORE_GIT = shutil.ignore_patterns(".git")

# How often the keep-alive thread refreshes the heartbeat and renews the
# lease while a build is running. Independent of the poll interval: a slow
# build must keep both fresh long before either one's own staleness window
# (heartbeat: settings.poll_seconds * 5 + 30s; lease: settings.lease_seconds)
# closes.
KEEPALIVE_SECONDS = 5.0


# How long the index may stay locked, tick after tick, before the builder
# stops deferring and lets the error crash it. The api holds the shared
# index's write lock while it rebuilds it at startup (about 30 s on 354
# drafts, issue 89), so this is a generous multiple of that; a lock that
# outlasts it is not a rebuild and a restart is the honest response.
LOCK_DEFER_LIMIT_SECONDS = 300.0


class BuildFailed(Exception):
    def __init__(self, error_class: str, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class


@dataclass(frozen=True)
class Heartbeat:
    builder_id: str
    last_loop_at: str
    hugo_version: str
    queue_depth: int | None
    preview_writable: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "builder_id": self.builder_id,
            "last_loop_at": self.last_loop_at,
            "hugo_version": self.hugo_version,
            "queue_depth": self.queue_depth,
            "preview_writable": self.preview_writable,
        }


def site_hugo_version(data_dir: Path) -> str:
    path = data_dir / "state" / TOOLCHAIN_FILE_NAME
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unknown"
    version = loaded.get("hugo_version")
    return str(version) if version else "unknown"


def check_writable(path: Path) -> bool:
    """True if `path` exists (or can be created) and this process can write into it.

    Never raises: a fresh named volume or PVC mounted root-owned (the
    class of defect a compose `down -v` / `up -d` cycle surfaces, see
    docs on the C3 fresh-volume fix) makes this False rather than
    crashing the poll loop, so the builder keeps polling and recovers on
    its own once the mount's ownership is fixed without a restart.
    """
    # A pid alone is not unique here: two builder replicas each see
    # themselves as pid 1 in their own container namespace, so a shared
    # volume needs a stronger disambiguator than the process id to keep one
    # builder's unlink from racing the other's write.
    probe = path / f".writable-check-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        log.error("path %s is not writable by uid %d: %s", path, os.getuid(), exc)
        return False
    return True


def write_heartbeat(
    settings: BuilderSettings,
    hugo_version: str,
    preview_writable: bool,
    queue_depth: int | None,
) -> None:
    heartbeat = Heartbeat(
        builder_id=settings.builder_id,
        last_loop_at=now_stamp(),
        hugo_version=hugo_version,
        queue_depth=queue_depth,
        preview_writable=preview_writable,
    )
    path = settings.heartbeat_path
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    payload = json.dumps(heartbeat.as_dict(), indent=2, sort_keys=True) + "\n"
    temp.write_text(payload, encoding="utf-8")
    temp.replace(path)


def write_loop_heartbeat(settings: BuilderSettings, store: Store | None) -> bool:
    """Stamp this loop pass and report whether a build could succeed right now.

    Every pass writes one, before anything that reads the index: a pass the
    lock deferral cuts short, and the startup wait before the store is even
    open (`store` is None then, so there is no queue to count), both still
    count as alive. Otherwise a builder correctly riding out the api's index
    rebuild (issue 89) goes stale to the liveness probe and is killed well
    inside `LOCK_DEFER_LIMIT_SECONDS`.

    Checked every pass, not only at startup, so a fresh-volume or PVC
    ownership defect fixed without restarting the builder is picked up on
    the next poll instead of requiring a restart to clear the flag. Both
    paths have to be writable for a build to succeed, so either one being
    wrong is reported the same way: `preview_writable` folds in the work
    dir check rather than adding a second heartbeat field for it.
    """
    installed = hugo.installed_version(settings.hugo_bin)
    preview_dir = store.preview_dir if store is not None else settings.data_dir / "preview"
    preview_writable = check_writable(preview_dir) and check_writable(settings.work_dir)
    queue_depth = store.queue_depth("preview") if store is not None else None
    write_heartbeat(settings, installed, preview_writable, queue_depth)
    return preview_writable


class BuildKeepAlive:
    """Refreshes the heartbeat and renews the lease while a build runs.

    `tick` writes the heartbeat once before a build starts, and a lease is
    claimed once before it too; without something running alongside the
    synchronous `hugo.build` call, a build that runs longer than either
    one's staleness window looks dead to a liveness probe, or gets its lease
    taken by another builder, even though it is still legitimately in
    progress (round C3 review). A background thread ticking every
    `KEEPALIVE_SECONDS` keeps both current for as long as the build runs.
    """

    def __init__(
        self,
        store: Store,
        settings: BuilderSettings,
        leases: LeaseDirectory,
        lease: Lease,
        hugo_version: str,
        preview_writable: bool,
    ) -> None:
        self._store = store
        self._settings = settings
        self._leases = leases
        self._lease = lease
        self._hugo_version = hugo_version
        self._preview_writable = preview_writable
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(KEEPALIVE_SECONDS):
            write_heartbeat(
                self._settings,
                self._hugo_version,
                self._preview_writable,
                self._store.queue_depth("preview"),
            )
            self._lease = self._leases.renew(self._lease)

    def stop(self) -> Lease:
        """Stop the thread and return the most recently renewed lease."""
        self._stop.set()
        self._thread.join()
        return self._lease


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


def claim_next(store: Store, leases: LeaseDirectory, builder_id: str) -> tuple[Run, Lease] | None:
    """The one queued preview run this builder now owns, or None.

    Walks the queue oldest first and takes the first entry whose lease is
    free; a run already leased by someone else, or one whose run record has
    moved on (raced by another claim in the same instant), is skipped rather
    than retried. Returns the lease token alongside the run so the caller can
    renew it through the build instead of discarding it (round C3 review).
    """
    for entry in store.queued_entries(kind="preview"):
        run_id = str(entry["run_id"])
        run = store.get_run(run_id)
        if run.status != "queued":
            continue
        lease = leases.claim(run_id, builder_id)
        if lease is not None:
            return run, lease
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
    static_dir = digest_mod.read_static_dir_from_state(store.data_dir)
    content_dir = digest_mod.read_content_dir_from_state(store.data_dir)
    new_post_dir = digest_mod.read_new_post_dir_from_state(store.data_dir)
    converted = convert.convert(draft, static_dir, content_dir, new_post_dir)
    post_path = scratch / converted.post_path
    post_path.parent.mkdir(parents=True, exist_ok=True)
    # `_copy_site` hard-links `scratch` to `store.site_dir` wherever it can, so
    # writing this path in place (`write_text` opens for truncate) would
    # truncate the same inode the digest's clone under `data/site` uses.
    # Unlinking first breaks the hard link before anything is written, so the
    # write always lands on a fresh inode and `data/site` is never touched
    # (round C3 review).
    post_path.unlink(missing_ok=True)
    post_path.write_text(converted.text, encoding="utf-8")
    for placement in converted.images:
        source = store.image_blob(placement.image_id)
        target = scratch / placement.site_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.unlink(missing_ok=True)
        shutil.copy2(source, target)
    return converted


def _atomic_swap(built: Path, destination: Path) -> None:
    """Point the slug's live preview at the finished build, in one rename.

    `destination` (`data/preview/<slug>`) is always a symlink into
    `data/preview/.builds/<run_id>/`, never a real directory, once this has
    run once for that slug. Renaming a temp symlink onto `destination` is a
    single directory-entry update: a request resolving `destination` either
    sees the old build or the new one, never a moment with neither (a plain
    remove-then-rename pair, which this replaces, had a real 404 window and
    could strand the old tree if the second rename failed).

    The preview server (`chronicle/preview/main.py`) already resolves
    symlinks before its containment check, so a `destination` pointing
    inside `data/preview/.builds/` needs no server-side change; the check
    only ever 404s a link that resolves outside the preview root.

    `built` and `destination` share the preview volume's filesystem (see
    `output_path`), so both the temp symlink's rename and the eventual
    garbage collection below stay on one mount.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    previous_target: Path | None = None
    if destination.is_symlink():
        previous_target = destination.resolve()
    elif destination.exists():
        # A directory from before a slug ever used this symlink scheme (or
        # any other stray path at the slug's name): clear it so the rename
        # below can place the symlink. The one non-atomic corner left, and
        # it is hit at most once per slug.
        if destination.is_dir():
            shutil.rmtree(destination)
        else:
            destination.unlink()

    temp_link = destination.parent / f".{destination.name}.next"
    temp_link.unlink(missing_ok=True)
    temp_link.symlink_to(os.path.relpath(built, destination.parent))
    os.rename(temp_link, destination)

    if previous_target is not None and previous_target != built.resolve():
        shutil.rmtree(previous_target, ignore_errors=True)


def output_path(store: Store, run_id: str) -> Path:
    """Where a run's Hugo build lands, and stays, once it is live.

    Under `store.preview_dir`, not `settings.work_dir`: see ADR 011. A
    rename across a mount boundary is not atomic, and `builder-work` is a
    deliberately separate mount from `preview` in both compose and the k8s
    reference. `.builds/<run_id>/` (not `.tmp/`) because a successful build
    is not temporary once `_atomic_swap` points the slug's symlink at it: it
    stays here, live, until a later build for the same slug replaces it.
    """
    return store.preview_dir / ".builds" / run_id


def build_one(
    store: Store, settings: BuilderSettings, run: Run, leases: LeaseDirectory, lease: Lease
) -> Lease:
    """Claimed, still `queued` in the record: build it, then record the outcome.

    `store.start_run` is the first write, so a run visibly moves to
    `building` before any Hugo process starts. A `BuildKeepAlive` runs
    alongside the (synchronous, possibly long) Hugo build to keep the
    heartbeat and the lease fresh; once the build finishes, the lease is
    read back one last time, and the run is only swapped live and finished
    if this builder still owns it. If it does not, another builder claimed
    it after this one's lease expired, and that builder owns finishing the
    run: this one discards its result and logs it (round C3 review).

    Returns the lease as last known, so the caller releases the right one.
    """
    draft = store.get_draft(run.draft_id)
    installed = hugo.installed_version(settings.hugo_bin)
    site_version = site_hugo_version(store.data_dir)
    drift = site_version != "unknown" and installed != "unknown" and site_version != installed
    store.start_run(
        run.id,
        settings.builder_id,
        installed,
        drift,
        built_version=draft.version_no,
        # The snapshot this build converts, so a save that lands before
        # `start_run` and changes only announcements does not make it stale.
        built_text=text_fingerprint(draft.frontmatter, draft.body),
    )

    keepalive = BuildKeepAlive(
        store, settings, leases, lease, installed, check_writable(store.preview_dir)
    )
    keepalive.start()

    started = time.monotonic()
    log_path = store.log_path_for(run.id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    scratch = settings.scratch_dir / run.id
    output = None
    swapped = False
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
        lease = keepalive.stop()
        current_lease = leases.read(run.id)
        if current_lease is None or current_lease.builder_id != settings.builder_id:
            log.warning("run %s: lease lost mid-build, discarding this build's result", run.id)
            store.lease_lost(run.id, settings.builder_id, "lease taken by another builder")
            return lease
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
            return lease
        destination = store.preview_dir / converted.slug
        _atomic_swap(output, destination)
        swapped = True
        preview_url = f"{settings.external_url}/preview/{converted.slug}/"
        post_url = convert.preview_post_url(preview_url, converted.url)
        store.finish_run(
            run.id,
            settings.builder_id,
            succeeded=True,
            result={
                "preview_url": preview_url,
                "post_url": post_url,
                "slug": converted.slug,
                "wall_time_seconds": round(wall_time, 3),
            },
        )
    except convert.ConversionError as exc:
        lease = keepalive.stop()
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
        lease = keepalive.stop()
        wall_time = time.monotonic() - started
        log_path.write_text(f"builder error: {exc}\n", encoding="utf-8")
        store.finish_run(
            run.id,
            settings.builder_id,
            succeeded=False,
            result={"wall_time_seconds": round(wall_time, 3), "error_class": "builder_error"},
        )
    finally:
        # `stop()` is safe to call more than once: the event is already set
        # and the thread already exited on every path above that calls it
        # first, so this is a no-op there and the only call on any path
        # that raised before reaching one.
        lease = keepalive.stop()
        shutil.rmtree(scratch, ignore_errors=True)
        if output is not None and not swapped:
            shutil.rmtree(output, ignore_errors=True)
    return lease


def _prepare_scratch(store: Store, scratch: Path) -> Path:
    _copy_site(store.site_dir, scratch)
    return scratch


def tick(store: Store, settings: BuilderSettings, leases: LeaseDirectory) -> bool:
    """One loop iteration: recover, claim at most one run, build it if claimed.

    Returns whether a run was claimed, purely so `main.run` can log something
    more useful than "polled" on every idle tick.
    """
    preview_writable = write_loop_heartbeat(settings, store)
    recover_expired_leases(store, leases, settings.builder_id)
    if not preview_writable:
        # Leave every queued run queued rather than claiming and burning it:
        # a claim here would finish the run `failed` with a misleading Hugo
        # error instead of a mount-ownership one, and the caller would have
        # to notice and re-trigger it once the mount is fixed. Not claiming
        # means the queue just waits, exactly as it does for an idle tick.
        return False
    claimed = claim_next(store, leases, settings.builder_id)
    if claimed is None:
        return False
    run, lease = claimed
    try:
        build_one(store, settings, run, leases, lease)
    finally:
        # Only drops the file if this builder still owns it: a lease this
        # builder lost mid-build already belongs to whoever took it over,
        # and unconditionally unlinking here would delete that builder's
        # live claim instead of this one's stale one (round C3 review).
        leases.release(run.id, settings.builder_id)
    return True


def is_index_locked(exc: BaseException) -> bool:
    """True for SQLite's transient "another connection holds the lock" errors.

    Matches the message, not the class: `OperationalError` also covers a
    missing table or a malformed query, which must never be deferred.
    """
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


class LockDeferral:
    """Defers a tick or a startup step that hit a locked index, boundedly.

    The index is a derived cache the api rebuilds in place (ADR 006), so a
    lock held by that rebuild passes on its own. Each locked failure is
    logged and counted; the first success clears the count, and a lock that
    lasts longer than `limit_seconds` since the first failure is re-raised.
    Anything that is not a lock error propagates untouched.
    """

    def __init__(self, limit_seconds: float = LOCK_DEFER_LIMIT_SECONDS) -> None:
        self.limit_seconds = limit_seconds
        self.since: float | None = None
        self.deferrals = 0

    def run(self, step: str, fn: Any, default: Any = False) -> Any:
        try:
            result = fn()
        except sqlite3.OperationalError as exc:
            if not is_index_locked(exc):
                raise
            now = time.monotonic()
            if self.since is None:
                self.since = now
            waited = now - self.since
            if waited > self.limit_seconds:
                log.error("index still locked after %.0fs during %s, giving up", waited, step)
                raise
            self.deferrals += 1
            log.warning(
                "index locked during %s (%s), deferring; locked for %.0fs, deferral %d",
                step,
                exc,
                waited,
                self.deferrals,
            )
            return default
        if self.since is not None:
            log.info(
                "index unlocked, resuming %s after %d deferral(s) over %.0fs",
                step,
                self.deferrals,
                time.monotonic() - self.since,
            )
        self.since = None
        self.deferrals = 0
        return result
