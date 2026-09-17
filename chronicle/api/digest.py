"""Digest of main: clone or fetch the blog repo, parse posts and toolchain.

Spec section 10 step 3. This module only reads: it clones or fetches
`data/site/`, walks `content/posts/**/*.md` (and `index.md` inside page
bundles), and returns `DiscoveredPost` records plus a `Toolchain` summary.
Nothing here writes to `data/repo/`; `Store.apply_digest` (store.py) is the
one place that turns what this module found into post records, following the
same write-then-commit recipe every other mutator uses.

Slug rule: the frontmatter's own `slug` if it set one, else the filename
stem (`content/posts/my-post.md` -> `my-post`) or the bundle directory name
(`content/posts/my-post/index.md` -> `my-post`). This matches Hugo's own
default page-bundle slug behaviour, so a post that never set an explicit
slug still lands on the URL Hugo already gives it.
"""

from __future__ import annotations

import base64
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

POSTS_GLOB_DIRS = ("content/posts",)
HUGO_WORKFLOW_PATH = ".github/workflows/hugo.yml"
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    # The repo URL is Chronicle's own configured source (the installation's
    # repo or CHRONICLE_DIGEST_REPO_URL), never attacker input, so widening
    # git's protocol allowlist past https carries no new risk here; it is
    # what lets a submodule pointed at a local path clone in tests, the same
    # way a real theme submodule clones over https in production.
    "GIT_ALLOW_PROTOCOL": "https:http:git:ssh:file",
}


class DigestError(Exception):
    pass


@dataclass
class DiscoveredPost:
    slug: str
    path: str
    title: str
    date: str
    sha: str


@dataclass
class Toolchain:
    hugo_version: str
    submodules: list[dict[str, str]]


def _run(
    args: list[str], cwd: Path | None = None, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**GIT_ENV, **(extra_env or {})}
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def _auth_env(token: str | None) -> dict[str, str]:
    """Per-invocation git auth via the environment, never argv or the origin URL.

    `GIT_CONFIG_COUNT`/`_KEY_0`/`_VALUE_0` inject `http.extraheader` for this
    one process only, so an installation token never lands in `remote.origin.url`
    (persisted in `.git/config`, reused by every later fetch after the token
    expires) and never appears in `ps` output the way a `git -c ...` argument
    would.
    """
    if not token:
        return {}
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }


def clone_or_update(
    site_dir: Path, repo_url: str, branch: str | None = None, token: str | None = None
) -> str:
    """Clone on first use, else fetch and hard-reset to the remote default branch.

    Returns the resulting HEAD sha. A shallow, read-only operation: nothing
    here ever pushes, and no working-tree edit made by hand in `data/site/`
    would survive the next digest, since a reset discards it (that is the
    point: `data/site/` is a disposable mirror, per README's data directory
    layout). `repo_url` is always credential-free; `token`, when given, is
    injected per invocation through the environment (see `_auth_env`) and
    never persisted.
    """
    auth_env = _auth_env(token)
    if (site_dir / ".git").exists():
        current_url = _run(["remote", "get-url", "origin"], cwd=site_dir).stdout.strip()
        if current_url != repo_url:
            _run(["remote", "set-url", "origin", repo_url], cwd=site_dir)
        _run(["fetch", "--depth", "1", "origin"], cwd=site_dir, extra_env=auth_env)
        target_branch = branch or _remote_default_branch(site_dir, auth_env)
        _run(["reset", "--hard", f"origin/{target_branch}"], cwd=site_dir)
    else:
        site_dir.parent.mkdir(parents=True, exist_ok=True)
        clone_args = ["clone", "--depth", "1", "--recurse-submodules", repo_url, str(site_dir)]
        if branch:
            clone_args[1:1] = ["--branch", branch]
        _run(clone_args, extra_env=auth_env)
    _run(["submodule", "update", "--init", "--recursive"], cwd=site_dir, extra_env=auth_env)
    return _run(["rev-parse", "HEAD"], cwd=site_dir).stdout.strip()


def _remote_default_branch(site_dir: Path, extra_env: dict[str, str] | None = None) -> str:
    result = _run(["remote", "show", "origin"], cwd=site_dir, extra_env=extra_env)
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("HEAD branch:"):
            return line.split(":", 1)[1].strip()
    return "main"


def _blob_shas(site_dir: Path) -> dict[str, str]:
    """Map of repo-relative path -> git blob sha, from a single `ls-tree`."""
    result = _run(["ls-tree", "-r", "HEAD"], cwd=site_dir)
    shas: dict[str, str] = {}
    for line in result.stdout.splitlines():
        # "<mode> blob <sha>\t<path>"
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) == 3:
            shas[path] = parts[2]
    return shas


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split `---`/YAML or `+++`/TOML frontmatter from the body."""
    if text.startswith("---"):
        _, _, rest = text.partition("---\n")
        fm_text, sep, body = rest.partition("\n---\n")
        if not sep:
            return {}, text
        frontmatter = yaml.safe_load(fm_text) or {}
        return dict(frontmatter), body
    if text.startswith("+++"):
        import tomllib

        _, _, rest = text.partition("+++\n")
        fm_text, sep, body = rest.partition("\n+++\n")
        if not sep:
            return {}, text
        frontmatter = tomllib.loads(fm_text)
        return dict(frontmatter), body
    return {}, text


def slug_for(relpath: str, frontmatter: dict[str, Any]) -> str:
    explicit = frontmatter.get("slug")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    path = Path(relpath)
    if path.name == "index.md":
        return path.parent.name
    return path.stem


def _post_date(frontmatter: dict[str, Any]) -> str:
    date = frontmatter.get("date")
    return str(date) if date is not None else ""


def _post_title(frontmatter: dict[str, Any], slug: str) -> str:
    title = frontmatter.get("title")
    return str(title) if title else slug


def discover_posts(site_dir: Path) -> list[DiscoveredPost]:
    shas = _blob_shas(site_dir)
    discovered: list[DiscoveredPost] = []
    for base in POSTS_GLOB_DIRS:
        posts_dir = site_dir / base
        if not posts_dir.exists():
            continue
        for md_path in sorted(posts_dir.rglob("*.md")):
            relpath = str(md_path.relative_to(site_dir))
            frontmatter, _ = parse_frontmatter(md_path.read_text(encoding="utf-8"))
            slug = slug_for(relpath, frontmatter)
            discovered.append(
                DiscoveredPost(
                    slug=slug,
                    path=relpath,
                    title=_post_title(frontmatter, slug),
                    date=_post_date(frontmatter),
                    sha=shas.get(relpath, ""),
                )
            )
    return discovered


def _hugo_version_from_workflow(workflow: dict[str, Any]) -> str | None:
    """Look for HUGO_VERSION in any `env:` block, or a Hugo action's `with:`."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            env = node.get("env")
            if isinstance(env, dict) and "HUGO_VERSION" in env:
                found.append(str(env["HUGO_VERSION"]))
            uses = node.get("uses", "")
            if isinstance(uses, str) and "actions-hugo" in uses:
                with_block = node.get("with")
                if isinstance(with_block, dict) and "hugo-version" in with_block:
                    found.append(str(with_block["hugo-version"]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(workflow)
    return found[0] if found else None


def parse_toolchain(site_dir: Path) -> Toolchain:
    workflow_path = site_dir / HUGO_WORKFLOW_PATH
    hugo_version = "unknown"
    if workflow_path.exists():
        loaded = yaml.safe_load(workflow_path.read_text(encoding="utf-8")) or {}
        hugo_version = _hugo_version_from_workflow(loaded) or "unknown"

    submodules: list[dict[str, str]] = []
    gitmodules = site_dir / ".gitmodules"
    if gitmodules.exists():
        paths = re.findall(r"path\s*=\s*(\S+)", gitmodules.read_text(encoding="utf-8"))
        shas = _blob_shas(site_dir)
        submodule_shas = _submodule_commits(site_dir)
        for path in paths:
            submodules.append(
                {"path": path, "commit": submodule_shas.get(path, shas.get(path, ""))}
            )

    return Toolchain(hugo_version=hugo_version, submodules=submodules)


def _submodule_commits(site_dir: Path) -> dict[str, str]:
    try:
        result = _run(["submodule", "status", "--recursive"], cwd=site_dir)
    except subprocess.CalledProcessError:
        return {}
    commits: dict[str, str] = {}
    for line in result.stdout.splitlines():
        stripped = line.strip().lstrip("+-U")
        parts = stripped.split()
        if len(parts) >= 2:
            commits[parts[1]] = parts[0]
    return commits
