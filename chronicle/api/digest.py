"""Digest of main: clone or fetch the blog repo, parse posts and toolchain.

Spec section 10 step 3. This module only reads: it clones or fetches
`data/site/`, walks the site's own content directory for posts (and
`index.md` inside page bundles), and returns `DiscoveredPost` records plus
a `Toolchain` summary. Nothing here writes to `data/repo/`;
`Store.apply_digest` (store.py) is the one place that turns what this
module found into post records, following the same write-then-commit
recipe every other mutator uses.

Slug rule: the frontmatter's own `slug` if it set one, else the filename
stem (`content/posts/my-post.md` -> `my-post`) or the bundle directory name
(`content/posts/my-post/index.md` -> `my-post`). This matches Hugo's own
default page-bundle slug behaviour, so a post that never set an explicit
slug still lands on the URL Hugo already gives it.

Where the walk root and post/page classification come from (ADR 017):
`read_hugo_conventions` asks the site's own `hugo config` what its
`contentdir`, `params.mainsections`, `staticdir`, and `taxonomies` actually
are, rather than assuming the `content/posts` directory convention Scott's
own site happens to use today but its Blowfish theme does not actually
depend on (it classifies archive content by frontmatter `type` through
`mainSections`). `discover_posts` walks `contentdir` and, when
`mainsections` names any, keeps only files whose frontmatter `type` is one
of them; a real page bundle like an About or Training page, which sets
`type: page`, is walked past but never returned as a post. When Hugo's own
answer cannot be had (no binary, a failed call, unparsable output),
`read_hugo_conventions` falls back to the directory this module hardcoded
before this ADR (`content/posts`, no type filter), so a digest never fails
just because Hugo's config could not be read.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

log = logging.getLogger("chronicle.api.digest")

# The pre-ADR-017 hardcoded convention, kept as the fallback `contentdir`
# `read_hugo_conventions` uses when Hugo's own config cannot be read: a walk
# root of exactly `content/posts`, and no `mainsections` filter (every `.md`
# under it is treated as a candidate post, matching this module's original,
# unconditional behaviour).
FALLBACK_CONTENT_DIR = "content/posts"
FALLBACK_STATIC_DIR = "static"
HUGO_WORKFLOW_PATH = ".github/workflows/hugo.yml"
HUGO_ENVIRONMENT_ENV = "CHRONICLE_HUGO_ENVIRONMENT"
DEFAULT_HUGO_ENVIRONMENT = "production"
HUGO_CONFIG_TIMEOUT_SECONDS = 20.0
# The frontmatter keys `FRONTMATTER_ALLOWLIST` (models.py) carries for
# Hugo's taxonomy system; `unconfigured_taxonomy_keys` checks these against
# what a site's own `hugo config` says it actually defines.
TAXONOMY_FRONTMATTER_KEYS = ("categories", "tags", "series")


def _is_safe_relative_dir(value: str) -> bool:
    """Whether a directory Hugo's own config reports stays inside the site.

    `read_hugo_conventions` joins `contentdir`/`staticdir` onto `site_dir`
    with `Path.__truediv__`, which does not defend itself: `Path("/a") /
    "/etc"` returns `/etc` outright, and a `..`-laden relative value walks
    back out of `site_dir` the same way a shell `cd` would. A digested
    site's `hugo.toml` is data from main, not Chronicle's own config, so a
    stray absolute path or a `contentDir = "../../etc"` typo must fall back
    rather than hand `discover_posts` a walk root outside the clone, the
    same defence `convert.post_path`'s `_is_safe_relative` already applies
    to a post's own recorded path.
    """
    parts = PurePosixPath(value).parts
    return bool(parts) and not value.startswith("/") and ".." not in parts


def _safe_directory_config() -> str:
    """A real, minimal git config file granting `safe.directory = *`.

    Git's dubious-ownership check deliberately ignores `GIT_CONFIG_COUNT`/
    `_KEY_n`/`_VALUE_n` for `safe.directory` specifically, on purpose: letting
    an environment variable waive that one check would defeat the point of
    having it. A real config file is the only way to grant the exception, so
    `GIT_CONFIG_GLOBAL` points here instead of `/dev/null`: still isolated
    from whatever `~/.gitconfig` the host or image happens to have, but with
    the one exception `CHRONICLE_DIGEST_REPO_URL` needs when it names a local
    clone owned by a different uid than the container's (README's documented
    way to test digest against a private repo without a token; found live
    during the C3 preview round's real-blog check).
    """
    path = Path(tempfile.gettempdir()) / "chronicle-digest-git-config"
    if not path.exists():
        path.write_text("[safe]\n\tdirectory = *\n", encoding="utf-8")
    return str(path)


GIT_ENV = {
    "GIT_CONFIG_GLOBAL": _safe_directory_config(),
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


@dataclass(frozen=True)
class HugoConventions:
    """What a site's own Hugo config says about content, images, and taxonomies.

    `source` is `"hugo_config"` when this came from a real `hugo config`
    call, `"fallback"` when it didn't and `fallback_reason` says why. Never
    raised as an error: a digest that cannot read Hugo's config still runs,
    against the pre-ADR-017 hardcoded convention.
    """

    contentdir: str
    staticdir: str
    mainsections: tuple[str, ...]
    taxonomies: dict[str, str]
    environment: str
    source: str
    fallback_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "contentdir": self.contentdir,
            "staticdir": self.staticdir,
            "mainsections": list(self.mainsections),
            "taxonomies": self.taxonomies,
            "environment": self.environment,
            "source": self.source,
            "fallback_reason": self.fallback_reason,
            "unconfigured_taxonomy_keys": list(unconfigured_taxonomy_keys(self)),
        }


def _fallback_conventions(environment: str, reason: str) -> HugoConventions:
    log.warning("hugo config unavailable, falling back to content/posts convention: %s", reason)
    return HugoConventions(
        contentdir=FALLBACK_CONTENT_DIR,
        staticdir=FALLBACK_STATIC_DIR,
        mainsections=(),
        taxonomies={},
        environment=environment,
        source="fallback",
        fallback_reason=reason,
    )


def read_hugo_conventions(site_dir: Path, environment: str | None = None) -> HugoConventions:
    """Ask the site's own `hugo config` for its content/image/taxonomy conventions.

    `environment` defaults to `CHRONICLE_HUGO_ENVIRONMENT`, or
    `DEFAULT_HUGO_ENVIRONMENT` when that is unset: Scott's own site reports
    the same `contentdir`/`mainsections`/`staticdir`/`taxonomies` across
    every environment it defines (only `baseURL` differs), and `production`
    is the environment whose config is what main's own live build actually
    uses. Falls back to `FALLBACK_CONTENT_DIR`/`FALLBACK_STATIC_DIR` with no
    mainsections filter and no taxonomies, on any failure: a missing Hugo
    binary, a non-zero exit, a timeout, or output that does not parse as
    the shape expected. Never raises.
    """
    env_name = (
        environment or os.environ.get(HUGO_ENVIRONMENT_ENV, "").strip() or DEFAULT_HUGO_ENVIRONMENT
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
                env_name,
            ],
            capture_output=True,
            text=True,
            timeout=HUGO_CONFIG_TIMEOUT_SECONDS,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _fallback_conventions(env_name, f"hugo config could not be run: {exc}")

    try:
        parsed = json.loads(result.stdout)
    except ValueError as exc:
        return _fallback_conventions(env_name, f"hugo config output was not valid json: {exc}")
    if not isinstance(parsed, dict):
        return _fallback_conventions(env_name, "hugo config output was not a json object")

    contentdir = parsed.get("contentdir")
    staticdir = parsed.get("staticdir")
    if isinstance(staticdir, list) and staticdir:
        staticdir = staticdir[0]
    if not isinstance(contentdir, str) or not contentdir.strip():
        return _fallback_conventions(env_name, "hugo config output had no usable contentdir")
    if not isinstance(staticdir, str) or not staticdir.strip():
        return _fallback_conventions(env_name, "hugo config output had no usable staticdir")
    if not _is_safe_relative_dir(contentdir):
        return _fallback_conventions(
            env_name, f"hugo config reported a contentdir outside the site: {contentdir!r}"
        )
    if not _is_safe_relative_dir(staticdir):
        return _fallback_conventions(
            env_name, f"hugo config reported a staticdir outside the site: {staticdir!r}"
        )

    params = parsed.get("params")
    raw_sections = params.get("mainsections") if isinstance(params, dict) else None
    mainsections = (
        tuple(str(item) for item in raw_sections) if isinstance(raw_sections, list) else ()
    )

    raw_taxonomies = parsed.get("taxonomies")
    taxonomies = (
        {str(key): str(value) for key, value in raw_taxonomies.items()}
        if isinstance(raw_taxonomies, dict)
        else {}
    )

    return HugoConventions(
        contentdir=contentdir,
        staticdir=staticdir,
        mainsections=mainsections,
        taxonomies=taxonomies,
        environment=env_name,
        source="hugo_config",
    )


TOOLCHAIN_STATE_PATH = ("state", "toolchain.json")


def read_static_dir_from_state(data_dir: Path) -> str:
    """The `staticdir` the last digest's `hugo config` read, from `data/state/toolchain.json`.

    Publish and preview both need this (ADR 017's staticdir wiring), but
    neither runs a fresh `hugo config` call of its own: they read whatever
    the last digest already wrote, the same way the builder's own
    `site_hugo_version` reads that file's `hugo_version` rather than
    re-parsing main's workflow on every build. Falls back to
    `FALLBACK_STATIC_DIR` on anything short of a clean read: no digest has
    ever run yet, the file does not parse, or it predates this field.
    """
    path = data_dir.joinpath(*TOOLCHAIN_STATE_PATH)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return FALLBACK_STATIC_DIR
    conventions = loaded.get("conventions") if isinstance(loaded, dict) else None
    static_dir = conventions.get("staticdir") if isinstance(conventions, dict) else None
    return static_dir if isinstance(static_dir, str) and static_dir.strip() else FALLBACK_STATIC_DIR


def unconfigured_taxonomy_keys(conventions: HugoConventions) -> tuple[str, ...]:
    """Which of Chronicle's own taxonomy frontmatter keys this site's Hugo
    config does not actually define as a taxonomy.

    A fact for an operator, never a rejection or a rewrite (same "flags,
    not corrections" posture as reconciliation, ADR 005): Chronicle's
    frontmatter allowlist accepts `categories`/`tags`/`series` regardless
    of what a given site's taxonomies actually are. When `conventions` came
    from the fallback path, there is nothing real to compare against, so
    nothing is reported as unconfigured.
    """
    if conventions.source != "hugo_config":
        return ()
    configured_plurals = set(conventions.taxonomies.values())
    return tuple(key for key in TAXONOMY_FRONTMATTER_KEYS if key not in configured_plurals)


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


def _is_archive_content(frontmatter: dict[str, Any], mainsections: tuple[str, ...]) -> bool:
    """Whether a walked file counts as a post, per `params.mainsections`.

    No `mainsections` (the fallback convention, or a site that never set
    one) means no filter at all: every `.md` under the walk root is a
    candidate post, matching this module's behaviour before ADR 017. When
    `mainsections` names types, only a file whose own frontmatter `type`
    is one of them counts; a page bundle like an About or Training page,
    which sets `type: page`, is walked past but never returned.
    """
    if not mainsections:
        return True
    content_type = frontmatter.get("type")
    return isinstance(content_type, str) and content_type.strip() in mainsections


def discover_posts(
    site_dir: Path, conventions: HugoConventions | None = None
) -> list[DiscoveredPost]:
    conv = conventions or HugoConventions(
        contentdir=FALLBACK_CONTENT_DIR,
        staticdir=FALLBACK_STATIC_DIR,
        mainsections=(),
        taxonomies={},
        environment="",
        source="fallback",
        fallback_reason="no conventions supplied to discover_posts",
    )
    shas = _blob_shas(site_dir)
    discovered: list[DiscoveredPost] = []
    content_root = site_dir / conv.contentdir
    if not content_root.exists():
        return discovered
    for md_path in sorted(content_root.rglob("*.md")):
        relpath = str(md_path.relative_to(site_dir))
        frontmatter, _ = parse_frontmatter(md_path.read_text(encoding="utf-8"))
        if not _is_archive_content(frontmatter, conv.mainsections):
            continue
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
