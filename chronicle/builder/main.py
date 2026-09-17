"""Chronicle builder entry point (spec section 8).

Polls `data/repo/runs/queue/` for `preview` runs, claims one at a time with a
lease (see `leases.py`), and drives one Hugo build per claimed run. A
crashed builder's lease expires and the next tick, on this builder or
another, puts the run back on the queue (`runner.recover_expired_leases`).
`CHRONICLE_BUILDER_ONCE=1` runs a single tick and exits, which is what the
integration test and a one-shot debug session both want instead of a
process that runs forever.
"""

from __future__ import annotations

import logging
import time

from ..api.store import Store
from . import hugo
from .leases import LeaseDirectory
from .runner import recover_expired_leases, tick
from .settings import BuilderSettings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("chronicle.builder")


def run() -> None:
    settings = BuilderSettings.from_env()
    log.info(
        "chronicle-builder %s starting, data_dir=%s, hugo=%s",
        settings.builder_id,
        settings.data_dir,
        hugo.version_line(settings.hugo_bin),
    )
    store = Store.open(settings.data_dir)
    leases = LeaseDirectory(settings.leases_dir, settings.lease_seconds)

    # A builder that starts after a crash must recover before it claims
    # anything, or it could pick a run its predecessor is still (wrongly)
    # marked as building.
    recovered = recover_expired_leases(store, leases, settings.builder_id)
    if recovered:
        log.info("recovered %d run(s) left mid-build by a previous builder", recovered)

    try:
        while True:
            claimed = tick(store, settings, leases)
            if settings.once:
                return
            if not claimed:
                time.sleep(settings.poll_seconds)
    finally:
        store.close()


if __name__ == "__main__":
    run()
