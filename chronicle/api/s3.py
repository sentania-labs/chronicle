"""A minimal S3 client for scheduled backups (issue #68, ADR 024).

Three calls are all a backup target needs: put an object, list objects under
a prefix, and delete one. They are signed here with AWS Signature Version 4
over the `httpx` the api already depends on, rather than pulling in an AWS
SDK for three requests. Works against AWS and S3-compatible stores (MinIO, a
NAS's S3 service): an endpoint on `amazonaws.com` is addressed virtual-host
style (`bucket.host/key`), anything else path style (`host/bucket/key`),
which is what S3-compatible servers expect by default.

Tests pass an `httpx.MockTransport`, never the network (the same rule the
GitHub client follows, AGENTS.md).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
TIMEOUT_SECONDS = 300.0
_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


class S3Error(Exception):
    """A request S3 answered with an error, or that could not be made. The
    message carries the status and S3's own error code, never a credential."""


@dataclass(frozen=True)
class S3Location:
    endpoint: str  # https://s3.us-east-1.amazonaws.com, or https://nas.lan:9000
    bucket: str
    region: str
    access_key_id: str
    secret_access_key: str


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _uri_encode(value: str, *, keep_slash: bool) -> str:
    return quote(value, safe="/-_.~" if keep_slash else "-_.~")


def _canonical_query(query: dict[str, str]) -> str:
    return "&".join(
        f"{_uri_encode(k, keep_slash=False)}={_uri_encode(v, keep_slash=False)}"
        for k, v in sorted(query.items())
    )


def sign(
    *,
    method: str,
    host: str,
    path: str,
    query: dict[str, str],
    headers: dict[str, str],
    payload_sha256: str,
    access_key_id: str,
    secret_access_key: str,
    region: str,
    now: dt.datetime,
) -> dict[str, str]:
    """The headers to send, `Authorization` included (SigV4, service `s3`).

    `path` is the raw object path (`/bucket/key` or `/key`); it is
    URI-encoded here once, as S3 expects. Every header passed in is signed,
    along with `host`, `x-amz-date` and `x-amz-content-sha256`.
    """
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    signed: dict[str, str] = {k.lower(): " ".join(v.strip().split()) for k, v in headers.items()}
    signed["host"] = host
    signed["x-amz-date"] = amz_date
    signed["x-amz-content-sha256"] = payload_sha256
    names = sorted(signed)
    canonical_headers = "".join(f"{name}:{signed[name]}\n" for name in names)
    signed_headers = ";".join(names)
    canonical_query = _canonical_query(query)
    canonical_request = "\n".join(
        [
            method,
            _uri_encode(path, keep_slash=True),
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_sha256,
        ]
    )
    scope = f"{day}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    key = _hmac(("AWS4" + secret_access_key).encode("utf-8"), day)
    key = _hmac(key, region)
    key = _hmac(key, "s3")
    key = _hmac(key, "aws4_request")
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    out = {name: signed[name] for name in names if name != "host"}
    out["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return out


class S3Client:
    def __init__(self, location: S3Location, transport: httpx.BaseTransport | None = None):
        self.location = location
        parts = urlsplit(location.endpoint.rstrip("/"))
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise S3Error(f"endpoint {location.endpoint!r} is not an http(s) URL")
        self._scheme = parts.scheme
        self._virtual = parts.hostname is not None and parts.hostname.endswith("amazonaws.com")
        self._host = f"{location.bucket}.{parts.netloc}" if self._virtual else parts.netloc
        self._base_path = parts.path.rstrip("/")
        self._http = httpx.Client(timeout=TIMEOUT_SECONDS, transport=transport)

    def close(self) -> None:
        self._http.close()

    def _path(self, key: str) -> str:
        prefix = self._base_path if self._virtual else f"{self._base_path}/{self.location.bucket}"
        return f"{prefix}/{key}" if key else (prefix or "/")

    def _request(
        self,
        method: str,
        key: str,
        *,
        query: dict[str, str] | None = None,
        content: Iterable[bytes] | bytes | None = None,
        payload_sha256: str = EMPTY_SHA256,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        path = self._path(key)
        query = query or {}
        signed = sign(
            method=method,
            host=self._host,
            path=path,
            query=query,
            headers=headers or {},
            payload_sha256=payload_sha256,
            access_key_id=self.location.access_key_id,
            secret_access_key=self.location.secret_access_key,
            region=self.location.region,
            now=dt.datetime.now(tz=dt.UTC),
        )
        # The query string is built with the same encoding that was signed,
        # never left to the HTTP client, so the two cannot disagree.
        url = f"{self._scheme}://{self._host}{_uri_encode(path, keep_slash=True)}"
        if query:
            url += "?" + _canonical_query(query)
        try:
            response = self._http.request(method, url, content=content, headers=signed)
        except httpx.HTTPError as exc:
            raise S3Error(f"{method} {key or '/'}: {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 300:
            code = _error_code(response.content)
            raise S3Error(f"{method} {key or '/'} returned {response.status_code} {code}".strip())
        return response

    def put_file(self, key: str, path: Path) -> None:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        size = path.stat().st_size

        def body() -> Iterable[bytes]:
            with path.open("rb") as handle:
                yield from iter(lambda: handle.read(1 << 20), b"")

        self._request(
            "PUT",
            key,
            content=body(),
            payload_sha256=digest.hexdigest(),
            headers={"content-length": str(size)},
        )

    def put_bytes(self, key: str, data: bytes) -> None:
        self._request(
            "PUT",
            key,
            content=data,
            payload_sha256=hashlib.sha256(data).hexdigest(),
            headers={"content-length": str(len(data))},
        )

    def delete(self, key: str) -> None:
        self._request("DELETE", key)

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            query = {"list-type": "2", "prefix": prefix}
            if token:
                query["continuation-token"] = token
            root = ET.fromstring(self._request("GET", "", query=query).content)
            keys.extend(
                (item.findtext(f"{_S3_NS}Key") or "") for item in root.iter(f"{_S3_NS}Contents")
            )
            if (root.findtext(f"{_S3_NS}IsTruncated") or "").lower() != "true":
                return [key for key in keys if key]
            token = root.findtext(f"{_S3_NS}NextContinuationToken")
            if not token:
                return [key for key in keys if key]


def _error_code(body: bytes) -> str:
    try:
        return ET.fromstring(body).findtext("Code") or ""
    except ET.ParseError:
        return ""
