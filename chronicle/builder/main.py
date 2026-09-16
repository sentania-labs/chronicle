"""Chronicle builder entry point, round C0 placeholder.

The real builder (section 8 of the spec) holds a clone of the blog repo,
watches the run queue in the data directory, and drives Hugo builds keyed to
the pinned HUGO_VERSION baked into its image. None of that exists yet: this
entry point only proves the builder image boots and exits cleanly, so it is
labelled a placeholder rather than a stand-in implementation.
"""

from __future__ import annotations

import logging
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("chronicle.builder")


def run() -> None:
    data_dir = os.environ.get("CHRONICLE_DATA_DIR", "(unset)")
    log.info("chronicle-builder placeholder starting, data_dir=%s", data_dir)
    log.info("chronicle-builder placeholder has no run queue to watch in this round, exiting")


if __name__ == "__main__":
    run()
