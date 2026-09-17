"""Chronicle preview entry point (spec section 8, ADR 010).

Serves `data/preview/<slug>/...` at the path prefix `/preview/<slug>/...`.
No directory listing, ever: a directory request without an `index.html`
inside it is a 404, the same as a slug that was never built. Path
containment is the one thing this module has to get right: every request is
resolved against the preview root and rejected unless the resolved path is
still inside it, which catches `..`, its percent-encoded form, and a
symlink that points outside the root in the same check.
"""

from __future__ import annotations

import http.server
import logging
import mimetypes
import os
import urllib.parse
from collections.abc import Callable
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("chronicle.preview")

PREVIEW_DIR_ENV = "CHRONICLE_PREVIEW_DIR"
PREVIEW_PORT_ENV = "CHRONICLE_PREVIEW_PORT"
DEFAULT_PREVIEW_DIR = "/data/preview"
DEFAULT_PORT = 8090
PREVIEW_PREFIX = "/preview/"

# mimetypes already knows html, css, and png; svg needs a hand because some
# stdlib builds still guess it as application/octet-stream or text/plain.
mimetypes.add_type("image/svg+xml", ".svg")


def resolve_within(root: Path, rel: str) -> Path | None:
    """The file `rel` names inside `root`, or None if it would escape it.

    `root` and the candidate are both fully resolved (symlinks included)
    before the containment check, so a symlink inside the preview tree that
    points outside `root` is refused exactly like a literal `..` would be:
    both produce a resolved path that fails `relative_to`.
    """
    candidate = (root / rel.lstrip("/")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def content_type_for(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


class PreviewHandler(http.server.BaseHTTPRequestHandler):
    directory: Path  # set by make_handler via a subclass

    server_version = "ChroniclePreview/1.0"

    def do_GET(self) -> None:  # noqa: N802 (stdlib's naming, not ours)
        self._serve(send_body=True)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(send_body=False)

    def _serve(self, send_body: bool) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        raw_path = urllib.parse.unquote(parsed.path)

        if raw_path == "/healthz":
            self._respond(200, b"ok\n", "text/plain", send_body)
            return

        if not raw_path.startswith(PREVIEW_PREFIX):
            self._respond(404, b"not found\n", "text/plain", send_body)
            return

        root = self.directory.resolve()
        target = resolve_within(root, raw_path[len(PREVIEW_PREFIX) :])
        if target is None or not target.exists():
            self._respond(404, b"not found\n", "text/plain", send_body)
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self._respond(404, b"not found\n", "text/plain", send_body)
            return

        data = target.read_bytes()
        self._respond(200, data, content_type_for(target), send_body)

    def _respond(self, status: int, body: bytes, content_type: str, send_body: bool) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        log.info("%s - %s", self.address_string(), format % args)


def make_handler(directory: Path) -> Callable[..., PreviewHandler]:
    def handler(*args: object, **kwargs: object) -> PreviewHandler:
        instance = PreviewHandler.__new__(PreviewHandler)
        instance.directory = directory
        PreviewHandler.__init__(instance, *args, **kwargs)  # type: ignore[arg-type]
        return instance

    return handler


def run() -> None:
    directory = Path(os.environ.get(PREVIEW_DIR_ENV, DEFAULT_PREVIEW_DIR))
    port = int(os.environ.get(PREVIEW_PORT_ENV, str(DEFAULT_PORT)))
    directory.mkdir(parents=True, exist_ok=True)

    # ThreadingHTTPServer, not plain HTTPServer or TCPServer: a single-threaded
    # server lets one slow client block every other request, including the
    # Docker HEALTHCHECK, and it also sets allow_reuse_address so a container
    # restarted while the old socket is in TIME_WAIT can still bind the port.
    with http.server.ThreadingHTTPServer(("0.0.0.0", port), make_handler(directory)) as httpd:
        log.info(
            "chronicle-preview serving %s on port %s, prefix %s", directory, port, PREVIEW_PREFIX
        )
        httpd.serve_forever()


if __name__ == "__main__":
    run()
