"""The internal git repository under `data/repo/`: history, never a remote.

Every write to the data directory is one commit, authored by the acting
consumer (ADR 001). Identity is passed per call through the git environment
rather than read from a config file, because the api container has no global
git config and must not depend on the host having one.

The derived index is excluded from tracking by `repo/.gitignore`: see
docs/decisions/006-sqlite-derived-index.md.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

AUTHOR_EMAIL_DOMAIN = "chronicle.local"
BOOTSTRAP_AUTHOR = "chronicle"
GITIGNORE_BODY = "# The derived SQLite index is rebuildable; see ADR 006.\nindex/*\n"


def _run(repo_dir: Path, args: list[str], author: str) -> subprocess.CompletedProcess[str]:
    email = f"{author}@{AUTHOR_EMAIL_DOMAIN}"
    env = {
        "GIT_AUTHOR_NAME": author,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": author,
        "GIT_COMMITTER_EMAIL": email,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(repo_dir),
    }
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def init_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    if (repo_dir / ".git").exists():
        return
    _run(repo_dir, ["init", "--initial-branch=main"], BOOTSTRAP_AUTHOR)
    (repo_dir / ".gitignore").write_text(GITIGNORE_BODY, encoding="utf-8")
    _run(repo_dir, ["add", "-A"], BOOTSTRAP_AUTHOR)
    _run(repo_dir, ["commit", "-m", "chronicle: initialise internal history"], BOOTSTRAP_AUTHOR)


def commit_all(repo_dir: Path, message: str, author: str) -> None:
    _run(repo_dir, ["add", "-A"], author)
    status = _run(repo_dir, ["status", "--porcelain"], author)
    if not status.stdout.strip():
        return
    _run(repo_dir, ["commit", "-m", message], author)


def log_authors(repo_dir: Path, limit: int = 10) -> list[str]:
    result = _run(repo_dir, ["log", f"-{limit}", "--format=%an"], BOOTSTRAP_AUTHOR)
    return result.stdout.splitlines()


def files_in_head(repo_dir: Path) -> list[str]:
    result = _run(repo_dir, ["show", "--name-only", "--format=", "HEAD"], BOOTSTRAP_AUTHOR)
    return result.stdout.split()


def tracked_files(repo_dir: Path) -> list[str]:
    return _run(repo_dir, ["ls-files"], BOOTSTRAP_AUTHOR).stdout.split()
