"""Chronicle preview entry point, round C0.

Serves a directory of static files with directory listing disabled. This is
real, not a placeholder: it is cheap to build correctly with the standard
library. What is missing until the preview builder arrives (section 8 of the
spec) is per-slug path prefixing, so today every build shares one flat root.
"""

from __future__ import annotations

import http.server
import logging
import os
from collections.abc import Callable

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("chronicle.preview")

PREVIEW_DIR_ENV = "CHRONICLE_PREVIEW_DIR"
PREVIEW_PORT_ENV = "CHRONICLE_PREVIEW_PORT"
DEFAULT_PREVIEW_DIR = "/data/preview"
DEFAULT_PORT = 8090


class NoListingHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler with directory listing refused outright."""

    def list_directory(self, path: str) -> None:  # type: ignore[override]
        self.send_error(403, "directory listing is disabled")
        return None


def make_handler(directory: str) -> Callable[..., NoListingHandler]:
    def handler(*args: object, **kwargs: object) -> NoListingHandler:
        return NoListingHandler(*args, directory=directory, **kwargs)  # type: ignore[arg-type]

    return handler


def run() -> None:
    directory = os.environ.get(PREVIEW_DIR_ENV, DEFAULT_PREVIEW_DIR)
    port = int(os.environ.get(PREVIEW_PORT_ENV, str(DEFAULT_PORT)))
    os.makedirs(directory, exist_ok=True)

    # ThreadingHTTPServer, not plain HTTPServer or TCPServer: a single-threaded
    # server lets one slow client block every other request, including the
    # Docker HEALTHCHECK, and it also sets allow_reuse_address so a container
    # restarted while the old socket is in TIME_WAIT can still bind the port.
    with http.server.ThreadingHTTPServer(("0.0.0.0", port), make_handler(directory)) as httpd:
        log.info("chronicle-preview serving %s on port %s, listing disabled", directory, port)
        httpd.serve_forever()


if __name__ == "__main__":
    run()
