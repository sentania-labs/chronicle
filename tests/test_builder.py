"""Leases, queue claim, atomic swap, toolchain drift, and run lifecycle."""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from chronicle.api.models import Post, Run
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


def _build(store: Store, settings: BuilderSettings, run: Run) -> None:
    """Claim a lease the way `tick` does, then run one build with it.

    `build_one` now takes the lease it is meant to renew and release rather
    than claiming its own (round C3 review), so every direct call needs one.
    """
    leases = _leases(settings)
    lease = leases.claim(run.id, settings.builder_id)
    assert lease is not None
    runner.build_one(store, settings, run, leases, lease)


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

    _build(store, builder_settings, run)

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
    _build(store, builder_settings, store.get_run(run_id))
    slug = store.get_draft(draft_id).slug or ""
    marker = store.preview_dir / slug / "marker.txt"
    marker.write_text("previous build", encoding="utf-8")

    # Now force a failure and confirm the previous tree survives untouched.
    monkeypatch.setenv("CHRONICLE_FAKE_HUGO_FAIL", "1")
    _, second_run = store.act_on_draft(draft_id, "preview", "ghostwriter", actor_is_ui=False)
    assert second_run is not None
    _build(store, builder_settings, second_run)

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
    _build(store, builder_settings, store.get_run(run_id))
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
    _build(store, builder_settings, store.get_run(run_id))
    assert store.get_run(run_id).toolchain_drift is False


def test_run_events_recorded_for_started_and_succeeded(
    store: Store, builder_settings: BuilderSettings
) -> None:
    _prep_site(store)
    draft_id = _make_draft(store)
    run_id = _queue_preview(store, draft_id)
    before = len(store.events_since(0)[0])
    _build(store, builder_settings, store.get_run(run_id))
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
    assert heartbeat["preview_writable"] is True


# --- healthcheck subcommand (examples/k8s/deployment.yaml's liveness probe) -


def test_healthcheck_fails_with_no_heartbeat_file(builder_settings: BuilderSettings) -> None:
    assert builder_main.healthcheck(builder_settings) == 1


def test_healthcheck_succeeds_with_a_fresh_heartbeat(
    store: Store, builder_settings: BuilderSettings
) -> None:
    _prep_site(store)
    draft_id = _make_draft(store)
    _queue_preview(store, draft_id)
    runner.tick(store, builder_settings, _leases(builder_settings))
    assert builder_main.healthcheck(builder_settings) == 0


def test_healthcheck_fails_with_a_stale_heartbeat(builder_settings: BuilderSettings) -> None:
    builder_settings.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    stale = (datetime.now().astimezone() - timedelta(hours=1)).isoformat()
    builder_settings.heartbeat_path.write_text(
        json.dumps({"last_loop_at": stale}), encoding="utf-8"
    )
    assert builder_main.healthcheck(builder_settings) == 1


# --- fresh-volume writability: C3 compose fix ----------------------------


def test_check_writable_true_for_a_dir_it_can_create(tmp_path: Path) -> None:
    target = tmp_path / "not-yet-created"
    assert runner.check_writable(target) is True
    assert target.is_dir()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permission bits")
def test_check_writable_false_for_a_read_only_dir(tmp_path: Path) -> None:
    read_only = tmp_path / "read-only"
    read_only.mkdir()
    read_only.chmod(0o500)
    try:
        assert runner.check_writable(read_only) is False
    finally:
        read_only.chmod(stat.S_IRWXU)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permission bits")
def test_heartbeat_flags_preview_not_writable_instead_of_crashing(
    store: Store, builder_settings: BuilderSettings
) -> None:
    """The fresh-volume defect: a root-owned /data/preview must not crash-loop.

    A tick with an unwritable preview dir still writes a heartbeat (with
    `preview_writable: false`) and keeps polling rather than raising, so a
    builder started against a fresh, wrongly-owned volume stays up and
    recovers on its own once the mount's ownership is fixed.
    """
    _prep_site(store)
    draft_id = _make_draft(store)
    _queue_preview(store, draft_id)
    leases = _leases(builder_settings)
    store.preview_dir.mkdir(parents=True, exist_ok=True)
    store.preview_dir.chmod(0o500)
    try:
        runner.tick(store, builder_settings, leases)
    finally:
        store.preview_dir.chmod(stat.S_IRWXU)
    heartbeat = json.loads(builder_settings.heartbeat_path.read_text(encoding="utf-8"))
    assert heartbeat["preview_writable"] is False


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


def test_output_path_shares_a_filesystem_with_the_preview_destination(
    store: Store, builder_settings: BuilderSettings
) -> None:
    """Regression: the pre-swap output must be on the same mount as
    data/preview/<slug>/, or the atomic rename in _atomic_swap raises
    "Invalid cross-device link" the moment builder-work and preview are
    separate volumes, which they are in both compose and the k8s reference.
    """
    path = runner.output_path(store, "some-run-id")
    assert path.is_relative_to(store.preview_dir)
    assert not path.is_relative_to(builder_settings.work_dir)


def test_recovers_a_building_run_whose_lease_is_gone_entirely(
    store: Store, builder_settings: BuilderSettings
) -> None:
    """Regression: a builder that crashes with an uncaught exception still
    releases the lease in tick's own finally, leaving a run `building` with
    no lease file at all rather than an expired one. That case has to be
    recovered too, not just an expired lease.
    """
    draft_id = _make_draft(store)
    run_id = _queue_preview(store, draft_id)
    store.start_run(run_id, "dead-builder", "0.164.0", toolchain_drift=False)
    leases = _leases(builder_settings)
    assert leases.read(run_id) is None

    recovered = runner.recover_expired_leases(store, leases, "new-builder")
    assert recovered == 1
    assert store.get_run(run_id).status == "queued"


def test_os_error_during_build_fails_the_run_instead_of_raising(
    store: Store, builder_settings: BuilderSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prep_site(store)
    draft_id = _make_draft(store)
    run_id = _queue_preview(store, draft_id)

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("Invalid cross-device link")

    monkeypatch.setattr(runner, "_atomic_swap", _boom)
    _build(store, builder_settings, store.get_run(run_id))

    finished = store.get_run(run_id)
    assert finished.status == "failed"
    assert finished.result is not None
    assert finished.result["error_class"] == "builder_error"


# --- hard links: fix #1 --------------------------------------------------


def _seed_site_post(store: Store, slug: str) -> tuple[Path, bytes]:
    """A post on `data/site` that a from_post import will keep the path of."""
    post_dir = store.site_dir / "content" / "posts"
    post_dir.mkdir(parents=True, exist_ok=True)
    path = post_dir / f"{slug}.md"
    original = (
        f"---\ntitle: Original\ndate: 2024-03-03\n---\n"
        f"original body ![alt](/images/{slug}/pic.png)\n"
    ).encode()
    path.write_bytes(original)
    post = Post(
        slug=slug,
        path=f"content/posts/{slug}.md",
        title="Original",
        date="2024-03-03",
        sha="abc123",
    )
    store.posts_dir.mkdir(parents=True, exist_ok=True)
    store._write_json(store.posts_dir / f"{slug}.json", post.model_dump(mode="json"))
    store.index.upsert_post(post)
    return path, original


def _seed_site_image(store: Store, slug: str, filename: str) -> tuple[Path, bytes]:
    image_dir = store.site_dir / "static" / "images" / slug
    image_dir.mkdir(parents=True, exist_ok=True)
    path = image_dir / filename
    original = png_bytes(color=(1, 2, 3))
    path.write_bytes(original)
    return path, original


def test_a_preview_never_mutates_the_hard_linked_site_clone(
    store: Store, builder_settings: BuilderSettings
) -> None:
    """Regression for the P1 review finding: `_copy_site` hard-links the
    scratch tree to `data/site` wherever it can, so writing the converted
    post or an image in place (rather than unlinking first) would truncate
    the same inode `data/site` uses, silently modifying the digest's clone.
    """
    _prep_site(store)
    post_path, original_post = _seed_site_post(store, "hello-world")
    image_path, original_image = _seed_site_image(store, "hello-world", "pic.png")

    draft, warnings = store.create_draft("ghostwriter", from_post="hello-world")
    assert draft.images, f"import discovered no images to exercise the mutation risk: {warnings}"

    run_id = _queue_preview(store, draft.id)
    _build(store, builder_settings, store.get_run(run_id))

    assert store.get_run(run_id).status == "succeeded"
    assert post_path.read_bytes() == original_post
    assert image_path.read_bytes() == original_image

    for path in store.site_dir.rglob("*"):
        if path.is_file():
            assert os.stat(path).st_nlink == 1, f"{path} is still hard-linked after a build"


# --- heartbeat during a long build: fix #2 -------------------------------


SLOW_FAKE_HUGO = FAKE_HUGO.replace(
    'destination = value("--destination")',
    'import time as _time\n_time.sleep(0.4)\ndestination = value("--destination")',
)


@pytest.fixture
def slow_fake_hugo(tmp_path: Path) -> Path:
    script = tmp_path / "slow-fake-hugo"
    script.write_text(SLOW_FAKE_HUGO, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_heartbeat_advances_during_a_long_build(
    store: Store, data_dir: Path, slow_fake_hugo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "KEEPALIVE_SECONDS", 0.05)
    settings = BuilderSettings(
        data_dir=data_dir,
        external_url="http://localhost:8080",
        builder_id="test-builder",
        poll_seconds=0.01,
        lease_seconds=60,
        work_dir=data_dir / "builder-work",
        hugo_bin=str(slow_fake_hugo),
        build_timeout_seconds=10,
        once=True,
    )
    _prep_site(store)
    draft_id = _make_draft(store)
    run_id = _queue_preview(store, draft_id)
    leases = _leases(settings)
    lease = leases.claim(run_id, settings.builder_id)
    assert lease is not None

    stamps: list[str] = []
    original_write = runner.write_heartbeat

    def _spy(*args: object, **kwargs: object) -> None:
        original_write(*args, **kwargs)  # type: ignore[arg-type]
        stamps.append(json.loads(settings.heartbeat_path.read_text())["last_loop_at"])

    monkeypatch.setattr(runner, "write_heartbeat", _spy)
    runner.build_one(store, settings, store.get_run(run_id), leases, lease)

    # At least one refresh from the keep-alive thread during the ~0.4s
    # build, on top of whatever `tick` would have written before claiming.
    assert len(stamps) >= 1


# --- stale preview: fix #3 ------------------------------------------------


def test_a_stale_preview_does_not_transition_a_draft_that_moved_on(store: Store) -> None:
    """`finish_run` only trusts `run.built_version`, stamped by `start_run`
    when a builder actually read the draft. A save landing between that read
    and the build finishing must not move the draft to `previewed`, because
    the preview a builder generated reflects a version that no longer exists
    (round C3 review, chronicle/api/store.py's finish_run).
    """
    draft_id = _make_draft(store)
    run_id = _queue_preview(store, draft_id)
    built_version = store.get_draft(draft_id).version_no
    store.start_run(
        run_id, "test-builder", "0.164.0", toolchain_drift=False, built_version=built_version
    )

    # The draft is saved again, mid-build from the builder's point of view.
    store.save_draft(
        draft_id,
        "ghostwriter",
        built_version,
        {"title": "A Real Post", "date": "2026-08-01"},
        "edited after the build started",
    )
    assert store.get_draft(draft_id).version_no != built_version

    before = len(store.events_since(0)[0])
    store.finish_run(run_id, "test-builder", succeeded=True, result={"slug": "a-real-post"})

    finished = store.get_run(run_id)
    assert finished.status == "succeeded"
    assert finished.result is not None
    assert finished.result["stale"] is True
    # The draft never moved to `previewed`: the build that succeeded was of
    # a version the draft has since moved past.
    assert store.get_draft(draft_id).status == "drafting"
    events = [e.type for e in store.events_since(0)[0]][before:]
    assert "draft.preview_stale" in events


def test_build_one_stamps_built_version_so_finish_run_can_detect_staleness(
    store: Store, builder_settings: BuilderSettings
) -> None:
    """End-to-end through `build_one`: without a mid-build save, the version
    it built is still current, so the draft transitions normally.
    """
    _prep_site(store)
    draft_id = _make_draft(store)
    run_id = _queue_preview(store, draft_id)
    _build(store, builder_settings, store.get_run(run_id))
    assert store.get_run(run_id).built_version == 1
    assert store.get_draft(draft_id).version_no == 1


def test_a_current_preview_still_transitions_the_draft(
    store: Store, builder_settings: BuilderSettings
) -> None:
    _prep_site(store)
    draft_id = _make_draft(store)
    run_id = _queue_preview(store, draft_id)
    _build(store, builder_settings, store.get_run(run_id))

    finished = store.get_run(run_id)
    assert finished.result is not None
    assert "stale" not in finished.result
    assert store.get_draft(draft_id).status == "previewed"


# --- lease retained and renewed through the build: fix #4 ----------------


def test_a_lease_taken_over_mid_build_is_not_clobbered_by_the_first_builders_release(
    store: Store, builder_settings: BuilderSettings
) -> None:
    """Regression for the P1 review finding: `claim_next` used to discard the
    `Lease` token, so `tick` never renewed it, and its unconditional
    `release(run.id)` at the end would delete whatever a second builder
    claimed after the first one's lease expired mid-build.
    """
    _prep_site(store)
    draft_id = _make_draft(store)
    run_id = _queue_preview(store, draft_id)
    leases = _leases(builder_settings)

    claimed = runner.claim_next(store, leases, "builder-a")
    assert claimed is not None
    run, lease = claimed

    # Simulate builder-a's lease having expired and a second builder taking
    # it over while builder-a is still (notionally) mid-build.
    leases.path(run_id).unlink()
    stale = Lease(
        run_id=run_id,
        builder_id="builder-a",
        claimed_at="2020-01-01T00:00:00-05:00",
        expires_at=(datetime.now().astimezone() - timedelta(hours=1)).isoformat(timespec="seconds"),
        renewed_at="2020-01-01T00:00:00-05:00",
    )
    leases.path(run_id).write_text(json.dumps(stale.__dict__), encoding="utf-8")
    taken_over = leases.claim(run_id, "builder-b")
    assert taken_over is not None
    assert taken_over.builder_id == "builder-b"

    # builder-a finishes and releases with the ownership check tick uses.
    leases.release(run_id, "builder-a")

    # builder-b's claim must still be standing.
    still_there = leases.read(run_id)
    assert still_there is not None
    assert still_there.builder_id == "builder-b"


def test_build_one_discards_its_result_when_the_lease_was_lost_mid_build(
    store: Store, builder_settings: BuilderSettings
) -> None:
    _prep_site(store)
    draft_id = _make_draft(store)
    run_id = _queue_preview(store, draft_id)
    leases = _leases(builder_settings)
    claimed = runner.claim_next(store, leases, "builder-a")
    assert claimed is not None
    run, lease = claimed

    # Someone else takes the run over before the (fake, near-instant) build
    # finishes: patch BuildKeepAlive to hand the run to builder-b as its
    # first and only act, standing in for a lease that expired mid-build.
    class _StealingKeepAlive(runner.BuildKeepAlive):
        def start(self) -> None:
            leases.path(run_id).unlink()
            leases.claim(run_id, "builder-b")

        def stop(self) -> Lease:
            return self._lease

    monkeypatch_target = runner.BuildKeepAlive
    try:
        runner.BuildKeepAlive = _StealingKeepAlive  # type: ignore[misc]
        runner.build_one(store, builder_settings, run, leases, lease)
    finally:
        runner.BuildKeepAlive = monkeypatch_target  # type: ignore[misc]

    # builder-a never swapped output or finished the run: it is still
    # `building`, exactly as builder-b (the real owner) left it, and the
    # slug was never published.
    finished = store.get_run(run_id)
    assert finished.status == "building"
    events = [e.type for e in store.events_since(0)[0]]
    assert "run.lease_lost" in events
    slug = store.get_draft(draft_id).slug
    assert slug is not None
    assert not (store.preview_dir / slug).exists()


# --- atomic swap by symlink indirection: fix #5 ---------------------------


def test_swap_has_no_404_window_while_a_new_build_replaces_the_old_one(
    store: Store, builder_settings: BuilderSettings
) -> None:
    _prep_site(store)
    draft_id = _make_draft(store)

    run_id = _queue_preview(store, draft_id)
    _build(store, builder_settings, store.get_run(run_id))
    slug = store.get_draft(draft_id).slug
    assert slug is not None
    destination = store.preview_dir / slug

    stop = threading.Event()
    missing_seen = threading.Event()

    def _watch() -> None:
        while not stop.is_set():
            if not destination.exists():
                missing_seen.set()
                return

    watcher = threading.Thread(target=_watch, daemon=True)
    watcher.start()
    try:
        for _ in range(20):
            _, second_run = store.act_on_draft(
                draft_id, "preview", "ghostwriter", actor_is_ui=False
            )
            assert second_run is not None
            _build(store, builder_settings, second_run)
    finally:
        stop.set()
        watcher.join(timeout=5)

    assert not missing_seen.is_set()
    assert destination.exists()


def test_failed_build_after_a_symlink_swap_leaves_the_old_preview_live(
    store: Store, builder_settings: BuilderSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prep_site(store)
    draft_id = _make_draft(store)
    run_id = _queue_preview(store, draft_id)
    _build(store, builder_settings, store.get_run(run_id))
    slug = store.get_draft(draft_id).slug
    assert slug is not None
    destination = store.preview_dir / slug
    assert destination.is_symlink()
    live_target = destination.resolve()

    monkeypatch.setenv("CHRONICLE_FAKE_HUGO_FAIL", "1")
    _, second_run = store.act_on_draft(draft_id, "preview", "ghostwriter", actor_is_ui=False)
    assert second_run is not None
    _build(store, builder_settings, second_run)

    assert store.get_run(second_run.id).status == "failed"
    assert destination.is_symlink()
    assert destination.resolve() == live_target
    assert (destination / "index.html").exists()
