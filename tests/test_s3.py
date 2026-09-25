"""The minimal S3 client scheduled backups use (issue #68)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest

from chronicle.api import s3

# AWS's own published SigV4 examples for S3 (header-based auth).
EXAMPLE = {
    "access_key_id": "AKIAIOSFODNN7EXAMPLE",
    "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "region": "us-east-1",
    "now": dt.datetime(2013, 5, 24, tzinfo=dt.UTC),
}


def test_get_object_signature_matches_the_aws_example() -> None:
    headers = s3.sign(
        method="GET",
        host="examplebucket.s3.amazonaws.com",
        path="/test.txt",
        query={},
        headers={"Range": "bytes=0-9"},
        payload_sha256=s3.EMPTY_SHA256,
        **EXAMPLE,  # type: ignore[arg-type]
    )
    assert headers["authorization"].endswith(
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


def test_list_objects_signature_matches_the_aws_example() -> None:
    headers = s3.sign(
        method="GET",
        host="examplebucket.s3.amazonaws.com",
        path="/",
        query={"max-keys": "2", "prefix": "J"},
        headers={},
        payload_sha256=s3.EMPTY_SHA256,
        **EXAMPLE,  # type: ignore[arg-type]
    )
    assert headers["authorization"].endswith(
        "Signature=34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"
    )


def _client(endpoint: str, handler: object) -> s3.S3Client:
    location = s3.S3Location(
        endpoint=endpoint,
        bucket="backups",
        region="us-east-1",
        access_key_id="AKID",
        secret_access_key="SECRET-NEVER-SHOWN",
    )
    return s3.S3Client(location, transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def test_a_non_aws_endpoint_is_addressed_path_style(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    bundle = tmp_path / "b.tar.gz"
    bundle.write_bytes(b"x" * 10)
    _client("https://nas.lan:9000", handler).put_file("pre/b.tar.gz", bundle)
    request = seen[0]
    assert str(request.url) == "https://nas.lan:9000/backups/pre/b.tar.gz"
    assert request.content == b"x" * 10
    assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKID/")
    assert "SECRET" not in str(request.headers)


def test_an_aws_endpoint_is_addressed_virtual_host_style() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    _client("https://s3.us-east-1.amazonaws.com", handler).delete("k.tar.gz")
    assert str(seen[0].url) == "https://backups.s3.us-east-1.amazonaws.com/k.tar.gz"


def test_listing_follows_continuation_tokens() -> None:
    pages = [
        b"<ListBucketResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'>"
        b"<IsTruncated>true</IsTruncated><NextContinuationToken>t2</NextContinuationToken>"
        b"<Contents><Key>p/a</Key></Contents></ListBucketResult>",
        b"<ListBucketResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'>"
        b"<IsTruncated>false</IsTruncated><Contents><Key>p/b</Key></Contents></ListBucketResult>",
    ]
    tokens: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tokens.append(request.url.params.get("continuation-token"))
        return httpx.Response(200, content=pages[len(tokens) - 1])

    assert _client("https://nas.lan", handler).list_keys("p/") == ["p/a", "p/b"]
    assert tokens == [None, "t2"]


def test_an_error_names_the_status_and_code_but_never_the_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"<Error><Code>SignatureDoesNotMatch</Code></Error>")

    with pytest.raises(s3.S3Error) as caught:
        _client("https://nas.lan", handler).delete("k")
    assert "403 SignatureDoesNotMatch" in str(caught.value)
    assert "SECRET" not in str(caught.value)


def test_the_signed_host_matches_what_httpx_sends_for_a_default_port() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    _client("https://nas.lan:443", handler).delete("k")
    signed = seen[0].headers["authorization"]
    assert seen[0].headers["host"] == "nas.lan"
    assert "SignedHeaders=host;" in signed


def test_a_dotted_bucket_on_aws_is_addressed_path_style() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    location = s3.S3Location(
        endpoint="https://s3.us-east-1.amazonaws.com",
        bucket="my.backups",
        region="us-east-1",
        access_key_id="AKID",
        secret_access_key="x",
    )
    s3.S3Client(location, transport=httpx.MockTransport(handler)).delete("k")
    assert str(seen[0].url) == "https://s3.us-east-1.amazonaws.com/my.backups/k"
