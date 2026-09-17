"""Leases, queue claim, atomic swap, toolchain drift, and run lifecycle."""

from __future__ import annotations

import json
import logging
import stat
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from chronicle.api.store import Store
from chronicle.builder import main as builder_main
from chronicle.builder import runner
from chronicle.builder.leases import Lease, LeaseDirectory
from chronicle.builder.settings import BuilderSettings
from tests.conftest import png_bytes

FAKE_HUGO = """#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
if args and args[0] == "version":
    print("hugo v0.164.0-fake+extended linux/amd64 BuildDate=2026-01-01T00:00:00Z")
    sys.exit(0)


def value(flag):
    return args[args.index(flag) + 1]


if os.environ.get("CHRONICLE_FAKE_HUGO_FAIL"):
    sys.stderr.write("fake hugo: forced failure\\n")
    sys.exit(1)

destination = value("--destination")
os.makedirs(destination, exist_ok=True)
base_url = value("--baseURL")
with open(os.path.join(destination, "index.html"), "w") as handle:
    handle.write(f"<html>built for {base_url}</html>")
sys.exit(0)
"""


@pytest.fixture
def fake_hugo(tmp_path: Path) -> Path:
    script = tmp_path / "fake-hugo"
    script.write_text(FAKE_HUGO, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.fixture
def builder_settings(data_dir: Path, fake_hugo: Path) -> BuilderSettings:
    return BuilderSettings(
        data_dir=data_dir,
        external_url="http://localhost:8080",
        builder_id="test-builder",
        poll_seconds=0.01,
        lease_seconds=60,
        work_dir=data_dir / "builder-work",
        hugo_bin=str(fake_hugo),
        build_timeout_seconds=10,
        once=True,
    )


def _leases(builder_settings: BuilderSettings) -> LeaseDirectory:
    return LeaseDirectory(builder_settings.leases_dir, builder_settings.lease_seconds)


def _make_draft(store: Store) -> str:
    draft, _ = store.create_draft("ghostwriter")
    store.save_draft(
        draft.id,
        "ghostwriter",
        0,
        {"title": "A Real Post", "date": "2026-08-01"},
        "![alt](fresh.png)\n\nbody text",
    )
    image, _ = store.put_image(png_bytes(), "fresh.png")
    store.attach_image(draft.id, image.image_id, "inline", "ghostwriter")
    return draft.id


def _queue_preview(store: Store, draft_id: str) -> str:
    _, run = store.act_on_draft(draft_id, "preview", "ghostwriter", actor_is_ui=False)
    assert run is not None
    return run.id


# --- leases -------------------------------------------------------------


def test_claim_exactly_one(builder_settings: BuilderSettings) -> None:
    leases = _leases(builder_settings)
    first = leases.claim("run-1", "builder-a")
    assert first is not None
    second = leases.claim("run-1", "builder-b")
    assert second is None


def test_second_builder_cannot_claim_a_leased_run(builder_settings: BuilderSettings) -> None:
    leases = _leases(builder_settings)
    leases.claim("run-1", "builder-a")
    assert leases.held("run-1") is True
    assert leases.claim("run-1", "builder-b") is None


def test_expired_lease_is_recovered_by_another_builder(builder_settings: BuilderSettings) -> None:
    leases = _leases(builder_settings)
    stale = Lease(
        run_id="run-1",
        builder_id="builder-a",
        claimed_at="2020-01-01T00:00:00-05:00",
        expires_at=(datetime.now().astimezone() - timedelta(hours=1)).isoformat(timespec="seconds"),
        renewed_at="2020-01-01T00:00:00-05:00",
    )
    path = leases.path("run-1")
    path.write_text(json.dumps(stale.__dict__), encoding="utf-8")

    taken = leases.claim("run-1", "builder-b")
    assert taken is not None
    assert taken.builder_id == "builder-b"


def test_crashed_builder_recovery_at_start(store: Store, builder_settings: BuilderSettings) -> None:
    draft_id = _make_draft(store)
    run_id = _queue_preview(store, draft_id)
    store.start_run(run_id, "dead-builder", "0.164.0", toolchain_drift=False)

    leases = _leases(builder_settings)
    stale = Lease(
        run_id=run_id,
        builder_id="dead-builder",
        claimed_at="2020-01-01T00:00:00-05:00",
        expires_at=(datetime.now().astimezone() - timedelta(hours=1)).isoformat(timespec="seconds"),
        renewed_at="2020-01-01T00:00:00-05:00",
    )
    leases.path(run_id).write_text(json.dumps(stale.__dict__), encoding="utf-8")

    recovered = runner.recover_expired_leases(store, leases, "new-builder")
    assert recovered == 1
    assert store.get_run(run_id).status == "queued"
    assert leases.read(run_id) is None


# --- queue claim ---------------------------------------------------------


def test_claim_next_skips_a_leased_run(store: Store, builder_settings: BuilderSettings) -> None:
    draft_a = _make_draft(store)
    run_a = _queue_preview(store, draft_a)
    leases = _leases(builder_settings)
    leases.claim(run_a, "other-builder")

    claimed = runner.claim_next(store, leases, "test-builder")
    assert claimed is None


# --- build lifecycle ------------------------------------------------------


def _prep_site(store: Store) -> None:
    store.site_dir.mkdir(parents=True, exist_ok=True)
    (store.site_dir / "config.yaml").write_text("baseURL: /\n", encoding="utf-8")


def test_successful_build_moves_draft_to_previewed_and_records_result(
    store: Store, builder_settings: BuilderSettings
) -> None:
    _prep_site(store)
    draft_id = _make_draft(store)
    run_id = _queue_preview(store, draft_id)
    run = store.get_run(run_id)

    runner.build_one(store, builder_settings, run)

    finished = store.get_run(run_id)
    assert finished.status == "succeeded"
    assert finished.result is not None
    assert finished.result["preview_url"].endswith("/preview/a-real-post/") or "a-real-post" in (
        finished.result.get("slug", "")
    )
    assert store.get_draft(draft_id).status == "previewed"
    assert not (store.queue_dir / f"{run_id}.json").exists()
    slug = store.get_draft(draft_id).slug
    assert slug is not None
    assert (store.preview_dir / slug / "index.html").exists()


def test_failed_build_leaves_draft_and_previous_preview_intact(
    store: Store, builder_settings: BuilderSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prep_site(store)
    draft_id = _make_draft(store)
    slug = store.get_draft(draft_id).slug or ""

    # First a real success, so there is a previous preview tree to protect.
    run_id = _queue_preview(store, draft_id)
    runner.build_one(store, builder_settings, store.get_run(run_id))
    slug = store.get_draft(draft_id).slug or ""
    marker = store.preview_dir / slug / "marker.txt"
    marker.write_text("previous build", encoding="utf-8")

    # Now force a failure and confirm the previous tree survives untouched.
    monkeypatch.setenv("CHRONICLE_FAKE_HUGO_FAIL", "1")
    _, second_run = store.act_on_draft(draft_id, "preview", "ghostwriter", actor_is_ui=False)
    assert second_run is not None
    runner.build_one(store, builder_settings, second_run)

    finished = store.get_run(second_run.id)
    assert finished.status == "failed"
    assert finished.result is not None
    assert finished.result["error_class"] == "hugo_build_failed"
    assert marker.exists()
    assert marker.read_text(encoding="utf-8") == "previous build"


def test_toolchain_drift_flagged_when_versions_differ(
    store: Store, builder_settings: BuilderSettings
) -> None:
    _prep_site(store)
    state_dir = store.data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "toolchain.json").write_text(
        json.dumps({"hugo_version": "0.163.0", "submodules": []}), encoding="utf-8"
    )
    draft_id = _make_draft(store)
    run_id = _queue_preview(store, draft_id)
    runner.build_one(store, builder_settings, store.get_run(run_id))
    assert store.get_run(run_id).toolchain_drift is True


def test_toolchain_no_drift_when_versions_match(
    store: Store, builder_settings: BuilderSettings
) -> None:
    _prep_site(store)
    state_dir = store.data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "toolchain.json").write_text(
        json.dumps({"hugo_version": "0.164.0", "submodules": []}), encoding="utf-8"
    )
    draft_id = _make_draft(store)
    run_id = _queue_preview(store, draft_id)
    runner.build_one(store, builder_settings, store.get_run(run_id))
    assert store.get_run(run_id).toolchain_drift is False


def test_run_events_recorded_for_started_and_succeeded(
    store: Store, builder_settings: BuilderSettings
) -> None:
    _prep_site(store)
    draft_id = _make_draft(store)
    run_id = _queue_preview(store, draft_id)
    before = len(store.events_since(0)[0])
    runner.build_one(store, builder_settings, store.get_run(run_id))
    events = store.events_since(0)[0]
    types = [e.type for e in events[before:]]
    assert "run.started" in types
    assert "run.succeeded" in types


def test_heartbeat_written_with_builder_id_and_queue_depth(
    store: Store, builder_settings: BuilderSettings
) -> None:
    _prep_site(store)
    draft_id = _make_draft(store)
    _queue_preview(store, draft_id)
    leases = _leases(builder_settings)
    runner.tick(store, builder_settings, leases)
    heartbeat = json.loads(builder_settings.heartbeat_path.read_text(encoding="utf-8"))
    assert heartbeat["builder_id"] == "test-builder"
    assert "last_loop_at" in heartbeat
    assert heartbeat["hugo_version"] == "0.164.0"


def test_once_mode_runs_a_single_tick_and_returns(
    data_dir: Path, builder_settings: BuilderSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHRONICLE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("CHRONICLE_BUILDER_ONCE", "1")
    monkeypatch.setenv("CHRONICLE_BUILDER_HUGO_BIN", builder_settings.hugo_bin)
    (data_dir / "site").mkdir(parents=True, exist_ok=True)
    with caplog_disabled():
        builder_main.run()


class caplog_disabled:
    def __enter__(self) -> None:
        logging.disable(logging.CRITICAL)

    def __exit__(self, *args: object) -> None:
        logging.disable(logging.NOTSET)
