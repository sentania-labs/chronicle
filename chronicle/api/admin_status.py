"""Builds the read-only status view the admin status page and /readyz share."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .admin_deps import AdminServices
from .deps import Services
from .github_app import readiness_state
from .index import SCHEMA_VERSION
from .models import DRAFT_STATUSES, RUN_STATUSES, SUBMISSION_STATUSES

BUILDER_HEARTBEAT_PATH = ("state", "builder", "heartbeat.json")


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def git_health(repo_dir: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        return f"unhealthy: {exc}"
    if result.stdout.strip():
        return "unhealthy: working tree not clean"
    return "clean, HEAD resolves"


def toolchain_summary(admin: AdminServices) -> dict[str, Any]:
    stored = admin.read_toolchain() or {"hugo_version": "unknown", "submodules": []}
    builder_version = admin.settings.builder_hugo_version
    site_version = stored.get("hugo_version", "unknown")
    match = builder_version != "unknown" and builder_version == site_version
    return {
        "hugo_version": site_version,
        "builder_hugo_version": builder_version,
        "match": match,
        "submodules": stored.get("submodules", []),
    }


def builder_heartbeat(data_dir: Path) -> dict[str, Any] | None:
    path = data_dir.joinpath(*BUILDER_HEARTBEAT_PATH)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return dict(loaded)


def preview_run_counts(services: Services) -> dict[str, int]:
    counts = services.store.run_counts("preview")
    return {status: counts.get(status, 0) for status in RUN_STATUSES}


def build_status(admin: AdminServices, services: Services) -> dict[str, Any]:
    store = services.store
    app_record = admin.github_store.load()
    digest_status = admin.read_digest_status()

    submissions_by_status = {
        status: len(store.list_submissions(status)) for status in SUBMISSION_STATUSES
    }
    drafts_by_status = {status: len(store.list_drafts(status)) for status in DRAFT_STATUSES}

    disk_use = {
        "repo": _human(_dir_size(store.repo_dir)),
        "images": _human(_dir_size(store.images_dir)),
        "state": _human(_dir_size(store.state_dir)),
        "preview": _human(_dir_size(store.preview_dir)),
        "site": _human(_dir_size(store.site_dir)),
    }

    return {
        "github_app_state": readiness_state(app_record),
        "github_repo": app_record.owner_repo if app_record else None,
        "github_default_branch": app_record.default_branch if app_record else None,
        "last_digest_at": digest_status.get("finished_at") if digest_status else None,
        "post_count": len(store.list_posts()),
        "toolchain": toolchain_summary(admin),
        "submissions_by_status": submissions_by_status,
        "drafts_by_status": drafts_by_status,
        "disk_use": disk_use,
        "git_health": git_health(store.repo_dir),
        "index_schema_version": SCHEMA_VERSION,
        "digest_running": admin.digest_running,
        "builder_heartbeat": builder_heartbeat(store.data_dir),
        "preview_runs_by_status": preview_run_counts(services),
    }
