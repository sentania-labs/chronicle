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
import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import yaml

log = logging.getLogger("chronicle.api.digest")

# Every call to `clone_or_update` against the one `data/site/` clone this
# process manages runs behind this lock, so two threads (the hourly
# reconcile loop, the watcher's post-merge reconcile, the admin digest
# route) can never race on the same working tree, and so a lock file this
# process's own git process holds is never mistaken for a stale one by
# another thread in the same process. Nothing outside the api process runs
# git against `data/site/`: the builder copies it with hardlinks
# (`chronicle/builder/runner.py`) and Hugo's own build never shells out to
# git against it (`--enableGitInfo` is never set, `chronicle/builder/hugo.py`).
#
# An `RLock`, not a plain `Lock` (round C7 review, P2): the clone alone is
# not the whole race. A caller that clones and then reads the clone
# (`read_hugo_conventions`, `discover_posts`, `parse_toolchain`) needs the
# lock held across that whole span through `site_clone_lock()` below, and
# `clone_or_update` takes the same lock itself so it still works when
# called on its own (the admin digest route's only use). Reentrancy is what
# lets a caller already holding `site_clone_lock()` call `clone_or_update`
# without deadlocking itself.
_SITE_GIT_LOCK = threading.RLock()


@contextmanager
def site_clone_lock() -> Iterator[None]:
    """Hold the process-wide site-clone lock across a clone plus every read of it.

    `clone_or_update` alone only serializes the clone; a caller that reads
    `data/site/` afterwards (`read_hugo_conventions`, `discover_posts`,
    `with_observed_post_dir`, `parse_toolchain`, or a store apply that reads
    the clone) must hold this across the whole span, or a second caller's
    clone can land in between the first caller's clone and its read,
    producing a snapshot mixing content and blob shas from two different
    commits (round C7 review, P2, issue #57's follow-up). `digest_runner.py`
    and `reconcile.py` are the callers that need this; `clone_or_update`
    keeps taking `_SITE_GIT_LOCK` itself so a bare call still serializes on
    its own. Reentrant, so nesting (`site_clone_lock()` wrapping a call that
    calls `clone_or_update`, which takes the same lock again) does not
    deadlock.
    """
    with _SITE_GIT_LOCK:
        yield


# How old `.git/index.lock` (or `.git/shallow.lock`) must be before it is
# treated as abandoned rather than live. A few minutes is comfortably
# longer than any fetch or reset this module runs against the blog repo
# takes, so a lock still younger than this may belong to a genuinely
# running git process and must be left alone; only a lock stamped well
# before that gets removed, on the theory that whatever git process
# created it has already died (the root cause reported in issue #57: a
# git step killed mid-write by the builder's own crash loop).
STALE_GIT_LOCK_SECONDS = 300.0

# The lock files a hard reset or a shallow fetch can leave behind mid-write.
# Not a general lock janitor: only these two, both directly in the path of
# `clone_or_update`'s existing-clone steps.
_GIT_LOCK_NAMES = ("index.lock", "shallow.lock")

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
# Every git call `_run` makes against `data/site/` runs under this timeout.
# `_run`'s calls all run inside `clone_or_update`'s `_SITE_GIT_LOCK`, so a
# git process that hangs (a stalled network fetch is the realistic case)
# would otherwise hold that process-wide lock forever, stalling every later
# reconcile, watch, and admin digest behind it instead of just its own
# thread. 300 seconds is generous for even a slow clone or fetch of this
# repo's size over a bad connection, while still bounding the lock's worst
# case. A git process killed on timeout can leave `.git/index.lock` behind;
# that is the same stale-lock shape `_clear_stale_git_locks` already clears
# once it ages past `STALE_GIT_LOCK_SECONDS`, so the next call recovers on
# its own.
GIT_TIMEOUT_SECONDS = 300.0
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


# Bounds how much of a failed git command's stderr ends up in a log line;
# git's own error output is never this long, but a hung or confused process
# writing to stderr in a loop must not be able to blow up log storage.
_MAX_SCRUBBED_STDERR_CHARS = 4000

# `https://user:pass@host/...` (a credential embedded in a URL, the shape
# `_auth_env`'s comment describes git as never persisting but that a
# misconfigured `CHRONICLE_DIGEST_REPO_URL` or a redirect could still echo
# into stderr) and the two other places a token can appear in this module's
# own git invocations: `AUTHORIZATION: basic ...` and `x-access-token:...`,
# both from `_auth_env`'s per-invocation `http.extraheader`, which git can
# echo back in a verbose or trace error line.
_USERINFO_URL_RE = re.compile(r"(https?://)[^/\s@]+@")
_AUTHORIZATION_HEADER_RE = re.compile(r"(?i)(authorization:\s*basic\s+)\S+")
_ACCESS_TOKEN_RE = re.compile(r"(?i)(x-access-token:)\S+")


def _scrub(text: str) -> str:
    """Redact anything token-bearing from git's stderr before it is logged."""
    text = _USERINFO_URL_RE.sub(r"\1[redacted]@", text)
    text = _AUTHORIZATION_HEADER_RE.sub(r"\1[redacted]", text)
    text = _ACCESS_TOKEN_RE.sub(r"\1[redacted]", text)
    return text


class GitCommandError(subprocess.CalledProcessError):
    """A failed git command, with scrubbed stderr folded into `str()`.

    Subclasses `CalledProcessError` so every existing
    `except subprocess.CalledProcessError` handler (reconcile.py,
    test fixtures, an admin route) keeps catching it with no change; only
    `str()` differs, which is what makes `log.warning("...: %s", exc)` and
    `log.exception(...)` (whose traceback line calls `str()` on the
    exception) both show git's own error instead of just the exit code.
    """

    def __init__(self, original: subprocess.CalledProcessError) -> None:
        super().__init__(original.returncode, original.cmd, original.output, original.stderr)

    def __str__(self) -> str:
        base = super().__str__()
        stderr = _scrub(self.stderr or "").strip()
        if not stderr:
            return base
        if len(stderr) > _MAX_SCRUBBED_STDERR_CHARS:
            stderr = stderr[:_MAX_SCRUBBED_STDERR_CHARS] + "... (truncated)"
        return f"{base}\nstderr: {stderr}"


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
    # The site-relative directory that holds most of this site's posts, as
    # observed by `with_observed_post_dir` after a walk; where a brand-new
    # post is written (`read_new_post_dir_from_state`). None until a digest
    # has walked a site that has any post.
    postdir: str | None = None
    # The environment's own `baseURL` (issue #71): the site's public root,
    # used to turn a post's site-relative url into the link announcements
    # carry. None on a fallback read or when the config has none usable.
    baseurl: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "contentdir": self.contentdir,
            "postdir": self.postdir,
            "staticdir": self.staticdir,
            "baseurl": self.baseurl,
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
        baseurl=_public_base_url(parsed.get("baseurl")),
    )


def _public_base_url(value: Any) -> str | None:
    """`baseURL` as an absolute http(s) root ending in `/`, or None. Hugo
    accepts a bare `/` or an empty value for local sites; neither is a link
    anyone outside can follow, so neither is used."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return candidate if candidate.endswith("/") else candidate + "/"


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


def read_content_dir_from_state(data_dir: Path) -> str:
    """The `contentdir` the last digest's `hugo config` read, from `data/state/toolchain.json`.

    The same shape as `read_static_dir_from_state`, for the same reason
    (issue #18): `convert.post_path` needs the site's real `contentdir` to
    decide whether a digest-created record's source path is still eligible
    for reuse, and it must not run a fresh `hugo config` call of its own to
    get it. Falls back to `FALLBACK_CONTENT_DIR` on anything short of a
    clean read: no digest has ever run yet, the file does not parse, or it
    predates this field.
    """
    path = data_dir.joinpath(*TOOLCHAIN_STATE_PATH)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return FALLBACK_CONTENT_DIR
    conventions = loaded.get("conventions") if isinstance(loaded, dict) else None
    content_dir = conventions.get("contentdir") if isinstance(conventions, dict) else None
    if isinstance(content_dir, str) and content_dir.strip():
        return content_dir
    return FALLBACK_CONTENT_DIR


def dominant_post_dir(discovered: list[DiscoveredPost], content_dir: str) -> str | None:
    """The section directory under `content_dir` that holds most of `discovered`.

    A Hugo section is a first-level directory of the content root, so this
    counts each post under the first segment of its path below `content_dir`
    (a post at the root of `content_dir` counts for `content_dir` itself) and
    returns the busiest, ties broken by name so the answer never flips
    between two digests of the same site. None when there is no post to
    count. It is observed rather than derived because Hugo has no key for
    it: `params.mainsections` names page *types* (Scott's site says `post`
    for posts that live in `content/posts`), not directories.
    """
    root = PurePosixPath(content_dir.rstrip("/"))
    counts: Counter[str] = Counter()
    for post in discovered:
        try:
            relative = PurePosixPath(post.path).relative_to(root)
        except ValueError:
            continue
        counts[str(root / relative.parts[0]) if len(relative.parts) > 1 else str(root)] += 1
    if not counts:
        return None
    return min(counts, key=lambda directory: (-counts[directory], directory))


def with_observed_post_dir(
    conventions: HugoConventions, discovered: list[DiscoveredPost]
) -> HugoConventions:
    """`conventions` with `postdir` filled in from the posts a walk found.

    Only a real `hugo config` read is annotated: the fallback's `contentdir`
    is the whole pre-ADR-017 path, not a root, so nothing observed under it
    can be told apart from the constant.
    """
    if conventions.source != "hugo_config":
        return conventions
    return replace(conventions, postdir=dominant_post_dir(discovered, conventions.contentdir))


def read_base_url_from_state(data_dir: Path) -> str | None:
    """The public `baseURL` the last digest's `hugo config` read, from
    `data/state/toolchain.json`, or None when no digest has run, the read
    fell back, or the site has no absolute http(s) `baseURL` (issue #71)."""
    path = data_dir.joinpath(*TOOLCHAIN_STATE_PATH)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    conventions = loaded.get("conventions") if isinstance(loaded, dict) else None
    if not isinstance(conventions, dict) or conventions.get("source") != "hugo_config":
        return None
    return _public_base_url(conventions.get("baseurl"))


def read_new_post_dir_from_state(data_dir: Path) -> str:
    """Where a brand-new post file goes, site-relative, from the last digest's state.

    `contentdir` is the content ROOT (`content`), while
    `FALLBACK_CONTENT_DIR` is the whole pre-ADR-017 convention
    (`content/posts`); they are not the same kind of value, so a new post's
    directory is never built from `contentdir` alone. In order:

    1. No digest state, an unparseable file, a fallback read, or a
       `contentdir` outside the site: `FALLBACK_CONTENT_DIR`, exactly the
       pre-ADR-017 behaviour, so a Chronicle that never read a site's
       conventions publishes where it always did.
    2. A real read with an observed `postdir` (`with_observed_post_dir`):
       that section, so a new post lands beside its siblings.
    3. A real read with no observed post (an empty site, or state written
       before `postdir` existed): `<contentdir>/posts`, Chronicle's own
       section name, or `contentdir` itself when it already ends in `posts`.
    """
    path = data_dir.joinpath(*TOOLCHAIN_STATE_PATH)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return FALLBACK_CONTENT_DIR
    conventions = loaded.get("conventions") if isinstance(loaded, dict) else None
    if not isinstance(conventions, dict) or conventions.get("source") != "hugo_config":
        return FALLBACK_CONTENT_DIR
    content_dir = conventions.get("contentdir")
    if not isinstance(content_dir, str) or not _is_safe_relative_dir(content_dir.rstrip("/")):
        return FALLBACK_CONTENT_DIR
    root = content_dir.rstrip("/")
    observed = conventions.get("postdir")
    if (
        isinstance(observed, str)
        and _is_safe_relative_dir(observed)
        and (observed == root or observed.startswith(f"{root}/"))
    ):
        return observed
    return root if PurePosixPath(root).name == "posts" else f"{root}/posts"


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
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as exc:
        raise GitCommandError(exc) from exc
    except subprocess.TimeoutExpired as exc:
        # `args` can carry the repo URL (a clone's argv includes it), so the
        # command line is scrubbed the same way a failed command's stderr
        # is before it lands in a log or an error message.
        command = _scrub(" ".join(args))
        raise DigestError(f"git {command} timed out after {GIT_TIMEOUT_SECONDS:.0f}s") from exc


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


def _clear_stale_git_locks(site_dir: Path) -> None:
    """Remove `.git/index.lock`/`.git/shallow.lock` if provably abandoned.

    Called only from inside `clone_or_update`'s own `_SITE_GIT_LOCK`, so a
    lock this process's own git process is holding right now can never be
    seen here (that git call is the very next thing this same lock guards).
    A lock younger than `STALE_GIT_LOCK_SECONDS` is left alone with a clear
    error naming it and its age, on the theory that something else (a
    process outside this one, or a genuine clock skew making the age look
    negative) may still be using it. Uses `lstat`, not `stat`, so a
    lock path that is itself a symlink is judged and removed by its own
    age and identity, never by following it into wherever it points.
    """
    git_dir = site_dir / ".git"
    for name in _GIT_LOCK_NAMES:
        lock_path = git_dir / name
        try:
            info = lock_path.lstat()
        except OSError:
            continue
        age_seconds = time.time() - info.st_mtime
        if age_seconds < STALE_GIT_LOCK_SECONDS:
            raise DigestError(
                f"{lock_path} exists and is {age_seconds:.0f}s old, younger than the"
                f" {STALE_GIT_LOCK_SECONDS:.0f}s staleness threshold; refusing to remove it"
                " in case a git process is still using it"
            )
        log.warning("removing stale git lock %s (%.0fs old)", lock_path, age_seconds)
        lock_path.unlink()


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

    The whole call runs under `_SITE_GIT_LOCK`, so no two threads in this
    process ever run git against the same clone at once, and a stale
    `index.lock`/`shallow.lock` cleared here can never actually belong to a
    git process this same lock is currently serializing.
    """
    auth_env = _auth_env(token)
    with _SITE_GIT_LOCK:
        if (site_dir / ".git").exists():
            _clear_stale_git_locks(site_dir)
            current_url = _run(["remote", "get-url", "origin"], cwd=site_dir).stdout.strip()
            if current_url != repo_url:
                _run(["remote", "set-url", "origin", repo_url], cwd=site_dir)
            _run(["fetch", "--depth", "1", "origin"], cwd=site_dir, extra_env=auth_env)
            target_branch = branch or _remote_default_branch(site_dir, auth_env)
            _run(["reset", "--hard", f"origin/{target_branch}"], cwd=site_dir)
        else:
            site_dir.parent.mkdir(parents=True, exist_ok=True)
            clone_args = [
                "clone",
                "--depth",
                "1",
                "--recurse-submodules",
                repo_url,
                str(site_dir),
            ]
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
