"""Scheduled backups: schedule, targets, retention, status (issue #68)."""

from __future__ import annotations

import datetime as dt
import json
import tarfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from chronicle import backup as backup_mod
from chronicle.api import crypto, scheduled_backup
from chronicle.api.admin_deps import AdminServices
from chronicle.api.admin_status import scheduled_backup_summary
from chronicle.api.scheduled_backup import BackupSettings
from chronicle.api.store import Store

CHICAGO = scheduled_backup.SCHEDULE_TZ


def _local(year: int, month: int, day: int, hour: int, minute: int) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=CHICAGO)


# --- The schedule -----------------------------------------------------------


def test_daily_runs_once_at_the_time_of_day() -> None:
    slots = scheduled_backup.schedule_slots(dt.date(2026, 9, 24), 24, "03:00")
    assert slots == [_local(2026, 9, 24, 3, 0)]


def test_every_six_hours_fills_the_day_around_the_time() -> None:
    slots = scheduled_backup.schedule_slots(dt.date(2026, 9, 24), 6, "09:30")
    assert [s.strftime("%H:%M") for s in slots] == ["03:30", "09:30", "15:30", "21:30"]


def test_weekly_runs_only_on_sunday() -> None:
    week = [dt.date(2026, 9, 20) + dt.timedelta(days=n) for n in range(7)]
    days = [d for d in week if scheduled_backup.schedule_slots(d, 168, "03:00")]
    assert days == [dt.date(2026, 9, 20)]
    assert days[0].weekday() == 6


def test_daily_keeps_its_wall_clock_time_across_a_dst_change() -> None:
    since = _local(2026, 10, 31, 3, 0)
    due = scheduled_backup.next_due(since, 24, "03:00")
    assert due == _local(2026, 11, 1, 3, 0)  # DST ended overnight; still 03:00 local
    assert due.utcoffset() == dt.timedelta(hours=-6)


def test_a_never_run_schedule_counts_from_when_it_was_saved() -> None:
    settings = BackupSettings(enabled=True, updated_at="2026-09-24T14:00:00-05:00")
    assert not scheduled_backup.is_due(settings, {}, _local(2026, 9, 25, 2, 59))
    assert scheduled_backup.is_due(settings, {}, _local(2026, 9, 25, 3, 0))


def test_missed_slots_run_once_not_once_each() -> None:
    settings = BackupSettings(enabled=True, updated_at="2026-09-01T00:00:00-05:00")
    status = {"last_attempt_at": "2026-09-20T03:00:05-05:00"}
    now = _local(2026, 9, 24, 12, 0)
    assert scheduled_backup.is_due(settings, status, now)
    status["last_attempt_at"] = now.isoformat()
    assert not scheduled_backup.is_due(settings, status, now)


def test_overdue_after_two_intervals_without_a_success() -> None:
    settings = BackupSettings(enabled=True, updated_at="2026-09-20T00:00:00-05:00")
    status = {"last_success_at": "2026-09-22T03:00:00-05:00"}
    assert not scheduled_backup.is_overdue(settings, status, _local(2026, 9, 24, 2, 0))
    assert scheduled_backup.is_overdue(settings, status, _local(2026, 9, 24, 4, 0))
    off = settings.model_copy(update={"enabled": False})
    assert not scheduled_backup.is_overdue(off, status, _local(2026, 9, 30, 4, 0))


# --- Settings validation ----------------------------------------------------


def test_a_local_path_under_the_data_directory_is_refused(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    for path in (str(data), str(data / "backups"), "relative/dir", ""):
        settings = BackupSettings(target="local", local_path=path)
        assert scheduled_backup.settings_problems(settings, data), path
    ok = BackupSettings(target="local", local_path=str(tmp_path / "backups"))
    assert scheduled_backup.settings_problems(ok, data) == []


def test_s3_settings_need_endpoint_bucket_and_both_credentials(tmp_path: Path) -> None:
    problems = scheduled_backup.settings_problems(BackupSettings(target="s3"), tmp_path)
    assert any("endpoint" in p for p in problems)
    assert any("bucket" in p for p in problems)
    assert any("access key" in p for p in problems)


# --- A run, local target ----------------------------------------------------


def _seed(data_dir: Path) -> None:
    store = Store.open(data_dir)
    draft, _ = store.create_draft("ghostwriter")
    store.save_draft(draft.id, "ghostwriter", 0, {"title": "A Post"}, "body")
    store.close()


def _state(data_dir: Path) -> tuple[Path, bytes]:
    state = data_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    return state, crypto.load_or_create_instance_key(state)


def test_a_local_run_lands_a_restorable_bundle_and_prunes_to_retention(
    data_dir: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    _seed(data_dir)
    state, key = _state(data_dir)
    target = tmp_path_factory.mktemp("backups")
    old = [f"chronicle-backup-2026010{n}T030000Z.tar.gz" for n in range(1, 4)]
    for name in old:
        (target / name).write_bytes(b"old")
    (target / "not-ours.txt").write_text("keep me", encoding="utf-8")
    settings = BackupSettings(target="local", local_path=str(target), retention=2)

    status = scheduled_backup.run_once(data_dir, state, key, settings=settings)

    assert status is not None and "last_error" not in status
    bundles = sorted(p.name for p in target.glob("chronicle-backup-*.tar.gz"))
    assert len(bundles) == 2 and bundles[0] == old[-1]  # newest old one + the new one
    assert (target / "not-ours.txt").exists()
    assert status["last_pruned"] == old[:2]
    assert status["last_location"] == str(target / bundles[-1])
    assert status["last_size_bytes"] == (target / bundles[-1]).stat().st_size
    assert not list((state / "backup-tmp").glob("*.tar.gz"))  # the temp copy is gone

    with tarfile.open(target / bundles[-1]) as tar:
        names = tar.getnames()
    assert "state/instance.key" not in names
    assert f"state/{scheduled_backup.SETTINGS_FILE_NAME}" not in names

    fresh = data_dir.parent / "restored"
    report = backup_mod.restore_backup(fresh, target / bundles[-1])
    assert report.after["drafts"] == 1


def test_a_failed_run_records_the_error_and_keeps_going(data_dir: Path, tmp_path: Path) -> None:
    state, key = _state(data_dir)
    blocker = tmp_path.parent / f"{tmp_path.name}-a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    settings = BackupSettings(target="local", local_path=str(blocker / "sub"))

    status = scheduled_backup.run_once(data_dir, state, key, settings=settings)

    assert status is not None
    assert status["last_failure_at"] and status["last_error"]
    assert "last_success_at" not in status
    assert scheduled_backup.load_status(state)["last_error"] == status["last_error"]


def test_the_loop_tick_runs_only_when_due(
    data_dir: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    state, key = _state(data_dir)
    target = tmp_path_factory.mktemp("tick")
    settings = BackupSettings(
        enabled=True, target="local", local_path=str(target), updated_at="2026-09-24T12:00:00-05:00"
    )
    scheduled_backup.save_settings(state, settings)
    assert not scheduled_backup.tick(data_dir, state, key, _local(2026, 9, 24, 13, 0))
    assert scheduled_backup.tick(data_dir, state, key, _local(2026, 9, 25, 3, 1))
    assert len(list(target.glob("chronicle-backup-*.tar.gz"))) == 1


# --- A run, S3 target -------------------------------------------------------


class FakeS3:
    def __init__(self, existing: list[str]):
        self.objects: dict[str, bytes] = {key: b"old" for key in existing}
        self.requests: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/backups/")
        self.requests.append((request.method, path))
        if request.method == "PUT":
            self.objects[path] = request.read()
            return httpx.Response(200)
        if request.method == "DELETE":
            self.objects.pop(path, None)
            return httpx.Response(204)
        prefix = request.url.params.get("prefix", "")
        contents = "".join(
            f"<Contents><Key>{key}</Key></Contents>"
            for key in sorted(self.objects)
            if key.startswith(prefix)
        )
        return httpx.Response(
            200,
            content=(
                "<ListBucketResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'>"
                f"<IsTruncated>false</IsTruncated>{contents}</ListBucketResult>"
            ).encode(),
        )


def _s3_settings(key: bytes, **extra: object) -> BackupSettings:
    return BackupSettings(
        target="s3",
        s3_endpoint="https://nas.lan:9000",
        s3_bucket="backups",
        s3_prefix="chronicle",
        s3_region="us-east-1",
        s3_access_key_id="AKID",
        s3_secret_enc=crypto.encrypt(key, "s3cret"),
        **extra,  # type: ignore[arg-type]
    )


def test_an_s3_run_uploads_under_the_prefix_and_prunes(data_dir: Path) -> None:
    _seed(data_dir)
    state, key = _state(data_dir)
    fake = FakeS3(
        [f"chronicle/chronicle-backup-2026010{n}T030000Z.tar.gz" for n in (1, 2)]
        + ["chronicle/other.txt", "elsewhere/chronicle-backup-20250101T030000Z.tar.gz"]
    )
    settings = _s3_settings(key, retention=2)

    status = scheduled_backup.run_once(
        data_dir, state, key, settings=settings, transport=httpx.MockTransport(fake)
    )

    assert status is not None and "last_error" not in status
    ours = sorted(k for k in fake.objects if k.startswith("chronicle/chronicle-backup-"))
    assert len(ours) == 2 and ours[0].endswith("20260102T030000Z.tar.gz")
    assert "chronicle/other.txt" in fake.objects
    assert "elsewhere/chronicle-backup-20250101T030000Z.tar.gz" in fake.objects
    assert status["last_location"].startswith("s3://backups/chronicle/chronicle-backup-")


def test_the_s3_probe_writes_and_removes_one_object(data_dir: Path) -> None:
    state, key = _state(data_dir)
    fake = FakeS3([])
    problem = scheduled_backup.test_target(
        _s3_settings(key), data_dir, key, transport=httpx.MockTransport(fake)
    )
    assert problem is None
    assert [m for m, _ in fake.requests] == ["PUT", "DELETE"]
    assert fake.objects == {}


def test_a_rejected_probe_reports_why(data_dir: Path) -> None:
    state, key = _state(data_dir)

    def deny(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"<Error><Code>AccessDenied</Code></Error>")

    problem = scheduled_backup.test_target(
        _s3_settings(key), data_dir, key, transport=httpx.MockTransport(deny)
    )
    assert problem is not None and "403 AccessDenied" in problem
    assert "s3cret" not in problem


# --- /admin -----------------------------------------------------------------


def _admin(data_dir: Path) -> AdminServices:
    return AdminServices.build(data_dir)


def test_saving_s3_settings_encrypts_the_secret_and_never_echoes_it(
    admin_client: TestClient, data_dir: Path
) -> None:
    form = {
        "enabled": "1",
        "interval_hours": "24",
        "time_of_day": "02:30",
        "retention": "7",
        "target": "s3",
        "s3_endpoint": "https://nas.lan:9000",
        "s3_bucket": "backups",
        "s3_prefix": "chronicle",
        "s3_region": "us-east-1",
        "s3_access_key_id": "AKID",
        "s3_secret": "top-secret-value",
    }
    response = admin_client.post("/admin/backup/schedule", data=form)
    assert response.status_code == 200
    assert "Schedule saved." in response.text
    assert "top-secret-value" not in response.text

    raw = (data_dir / "state" / scheduled_backup.SETTINGS_FILE_NAME).read_text(encoding="utf-8")
    assert "top-secret-value" not in raw
    saved = scheduled_backup.load_settings(data_dir / "state")
    key = crypto.load_or_create_instance_key(data_dir / "state")
    assert crypto.decrypt(key, saved.s3_secret_enc) == "top-secret-value"
    assert (
        oct((data_dir / "state" / scheduled_backup.SETTINGS_FILE_NAME).stat().st_mode)[-3:] == "600"
    )

    # A blank secret on the next save keeps the stored one.
    admin_client.post("/admin/backup/schedule", data={**form, "s3_secret": ""})
    again = scheduled_backup.load_settings(data_dir / "state")
    assert crypto.decrypt(key, again.s3_secret_enc) == "top-secret-value"


def test_enabling_a_local_path_under_data_is_refused(
    admin_client: TestClient, data_dir: Path
) -> None:
    response = admin_client.post(
        "/admin/backup/schedule",
        data={
            "enabled": "1",
            "interval_hours": "24",
            "time_of_day": "03:00",
            "retention": "14",
            "target": "local",
            "local_path": str(data_dir / "backups"),
        },
    )
    assert response.status_code == 400
    assert "must not be under the data directory" in response.text
    assert not scheduled_backup.load_settings(data_dir / "state").enabled


def test_test_target_button_reports_success(
    admin_client: TestClient, data_dir: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    target = tmp_path_factory.mktemp("probe")
    scheduled_backup.save_settings(
        data_dir / "state", BackupSettings(target="local", local_path=str(target))
    )
    response = admin_client.post("/admin/backup/test")
    assert response.status_code == 200
    assert "Target test passed" in response.text
    assert list(target.iterdir()) == []


def test_the_status_page_warns_when_backups_are_overdue(
    admin_client: TestClient, data_dir: Path
) -> None:
    scheduled_backup.save_settings(
        data_dir / "state",
        BackupSettings(
            enabled=True,
            target="local",
            local_path="/backups",
            updated_at="2020-01-01T00:00:00+00:00",
        ),
    )
    summary = scheduled_backup_summary(_admin(data_dir))
    assert summary["overdue"] is True
    html = admin_client.get("/admin").text
    assert "overdue" in html
    assert "no successful backup in over two intervals" in html


def test_the_status_shows_the_last_failure_with_its_error(data_dir: Path) -> None:
    state = data_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / scheduled_backup.STATUS_FILE_NAME).write_text(
        json.dumps({"last_failure_at": "2026-09-24T03:00:00+00:00", "last_error": "OSError: x"}),
        encoding="utf-8",
    )
    scheduled_backup.save_settings(state, BackupSettings(enabled=True, local_path="/b"))
    summary = scheduled_backup_summary(_admin(data_dir))
    assert summary["last_error"] == "OSError: x"
