"""The published post's link filled into its announcements (issue #71)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from chronicle.api import announce, convert, digest, publisher, watcher
from chronicle.api.store import Store
from tests.fakes import FakeRepoOps

LINK = "https://blog.example.com/2026/09/a-post/"


# --- The pure fill -------------------------------------------------------


def test_the_placeholder_becomes_the_link() -> None:
    filled = announce.fill_links({"x": "New post: {link} #lab"}, LINK)
    assert filled == {"x": f"New post: {LINK} #lab"}


def test_no_placeholder_appends_the_link_on_its_own_line() -> None:
    filled = announce.fill_links({"bluesky": "New post about the lab.\n"}, LINK)
    assert filled == {"bluesky": f"New post about the lab.\n{LINK}"}


def test_an_empty_announcement_stays_empty() -> None:
    assert announce.fill_links({"x": "", "linkedin": "  "}, LINK) == {"x": "", "linkedin": "  "}


def test_filling_twice_changes_nothing_the_second_time() -> None:
    once = announce.fill_links({"x": "a {link}", "bluesky": "b"}, LINK)
    assert announce.fill_links(once, LINK, LINK) == once


def test_a_changed_url_replaces_the_old_link_rather_than_adding_one() -> None:
    old = "https://blog.example.com/2026/09/old-slug/"
    before = {"x": f"New post: {old}", "bluesky": f"b\n{old}"}
    filled = announce.fill_links(before, LINK, old)
    assert filled == {"x": f"New post: {LINK}", "bluesky": f"b\n{LINK}"}


def test_public_link_joins_without_doubling_or_losing_a_slash() -> None:
    assert announce.public_link("https://b.example.com/", "/2026/09/x/") == (
        "https://b.example.com/2026/09/x/"
    )
    assert announce.public_link("https://b.example.com/blog/", "2026/09/x/") == (
        "https://b.example.com/blog/2026/09/x/"
    )


# --- baseURL from the site's own config ---------------------------------


def _hugo_config(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    class FakeResult:
        stdout = json.dumps({"contentdir": "content", "staticdir": ["static"], **payload})

    monkeypatch.setattr(digest.subprocess, "run", lambda *a, **k: FakeResult())


def test_hugo_config_baseurl_is_read_and_normalised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hugo_config(monkeypatch, {"baseurl": "https://blog.example.com"})
    conventions = digest.read_hugo_conventions(tmp_path)
    assert conventions.baseurl == "https://blog.example.com/"
    assert conventions.as_dict()["baseurl"] == "https://blog.example.com/"


@pytest.mark.parametrize("value", ["/", "", "blog.example.com", "javascript:alert(1)", None])
def test_a_baseurl_that_is_not_an_absolute_http_url_is_not_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: Any
) -> None:
    _hugo_config(monkeypatch, {"baseurl": value})
    assert digest.read_hugo_conventions(tmp_path).baseurl is None


def _write_state(data_dir: Path, conventions: dict[str, Any]) -> None:
    path = data_dir.joinpath(*digest.TOOLCHAIN_STATE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"conventions": conventions}), encoding="utf-8")


def test_the_state_reader_returns_the_base_only_from_a_real_read(tmp_path: Path) -> None:
    assert digest.read_base_url_from_state(tmp_path) is None
    _write_state(tmp_path, {"source": "fallback", "baseurl": "https://b.example.com/"})
    assert digest.read_base_url_from_state(tmp_path) is None
    _write_state(tmp_path, {"source": "hugo_config", "baseurl": "https://b.example.com/"})
    assert digest.read_base_url_from_state(tmp_path) == "https://b.example.com/"


# --- The store write -----------------------------------------------------

FRONTMATTER = {"title": "A Post", "date": "2026-09-01T09:00:00-05:00"}


def _published_with_announcements(store: Store, announcements: dict[str, str]) -> str:
    draft, _ = store.create_draft("ghostwriter")
    store.save_draft(
        draft.id, "ghostwriter", 0, FRONTMATTER, "Body.\n", announcements=announcements
    )
    record = store.get_draft(draft.id)
    record.status = "published"
    record.slug = "a-post"
    record.published = {"url": "/2026/09/a-post/"}
    path = store.drafts_dir / draft.id / "draft.json"
    path.write_text(json.dumps(record.model_dump(mode="json")), encoding="utf-8")
    store.index.upsert_draft(record)
    return draft.id


def test_filling_writes_a_chronicle_version_and_keeps_the_status_and_post(store: Store) -> None:
    draft_id = _published_with_announcements(store, {"x": "New: {link}", "bluesky": "b"})
    before = store.get_draft(draft_id)
    converted_before = convert.convert(before)

    filled = store.fill_announcement_links(draft_id, LINK, "chronicle-watcher")

    assert filled is not None
    assert filled.status == "published"
    assert filled.announcements == {"x": f"New: {LINK}", "bluesky": f"b\n{LINK}"}
    assert filled.announcement_link == LINK
    version = store.get_version(draft_id, filled.version_no)
    assert version.author == "chronicle"
    assert version.frontmatter == before.frontmatter and version.body == before.body
    assert convert.convert(store.get_draft(draft_id)) == converted_before

    assert store.fill_announcement_links(draft_id, LINK, "chronicle-watcher") is None
    assert store.get_draft(draft_id).version_no == filled.version_no


def test_a_draft_with_no_announcements_gets_no_new_version(store: Store) -> None:
    draft_id = _published_with_announcements(store, {})
    version_no = store.get_draft(draft_id).version_no
    store.fill_announcement_links(draft_id, LINK, "chronicle-watcher")
    draft = store.get_draft(draft_id)
    assert draft.version_no == version_no
    assert draft.announcement_link == LINK


# --- The watcher, end to end --------------------------------------------


def _target() -> tuple[publisher.RepoTarget, FakeRepoOps]:
    ops = FakeRepoOps()
    target = publisher.RepoTarget(
        ops=ops, owner="o", repo="r", default_branch="main", token_provider=lambda: "tok"
    )
    return target, ops


def _merge_a_publish(store: Store, announcements: dict[str, str]) -> str:
    draft, _ = store.create_draft("scott")
    store.save_draft(draft.id, "scott", 0, FRONTMATTER, "Body.\n", announcements=announcements)
    store.act_on_draft(draft.id, "submit", "scott", True)
    _draft, run = store.act_on_draft(draft.id, "approve", "scott", True)
    assert run is not None
    target, ops = _target()
    publisher.run_one(store, target, run)
    watch = store.get_watch(draft.id)
    assert watch is not None
    ops.merge(watch.pr_number)
    assert watcher.check_one(store, target, watch) == "merged"
    return draft.id


@pytest.fixture(autouse=True)
def _no_real_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watcher, "refresh_from_target", lambda *a, **k: None)


def test_a_merged_publish_fills_the_public_link(store: Store) -> None:
    _write_state(store.data_dir, {"source": "hugo_config", "baseurl": "https://blog.example.com/"})
    draft_id = _merge_a_publish(store, {"x": "Out now: {link}", "linkedin": "Long text."})
    draft = store.get_draft(draft_id)
    url = (draft.published or {})["url"]
    link = "https://blog.example.com" + url
    assert draft.status == "published"
    assert draft.announcements == {"x": f"Out now: {link}", "linkedin": f"Long text.\n{link}"}


def test_a_site_with_no_baseurl_leaves_announcements_alone(store: Store) -> None:
    _write_state(store.data_dir, {"source": "hugo_config", "baseurl": None})
    draft_id = _merge_a_publish(store, {"x": "Out now: {link}"})
    draft = store.get_draft(draft_id)
    assert draft.status == "published"
    assert draft.announcements == {"x": "Out now: {link}"}
    assert draft.announcement_link is None


def test_a_failed_fill_never_blocks_the_merge(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_state(store.data_dir, {"source": "hugo_config", "baseurl": "https://blog.example.com/"})

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(Store, "fill_announcement_links", boom)
    draft_id = _merge_a_publish(store, {"x": "Out now: {link}"})
    assert store.get_draft(draft_id).status == "published"
    assert store.get_watch(draft_id) is None


def test_the_panel_shows_the_filled_public_link(client: Any, services: Any) -> None:
    draft_id = _published_with_announcements(services.store, {"x": "New: {link}"})
    services.store.fill_announcement_links(draft_id, LINK, "chronicle-watcher")
    html = client.get(f"/content/drafts/{draft_id}").text
    assert f"Published at <code>{LINK}</code>" in html


# --- Review round: boundaries, absolute urls, races ----------------------


def test_a_longer_url_that_starts_with_the_link_does_not_count_as_the_link() -> None:
    filled = announce.fill_links({"x": "see https://b.example/foo-bar"}, "https://b.example/foo")
    assert filled == {"x": "see https://b.example/foo-bar\nhttps://b.example/foo"}


def test_swapping_a_previous_link_that_prefixes_the_new_one_does_not_corrupt_it() -> None:
    old, new = "https://b.example/a/", "https://b.example/a/b/"
    filled = announce.fill_links({"x": f"new {new} old {old}"}, new, old)
    assert filled == {"x": f"new {new} old {new}"}


def test_a_link_followed_by_sentence_punctuation_is_still_found() -> None:
    once = announce.fill_links({"x": "Read it: {link}. More soon."}, LINK)
    assert once == {"x": f"Read it: {LINK}. More soon."}
    assert announce.fill_links(once, LINK, LINK) == once


def test_an_absolute_post_url_is_used_as_is() -> None:
    assert announce.public_link("https://b.example/blog/", "https://other.example/foo/") == (
        "https://other.example/foo/"
    )


def test_a_draft_revised_after_the_merge_is_not_filled(store: Store) -> None:
    draft_id = _published_with_announcements(store, {"x": "New: {link}"})
    store.save_draft(draft_id, "ghostwriter", 1, FRONTMATTER, "Edited.\n")
    assert store.get_draft(draft_id).status == "drafting"
    assert store.fill_announcement_links(draft_id, LINK, "chronicle-watcher") is None
    assert store.get_draft(draft_id).announcements == {"x": "New: {link}"}


def test_the_watcher_prefers_the_base_url_this_merge_just_read(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_state(store.data_dir, {"source": "hugo_config", "baseurl": "https://old.example/"})
    fresh = digest.HugoConventions(
        contentdir="content",
        staticdir="static",
        mainsections=(),
        taxonomies={},
        environment="production",
        source="hugo_config",
        baseurl="https://new.example/",
    )
    monkeypatch.setattr(watcher, "refresh_from_target", lambda *a, **k: fresh)
    draft_id = _merge_a_publish(store, {"x": "{link}"})
    draft = store.get_draft(draft_id)
    assert draft.announcements["x"].startswith("https://new.example/")


def test_a_fresh_read_without_a_baseurl_does_not_revive_the_saved_one(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_state(store.data_dir, {"source": "hugo_config", "baseurl": "https://old.example/"})
    removed = digest.HugoConventions(
        contentdir="content",
        staticdir="static",
        mainsections=(),
        taxonomies={},
        environment="production",
        source="hugo_config",
        baseurl=None,
    )
    monkeypatch.setattr(watcher, "refresh_from_target", lambda *a, **k: removed)
    draft_id = _merge_a_publish(store, {"x": "{link}"})
    assert store.get_draft(draft_id).announcements == {"x": "{link}"}


def test_a_fallback_read_uses_the_saved_baseurl(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_state(store.data_dir, {"source": "hugo_config", "baseurl": "https://saved.example/"})
    fallback = digest.HugoConventions(
        contentdir="content/posts",
        staticdir="static",
        mainsections=(),
        taxonomies={},
        environment="production",
        source="fallback",
        fallback_reason="hugo missing",
    )
    monkeypatch.setattr(watcher, "refresh_from_target", lambda *a, **k: fallback)
    draft_id = _merge_a_publish(store, {"x": "{link}"})
    assert store.get_draft(draft_id).announcements["x"].startswith("https://saved.example/")
