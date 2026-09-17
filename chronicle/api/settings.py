"""Environment-driven settings, collected once instead of read ad hoc.

C0 and C1 read a handful of `CHRONICLE_*` variables inline, next to their one
call site (`DATA_DIR_ENV` in `main.py`, `PREVIEW_DIR_ENV` in `preview/main.py`).
C2 adds enough settings that all feed the same admin bootstrap flow (the
external URL and the manifest redirect it builds, the cookie security flag,
the digest source, the builder's pinned Hugo version, and a GitHub API base
tests can override) that collecting them in one place is worth the small new
pattern. Nothing here is secret; secrets live in `credentials.py` behind the
instance key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

EXTERNAL_URL_ENV = "CHRONICLE_EXTERNAL_URL"
COOKIE_SECURE_ENV = "CHRONICLE_COOKIE_SECURE"
DIGEST_REPO_URL_ENV = "CHRONICLE_DIGEST_REPO_URL"
BUILDER_HUGO_VERSION_ENV = "CHRONICLE_BUILDER_HUGO_VERSION"
GITHUB_API_BASE_ENV = "CHRONICLE_GITHUB_API_BASE"
GITHUB_WEB_BASE_ENV = "CHRONICLE_GITHUB_WEB_BASE"
APP_NAME_PREFIX_ENV = "CHRONICLE_GITHUB_APP_NAME"
# ADR 012: test-only, never mentioned in examples/k8s/.
GITHUB_TEST_TOKEN_ENV = "CHRONICLE_GITHUB_TEST_TOKEN"
ALLOW_TEST_TOKEN_ENV = "CHRONICLE_ALLOW_TEST_TOKEN"
GITHUB_TEST_REPO_ENV = "CHRONICLE_GITHUB_TEST_REPO"
# ADR 013.
WATCH_POLL_SECONDS_ENV = "CHRONICLE_WATCH_POLL_SECONDS"
WATCH_POLL_MAX_SECONDS_ENV = "CHRONICLE_WATCH_POLL_MAX_SECONDS"
RECONCILE_INTERVAL_SECONDS_ENV = "CHRONICLE_RECONCILE_INTERVAL_SECONDS"

DEFAULT_EXTERNAL_URL = "http://localhost:8080"
DEFAULT_GITHUB_API_BASE = "https://api.github.com"
DEFAULT_GITHUB_WEB_BASE = "https://github.com"
DEFAULT_APP_NAME_PREFIX = "chronicle"
UNKNOWN_HUGO_VERSION = "unknown"
DEFAULT_WATCH_POLL_SECONDS = 60.0
DEFAULT_WATCH_POLL_MAX_SECONDS = 900.0
DEFAULT_RECONCILE_INTERVAL_SECONDS = 3600.0


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


class TestTokenNotAllowed(Exception):
    """`CHRONICLE_GITHUB_TEST_TOKEN` is set without `CHRONICLE_ALLOW_TEST_TOKEN=1`.

    A token alone must never be enough to grant test-token mode (ADR 012):
    this is a startup refusal, not a silent downgrade, so a real deployment
    that picks up a leftover test token from its environment fails loudly
    instead of quietly running against a personal access token no one
    intended it to use.
    """


@dataclass(frozen=True)
class Settings:
    external_url: str
    cookie_secure: bool
    digest_repo_url: str | None
    builder_hugo_version: str
    github_api_base: str
    github_web_base: str
    app_name_prefix: str
    github_test_token: str | None
    github_test_repo: str | None
    watch_poll_seconds: float
    watch_poll_max_seconds: float
    reconcile_interval_seconds: float

    @classmethod
    def from_env(cls) -> Settings:
        test_token = os.environ.get(GITHUB_TEST_TOKEN_ENV, "").strip() or None
        allow_test_token = _truthy(os.environ.get(ALLOW_TEST_TOKEN_ENV))
        if test_token and not allow_test_token:
            raise TestTokenNotAllowed(
                f"{GITHUB_TEST_TOKEN_ENV} is set but {ALLOW_TEST_TOKEN_ENV}=1 is not;"
                " refusing to start rather than silently run in test-token mode"
                " (see docs/decisions/012-test-token-github-mode.md)"
            )
        return cls(
            external_url=os.environ.get(EXTERNAL_URL_ENV, DEFAULT_EXTERNAL_URL).rstrip("/"),
            cookie_secure=_truthy(os.environ.get(COOKIE_SECURE_ENV)),
            digest_repo_url=os.environ.get(DIGEST_REPO_URL_ENV, "").strip() or None,
            builder_hugo_version=os.environ.get(
                BUILDER_HUGO_VERSION_ENV, UNKNOWN_HUGO_VERSION
            ).strip()
            or UNKNOWN_HUGO_VERSION,
            github_api_base=os.environ.get(GITHUB_API_BASE_ENV, DEFAULT_GITHUB_API_BASE).rstrip(
                "/"
            ),
            github_web_base=os.environ.get(GITHUB_WEB_BASE_ENV, DEFAULT_GITHUB_WEB_BASE).rstrip(
                "/"
            ),
            app_name_prefix=os.environ.get(APP_NAME_PREFIX_ENV, DEFAULT_APP_NAME_PREFIX).strip()
            or DEFAULT_APP_NAME_PREFIX,
            github_test_token=test_token,
            github_test_repo=os.environ.get(GITHUB_TEST_REPO_ENV, "").strip() or None,
            watch_poll_seconds=_float_env(WATCH_POLL_SECONDS_ENV, DEFAULT_WATCH_POLL_SECONDS),
            watch_poll_max_seconds=_float_env(
                WATCH_POLL_MAX_SECONDS_ENV, DEFAULT_WATCH_POLL_MAX_SECONDS
            ),
            reconcile_interval_seconds=_float_env(
                RECONCILE_INTERVAL_SECONDS_ENV, DEFAULT_RECONCILE_INTERVAL_SECONDS
            ),
        )

    @property
    def test_token_mode(self) -> bool:
        return bool(self.github_test_token)
