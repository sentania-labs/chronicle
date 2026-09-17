"""The preview server: path prefixing, containment, and content types."""

from __future__ import annotations

import http.client
import http.server
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from chronicle.preview.main import make_handler, resolve_within


@pytest.fixture
def preview_root(tmp_path: Path) -> Path:
    slug_dir = tmp_path / "my-slug"
    slug_dir.mkdir()
    (slug_dir / "index.html").write_text("<html>hi</html>")
    (slug_dir / "style.css").write_text("body{}")
    images = slug_dir / "images" / "my-slug"
    images.mkdir(parents=True)
    (images / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (images / "pic.svg").write_text("<svg></svg>")
    (tmp_path / "empty-slug").mkdir()
    return tmp_path


@pytest.fixture
def running_server(preview_root: Path) -> Iterator[tuple[str, int]]:
    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(preview_root)) as httpd:
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield "127.0.0.1", port
        finally:
            httpd.shutdown()
            thread.join(timeout=5)


def _get(host: str, port: int, path: str) -> tuple[int, str, bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", path)
    response = conn.getresponse()
    content_type = response.getheader("Content-Type", "")
    body = response.read()
    conn.close()
    return response.status, content_type, body


def test_serves_a_built_slug(running_server: tuple[str, int]) -> None:
    host, port = running_server
    status, content_type, body = _get(host, port, "/preview/my-slug/")
    assert status == 200
    assert "text/html" in content_type
    assert body == b"<html>hi</html>"


def test_index_html_for_a_directory_without_trailing_slash(
    running_server: tuple[str, int],
) -> None:
    host, port = running_server
    status, _, _ = _get(host, port, "/preview/my-slug")
    assert status == 200


def test_404_for_unknown_slug(running_server: tuple[str, int]) -> None:
    host, port = running_server
    status, _, _ = _get(host, port, "/preview/does-not-exist/")
    assert status == 404


def test_404_for_a_directory_with_no_index(running_server: tuple[str, int]) -> None:
    host, port = running_server
    status, _, _ = _get(host, port, "/preview/empty-slug/")
    assert status == 404


def test_no_directory_listing(running_server: tuple[str, int]) -> None:
    host, port = running_server
    status, _, body = _get(host, port, "/preview/my-slug/images/")
    assert status == 404
    assert b"<a href" not in body


def test_traversal_is_refused(running_server: tuple[str, int]) -> None:
    host, port = running_server
    status, _, _ = _get(host, port, "/preview/my-slug/../../etc/passwd")
    assert status == 404


def test_encoded_traversal_is_refused(running_server: tuple[str, int]) -> None:
    host, port = running_server
    status, _, _ = _get(host, port, "/preview/my-slug/%2e%2e/%2e%2e/etc/passwd")
    assert status == 404


def test_symlink_escape_is_refused(preview_root: Path, running_server: tuple[str, int]) -> None:
    escape_target = preview_root.parent / "outside.txt"
    escape_target.write_text("secret")
    (preview_root / "my-slug" / "escape").symlink_to(escape_target)
    host, port = running_server
    status, _, _ = _get(host, port, "/preview/my-slug/escape")
    assert status == 404


def test_path_outside_preview_prefix_is_404(running_server: tuple[str, int]) -> None:
    host, port = running_server
    status, _, _ = _get(host, port, "/anything")
    assert status == 404


def test_health_endpoint(running_server: tuple[str, int]) -> None:
    host, port = running_server
    status, _, _ = _get(host, port, "/healthz")
    assert status == 200


@pytest.mark.parametrize(
    ("path", "expected_fragment"),
    [
        ("/preview/my-slug/", "text/html"),
        ("/preview/my-slug/style.css", "text/css"),
        ("/preview/my-slug/images/my-slug/pic.png", "image/png"),
        ("/preview/my-slug/images/my-slug/pic.svg", "image/svg+xml"),
    ],
)
def test_content_types(running_server: tuple[str, int], path: str, expected_fragment: str) -> None:
    host, port = running_server
    _, content_type, _ = _get(host, port, path)
    assert expected_fragment in content_type


def test_resolve_within_rejects_dotdot(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    assert resolve_within(root, "../outside") is None


def test_resolve_within_allows_nested_path(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "a").mkdir()
    assert resolve_within(root, "a") == root / "a"
