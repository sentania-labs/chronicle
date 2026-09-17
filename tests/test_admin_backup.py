"""The admin backup page: create/download, upload/confirm/restore."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from chronicle import backup as backup_mod
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
