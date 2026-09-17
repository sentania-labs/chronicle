"""Backup create and restore (spec section 13, ADR 016).

A bundle is a gzip tarball of `repo/` (with `.git`), `images/`, and the
encrypted portion of `state/`: `tokens.json`, `admin.json`,
`github-app.json`, and `ui_disabled` when present. Never `instance.key`,
never `preview/`, `site/`, `builder-work/`, `claim-code`, `ui_token.txt`
(ADR 016): the instance key stays with the running instance, and the rest
are either disposable or process-local, not domain history.

`manifest.json` at the tarball root is not itself hashed (it is the list of
hashes); everything else in the bundle is. Restore verifies every member
against the manifest before anything touches the data directory, then
swaps `repo/` and `images/` and the state files in place, keeping the
displaced trees until a reindex against the new files succeeds.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__ as chronicle_version
from .api import gitrepo
from .api.store import Store

SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"
STATE_MEMBERS = ("tokens.json", "admin.json", "github-app.json", "ui_disabled")
COUNT_KEYS = ("drafts", "submissions", "versions", "images", "posts", "tokens")


class BackupError(Exception):
    pass


@dataclass(frozen=True)
class RestoreReport:
    before: dict[str, int]
    after: dict[str, int]
    initialised_git: bool
    credentials_decryptable: bool | None


def _utc_stamp() -> str:
    return datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _counts(data_dir: Path) -> dict[str, int]:
    store = Store.open(data_dir)
    try:
        found = store.reindex()
    finally:
        store.close()
    tokens_path = data_dir / "state" / "tokens.json"
    tokens = 0
    if tokens_path.exists():
        try:
            tokens = len(json.loads(tokens_path.read_text(encoding="utf-8")).get("tokens", []))
        except (OSError, json.JSONDecodeError):
            tokens = 0
    return {
        "drafts": found["drafts"],
        "submissions": found["submissions"],
        "versions": found["versions"],
        "images": found["images"],
        "posts": found["posts"],
        "tokens": tokens,
    }


def _iter_bundle_files(data_dir: Path) -> list[tuple[Path, str]]:
    """(absolute path, arcname) for every file the bundle carries."""
    members: list[tuple[Path, str]] = []
    repo_dir = data_dir / "repo"
    if repo_dir.is_dir():
        for path in sorted(repo_dir.rglob("*")):
            if path.is_file():
                members.append((path, str(Path("repo") / path.relative_to(repo_dir))))
    images_dir = data_dir / "images"
    if images_dir.is_dir():
        for path in sorted(images_dir.rglob("*")):
            if path.is_file():
                members.append((path, str(Path("images") / path.relative_to(images_dir))))
    state_dir = data_dir / "state"
    for name in STATE_MEMBERS:
        path = state_dir / name
        if path.is_file():
            members.append((path, str(Path("state") / name)))
    return members


def create_backup(data_dir: Path, out: Path | None = None) -> Path:
    """Write a gzip tarball bundle and return its path.

    The derived index (`repo/index/`) is excluded even though it lives
    under `repo/`: it is a rebuildable cache (ADR 006), never a second copy
    of record, and shipping it would only grow the bundle and go stale the
    moment restore's own reindex runs.
    """
    candidates = [
        (path, arcname)
        for path, arcname in _iter_bundle_files(data_dir)
        if not arcname.startswith("repo/index/")
    ]
    counts = _counts(data_dir)

    bundle_name = f"chronicle-backup-{_utc_stamp()}.tar.gz"
    bundle_path = (out / bundle_name) if out and out.is_dir() else (out or Path(bundle_name))
    bundle_path.parent.mkdir(parents=True, exist_ok=True)

    # Each member is hashed and tarred from the same single read of the
    # file, through _HashingReader, rather than one read to hash and a
    # second (tar.add's own) to tar: this is a live tree (the api keeps
    # writing while a backup runs), and two separate reads of a file
    # rewritten in between would produce a bundle whose own manifest
    # checksum does not match its own tar member. The manifest itself is
    # written last, once every real member's hash is known, so it can
    # still be the thing a caller reads first on extraction (member order
    # in a tar does not matter, `_load_manifest` looks it up by name). A
    # file that vanishes between listing and opening it (a builder
    # claiming `repo/runs/queue/<run_id>.json`, a git repack) is dropped
    # from the bundle rather than crashing the whole backup; it is
    # domain-transient, not a record backup exists to protect.
    files: dict[str, str] = {}
    with tarfile.open(bundle_path, "w:gz") as tar:
        for path, arcname in candidates:
            try:
                info = tar.gettarinfo(path, arcname=arcname)
                handle = path.open("rb")
            except FileNotFoundError:
                continue
            with handle:
                reader = _HashingReader(handle)
                tar.addfile(info, reader)
                files[arcname] = reader.sha256.hexdigest()

        manifest: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "created_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
            "chronicle_version": chronicle_version,
            "counts": counts,
            "files": files,
        }
        manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
        manifest_info = tarfile.TarInfo(name=MANIFEST_NAME)
        manifest_info.size = len(manifest_bytes)
        tar.addfile(manifest_info, io.BytesIO(manifest_bytes))

    return bundle_path


class _HashingReader:
    """Wraps a file so tar.addfile's own streamed read computes sha256
    over the identical bytes it writes into the tar, one filesystem read
    per member, never a separate read for hashing and another for taring."""

    def __init__(self, fileobj: Any) -> None:
        self._fileobj = fileobj
        self.sha256 = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        chunk: bytes = self._fileobj.read(size)
        self.sha256.update(chunk)
        return chunk


def _safe_member(member: tarfile.TarInfo, staging: Path) -> Path:
    """Refuse anything that could write outside `staging` or isn't a plain file.

    A round C6 review target on its own: an absolute path, a `..` segment,
    or a symlink/hardlink member in a bundle from an untrusted or corrupted
    source must never be extracted, because tarfile's own `extractall`
    follows exactly none of these checks by default. `manifest.json` is not
    exempt from any of this: a bundle naming it as a symlink or hardlink
    must be refused exactly like any other member, so only the root entry
    names ("" and ".") skip straight to a path.
    """
    name = member.name
    if name in ("", "."):
        return staging / name
    if os.path.isabs(name) or ".." in Path(name).parts:
        raise BackupError(f"refusing unsafe bundle member path {name!r}")
    if member.issym() or member.islnk():
        raise BackupError(f"refusing symlink/hardlink bundle member {name!r}")
    if not (member.isfile() or member.isdir()):
        raise BackupError(f"refusing non-regular bundle member {name!r}")
    resolved = (staging / name).resolve()
    if resolved != staging.resolve() and staging.resolve() not in resolved.parents:
        raise BackupError(f"refusing bundle member outside staging: {name!r}")
    return staging / name


def _extract_bundle(bundle_path: Path, staging: Path) -> None:
    staging.mkdir(parents=True, exist_ok=True)
    with tarfile.open(bundle_path, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            _safe_member(member, staging)
        # Validated every member before extracting any of them, so a bundle
        # that fails validation halfway through never leaves a partial
        # extraction for the swap step to pick up.
        tar.extractall(staging, members=members, filter="data")


def _load_manifest(staging: Path) -> dict[str, Any]:
    manifest_path = staging / MANIFEST_NAME
    if not manifest_path.exists():
        raise BackupError("bundle has no manifest.json")
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = manifest.get("schema_version")
    if version != SCHEMA_VERSION:
        raise BackupError(
            f"bundle schema_version {version!r} is not supported; this build expects "
            f"{SCHEMA_VERSION}"
        )
    return manifest


def _verify_members(staging: Path, manifest: dict[str, Any]) -> None:
    expected: dict[str, str] = manifest.get("files", {})
    found: set[str] = set()
    for path in sorted(staging.rglob("*")):
        if not path.is_file():
            continue
        arcname = str(path.relative_to(staging))
        if arcname == MANIFEST_NAME:
            continue
        found.add(arcname)
    expected_names = set(expected)
    missing = expected_names - found
    extra = found - expected_names
    if missing:
        raise BackupError(f"bundle is missing members the manifest lists: {sorted(missing)}")
    if extra:
        raise BackupError(f"bundle carries members the manifest does not list: {sorted(extra)}")
    mismatched = [name for name, sha in expected.items() if _sha256_file(staging / name) != sha]
    if mismatched:
        raise BackupError(f"checksum mismatch on bundle members: {sorted(mismatched)}")


def _decrypt_check(data_dir: Path) -> bool | None:
    """Best-effort: can the restored github-app.json be decrypted under this
    instance's key? None if there is nothing to check (no credentials in
    the bundle), so the admin page can say so without a crash either way.
    """
    from .api import crypto
    from .api.github_app import GitHubAppStore

    state_dir = data_dir / "state"
    if not (state_dir / "github-app.json").exists():
        return None
    try:
        key = crypto.load_or_create_instance_key(state_dir)
        store = GitHubAppStore(state_dir, key)
        record = store.load()
        if record is None:
            return None
        store.pem(record)
        return True
    except Exception:  # noqa: BLE001 - any decrypt failure means "not decryptable"
        return False


def restore_backup(data_dir: Path, bundle_path: Path) -> RestoreReport:
    """Validate, swap, reindex. The api process must be stopped first: this
    round assumes a single writer against the data directory the same way
    the publisher does (ADR 013), and a restore mid-request would let a
    write land on a tree that's about to be renamed out from under it.
    """
    stamp = _utc_stamp()
    # Under data_dir itself, not data_dir.parent: the data directory is the
    # one path guaranteed to be a writable mounted volume (CHRONICLE_DATA_DIR)
    # in every deployment shape (compose, k8s), while its parent is the
    # container's root filesystem, frequently read-only and never uid 1000's
    # to write into. Found live in the C6 backup/restore check: mkdir on
    # data_dir.parent raised PermissionError against a real compose stack.
    staging = data_dir / f".restore-staging-{stamp}"
    pre_restore = data_dir / f".pre-restore-{stamp}"

    _extract_bundle(bundle_path, staging)
    manifest = _load_manifest(staging)
    _verify_members(staging, manifest)

    before = _counts(data_dir) if (data_dir / "repo").exists() else dict.fromkeys(COUNT_KEYS, 0)

    initialised_git = False
    staged_repo = staging / "repo"
    if staged_repo.is_dir() and not (staged_repo / ".git").exists():
        gitrepo.init_repo(staged_repo)
        initialised_git = True

    pre_restore.mkdir(parents=True, exist_ok=True)
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    for name, staged_sub in (("repo", staged_repo), ("images", staging / "images")):
        current = data_dir / name
        if current.exists():
            shutil.move(str(current), str(pre_restore / name))
        if staged_sub.exists():
            shutil.move(str(staged_sub), str(current))
        else:
            current.mkdir(parents=True, exist_ok=True)

    # Every one of STATE_MEMBERS is moved aside into pre_restore whenever it
    # currently exists, whether or not the bundle carries a replacement: a
    # bundle omitting admin.json or github-app.json means "unclaimed" or
    # "disconnected" after restore, not "leave whatever is already there".
    # Only the presence of a staged file decides whether one gets installed.
    staged_state = staging / "state"
    pre_state = pre_restore / "state"
    pre_state.mkdir(parents=True, exist_ok=True)
    for name in STATE_MEMBERS:
        current_file = state_dir / name
        if current_file.exists():
            shutil.move(str(current_file), str(pre_state / name))
        staged_file = staged_state / name
        if staged_file.exists():
            shutil.move(str(staged_file), str(current_file))

    after = _counts(data_dir)
    shutil.rmtree(staging, ignore_errors=True)
    # Only removed once reindex above has already succeeded against the new
    # tree; a reindex failure raises before this line and leaves
    # pre_restore in place for manual recovery.
    shutil.rmtree(pre_restore, ignore_errors=True)

    decryptable = _decrypt_check(data_dir)
    return RestoreReport(
        before=before,
        after=after,
        initialised_git=initialised_git,
        credentials_decryptable=decryptable,
    )
