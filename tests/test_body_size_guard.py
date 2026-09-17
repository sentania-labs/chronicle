"""The request body ceiling is enforced on the stream, not only Content-Length.

A client that sends chunked transfer encoding and lies about (or omits) its
size must still be cut off once the actual bytes exceed the ceiling. The
full stack (TestClient over an in-memory ASGI transport) cannot actually
represent a raw socket lying about its framing, so the chunked case drives
`BodySizeLimitMiddleware` directly at the ASGI layer, the same interface
uvicorn calls it through in production.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from chronicle.api.main import MAX_REQUEST_BODY_BYTES, BodySizeLimitMiddleware
from tests.conftest import auth


def test_backup_upload_path_gets_a_higher_ceiling_than_the_default(
    admin_client: TestClient,
) -> None:
    """A restore bundle (repo/ with .git history, every image) routinely
    exceeds the default 8 MiB ceiling; /admin/backup/upload must accept a
    body past that default without needing the whole request exempted from
    size limits altogether. Body is garbage, so this only proves the size
    gate let it through (it fails validation afterwards, not on size)."""
    oversized = b"0" * (MAX_REQUEST_BODY_BYTES + (1024 * 1024))
    response = admin_client.post(
        "/admin/backup/upload",
        files={"file": ("bundle.tar.gz", oversized, "application/gzip")},
    )
    assert response.status_code != 413
    assert response.status_code == 400
    assert "not a valid bundle" in response.text


def test_declared_content_length_over_the_ceiling_is_rejected(
    client: TestClient, agent_token: str
) -> None:
    response = client.post(
        "/v1/submissions",
        content=b"x" * 10,
        headers={
            **auth(agent_token),
            "content-length": str(MAX_REQUEST_BODY_BYTES + 1),
        },
    )
    assert response.status_code == 413
    assert response.json()["error"] == "request_too_large"


async def test_a_chunked_body_that_lies_about_its_size_is_still_capped() -> None:
    # No content-length header at all: exactly what a chunked-encoded
    # request looks like once ASGI has already dechunked it. The declared
    # size check cannot see this coming; only counting bytes as they arrive
    # can.
    chunk = b"a" * (1024 * 1024)
    chunk_count = (MAX_REQUEST_BODY_BYTES // len(chunk)) + 4
    sent_chunks = 0

    async def fake_receive() -> dict[str, Any]:
        nonlocal sent_chunks
        sent_chunks += 1
        more = sent_chunks < chunk_count
        return {"type": "http.request", "body": chunk, "more_body": more}

    responses: list[dict[str, Any]] = []

    async def fake_send(message: dict[str, Any]) -> None:
        responses.append(message)

    calls = 0

    async def inner_app(scope: Any, receive: Any, send: Any) -> None:
        nonlocal calls
        calls += 1
        # A real route would call receive() in a loop until more_body is
        # False; this stands in for that loop and lets the middleware's
        # OversizedBody propagate out of one of those calls.
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break

    middleware = BodySizeLimitMiddleware(inner_app, MAX_REQUEST_BODY_BYTES)
    await middleware({"type": "http", "headers": []}, fake_receive, fake_send)  # type: ignore[arg-type]

    start = next(m for m in responses if m["type"] == "http.response.start")
    assert start["status"] == 413
    body = b"".join(m["body"] for m in responses if m["type"] == "http.response.body")
    assert b"request_too_large" in body
    # The middleware must have aborted partway through, not after buffering
    # every chunk the "client" sent.
    assert sent_chunks < chunk_count


def test_a_normal_sized_request_still_works(client: TestClient, agent_token: str) -> None:
    response = client.post(
        "/v1/submissions",
        json={"brief": "a normal brief", "materials": [], "image_ids": []},
        headers=auth(agent_token),
    )
    assert response.status_code == 201
