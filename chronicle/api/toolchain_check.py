"""Toolchain check for the admin page (issue #67, ADR 026).

Reads what the blog pins (Hugo in its deploy workflow and in Chronicle's own
image, theme submodules, Hugo modules) and compares it with what upstream
offers, using `git ls-remote` only: no API token, no clone, and nothing
written anywhere but `data/state/toolchain_check.json`. The two actions it
offers (move a theme submodule, remove an unused one) each open a PR on the
blog repo through the same `GitHubRepoOps` the publisher uses, never a push
to the default branch.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from . import digest as digest_mod
from .admin_deps import AdminServices
from .admin_status import builder_heartbeat, toolchain_summary
from .github_client import GitHubRepoOps
from .store import Store

log = logging.getLogger(__name__)

STATE_FILE = "toolchain_check.json"
ACTIONS_FILE = "toolchain_actions.json"
CHECK_EVERY = timedelta(days=1)
LOOP_WAKE_SECONDS = 3600.0
STARTUP_DELAY_SECONDS = 300.0
LS_REMOTE_TIMEOUT_SECONDS = 30.0
HUGO_REPO_URL = "https://github.com/gohugoio/hugo.git"
HUGO_RELEASE_URL = "https://github.com/gohugoio/hugo/releases/tag/v{version}"
CHRONICLE_ISSUE_URL = "https://github.com/sentania-labs/chronicle/issues/new"
BRANCH_PREFIX = "chronicle/toolchain/"
# Only https upstreams are queried; an ssh GitHub URL is rewritten to its
# https form first. Tests widen this to `file` to point at local repos.
ALLOWED_PROTOCOLS = "https"

_SEMVER_TAG = re.compile(r"^v?(\d+(?:\.\d+)+)$")
_GITHUB_SSH = re.compile(r"^git@github\.com:(?P<path>.+)$")
_CHECK_LOCK = threading.Lock()

LsRemote = Callable[[str, list[str]], list[tuple[str, str]]]


class ToolchainActionError(Exception):
    """A refused or failed toolchain action; the message is shown as is."""


# --- Reading what the blog pins ---------------------------------------------


def parse_gitmodules(text: str) -> list[dict[str, str]]:
    """Each `[submodule "name"]` section's name, path and url, in file order."""
    sections: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        header = re.match(r'^\[submodule\s+"(?P<name>[^"]+)"\]$', line)
        if header:
            current = {"name": header.group("name")}
            sections.append(current)
            continue
        if line.startswith("["):
            current = None
            continue
        if current is not None and "=" in line and not line.startswith(("#", ";")):
            key, _, value = line.partition("=")
            current[key.strip().lower()] = value.strip().strip('"')
    return [s for s in sections if s.get("path")]


def remove_gitmodules_section(text: str, path: str) -> str | None:
    """`.gitmodules` without the section whose `path` is `path`, or None when
    no section names it. Every other line is kept byte for byte."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    section: list[str] = []
    removed = False

    def flush() -> None:
        nonlocal removed
        paths = [
            ln.partition("=")[2].strip().strip('"')
            for ln in section
            if ln.strip().lower().startswith("path") and "=" in ln
        ]
        if section and path in paths:
            removed = True
        else:
            out.extend(section)

    for line in lines:
        if line.lstrip().startswith("["):
            flush()
            section = [line]
        elif section:
            section.append(line)
        else:
            out.append(line)
    flush()
    return "".join(out) if removed else None


def parse_go_mod(text: str) -> list[dict[str, str]]:
    """Every `require` in a go.mod, as module path and version."""
    required: list[dict[str, str]] = []
    in_block = False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if in_block:
            if line == ")":
                in_block = False
                continue
            parts = line.split()
        elif line.startswith("require ("):
            in_block = True
            continue
        elif line.startswith("require "):
            parts = line.split()[1:]
        else:
            continue
        if len(parts) >= 2:
            required.append({"module": parts[0], "version": parts[1]})
    return required


def https_url(url: str) -> str | None:
    """The https form of a submodule URL, or None when it has none (a
    relative path, or an ssh host other than GitHub)."""
    match = _GITHUB_SSH.match(url)
    if match:
        return f"https://github.com/{match.group('path')}"
    if url.startswith("https://") or (ALLOWED_PROTOCOLS != "https" and "://" in url):
        return url
    return None


def module_repo_url(module: str) -> str | None:
    """The repository a `github.com/owner/repo[/...]` module lives in."""
    parts = module.split("/")
    if len(parts) >= 3 and parts[0] == "github.com":
        return f"https://github.com/{parts[1]}/{parts[2]}"
    return None


def _theme_names(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _imported_names(config: dict[str, Any]) -> list[str]:
    """Theme names a config loads: its `theme`, and each `module.imports`
    path both as given and by its last segment (a theme under `themes/` is
    imported by its directory name; a Hugo module by its full path)."""
    names = _theme_names(config.get("theme"))
    module = config.get("module")
    imports = module.get("imports") if isinstance(module, dict) else None
    for entry in imports if isinstance(imports, list) else []:
        path = entry.get("path") if isinstance(entry, dict) else None
        if isinstance(path, str) and path.strip():
            names.extend([path.strip(), path.strip().rstrip("/").rsplit("/", 1)[-1]])
    # Hugo lists a `theme` among `module.imports` too; keep each name once.
    return list(dict.fromkeys(names))


def _theme_own_themes(theme_dir: Path) -> list[str]:
    """What a theme component itself imports, from its own config."""
    for name in ("theme.toml", "hugo.toml", "config.toml"):
        path = theme_dir / name
        if path.is_file():
            try:
                return _imported_names(tomllib.loads(path.read_text(encoding="utf-8")))
            except (OSError, tomllib.TOMLDecodeError):
                return []
    for name in ("theme.yaml", "hugo.yaml", "config.yaml", "theme.yml", "hugo.yml"):
        path = theme_dir / name
        if path.is_file():
            try:
                loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                return []
            return _imported_names(loaded) if isinstance(loaded, dict) else []
    return []


def used_themes(site_dir: Path, themes_dir: str, configured: list[str]) -> set[str]:
    """The configured themes plus every theme they import, transitively."""
    used: set[str] = set()
    pending = list(configured)
    while pending:
        name = pending.pop()
        if name in used:
            continue
        used.add(name)
        pending.extend(_theme_own_themes(site_dir / themes_dir / name))
    return used


def read_site_hugo_config(site_dir: Path) -> dict[str, Any] | None:
    """The themes the site loads (`theme` and `module.imports`) and its
    `themesdir`, from its own `hugo config` in the same environment digest
    reads. None when it cannot be read, names no theme at all, or puts
    `themesdir` outside the site: without a known theme, no theme is ever
    called unused."""
    environment = (
        os.environ.get(digest_mod.HUGO_ENVIRONMENT_ENV, "").strip()
        or digest_mod.DEFAULT_HUGO_ENVIRONMENT
    )
    try:
        result = subprocess.run(
            [
                "hugo",
                "--source",
                str(site_dir),
                "config",
                "--format",
                "json",
                "--environment",
                environment,
            ],
            capture_output=True,
            text=True,
            timeout=digest_mod.HUGO_CONFIG_TIMEOUT_SECONDS,
            check=True,
        )
        parsed = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    themes = _imported_names(parsed)
    if not themes:
        return None
    raw_dir = parsed.get("themesdir")
    themes_dir = raw_dir if isinstance(raw_dir, str) and raw_dir.strip() else "themes"
    if Path(themes_dir).is_absolute():
        try:
            themes_dir = Path(themes_dir).resolve().relative_to(site_dir.resolve()).as_posix()
        except ValueError:
            return None
    if not digest_mod._is_safe_relative_dir(themes_dir):
        return None
    return {"themes": themes, "themesdir": themes_dir}


# --- Asking upstream ---------------------------------------------------------


def ls_remote(url: str, args: list[str]) -> list[tuple[str, str]]:
    """`git ls-remote` as (sha, ref) pairs, https only, bounded in time."""
    env = {**digest_mod.GIT_ENV, "GIT_ALLOW_PROTOCOL": ALLOWED_PROTOCOLS}
    # Options go before the repository and ref patterns after it.
    options = [arg for arg in args if arg.startswith("-")]
    patterns = [arg for arg in args if not arg.startswith("-")]
    result = subprocess.run(
        ["git", "ls-remote", *options, url, *patterns],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=LS_REMOTE_TIMEOUT_SECONDS,
    )
    pairs = []
    for line in result.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if sha and ref:
            pairs.append((sha.strip(), ref.strip()))
    return pairs


def _version_key(tag: str) -> tuple[int, ...] | None:
    match = _SEMVER_TAG.match(tag)
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def latest_tag(pairs: list[tuple[str, str]]) -> dict[str, str] | None:
    """The highest version-shaped tag and the commit it points at (the
    peeled `^{}` sha for an annotated tag). Pre-release tags never match."""
    shas: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for sha, ref in pairs:
        if not ref.startswith("refs/tags/"):
            continue
        name = ref.removeprefix("refs/tags/")
        if name.endswith("^{}"):
            peeled[name[:-3]] = sha
        else:
            shas[name] = sha
    best = max(
        (name for name in shas if _version_key(name) is not None),
        key=lambda name: _version_key(name) or (),
        default=None,
    )
    if best is None:
        return None
    return {"tag": best, "sha": peeled.get(best, shas[best])}


def upstream_state(url: str, run: LsRemote) -> dict[str, Any]:
    head = run(url, ["HEAD"])
    tags = run(url, ["--tags"])
    head_sha = next((sha for sha, ref in head if ref == "HEAD"), "")
    return {"head": head_sha, "latest_tag": latest_tag(tags)}


# --- The check ---------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(tz=UTC)


def load_result(state_dir: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads((state_dir / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read_local(site_dir: Path) -> dict[str, Any]:
    """Everything the check needs from the site clone, read under the site
    clone lock so a concurrent digest never hands it a torn snapshot."""
    with digest_mod.site_clone_lock():
        if not (site_dir / ".git").exists():
            return {"missing": True}
        site_commit = digest_mod._run(["rev-parse", "HEAD"], cwd=site_dir).stdout.strip()
        toolchain = digest_mod.parse_toolchain(site_dir)
        pinned = {item["path"]: item["commit"] for item in toolchain.submodules}
        gitmodules_path = site_dir / ".gitmodules"
        gitmodules = (
            parse_gitmodules(gitmodules_path.read_text(encoding="utf-8"))
            if gitmodules_path.exists()
            else []
        )
        go_mod_path = site_dir / "go.mod"
        go_mod = (
            parse_go_mod(go_mod_path.read_text(encoding="utf-8")) if go_mod_path.exists() else []
        )
        config = read_site_hugo_config(site_dir)
        used = (
            used_themes(site_dir, config["themesdir"], config["themes"])
            if config is not None
            else None
        )
    return {
        "missing": False,
        "site_commit": site_commit,
        "site_hugo": toolchain.hugo_version,
        "pinned": pinned,
        "gitmodules": gitmodules,
        "go_mod": go_mod,
        "config": config,
        "used": used,
    }


def run_check(store: Store, admin: AdminServices, run: LsRemote = ls_remote) -> dict[str, Any]:
    """One full check. Local reads happen under the site clone lock; every
    upstream query happens after it is released. The result is saved and
    returned; a failed upstream query is recorded against its row, never
    raised."""
    local = _read_local(store.site_dir)
    summary = toolchain_summary(admin, builder_heartbeat(store.data_dir))
    result: dict[str, Any] = {"checked_at": _now().isoformat(timespec="seconds"), "errors": []}

    try:
        latest = latest_tag(run(HUGO_REPO_URL, ["--tags"]))
    except (OSError, subprocess.SubprocessError) as exc:
        latest = None
        result["errors"].append(f"Hugo releases: {exc}")
    latest_version = latest["tag"].removeprefix("v") if latest else None
    result["hugo"] = {
        "image": summary["builder_hugo_version"],
        # The checkout just read wins over the last digest's copy of it.
        "site": local.get("site_hugo") or summary["hugo_version"],
        "latest": latest_version,
    }

    if local["missing"]:
        result["site_missing"] = True
        result["themes"] = []
        result["modules"] = []
        _save_json(admin.state_dir / STATE_FILE, result)
        return result

    config = local["config"]
    result["config"] = config
    # The main commit every row below describes; a removal is refused once
    # main has moved past it (Codex round), since the theme may be in use.
    result["site_commit"] = local["site_commit"]
    themes: list[dict[str, Any]] = []
    for section in local["gitmodules"]:
        path = section["path"]
        row: dict[str, Any] = {
            "name": section["name"],
            "path": path,
            "url": section.get("url", ""),
            "pinned": local["pinned"].get(path, ""),
        }
        if config is not None and Path(path).parent.as_posix() == config["themesdir"]:
            row["is_theme"] = True
            row["unused"] = Path(path).name not in local["used"]
        else:
            row["is_theme"] = Path(path).parent.as_posix() == "themes"
            row["unused"] = False
        upstream = https_url(row["url"])
        if upstream is None:
            row["error"] = "not an https or GitHub URL; not checked"
        else:
            try:
                row.update(upstream_state(upstream, run))
            except (OSError, subprocess.SubprocessError) as exc:
                row["error"] = f"upstream query failed: {exc}"
        themes.append(row)
    result["themes"] = themes

    modules: list[dict[str, Any]] = []
    for required in local["go_mod"]:
        entry: dict[str, Any] = dict(required)
        repo = module_repo_url(required["module"])
        if repo is None:
            entry["error"] = "not a github.com module; not checked"
        else:
            try:
                tag = latest_tag(run(repo, ["--tags"]))
                entry["latest"] = tag["tag"] if tag else None
            except (OSError, subprocess.SubprocessError) as exc:
                entry["error"] = f"upstream query failed: {exc}"
        modules.append(entry)
    result["modules"] = modules

    _save_json(admin.state_dir / STATE_FILE, result)
    return result


def run_check_logged(store: Store, admin: AdminServices) -> None:
    try:
        run_check(store, admin)
    except Exception:  # noqa: BLE001 - a background check must never kill its thread
        log.exception("toolchain check failed; will retry on the next trigger")


def start_check_now(store: Store, admin: AdminServices) -> bool:
    """Run one check in the background; False, and nothing started, when a
    check is already running."""
    if not _CHECK_LOCK.acquire(blocking=False):
        return False

    def work() -> None:
        try:
            run_check_logged(store, admin)
        finally:
            _CHECK_LOCK.release()

    threading.Thread(target=work, name="chronicle-toolchain-check", daemon=True).start()
    return True


def check_running() -> bool:
    return _CHECK_LOCK.locked()


def is_due(state_dir: Path, now: datetime | None = None) -> bool:
    last = load_result(state_dir)
    try:
        checked = datetime.fromisoformat(str((last or {})["checked_at"]))
    except (KeyError, ValueError):
        return True
    return (now or _now()) - checked >= CHECK_EVERY


def run_loop(store: Store, admin: AdminServices, stop_event: threading.Event) -> None:
    """A check once a day: wakes hourly and runs when the last one is a day
    old. The first wake waits `STARTUP_DELAY_SECONDS` so a restart (or a
    test app) never queries upstream in its first minutes, and nothing runs
    until a digest has left a site checkout to compare against."""
    wait = STARTUP_DELAY_SECONDS
    while not stop_event.wait(wait):
        wait = LOOP_WAKE_SECONDS
        if not (store.site_dir / ".git").exists() or not is_due(admin.state_dir):
            continue
        if _CHECK_LOCK.acquire(blocking=False):
            try:
                run_check_logged(store, admin)
            finally:
                _CHECK_LOCK.release()


# --- Actions: each opens a PR on the blog repo -------------------------------


def load_actions(state_dir: Path) -> dict[str, Any]:
    try:
        loaded = json.loads((state_dir / ACTIONS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


_ACTIONS_LOCK = threading.Lock()


def _record_action(state_dir: Path, path: str, record: dict[str, Any]) -> None:
    with _ACTIONS_LOCK:
        actions = load_actions(state_dir)
        actions[path] = record
        _save_json(state_dir / ACTIONS_FILE, actions)


def _theme_row(state_dir: Path, path: str) -> dict[str, Any]:
    result = load_result(state_dir) or {}
    for row in result.get("themes", []):
        if row.get("path") == path:
            return dict(row)
    raise ToolchainActionError(f"{path} is not in the last toolchain check; run a check first")


def _branch_name(kind: str, path: str) -> str:
    """A readable slug plus a short hash of the exact path, so two paths
    that slug the same (`a+b`, `a b`) never share a branch (Codex round)."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", path).strip("-") or "submodule"
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:8]
    return f"{BRANCH_PREFIX}{kind}-{slug}-{digest}"


def _main_gitmodules(ops: GitHubRepoOps, default_branch: str) -> str:
    found = ops.get_contents(".gitmodules", default_branch)
    if not found or "content" not in found:
        raise ToolchainActionError("the blog repo's default branch has no .gitmodules")
    return base64.b64decode(str(found["content"])).decode("utf-8")


def _open_pr(
    ops: GitHubRepoOps,
    default_branch: str,
    branch: str,
    entries: list[dict[str, Any]],
    title: str,
    body: str,
) -> dict[str, Any]:
    """One commit on `branch` off the default branch's head, then a PR, or
    the already-open PR for that branch with its body refreshed."""
    ref = ops.get_ref(f"heads/{default_branch}")
    if ref is None:
        raise ToolchainActionError(f"no ref heads/{default_branch} on the blog repo")
    base_sha = str(ref["object"]["sha"])
    base_tree = str(ops.get_commit(base_sha)["tree"]["sha"])
    tree = ops.create_tree(base_tree, entries)
    commit = ops.create_commit(title, tree, [base_sha])
    if ops.get_ref(f"heads/{branch}") is None:
        ops.create_ref(f"heads/{branch}", commit)
    else:
        ops.update_ref(f"heads/{branch}", commit, force=True)
    open_prs = ops.list_open_pulls_by_head(branch)
    if open_prs:
        pr = open_prs[0]
        ops.update_pull_body(int(pr["number"]), body)
        return pr
    return ops.create_pull(title, branch, default_branch, body)


def bump_submodule(
    state_dir: Path, ops: GitHubRepoOps, default_branch: str, path: str, which: str
) -> dict[str, Any]:
    """Open a PR moving the submodule at `path` to the commit the last check
    found for `which` ("tag" or "head"). Only those two commits are ever
    offered, so nothing a form posts can name an arbitrary sha."""
    row = _theme_row(state_dir, path)
    if which == "tag":
        tag = row.get("latest_tag") or {}
        target, label = str(tag.get("sha") or ""), str(tag.get("tag") or "")
    elif which == "head":
        target, label = str(row.get("head") or ""), "upstream head"
    else:
        raise ToolchainActionError(f"unknown target {which!r}")
    if not re.fullmatch(r"[0-9a-f]{40}", target):
        raise ToolchainActionError(f"the last check found no {which} commit for {path}")
    if target == row.get("pinned"):
        raise ToolchainActionError(f"{path} is already at {label}")
    on_main_section = next(
        (s for s in parse_gitmodules(_main_gitmodules(ops, default_branch)) if s["path"] == path),
        None,
    )
    if on_main_section is None:
        raise ToolchainActionError(f"{path} is no longer a submodule on the default branch")
    if on_main_section.get("url", "") != row.get("url", ""):
        # The target commit came from the old upstream (Codex round).
        raise ToolchainActionError(
            f"{path}'s url on the default branch has changed since the last check;"
            " run a check first"
        )
    on_main = ops.get_contents(path, default_branch) or {}
    if on_main.get("type") != "submodule" or on_main.get("sha") != row.get("pinned"):
        # Main moved (a hand bump, another PR) after the check: a PR built
        # from the stale pin could move the theme backwards.
        raise ToolchainActionError(
            f"{path} on the default branch has changed since the last check; run a check first"
        )

    title = f"Move {path} to {label}"
    body = "\n".join(
        [
            f"Moves the `{path}` submodule to {label}.",
            "",
            f"- Upstream: {row.get('url', '')}",
            f"- From: `{row.get('pinned', '')}`",
            f"- To: `{target}`",
            "",
            "Opened from Chronicle's admin Toolchain page. Review and merge it like any other"
            " blog change; Chronicle picks the new commit up on its next digest.",
            "",
            "chronicle: toolchain",
        ]
    )
    entries = [{"path": path, "mode": "160000", "type": "commit", "sha": target}]
    pr = _open_pr(ops, default_branch, _branch_name(f"bump-{which}", path), entries, title, body)
    record = {
        "kind": "bump",
        "which": which,
        "to": label,
        "pinned_at_open": row.get("pinned", ""),
        "pr_url": str(pr["html_url"]),
        "opened_at": _now().isoformat(timespec="seconds"),
    }
    _record_action(state_dir, path, record)
    return record


def remove_theme(
    state_dir: Path, ops: GitHubRepoOps, default_branch: str, path: str
) -> dict[str, Any]:
    """Open a PR removing an unused theme submodule and its .gitmodules
    section. Refused unless the last check read the site's own Hugo config
    and found the theme unused."""
    row = _theme_row(state_dir, path)
    if not row.get("unused"):
        raise ToolchainActionError(f"{path} is not flagged unused by the last check")
    checked_at_commit = (load_result(state_dir) or {}).get("site_commit")
    ref = ops.get_ref(f"heads/{default_branch}")
    if not checked_at_commit or ref is None or ref["object"]["sha"] != checked_at_commit:
        # "Unused" was decided against that commit's config; main has moved
        # since, and may use the theme now (Codex round).
        raise ToolchainActionError(
            "the default branch has changed since the last check; run a digest and a check first"
        )
    remaining = remove_gitmodules_section(_main_gitmodules(ops, default_branch), path)
    if remaining is None:
        raise ToolchainActionError(f"{path} is no longer a submodule on the default branch")

    entries: list[dict[str, Any]] = [
        {"path": path, "mode": "160000", "type": "commit", "sha": None}
    ]
    if remaining.strip():
        blob = ops.create_blob(base64.b64encode(remaining.encode("utf-8")).decode("ascii"))
        entries.append({"path": ".gitmodules", "mode": "100644", "type": "blob", "sha": blob})
    else:
        entries.append({"path": ".gitmodules", "mode": "100644", "type": "blob", "sha": None})

    title = f"Remove unused theme {Path(path).name}"
    body = "\n".join(
        [
            f"Removes the `{path}` submodule and its `.gitmodules` entry.",
            "",
            "Chronicle's toolchain check found no reference to it: it is not the site's"
            " configured `theme`, and no configured theme imports it.",
            "",
            "Opened from Chronicle's admin Toolchain page. Review and merge it like any other"
            " blog change.",
            "",
            "chronicle: toolchain",
        ]
    )
    pr = _open_pr(ops, default_branch, _branch_name("remove", path), entries, title, body)
    record = {
        "kind": "remove",
        "pinned_at_open": row.get("pinned", ""),
        "pr_url": str(pr["html_url"]),
        "opened_at": _now().isoformat(timespec="seconds"),
    }
    _record_action(state_dir, path, record)
    return record


def hugo_behind(image: Any, latest: Any) -> bool:
    """Whether Chronicle's image is known to run an older Hugo than the
    latest release. An unknown or unparseable version is never behind."""
    have = _version_key(image) if isinstance(image, str) else None
    want = _version_key(latest) if isinstance(latest, str) else None
    return have is not None and want is not None and have < want


def hugo_issue_url(image: str, latest: str) -> str:
    """A prefilled new-issue link on Chronicle's own repo for a Hugo bump:
    Hugo is baked into Chronicle's image, so the admin page cannot move it."""
    title = f"Bump Hugo from {image} to {latest}"
    body = (
        f"The admin Toolchain page reports Chronicle's image runs Hugo {image};"
        f" the latest release is {latest}.\n\n"
        f"Release notes: {HUGO_RELEASE_URL.format(version=latest)}\n\n"
        "Change `ARG HUGO_VERSION` in the Dockerfile and ship it in a normal release."
    )
    return f"{CHRONICLE_ISSUE_URL}?title={quote(title)}&body={quote(body)}"
