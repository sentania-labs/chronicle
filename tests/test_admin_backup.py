"""The admin backup page: create/download, upload/confirm/restore."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile as StarletteUploadFile

from chronicle import backup as backup_mod
from chronicle.api.routes import admin as admin_routes
from chronicle.api.store import Store

from .conftest import png_bytes


def _seed_draft(data_dir: Path) -> None:
    store = Store.open(data_dir)
    draft, _ = store.create_draft("ghostwriter")
    store.save_draft(draft.id, "ghostwriter", 0, {"title": "A Post"}, "body")
    image, _ = store.put_image(png_bytes(), "feature.png")
    store.attach_image(draft.id, image.image_id, "feature", "ghostwriter")
    store.close()


def test_backup_page_shows_never_before_a_backup(admin_client: TestClient) -> None:
    response = admin_client.get("/admin/backup")
    assert response.status_code == 200
    assert "never" in response.text


def test_backup_create_streams_a_tarball_and_records_last_backup(
    admin_client: TestClient, data_dir: Path
) -> None:
    _seed_draft(data_dir)
    response = admin_client.get("/admin/backup/create")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/gzip"
    assert response.content[:2] == b"\x1f\x8b"  # gzip magic
    assert not list((data_dir / "state" / "backup-tmp").glob("*.tar.gz"))  # deleted after streaming

    status = admin_client.get("/admin/backup")
    assert "never" not in status.text


def test_backup_upload_previews_manifest_counts_without_restoring(
    admin_client: TestClient, data_dir: Path
) -> None:
    _seed_draft(data_dir)
    bundle = backup_mod.create_backup(data_dir, data_dir.parent / "out.tar.gz")
    with bundle.open("rb") as handle:
        response = admin_client.post(
            "/admin/backup/upload", files={"file": ("bundle.tar.gz", handle, "application/gzip")}
        )
    assert response.status_code == 200
    assert "drafts" in response.text
    assert 'name="token"' in response.text
    assert 'name="confirm"' in response.text


def test_backup_upload_reads_the_body_in_bounded_chunks(
    admin_client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`await file.read()` with no size argument materialises the whole
    upload as one bytes object; the reference deployment's 512 MiB memory
    limit (examples/k8s/deployment.yaml) makes that an OOM risk for a
    near-ceiling upload. Every read the route makes must ask for a bounded
    chunk, never the whole body at once."""
    _seed_draft(data_dir)
    bundle = backup_mod.create_backup(data_dir, data_dir.parent / "chunked.tar.gz")

    requested_sizes: list[int] = []
    original_read = StarletteUploadFile.read

    async def spying_read(self: UploadFile, size: int = -1) -> bytes:
        requested_sizes.append(size)
        return await original_read(self, size)

    monkeypatch.setattr(StarletteUploadFile, "read", spying_read)

    with bundle.open("rb") as handle:
        response = admin_client.post(
            "/admin/backup/upload", files={"file": ("bundle.tar.gz", handle, "application/gzip")}
        )
    assert response.status_code == 200
    assert requested_sizes, "backup_upload never called file.read"
    assert all(size > 0 for size in requested_sizes), (
        f"backup_upload requested an unbounded read: {requested_sizes}"
    )


def test_backup_restore_without_typing_restore_is_refused(admin_client: TestClient) -> None:
    response = admin_client.post(
        "/admin/backup/restore", data={"token": "nope", "confirm": "yes please"}
    )
    assert response.status_code == 400
    assert "restore" in response.text.lower()


def test_backup_upload_then_restore_round_trip(admin_client: TestClient, data_dir: Path) -> None:
    _seed_draft(data_dir)
    bundle = backup_mod.create_backup(data_dir, data_dir.parent / "out2.tar.gz")
    with bundle.open("rb") as handle:
        preview = admin_client.post(
            "/admin/backup/upload", files={"file": ("bundle.tar.gz", handle, "application/gzip")}
        )
    token = preview.text.split('name="token" value="')[1].split('"')[0]

    restore = admin_client.post(
        "/admin/backup/restore", data={"token": token, "confirm": "restore"}
    )
    assert restore.status_code == 200
    assert "restored:" in restore.text

    # The restore swap replaces the data directory underneath the running
    # process; reload_after_restore must rebind Services so the very next
    # request is served from the restored store, not a closed, stale one.
    status = admin_client.get("/admin/backup")
    assert status.status_code == 200


def test_backup_restore_rejects_a_path_traversal_token(admin_client: TestClient) -> None:
    response = admin_client.post(
        "/admin/backup/restore",
        data={"token": "../../../../etc/passwd", "confirm": "restore"},
    )
    assert response.status_code == 400
    assert "expired" in response.text.lower()


def test_backup_restore_rejects_a_malformed_token(admin_client: TestClient) -> None:
    response = admin_client.post(
        "/admin/backup/restore",
        data={"token": "not-32-hex-chars", "confirm": "restore"},
    )
    assert response.status_code == 400
    assert "expired" in response.text.lower()


def _age_file(path: Path, seconds_old: float) -> None:
    stamp = time.time() - seconds_old
    os.utime(path, (stamp, stamp))


def test_backup_page_sweeps_a_stale_upload(admin_client: TestClient, data_dir: Path) -> None:
    """An upload nobody ever confirmed (Cancel, a closed tab, a lost
    session) has no other trigger to remove it; visiting the backup page
    must sweep anything past UPLOAD_TTL_SECONDS so repeated cancellations
    cannot exhaust the data volume."""
    tmp_dir = data_dir / "state" / "backup-tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    stale = tmp_dir / "upload-deadbeefdeadbeefdeadbeefdeadbeef.tar.gz"
    stale.write_bytes(b"stale bundle bytes")
    _age_file(stale, admin_routes.UPLOAD_TTL_SECONDS + 60)

    fresh = tmp_dir / "upload-00000000000000000000000000000000.tar.gz"
    fresh.write_bytes(b"fresh bundle bytes")

    response = admin_client.get("/admin/backup")
    assert response.status_code == 200
    assert not stale.exists()
    assert fresh.exists()


def test_backup_restore_refuses_an_expired_token_and_removes_the_file(
    admin_client: TestClient, data_dir: Path
) -> None:
    tmp_dir = data_dir / "state" / "backup-tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    token = "deadbeefdeadbeefdeadbeefdeadbeef"
    staged = tmp_dir / f"upload-{token}.tar.gz"
    staged.write_bytes(b"stale bundle bytes")
    _age_file(staged, admin_routes.UPLOAD_TTL_SECONDS + 60)

    response = admin_client.post(
        "/admin/backup/restore", data={"token": token, "confirm": "restore"}
    )
    assert response.status_code == 400
    assert "expired" in response.text.lower()
    assert not staged.exists()
