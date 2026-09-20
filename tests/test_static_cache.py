"""`/static` revalidates on every use, so a deploy cannot leave a stale asset (#35).

The UI's JavaScript carries data-safety behaviour (the local backup); a browser
that kept running an old `editor.js` after an image rebuild hid a fix once.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

ASSETS = ["/static/editor.js", "/static/ui.js", "/static/style.css", "/static/vendor/lattice.css"]


@pytest.mark.parametrize("path", ASSETS)
def test_a_static_response_says_to_revalidate(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["etag"]


def test_an_unchanged_asset_is_a_304_that_still_says_to_revalidate(client: TestClient) -> None:
    first = client.get("/static/editor.js")
    again = client.get("/static/editor.js", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
    assert again.headers["cache-control"] == "no-cache"


def test_a_changed_asset_is_fetched_not_answered_304(client: TestClient) -> None:
    response = client.get("/static/editor.js", headers={"If-None-Match": '"not-the-current-etag"'})
    assert response.status_code == 200
    assert "cache-control" in response.headers


def test_only_one_cache_control_header_is_sent(client: TestClient) -> None:
    response = client.get("/static/editor.js")
    assert len(response.headers.get_list("cache-control")) == 1


def test_the_policy_is_not_leaked_onto_other_routes(client: TestClient) -> None:
    assert "cache-control" not in client.get("/healthz").headers
