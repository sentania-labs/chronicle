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

DEFAULT_EXTERNAL_URL = "http://localhost:8080"
DEFAULT_GITHUB_API_BASE = "https://api.github.com"
DEFAULT_GITHUB_WEB_BASE = "https://github.com"
DEFAULT_APP_NAME_PREFIX = "chronicle"
UNKNOWN_HUGO_VERSION = "unknown"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    external_url: str
    cookie_secure: bool
    digest_repo_url: str | None
    builder_hugo_version: str
    github_api_base: str
    github_web_base: str
    app_name_prefix: str

    @classmethod
    def from_env(cls) -> Settings:
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
        )
