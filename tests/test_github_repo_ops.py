"""GitHubRepoOps.delete_ref against a real httpx.MockTransport (issue 60).

GitHub answers a delete of a ref that no longer exists with 422 and message
"Reference does not exist", not 404, so `delete_ref` must treat that specific
422 as success the same way it already treats 404, while any other 422
(a real conflict) still raises `delete_ref_failed`.
"""

from __future__ import annotations

import httpx

from chronicle.api.github_client import GitHubApiError, TestRepoOps


def _ops(transport: httpx.MockTransport) -> TestRepoOps:
    return TestRepoOps(
        owner="o",
        repo="r",
        api_base="https://api.github.com",
        transport=transport,
        static_token="tok",
    )


def test_delete_ref_treats_reference_does_not_exist_422_as_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "Reference does not exist"})

    ops = _ops(httpx.MockTransport(handler))

    ops.delete_ref("heads/post/some-slug")  # must not raise


def test_delete_ref_treats_404_as_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    ops = _ops(httpx.MockTransport(handler))

    ops.delete_ref("heads/post/some-slug")  # must not raise


def test_delete_ref_raises_on_a_different_422() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "Reference update failed"})

    ops = _ops(httpx.MockTransport(handler))

    try:
        ops.delete_ref("heads/post/some-slug")
    except GitHubApiError as exc:
        assert exc.error_class == "delete_ref_failed"
    else:
        raise AssertionError("expected delete_ref to raise on an unrelated 422")


def test_delete_ref_treats_a_non_json_422_body_as_a_real_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, content=b"not json")

    ops = _ops(httpx.MockTransport(handler))

    try:
        ops.delete_ref("heads/post/some-slug")
    except GitHubApiError as exc:
        assert exc.error_class == "delete_ref_failed"
    else:
        raise AssertionError("expected delete_ref to raise on a non-JSON 422 body")
