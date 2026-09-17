"""Backup create and restore: round trip, tamper refusal, schema refusal."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from chronicle import backup
from chronicle.api.store import Store
from tests.conftest import png_bytes

FRONTMATTER = {"title": "A Post About Backup", "tags": ["lab"]}


def _seed(data_dir: Path) -> Store:
    store = Store.open(data_dir)
    draft, _ = store.create_draft("ghostwriter")
    store.save_draft(draft.id, "ghostwriter", 0, FRONTMATTER, "body")
    image, _ = store.put_image(png_bytes(), "feature.png")
    store.attach_image(draft.id, image.image_id, "feature", "ghostwriter")
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    (data_dir / "state" / "instance.key").write_bytes(b"secret-instance-key")
    (data_dir / "state" / "claim-code").write_text("claim-me", encoding="utf-8")
    (data_dir / "state" / "ui_token.txt").write_text("ui-secret", encoding="utf-8")
    (data_dir / "state" / "tokens.json").write_text(
        json.dumps({"tokens": [{"name": "ghostwriter", "hash": "x", "created_at": "now"}]}),
        encoding="utf-8",
    )
    return store


def test_backup_create_excludes_secrets_and_disposable_trees(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    store = _seed(data_dir)
    store.close()

    bundle = backup.create_backup(data_dir, tmp_path / "out")
    with tarfile.open(bundle, "r:gz") as tar:
        names = tar.getnames()

    assert not any("instance.key" in name for name in names)
    assert not any("claim-code" in name for name in names)
    assert not any("ui_token.txt" in name for name in names)
    assert not any(name.startswith("preview/") for name in names)
    assert not any(name.startswith("site/") for name in names)
    assert not any(name.startswith("repo/index/") for name in names)
    assert any(name.startswith("repo/drafts/") for name in names)
    assert any(name.startswith("images/") for name in names)
    assert any(name == "state/tokens.json" for name in names)
    assert "manifest.json" in names


def test_backup_restore_round_trip_matches_counts(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    store = _seed(data_dir)
    before_counts = store.reindex()
    store.close()

    bundle = backup.create_backup(data_dir, tmp_path / "out")

    fresh_dir = tmp_path / "fresh"
    Store.open(fresh_dir).close()
    report = backup.restore_backup(fresh_dir, bundle)

    assert report.after["drafts"] == before_counts["drafts"] == 1
    assert report.after["images"] == before_counts["images"] == 1
    assert report.initialised_git is False

    restored = Store.open(fresh_dir)
    drafts = restored.list_drafts()
    assert len(drafts) == 1
    assert drafts[0].body == "body"
    restored.close()

    assert not (fresh_dir / "state" / "instance.key").exists()


def test_restore_refuses_a_tampered_member(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed(data_dir).close()
    bundle = backup.create_backup(data_dir, tmp_path / "out")

    tampered = tmp_path / "tampered.tar.gz"
    _rewrite_member(bundle, tampered, "state/tokens.json", b'{"tokens": []}')

    with pytest.raises(backup.BackupError, match="checksum mismatch"):
        backup.restore_backup(tmp_path / "fresh1", tampered)


def test_restore_refuses_a_missing_member(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed(data_dir).close()
    bundle = backup.create_backup(data_dir, tmp_path / "out")

    stripped = tmp_path / "stripped.tar.gz"
    _drop_member(bundle, stripped, "state/tokens.json")

    with pytest.raises(backup.BackupError, match="missing"):
        backup.restore_backup(tmp_path / "fresh2", stripped)


def test_restore_refuses_an_extra_member(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed(data_dir).close()
    bundle = backup.create_backup(data_dir, tmp_path / "out")

    extra = tmp_path / "extra.tar.gz"
    _add_member(bundle, extra, "state/extra-secret.txt", b"not in the manifest")

    with pytest.raises(backup.BackupError, match="does not list"):
        backup.restore_backup(tmp_path / "fresh3", extra)


def test_restore_refuses_unknown_schema_version(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed(data_dir).close()
    bundle = backup.create_backup(data_dir, tmp_path / "out")

    bumped = tmp_path / "bumped.tar.gz"
    _rewrite_manifest_schema_version(bundle, bumped, 99)

    with pytest.raises(backup.BackupError, match="schema_version"):
        backup.restore_backup(tmp_path / "fresh4", bumped)


def test_restore_refuses_path_traversal_member(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed(data_dir).close()
    bundle = backup.create_backup(data_dir, tmp_path / "out")

    traversal = tmp_path / "traversal.tar.gz"
    _add_member(bundle, traversal, "../evil.txt", b"escape")

    with pytest.raises(backup.BackupError, match="unsafe"):
        backup.restore_backup(tmp_path / "fresh5", traversal)


def test_restore_removes_a_state_file_the_bundle_omits(tmp_path: Path) -> None:
    """A bundle that never had admin.json (never claimed, or the operator
    left it out by hand per docs/backup.md) must leave the restored
    instance unclaimed, not carry over whatever the destination already
    had staged for it."""
    data_dir = tmp_path / "data"
    _seed(data_dir).close()
    bundle = backup.create_backup(data_dir, tmp_path / "out")

    fresh_dir = tmp_path / "fresh"
    Store.open(fresh_dir).close()
    (fresh_dir / "state").mkdir(parents=True, exist_ok=True)
    (fresh_dir / "state" / "admin.json").write_text("claimed-elsewhere", encoding="utf-8")

    backup.restore_backup(fresh_dir, bundle)

    assert not (fresh_dir / "state" / "admin.json").exists()


def test_restore_initialises_git_when_bundle_repo_has_none(tmp_path: Path) -> None:
    """The hand-built import bundle case (docs/backup.md): a bundle may
    arrive with no .git at all, and restore gives it one commit authored
    `chronicle` rather than refusing it."""
    data_dir = tmp_path / "data"
    store = _seed(data_dir)
    store.close()
    bundle = backup.create_backup(data_dir, tmp_path / "out")

    stripped = tmp_path / "no-git.tar.gz"
    _drop_git(bundle, stripped)

    report = backup.restore_backup(tmp_path / "fresh6", stripped)
    assert report.initialised_git is True
    assert report.after["drafts"] == 1


def _rewrite_member(src: Path, dest: Path, name: str, content: bytes) -> None:
    with tarfile.open(src, "r:gz") as tar_in, tarfile.open(dest, "w:gz") as tar_out:
        for member in tar_in.getmembers():
            if member.name == name:
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                tar_out.addfile(info, __import__("io").BytesIO(content))
            else:
                tar_out.addfile(member, tar_in.extractfile(member))


def _drop_member(src: Path, dest: Path, name: str) -> None:
    with tarfile.open(src, "r:gz") as tar_in, tarfile.open(dest, "w:gz") as tar_out:
        for member in tar_in.getmembers():
            if member.name != name:
                tar_out.addfile(member, tar_in.extractfile(member))


def _add_member(src: Path, dest: Path, name: str, content: bytes) -> None:
    with tarfile.open(src, "r:gz") as tar_in, tarfile.open(dest, "w:gz") as tar_out:
        for member in tar_in.getmembers():
            tar_out.addfile(member, tar_in.extractfile(member))
        info = tarfile.TarInfo(name=name)
        info.size = len(content)
        tar_out.addfile(info, __import__("io").BytesIO(content))


def _rewrite_manifest_schema_version(src: Path, dest: Path, version: int) -> None:
    with tarfile.open(src, "r:gz") as tar_in, tarfile.open(dest, "w:gz") as tar_out:
        for member in tar_in.getmembers():
            if member.name == "manifest.json":
                data = tar_in.extractfile(member)
                assert data is not None
                manifest = json.loads(data.read())
                manifest["schema_version"] = version
                content = (json.dumps(manifest) + "\n").encode("utf-8")
                info = tarfile.TarInfo(name="manifest.json")
                info.size = len(content)
                tar_out.addfile(info, __import__("io").BytesIO(content))
            else:
                tar_out.addfile(member, tar_in.extractfile(member))


def _drop_git(src: Path, dest: Path) -> None:
    with tarfile.open(src, "r:gz") as tar_in, tarfile.open(dest, "w:gz") as tar_out:
        for member in tar_in.getmembers():
            if member.name.startswith("repo/.git"):
                continue
            if member.name == "manifest.json":
                data = tar_in.extractfile(member)
                assert data is not None
                manifest = json.loads(data.read())
                manifest["files"] = {
                    name: sha
                    for name, sha in manifest["files"].items()
                    if not name.startswith("repo/.git")
                }
                content = (json.dumps(manifest) + "\n").encode("utf-8")
                info = tarfile.TarInfo(name="manifest.json")
                info.size = len(content)
                tar_out.addfile(info, __import__("io").BytesIO(content))
            else:
                tar_out.addfile(member, tar_in.extractfile(member))
