"""The store: real files, real commits, and an index that is only a cache."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from chronicle.api import gitrepo
from chronicle.api.errors import ApiError
from chronicle.api.index import SCHEMA_VERSION, Index, index_path
from chronicle.api.models import Material, Post, is_valid_slug
from chronicle.api.store import Store
from chronicle.cli import main as cli_main

from .conftest import png_bytes

FRONTMATTER = {"title": "A Post About Drift", "tags": ["lab"]}


def seed(store: Store) -> dict[str, str]:
    submission = store.create_submission(
        "ghostwriter", "a brief", [Material(name="notes", text="raw")], []
    )
    store.act_on_submission(submission.id, "claim", "ghostwriter")
    draft, _ = store.create_draft("ghostwriter", from_submission=submission.id)
    store.save_draft(draft.id, "ghostwriter", 0, FRONTMATTER, "first body")
    store.save_draft(draft.id, "scott", 1, FRONTMATTER, "second body", message="tightened")
    image, _ = store.put_image(png_bytes(), "feature.png")
    store.attach_image(draft.id, image.image_id, "feature", "ghostwriter")
    _, run = store.act_on_draft(draft.id, "preview", "ghostwriter", actor_is_ui=False)
    assert run is not None
    return {"submission": submission.id, "draft": draft.id, "image": image.image_id, "run": run.id}


def snapshot(store: Store) -> dict[str, object]:
    return {
        "submissions": [item.model_dump(mode="json") for item in store.list_submissions()],
        "claimed": [item.id for item in store.list_submissions("drafted")],
        "drafts": [item.model_dump(mode="json") for item in store.list_drafts()],
        "drafting": [item.id for item in store.list_drafts("drafting")],
        "posts": [item.model_dump(mode="json") for item in store.list_posts()],
        "events": [item.model_dump(mode="json") for item in store.events_since(0)[0]],
    }


def test_git_author_is_the_acting_token_name(store: Store) -> None:
    store.create_submission("ghostwriter", "from an agent", [], [])
    assert gitrepo.log_authors(store.repo_dir, limit=1) == ["ghostwriter"]


def test_every_write_is_one_commit(store: Store) -> None:
    before = len(gitrepo.log_authors(store.repo_dir, limit=100))
    store.create_submission("ghostwriter", "one", [], [])
    store.create_submission("ghostwriter", "two", [], [])
    after = len(gitrepo.log_authors(store.repo_dir, limit=100))
    assert after - before == 2


def test_feedback_lands_in_the_same_commit_as_the_status_change(store: Store) -> None:
    draft, _ = store.create_draft("ghostwriter")
    store.save_draft(draft.id, "ghostwriter", 0, FRONTMATTER, "body")
    store.act_on_draft(draft.id, "submit", "ghostwriter", actor_is_ui=False)
    store.act_on_draft(
        draft.id, "request_revision", "ui", actor_is_ui=True, feedback="tighten the opening"
    )

    feedback_file = store.feedback_dir / f"{draft.id}.md"
    assert "tighten the opening" in feedback_file.read_text(encoding="utf-8")
    changed = set(gitrepo.files_in_head(store.repo_dir))
    assert f"feedback/{draft.id}.md" in changed
    assert f"drafts/{draft.id}/draft.json" in changed


def test_index_is_not_tracked_by_git(store: Store) -> None:
    store.create_submission("ghostwriter", "one", [], [])
    tracked = gitrepo.tracked_files(store.repo_dir)
    assert not any(path.startswith("index/") for path in tracked)
    assert index_path(store.repo_dir).exists()


def test_run_queue_entry_is_written_for_a_preview(store: Store) -> None:
    ids = seed(store)
    entry = json.loads((store.queue_dir / f"{ids['run']}.json").read_text(encoding="utf-8"))
    assert entry["run_id"] == ids["run"]
    assert entry["draft_id"] == ids["draft"]
    assert entry["kind"] == "preview"
    assert entry["enqueued_at"]


def test_revision_answer_seqs_orders_a_request_against_what_answers_it(store: Store) -> None:
    """`Index.revision_answer_seqs`: the seq a request last landed, and the
    seq the draft was last moved out of the author's hands, per draft id.
    Both are None for a draft with neither kind of event yet."""
    draft, _ = store.create_draft("ghostwriter")
    store.save_draft(draft.id, "ghostwriter", 0, FRONTMATTER, "body")
    store.act_on_draft(draft.id, "submit", "ghostwriter", actor_is_ui=False)
    seqs = store.index.revision_answer_seqs([draft.id])
    request_seq, answered_seq = seqs[draft.id]
    assert request_seq is None
    assert answered_seq is not None

    store.act_on_draft(
        draft.id, "request_revision", "editor", actor_is_ui=True, feedback="tighten it"
    )
    seqs = store.index.revision_answer_seqs([draft.id])
    request_seq, answered_seq_after_request = seqs[draft.id]
    assert request_seq is not None
    assert answered_seq_after_request is not None
    assert request_seq > answered_seq_after_request

    # A resubmit is a newer answer than the request, in the same table.
    current_version = store.get_draft(draft.id).version_no
    store.save_draft(draft.id, "ghostwriter", current_version, FRONTMATTER, "revised body")
    store.act_on_draft(draft.id, "submit", "ghostwriter", actor_is_ui=False)
    seqs = store.index.revision_answer_seqs([draft.id])
    request_seq_final, answered_seq_final = seqs[draft.id]
    assert request_seq_final == request_seq
    assert answered_seq_final is not None and answered_seq_final > request_seq_final

    # A draft that was never sent back or submitted has neither seq, even
    # though it does have events (creation) of its own.
    other, _ = store.create_draft("ghostwriter")
    assert store.index.revision_answer_seqs([other.id]) == {other.id: (None, None)}

    # A draft id with no events at all is absent from the result.
    assert store.index.revision_answer_seqs(["no-such-draft"]) == {}


def test_reindex_rebuilds_every_row_from_the_files(store: Store, data_dir: Path) -> None:
    ids = seed(store)
    store.start_run(ids["run"], "test-builder", "0.164.0", toolchain_drift=False)
    store.finish_run(
        ids["run"], "test-builder", succeeded=True, result={"preview_url": "/preview/drift/"}
    )
    before = snapshot(store)
    store.close()

    index_path(store.repo_dir).unlink()
    assert cli_main(["--data-dir", str(data_dir), "reindex"]) == 0

    rebuilt = Store.open(data_dir)
    assert snapshot(rebuilt) == before
    assert rebuilt.index.image_id_for_sha(seed_sha(rebuilt)) is not None
    rebuilt.close()


def seed_sha(store: Store) -> str:
    # A succeeded preview leaves the seeded draft at `drafting` (issue #70).
    draft = store.list_drafts("drafting")[0]
    return draft.images[0].image_id


def test_concurrent_saves_at_one_base_version_leave_a_single_winner(store: Store) -> None:
    draft, _ = store.create_draft("ghostwriter")
    writers = 8
    start = threading.Barrier(writers)
    outcomes: list[str] = []

    def save(number: int) -> None:
        start.wait()
        try:
            store.save_draft(draft.id, f"writer{number}", 0, FRONTMATTER, f"body {number}")
            outcomes.append("saved")
        except ApiError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=save, args=(number,)) for number in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("saved") == 1
    assert set(outcomes) == {"saved", "stale_base_version"}
    saved = store.get_draft(draft.id)
    assert saved.version_no == 1
    assert store.get_version(draft.id, 1).body == saved.body
    assert len({event.seq for event in store.events_since(0)[0]}) == len(store.events_since(0)[0])


def test_an_existing_schema_version_is_never_restamped(tmp_path: Path) -> None:
    path = tmp_path / "chronicle.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        f"INSERT INTO schema_meta VALUES ('schema_version', '{SCHEMA_VERSION + 41}');"
    )
    conn.commit()
    conn.close()

    index = Index(path)
    assert index.schema_version() == SCHEMA_VERSION + 41
    index.close()


def test_event_sequence_survives_a_dropped_index(store: Store, data_dir: Path) -> None:
    store.create_submission("ghostwriter", "one", [], [])
    first_seq = store.events_since(0)[0][-1].seq
    store.close()

    index_path(store.repo_dir).unlink()
    reopened = Store.open(data_dir)
    reopened.create_submission("ghostwriter", "two", [], [])
    second_seq = reopened.events_since(0)[0][-1].seq
    reopened.close()

    assert second_seq == first_seq + 1
    assert cli_main(["--data-dir", str(data_dir), "reindex"]) == 0
    rebuilt = Store.open(data_dir)
    seqs = [event.seq for event in rebuilt.events_since(0)[0]]
    assert seqs == sorted(set(seqs))
    rebuilt.close()


def test_list_submissions_is_a_snapshot_under_the_store_lock(store: Store) -> None:
    submission = store.create_submission("ghostwriter", "one", [], [])

    entered = threading.Event()
    release = threading.Event()
    original_ids = store.index.submission_ids

    def blocking_ids(status: str | None = None) -> list[str]:
        entered.set()
        release.wait(timeout=5)
        return original_ids(status)

    store.index.submission_ids = blocking_ids  # type: ignore[method-assign]

    results: list[list[str]] = []
    lister = threading.Thread(
        target=lambda: results.append([item.status for item in store.list_submissions()])
    )
    lister.start()
    assert entered.wait(timeout=5)

    claimed = threading.Event()

    def do_claim() -> None:
        store.act_on_submission(submission.id, "claim", "ghostwriter")
        claimed.set()

    claimer = threading.Thread(target=do_claim)
    claimer.start()
    # The store lock held by the in-progress list must block a concurrent
    # mutation; without the fix, act_on_submission would not need the lock
    # the list holds and this claim would finish immediately.
    claimer_finished_early = claimed.wait(timeout=0.2)

    release.set()
    lister.join()
    claimer.join(timeout=5)

    assert not claimer_finished_early
    assert results[0] == ["new"]
    assert store.get_submission(submission.id).status == "claimed"


def test_slug_is_pinned_at_first_preview_and_stays(store: Store) -> None:
    draft, _ = store.create_draft("ghostwriter")
    store.save_draft(draft.id, "ghostwriter", 0, FRONTMATTER, "body")
    previewed, _ = store.act_on_draft(draft.id, "preview", "ghostwriter", actor_is_ui=False)
    assert previewed.slug == "a-post-about-drift"

    store.save_draft(draft.id, "ghostwriter", 1, FRONTMATTER, "more body")
    assert store.get_draft(draft.id).slug == "a-post-about-drift"


def test_slug_pin_sets_image_dir_from_slug_when_no_url(store: Store) -> None:
    draft, _ = store.create_draft("ghostwriter")
    store.save_draft(draft.id, "ghostwriter", 0, FRONTMATTER, "body")
    previewed, _ = store.act_on_draft(draft.id, "preview", "ghostwriter", actor_is_ui=False)
    assert previewed.image_dir == "a-post-about-drift"


def test_slug_pin_stamps_a_missing_date(store: Store) -> None:
    """ADR 022: the date the filename and url are derived from is pinned at
    the same moment as the slug, not only at first publish."""
    from datetime import datetime

    draft, _ = store.create_draft("ghostwriter")
    store.save_draft(draft.id, "ghostwriter", 0, FRONTMATTER, "body")
    previewed, _ = store.act_on_draft(draft.id, "preview", "ghostwriter", actor_is_ui=False)
    stamped = previewed.frontmatter.get("date")
    assert isinstance(stamped, str) and stamped
    parsed = datetime.fromisoformat(stamped)
    assert parsed.tzinfo is not None


def test_slug_pin_never_overwrites_a_hand_set_date(store: Store) -> None:
    draft, _ = store.create_draft("ghostwriter")
    store.save_draft(
        draft.id, "ghostwriter", 0, {**FRONTMATTER, "date": "2020-01-01T00:00:00-06:00"}, "body"
    )
    previewed, _ = store.act_on_draft(draft.id, "preview", "ghostwriter", actor_is_ui=False)
    assert previewed.frontmatter["date"] == "2020-01-01T00:00:00-06:00"


def test_slug_pin_stamps_the_date_once_and_a_later_save_never_moves_it(store: Store) -> None:
    draft, _ = store.create_draft("ghostwriter")
    store.save_draft(draft.id, "ghostwriter", 0, FRONTMATTER, "body")
    previewed, _ = store.act_on_draft(draft.id, "preview", "ghostwriter", actor_is_ui=False)
    stamped = previewed.frontmatter["date"]
    store.save_draft(draft.id, "ghostwriter", 1, {**FRONTMATTER, "date": stamped}, "more body")
    assert store.get_draft(draft.id).frontmatter["date"] == stamped


def test_a_pinned_drafts_save_without_date_keeps_the_stamped_value(store: Store) -> None:
    """ADR 022: once the slug is pinned, a later save that omits date (the
    API) or clears it (the editor's Date field) must not lose the stamp
    `_pin_slug` wrote; a later publish still derives the filename and url
    from it."""
    draft, _ = store.create_draft("ghostwriter")
    store.save_draft(draft.id, "ghostwriter", 0, FRONTMATTER, "body")
    previewed, _ = store.act_on_draft(draft.id, "preview", "ghostwriter", actor_is_ui=False)
    stamped = previewed.frontmatter["date"]

    no_date_frontmatter = {k: v for k, v in FRONTMATTER.items() if k != "date"}
    saved = store.save_draft(draft.id, "ghostwriter", 1, no_date_frontmatter, "more body")
    assert saved.frontmatter["date"] == stamped


def test_a_pinned_drafts_save_with_a_different_hand_set_date_wins(store: Store) -> None:
    draft, _ = store.create_draft("ghostwriter")
    store.save_draft(draft.id, "ghostwriter", 0, FRONTMATTER, "body")
    store.act_on_draft(draft.id, "preview", "ghostwriter", actor_is_ui=False)

    saved = store.save_draft(
        draft.id, "ghostwriter", 1, {**FRONTMATTER, "date": "2020-01-01T00:00:00-06:00"}, "more"
    )
    assert saved.frontmatter["date"] == "2020-01-01T00:00:00-06:00"


def test_an_unpinned_drafts_save_without_date_stays_without_one(store: Store) -> None:
    draft, _ = store.create_draft("ghostwriter")
    no_date_frontmatter = {k: v for k, v in FRONTMATTER.items() if k != "date"}
    saved = store.save_draft(draft.id, "ghostwriter", 0, no_date_frontmatter, "body")
    assert "date" not in saved.frontmatter


@pytest.mark.parametrize("url", ["/a/..", "/a/.", "/a/b\\c"])
def test_save_refuses_a_url_whose_last_segment_is_not_a_directory_name(
    store: Store, url: str
) -> None:
    """Issue 28: refused at save with a named code, before anything is pinned."""
    draft, _ = store.create_draft("ghostwriter")
    with pytest.raises(ApiError) as excinfo:
        store.save_draft(draft.id, "ghostwriter", 0, {"title": "Probe", "url": url}, "body\n")
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "frontmatter_url_invalid"
    assert excinfo.value.extra["url"] == url
    assert store.get_draft(draft.id).version_no == 0


def test_slug_pin_falls_back_when_an_older_save_left_an_unusable_url(store: Store) -> None:
    """A draft saved with `url: /a/..` before the save refused it still pins a
    safe directory the first time it is previewed."""
    draft, _ = store.create_draft("ghostwriter")
    record = store.get_draft(draft.id)
    record.frontmatter = {"title": "Probe", "url": "/a/.."}
    record.title = "Probe"
    store._write_json(store._draft_path(draft.id), record.model_dump(mode="json"))
    previewed, _ = store.act_on_draft(draft.id, "preview", "ghostwriter", False)
    assert previewed.image_dir == "probe"


def test_slug_pin_refuses_when_image_dir_exists_on_main(store: Store) -> None:
    (store.site_dir / "static" / "images" / "a-post-about-drift").mkdir(parents=True)
    draft, _ = store.create_draft("ghostwriter")
    store.save_draft(draft.id, "ghostwriter", 0, FRONTMATTER, "body")
    with pytest.raises(ApiError) as excinfo:
        store.act_on_draft(draft.id, "preview", "ghostwriter", actor_is_ui=False)
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "image_dir_collision"


def test_slug_pin_refuses_when_another_draft_already_pins_the_image_dir(store: Store) -> None:
    first, _ = store.create_draft("ghostwriter")
    store.save_draft(first.id, "ghostwriter", 0, FRONTMATTER, "body")
    store.act_on_draft(first.id, "preview", "ghostwriter", actor_is_ui=False)

    second, _ = store.create_draft("ghostwriter")
    store.save_draft(
        second.id,
        "ghostwriter",
        0,
        {**FRONTMATTER, "slug": "a-second-post", "url": "/2026/09/a-post-about-drift/"},
        "body",
    )
    with pytest.raises(ApiError) as excinfo:
        store.act_on_draft(second.id, "preview", "ghostwriter", actor_is_ui=False)
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "image_dir_collision"


@pytest.mark.parametrize("bad_slug", ["../x", "a/b", ""])
def test_is_valid_slug_rejects_path_components(bad_slug: str) -> None:
    assert not is_valid_slug(bad_slug)


def test_is_valid_slug_accepts_a_plain_filename_component() -> None:
    assert is_valid_slug("a-post-about-drift")
    assert is_valid_slug("post_2024")


def test_get_post_refuses_a_traversing_slug_instead_of_reading_outside_posts_dir(
    store: Store,
) -> None:
    for bad_slug in ("../x", "a/b", ""):
        try:
            store.get_post(bad_slug)
        except ApiError as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError(f"expected get_post({bad_slug!r}) to fail")


def test_apply_digest_refuses_to_write_a_post_record_with_a_bad_slug(store: Store) -> None:
    posts = [
        Post(slug="../x", path="content/posts/x.md", title="Escape", date="2024-01-01", sha="a"),
        Post(slug="a/b", path="content/posts/b.md", title="Nested", date="2024-01-01", sha="b"),
        Post(slug="", path="content/posts/c.md", title="Empty", date="2024-01-01", sha="c"),
        Post(slug="fine", path="content/posts/fine.md", title="Fine", date="2024-01-01", sha="d"),
    ]
    counts = store.apply_digest("chronicle", posts)
    assert counts["created"] == 1
    assert store.list_posts() == [store.get_post("fine")]
    assert not (store.repo_dir / "x.json").exists()
    assert not (store.posts_dir / "a").exists()


# --- resolve_run_post_url (Codex round on issue #61: derive from the built
# version, not a later save) ------------------------------------------------


def test_resolve_run_post_url_uses_the_built_versions_frontmatter(store: Store) -> None:
    """A save after the run built changes `url`; the live preview that run
    already rendered must not move underneath it."""
    draft, _ = store.create_draft("scott")
    store.save_draft(draft.id, "scott", 0, {"title": "T", "url": "/2026/08/original/"}, "body")
    store.act_on_draft(draft.id, "preview", "scott", True)
    built = store.get_draft(draft.id)
    run = store.last_run(draft.id, kind="preview")
    assert run is not None
    store.start_run(run.id, "builder-1", "0.164.0", False, built_version=built.version_no)
    store.finish_run(run.id, "builder-1", True, {"preview_url": "https://x/preview/t/"})

    store.save_draft(
        draft.id, "scott", built.version_no, {"title": "T", "url": "/2026/09/changed/"}, "body"
    )

    stored_run = store.last_run(draft.id, kind="preview")
    url = store.resolve_run_post_url(stored_run, store.get_draft(draft.id))
    assert url == "https://x/preview/t/2026/08/original/"


def test_resolve_run_post_url_falls_back_to_the_draft_when_built_version_is_none(
    store: Store,
) -> None:
    draft, _ = store.create_draft("scott")
    store.save_draft(draft.id, "scott", 0, {"title": "T", "url": "/current/"}, "body")
    store.act_on_draft(draft.id, "preview", "scott", True)
    run = store.last_run(draft.id, kind="preview")
    assert run is not None
    store.finish_run(run.id, "scott", True, {"preview_url": "https://x/preview/t/"})

    stored_run = store.last_run(draft.id, kind="preview")
    assert stored_run is not None and stored_run.built_version is None
    url = store.resolve_run_post_url(stored_run, store.get_draft(draft.id))
    assert url == "https://x/preview/t/current/"


def test_resolve_run_post_url_falls_back_to_the_draft_when_the_built_version_is_missing(
    store: Store,
) -> None:
    """A run's `built_version` can name a version older than version tracking
    itself, or one otherwise no longer on disk; this must not raise, only
    fall back the same way a run with no `built_version` at all does."""
    draft, _ = store.create_draft("scott")
    store.save_draft(draft.id, "scott", 0, {"title": "T", "url": "/current/"}, "body")
    store.act_on_draft(draft.id, "preview", "scott", True)
    run = store.last_run(draft.id, kind="preview")
    assert run is not None
    store.start_run(run.id, "builder-1", "0.164.0", False, built_version=99)
    store.finish_run(run.id, "builder-1", True, {"preview_url": "https://x/preview/t/"})

    stored_run = store.last_run(draft.id, kind="preview")
    url = store.resolve_run_post_url(stored_run, store.get_draft(draft.id))
    assert url == "https://x/preview/t/current/"


def test_resolve_run_post_url_skips_the_version_read_when_the_run_already_has_one(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A board full of current runs (each already carrying a stored
    `post_url`) must not take one version read per card."""
    draft, _ = store.create_draft("scott")
    store.save_draft(draft.id, "scott", 0, {"title": "T"}, "body")
    store.act_on_draft(draft.id, "preview", "scott", True)
    run = store.last_run(draft.id, kind="preview")
    assert run is not None
    store.start_run(run.id, "builder-1", "0.164.0", False, built_version=1)
    store.finish_run(
        run.id,
        "builder-1",
        True,
        {"preview_url": "https://x/preview/t/", "post_url": "https://x/preview/t/somewhere/"},
    )
    stored_run = store.last_run(draft.id, kind="preview")

    calls: list[tuple[str, int]] = []
    original_get_version = Store.get_version

    def spy(self: Store, draft_id: str, version_no: int):
        calls.append((draft_id, version_no))
        return original_get_version(self, draft_id, version_no)

    monkeypatch.setattr(Store, "get_version", spy)
    url = store.resolve_run_post_url(stored_run, store.get_draft(draft.id))
    assert url == "https://x/preview/t/somewhere/"
    assert calls == []


# --- issue #70: `previewed` is migrated away --------------------------------


def _force_previewed(store: Store, draft_id: str) -> None:
    """Put a draft at `previewed` the way a record written before issue #70
    carries it: on disk and in the index, with no event of its own."""
    draft = store.get_draft(draft_id)
    draft.status = "previewed"
    path = store.drafts_dir / draft_id / "draft.json"
    path.write_text(json.dumps(draft.model_dump(mode="json")), encoding="utf-8")
    store.index.upsert_draft(draft)


def _legacy_preview_succeeded(store: Store, draft_id: str, from_status: str) -> None:
    store._append_event(
        type="draft.preview_succeeded",
        actor="builder-1",
        draft_id=draft_id,
        from_status=from_status,
        to_status="previewed",
    )


def test_migrate_previewed_restores_the_status_before_the_build(store: Store) -> None:
    from_drafting, _ = store.create_draft("ghostwriter")
    store.save_draft(from_drafting.id, "ghostwriter", 0, FRONTMATTER, "body")
    _legacy_preview_succeeded(store, from_drafting.id, "drafting")
    _force_previewed(store, from_drafting.id)

    from_review, _ = store.create_draft("ghostwriter")
    store.save_draft(from_review.id, "ghostwriter", 0, FRONTMATTER, "body")
    _legacy_preview_succeeded(store, from_review.id, "in_review")
    _force_previewed(store, from_review.id)

    no_event, _ = store.create_draft("ghostwriter")
    store.save_draft(no_event.id, "ghostwriter", 0, FRONTMATTER, "body")
    _force_previewed(store, no_event.id)

    moved = store.migrate_previewed()

    assert sorted(moved) == sorted([from_drafting.id, from_review.id, no_event.id])
    assert store.get_draft(from_drafting.id).status == "drafting"
    assert store.get_draft(from_review.id).status == "in_review"
    assert store.get_draft(no_event.id).status == "in_review"
    assert store.list_drafts("previewed") == []
    migrated = {
        event.draft_id: (event.from_status, event.to_status)
        for event in store.events_since(0)[0]
        if event.type == "draft.status_migrated"
    }
    assert migrated == {
        from_drafting.id: ("previewed", "drafting"),
        from_review.id: ("previewed", "in_review"),
        no_event.id: ("previewed", "in_review"),
    }

    assert store.migrate_previewed() == []


def test_reindex_migrates_a_previewed_draft_the_index_did_not_know(store: Store) -> None:
    # The index is a cache (ADR 006): a legacy file the index has never
    # seen as `previewed` (a deleted or dropped index) is found by the
    # rebuild, which must migrate it too rather than leave it with no
    # transition out until the next restart.
    draft, _ = store.create_draft("ghostwriter")
    store.save_draft(draft.id, "ghostwriter", 0, FRONTMATTER, "body")
    _legacy_preview_succeeded(store, draft.id, "drafting")
    record = store.get_draft(draft.id)
    record.status = "previewed"
    path = store.drafts_dir / draft.id / "draft.json"
    path.write_text(json.dumps(record.model_dump(mode="json")), encoding="utf-8")
    assert store.migrate_previewed() == []  # the index still says drafting

    store.reindex()

    assert store.get_draft(draft.id).status == "drafting"
    assert store.list_drafts("previewed") == []


def test_a_blank_event_line_does_not_stop_the_migration(store: Store, data_dir: Path) -> None:
    draft, _ = store.create_draft("ghostwriter")
    store.save_draft(draft.id, "ghostwriter", 0, FRONTMATTER, "body")
    _legacy_preview_succeeded(store, draft.id, "drafting")
    _force_previewed(store, draft.id)
    with store.events_file.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    store.close()

    reopened = Store.open(data_dir)
    try:
        assert reopened.get_draft(draft.id).status == "drafting"
    finally:
        reopened.close()


def test_opening_the_store_migrates_a_previewed_draft(store: Store, data_dir: Path) -> None:
    draft, _ = store.create_draft("ghostwriter")
    store.save_draft(draft.id, "ghostwriter", 0, FRONTMATTER, "body")
    _legacy_preview_succeeded(store, draft.id, "drafting")
    _force_previewed(store, draft.id)
    store.close()

    reopened = Store.open(data_dir)
    try:
        assert reopened.get_draft(draft.id).status == "drafting"
        assert reopened.migrate_previewed() == []
    finally:
        reopened.close()
