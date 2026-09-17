"""The builder's environment, collected once (the api's `settings.py` pattern).

Every value has a default that works in a container with only
`CHRONICLE_DATA_DIR` and `CHRONICLE_EXTERNAL_URL` set, which is what
docker-compose.yml and examples/k8s/ pass.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path

from ..api.settings import DEFAULT_EXTERNAL_URL, EXTERNAL_URL_ENV

DATA_DIR_ENV = "CHRONICLE_DATA_DIR"
BUILDER_ID_ENV = "CHRONICLE_BUILDER_ID"
POLL_SECONDS_ENV = "CHRONICLE_BUILDER_POLL_SECONDS"
LEASE_SECONDS_ENV = "CHRONICLE_BUILDER_LEASE_SECONDS"
WORK_DIR_ENV = "CHRONICLE_BUILDER_WORK_DIR"
HUGO_BIN_ENV = "CHRONICLE_BUILDER_HUGO_BIN"
BUILD_TIMEOUT_ENV = "CHRONICLE_BUILDER_TIMEOUT_SECONDS"
ONCE_ENV = "CHRONICLE_BUILDER_ONCE"

DEFAULT_POLL_SECONDS = 5.0
# Comfortably longer than the build timeout: a lease must not expire under a
# build that is still legitimately running, or a second builder would start
# the same run while the first is still writing its output.
DEFAULT_LEASE_SECONDS = 900.0
DEFAULT_BUILD_TIMEOUT_SECONDS = 600.0
DEFAULT_HUGO_BIN = "hugo"
# Under `data/`, not `data/preview/`: the preview container mounts only the
# preview volume, and a scratch tree holding a full site clone plus Hugo's
# resource cache has no business being reachable through it even by
# accident (ADR 011). It shares the data volume the builder already has
# write access to, so no new volume is needed.
WORK_DIR_NAME = "builder-work"


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def default_builder_id() -> str:
    """Host plus pid: unique per container, and readable in a lease file."""
    return f"{socket.gethostname()}-{os.getpid()}"


@dataclass(frozen=True)
class BuilderSettings:
    data_dir: Path
    external_url: str
    builder_id: str
    poll_seconds: float
    lease_seconds: float
    work_dir: Path
    hugo_bin: str
    build_timeout_seconds: float
    once: bool

    @classmethod
    def from_env(cls) -> BuilderSettings:
        raw_data_dir = os.environ.get(DATA_DIR_ENV, "").strip()
        if not raw_data_dir:
            raise SystemExit(f"{DATA_DIR_ENV} is not set")
        data_dir = Path(raw_data_dir)
        work_dir = Path(os.environ.get(WORK_DIR_ENV, "").strip() or data_dir / WORK_DIR_NAME)
        return cls(
            data_dir=data_dir,
            external_url=os.environ.get(EXTERNAL_URL_ENV, DEFAULT_EXTERNAL_URL).rstrip("/"),
            builder_id=os.environ.get(BUILDER_ID_ENV, "").strip() or default_builder_id(),
            poll_seconds=_float_env(POLL_SECONDS_ENV, DEFAULT_POLL_SECONDS),
            lease_seconds=_float_env(LEASE_SECONDS_ENV, DEFAULT_LEASE_SECONDS),
            work_dir=work_dir,
            hugo_bin=os.environ.get(HUGO_BIN_ENV, "").strip() or DEFAULT_HUGO_BIN,
            build_timeout_seconds=_float_env(BUILD_TIMEOUT_ENV, DEFAULT_BUILD_TIMEOUT_SECONDS),
            once=_truthy(os.environ.get(ONCE_ENV)),
        )

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state" / "builder"

    @property
    def leases_dir(self) -> Path:
        return self.state_dir / "leases"

    @property
    def heartbeat_path(self) -> Path:
        return self.state_dir / "heartbeat.json"

    @property
    def scratch_dir(self) -> Path:
        return self.work_dir / "scratch"

    @property
    def resource_dir(self) -> Path:
        return self.work_dir / "resources"

    @property
    def cache_dir(self) -> Path:
        return self.work_dir / "cache"
