"""A draft's body is stored and compared with LF line endings only (issue #51).

A body that arrives with CRLF (an imported post, a `/v1` save, a github
version) is stored as LF, and a record written with CRLF before this rule
loads as LF, so its next save diffs as the lines that changed, not the
whole file."""

from __future__ import annotations

import json

from chronicle.api.models import Material, Post, to_lf
from chronicle.api.store import Store


def _changed_lines(diff: str) -> list[str]:
    return [
        line for line in diff.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---")
    ]


def _write_crlf_body_on_disk(store: Store, draft_id: str, version_no: int, body: str) -> None:
    """Rewrite a stored draft and one version as a pre-#51 writer left them."""
    for path in (store._draft_path(draft_id), store._version_path(draft_id, version_no)):
        record = json.loads(path.read_text(encoding="utf-8"))
        record["body"] = body
        path.write_text(json.dumps(record), encoding="utf-8")


def test_to_lf_converts_crlf_and_a_lone_cr() -> None:
    assert to_lf("a\r\nb\rc\nd") == "a\nb\nc\nd"


def test_a_crlf_save_is_stored_as_lf_on_disk(store: Store) -> None:
    draft, _ = store.create_draft("ghostwriter")
    saved = store.save_draft(draft.id, "ghostwriter", 0, {"title": "T"}, "one\r\ntwo\r\n")
    assert saved.body == "one\ntwo\n"
    assert "\\r" not in store._draft_path(draft.id).read_text(encoding="utf-8")
    assert "\\r" not in store._version_path(draft.id, saved.version_no).read_text(encoding="utf-8")


def test_a_legacy_crlf_record_loads_as_lf(store: Store) -> None:
    draft, _ = store.create_draft("ghostwriter")
    saved = store.save_draft(draft.id, "ghostwriter", 0, {"title": "T"}, "x")
    _write_crlf_body_on_disk(store, draft.id, saved.version_no, "one\r\ntwo\r\n")
    assert store.get_draft(draft.id).body == "one\ntwo\n"
    assert store.get_version(draft.id, saved.version_no).body == "one\ntwo\n"


def test_editing_one_line_of_a_legacy_crlf_record_diffs_as_one_line(store: Store) -> None:
    draft, _ = store.create_draft("ghostwriter")
    saved = store.save_draft(draft.id, "ghostwriter", 0, {"title": "T"}, "x")
    _write_crlf_body_on_disk(store, draft.id, saved.version_no, "one\r\ntwo\r\nthree\r\n")

    edited = store.save_draft(
        draft.id, "ghostwriter", saved.version_no, {"title": "T"}, "one\ntwo!\nthree\n"
    )
    diff = store.diff_between(draft.id, saved.version_no, edited.version_no)
    assert _changed_lines(diff) == ["-two", "+two!"]


def test_resaving_a_legacy_crlf_published_post_unchanged_keeps_it_published(
    store: Store,
) -> None:
    """The same text in other line endings is not a text change, so it does
    not revise a published post (issue #69's rule)."""
    draft, _ = store.create_draft("ghostwriter")
    saved = store.save_draft(draft.id, "ghostwriter", 0, {"title": "T"}, "x")
    _write_crlf_body_on_disk(store, draft.id, saved.version_no, "one\r\ntwo\r\n")
    path = store._draft_path(draft.id)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["status"] = "published"
    path.write_text(json.dumps(record), encoding="utf-8")

    again = store.save_draft(
        draft.id,
        "ghostwriter",
        saved.version_no,
        {"title": "T"},
        "one\r\ntwo\r\n",
        announcements={"bluesky": "new"},
    )
    assert again.status == "published"
    assert again.body == "one\ntwo\n"


def test_importing_a_crlf_post_from_main_stores_lf(store: Store) -> None:
    post_dir = store.site_dir / "content" / "posts"
    post_dir.mkdir(parents=True, exist_ok=True)
    (post_dir / "crlf-post.md").write_bytes(
        b"---\r\ntitle: CRLF Post\r\ndate: 2024-03-03\r\n---\r\nline one\r\nline two\r\n"
    )
    post = Post(
        slug="crlf-post",
        path="content/posts/crlf-post.md",
        title="CRLF Post",
        date="2024-03-03",
        sha="abc123",
    )
    store.posts_dir.mkdir(parents=True, exist_ok=True)
    store._write_json(store.posts_dir / "crlf-post.json", post.model_dump(mode="json"))
    store.index.upsert_post(post)

    draft, _ = store.create_draft("ghostwriter", from_post="crlf-post")
    assert "\r" not in draft.body
    assert "line one\nline two\n" in draft.body
    assert draft.frontmatter["title"] == "CRLF Post"
    assert "\\r" not in store._draft_path(draft.id).read_text(encoding="utf-8")


def test_a_crlf_github_version_is_stored_as_lf(store: Store) -> None:
    draft, _ = store.create_draft("ghostwriter")
    store.save_draft(draft.id, "ghostwriter", 0, {"title": "T"}, "x")
    recorded = store.record_github_version(draft.id, {"title": "T"}, "a\r\nb\r\n", "drift")
    assert recorded.body == "a\nb\n"
    assert store.get_version(draft.id, recorded.version_no).body == "a\nb\n"


def test_a_crlf_submission_seeds_an_lf_body(store: Store) -> None:
    submission = store.create_submission(
        "ghostwriter",
        brief="b",
        materials=[Material(name="post.md", text="# Title\r\n\r\nline one\r\nline two\r\n")],
        image_ids=[],
    )
    store.act_on_submission(submission.id, "claim", "ghostwriter")
    draft, _ = store.create_draft("ghostwriter", from_submission=submission.id)
    assert "\r" not in draft.body
    assert "line one\nline two\n" in draft.body


def test_a_lone_cr_submission_still_seeds_its_frontmatter(store: Store) -> None:
    """Codex round: lone-CR text must be normalised before the frontmatter
    fence is matched, not only after, or the whole block lands in the body."""
    submission = store.create_submission(
        "ghostwriter",
        brief="b",
        materials=[Material(name="post.md", text="---\rtitle: Lone CR\r---\rbody line\r")],
        image_ids=[],
    )
    store.act_on_submission(submission.id, "claim", "ghostwriter")
    draft, _ = store.create_draft("ghostwriter", from_submission=submission.id)
    assert draft.frontmatter.get("title") == "Lone CR"
    assert draft.body.strip() == "body line"
