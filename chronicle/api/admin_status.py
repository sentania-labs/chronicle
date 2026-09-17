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
from .reconcile import APPLICABLE_RESOLUTIONS

BUILDER_HEARTBEAT_PATH = ("state", "builder", "heartbeat.json")
PUBLISHER_HEARTBEAT_PATH = ("state", "publisher", "heartbeat.json")
WATCHER_HEARTBEAT_PATH = ("state", "watcher", "heartbeat.json")
RECONCILE_HEARTBEAT_PATH = ("state", "reconcile", "heartbeat.json")
# Matches routes/admin.py's LAST_BACKUP_FILE_NAME; duplicated as a literal
# rather than imported to avoid a routes -> status -> routes import cycle.
LAST_BACKUP_FILE_NAME = "last_backup.json"


def _last_backup_at(admin: AdminServices) -> str | None:
    path = admin.state_dir / LAST_BACKUP_FILE_NAME
    if not path.exists():
        return None
    try:
        return str(json.loads(path.read_text(encoding="utf-8"))["created_at"])
    except (OSError, json.JSONDecodeError, KeyError):
        return None


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


def toolchain_summary(admin: AdminServices, heartbeat: dict[str, Any] | None) -> dict[str, Any]:
    """The builder's actual Hugo version, from its heartbeat when one exists.

    C3 noticed this compared the site's toolchain against
    `CHRONICLE_BUILDER_HUGO_VERSION`, a value set once at deploy time, not
    against what the builder container actually reports it is running;
    the heartbeat is what `runner.write_heartbeat` stamps from
    `hugo.installed_version` every poll tick, so it reflects a rebuilt
    image immediately. The env var is now only a fallback for a builder
    that has never completed a single poll loop.
    """
    stored = admin.read_toolchain() or {"hugo_version": "unknown", "submodules": []}
    builder_version = (
        heartbeat.get("hugo_version") if heartbeat else None
    ) or admin.settings.builder_hugo_version
    site_version = stored.get("hugo_version", "unknown")
    match = builder_version != "unknown" and builder_version == site_version
    return {
        "hugo_version": site_version,
        "builder_hugo_version": builder_version,
        "match": match,
        "submodules": stored.get("submodules", []),
    }


def _read_heartbeat(data_dir: Path, path_parts: tuple[str, ...]) -> dict[str, Any] | None:
    path = data_dir.joinpath(*path_parts)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return dict(loaded)


def builder_heartbeat(data_dir: Path) -> dict[str, Any] | None:
    return _read_heartbeat(data_dir, BUILDER_HEARTBEAT_PATH)


def publisher_heartbeat(data_dir: Path) -> dict[str, Any] | None:
    return _read_heartbeat(data_dir, PUBLISHER_HEARTBEAT_PATH)


def watcher_heartbeat(data_dir: Path) -> dict[str, Any] | None:
    return _read_heartbeat(data_dir, WATCHER_HEARTBEAT_PATH)


def reconcile_heartbeat(data_dir: Path) -> dict[str, Any] | None:
    return _read_heartbeat(data_dir, RECONCILE_HEARTBEAT_PATH)


def github_app_state(admin: AdminServices) -> str:
    """`readiness_state`, unless test-token mode is active (ADR 012).

    Test-token mode never touches `github-app.json`, so `readiness_state`
    would otherwise honestly (and misleadingly) say "not configured" while
    publish, watch, and reconcile are all working against a real repo.
    """
    if admin.settings.test_token_mode:
        return "test token mode"
    return readiness_state(admin.github_store.load())


def preview_run_counts(services: Services) -> dict[str, int]:
    counts = services.store.run_counts("preview")
    return {status: counts.get(status, 0) for status in RUN_STATUSES}


def build_status(admin: AdminServices, services: Services) -> dict[str, Any]:
    store = services.store
    app_record = admin.github_store.load()
    digest_status = admin.read_digest_status()
    builder_hb = builder_heartbeat(store.data_dir)

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

    open_flags = []
    for flag in store.list_flags(resolved=False):
        row = flag.model_dump(mode="json")
        row["applicable_resolutions"] = list(APPLICABLE_RESOLUTIONS.get(flag.type, ("ignore",)))
        open_flags.append(row)

    return {
        "github_app_state": github_app_state(admin),
        "github_repo": app_record.owner_repo if app_record else admin.settings.github_test_repo,
        "github_default_branch": app_record.default_branch if app_record else None,
        "last_digest_at": digest_status.get("finished_at") if digest_status else None,
        "last_backup_at": _last_backup_at(admin),
        "post_count": len(store.list_posts()),
        "toolchain": toolchain_summary(admin, builder_hb),
        "submissions_by_status": submissions_by_status,
        "drafts_by_status": drafts_by_status,
        "disk_use": disk_use,
        "git_health": git_health(store.repo_dir),
        "index_schema_version": SCHEMA_VERSION,
        "digest_running": admin.digest_running,
        "builder_heartbeat": builder_hb,
        "publisher_heartbeat": publisher_heartbeat(store.data_dir),
        "watcher_heartbeat": watcher_heartbeat(store.data_dir),
        "reconcile_heartbeat": reconcile_heartbeat(store.data_dir),
        "preview_runs_by_status": preview_run_counts(services),
        "reconcile_flags": open_flags,
    }
