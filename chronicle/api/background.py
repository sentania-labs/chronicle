"""Starts and stops the publisher, watcher, reconciliation and backup threads.

ADR 013: all three run inside the api process. Kept in one module so
`main.py`'s bootstrap and shutdown stay a one-line call each, and so a test
that boots the full app (most of `tests/test_api*.py`, `test_admin_auth.py`,
and friends) starts and cleanly joins the same threads production does,
rather than a special-cased test-only path.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from . import publisher, reconcile, scheduled_backup, toolchain_check, watcher
from .admin_deps import AdminServices
from .deps import Services

log = logging.getLogger("chronicle.api.background")
THREAD_JOIN_TIMEOUT_SECONDS = 5.0


@dataclass
class Background:
    stop_event: threading.Event = field(default_factory=threading.Event)
    threads: list[threading.Thread] = field(default_factory=list)

    def stop(self) -> None:
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)


def start(services: Services, admin: AdminServices) -> Background:
    store = services.store
    settings = admin.settings
    background = Background()

    recovered = publisher.recover_stuck_runs(store)
    if recovered:
        log.info(
            "publisher: recovered %d run(s) left mid-flight by a previous api process", recovered
        )

    def reconcile_after_merge() -> None:
        reconcile.run_once_logged(store, admin)

    publisher_thread = threading.Thread(
        target=publisher.run_loop,
        args=(
            store,
            admin,
            settings.publish_poll_seconds,
            background.stop_event,
            settings.publish_queue_timeout_seconds,
        ),
        name="chronicle-publisher",
        daemon=True,
    )
    watcher_thread = threading.Thread(
        target=watcher.run_loop,
        args=(
            store,
            admin,
            settings.watch_poll_seconds,
            settings.watch_poll_max_seconds,
            background.stop_event,
            reconcile_after_merge,
        ),
        name="chronicle-watcher",
        daemon=True,
    )
    reconcile_thread = threading.Thread(
        target=reconcile.run_loop,
        args=(store, admin, settings.reconcile_interval_seconds, background.stop_event),
        name="chronicle-reconcile",
        daemon=True,
    )
    # Scheduled backups (issue #68, ADR 024): checks once a minute whether a
    # run is due under the settings saved on /admin/backup.
    backup_thread = threading.Thread(
        target=scheduled_backup.run_loop,
        args=(background.stop_event, admin.state_dir.parent, admin.state_dir, admin.instance_key),
        name="chronicle-backup",
        daemon=True,
    )
    # The admin Toolchain page's daily upstream check (issue #67, ADR 026).
    toolchain_thread = threading.Thread(
        target=toolchain_check.run_loop,
        args=(store, admin, background.stop_event),
        name="chronicle-toolchain",
        daemon=True,
    )
    background.threads = [
        publisher_thread,
        watcher_thread,
        reconcile_thread,
        backup_thread,
        toolchain_thread,
    ]
    for thread in background.threads:
        thread.start()
    return background
