"""The content and preview UI (C5, ADR 014).

No route here is exercised through a bearer header: the UI backend
authenticates its own calls in-process off `data/state/ui_token.txt`
(`ui_deps.require_ui_consumer`), so every request in this file goes through
plain form posts and cookie-less GETs, the same way a browser on the
internal network would use it.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image as PillowImage

from chronicle.api.deps import Services
from chronicle.api.models import DRAFT_STATUSES, Post

from .conftest import auth, png_bytes

# Every response body collected across this file, checked once at the end of
# each test that renders pages: the deliverable's own bar is that no token
# value ever appears in *any* rendered page, not just the ones a given test
# happens to be asserting on.


def assert_no_token_leak(response: Any, *tokens: str) -> None:
    body = response.text
    for token in tokens:
        assert token not in body, f"token leaked into rendered HTML: {token!r}"


def make_draft(
    services: Services,
    status: str,
    *,
    title: str = "A Draft",
    with_image: bool = False,
    with_publish: bool = False,
) -> str:
    """A draft forced straight to `status`, bypassing the transition table.

    Only for setting up rendering fixtures: the lifecycle itself is
    `transitions.py`'s job and is already covered by `test_transitions.py`
    and `test_store.py`. This exists so every status can be rendered without
    driving a publish/merge cycle end to end for each one.
    """
    store = services.store
    draft, _warnings = store.create_draft("scott")
    store.save_draft(draft.id, "scott", draft.version_no, {"title": title}, "body text")
    draft = store.get_draft(draft.id)
    if with_image:
        raw = png_bytes()
        image, _created = store.put_image(raw, "feature.png")
        store.attach_image(draft.id, image.image_id, "feature", "scott")
    draft = store.get_draft(draft.id)
    draft.status = status
    if status != "drafting":
        draft.slug = draft.slug or "a-draft"
    if with_publish:
        draft.published = {
            "kind": "publish",
            "branch": f"post/{draft.slug}",
            "pr_number": 7,
            "pr_url": "https://github.com/o/r/pull/7",
            "commit_sha": "deadbeef",
            "post_path": f"content/posts/{draft.slug}.md",
            "url": f"/2026/09/{draft.slug}/",
            "date": "2026-09-17",
            "images": [],
            "post_blob_sha": "blobsha",
        }
    store._write_json(store._draft_path(draft.id), draft.model_dump(mode="json"))
    store.index.upsert_draft(draft)
    return draft.id


# --- Submissions -------------------------------------------------------


def test_submissions_list_and_detail_render(client: TestClient, agent_token: str) -> None:
    response = client.post(
        "/v1/submissions",
        json={
            "brief": "a pitch about the lab",
            "materials": [{"name": "notes", "text": "some notes", "url": "https://example.com"}],
            "image_ids": [],
        },
        headers=auth(agent_token),
    )
    assert response.status_code == 201
    submission_id = response.json()["id"]

    listing = client.get("/content/submissions")
    assert listing.status_code == 200
    assert "a pitch about the lab" in listing.text
    assert_no_token_leak(listing, agent_token)

    detail = client.get(f"/content/submissions/{submission_id}")
    assert detail.status_code == 200
    assert "some notes" in detail.text
    assert "https://example.com" in detail.text
    assert "Create post from this submission" in detail.text
    assert "Discard" in detail.text
    assert_no_token_leak(detail, agent_token)


def test_submission_to_draft_claims_a_new_submission_then_creates_a_draft(
    client: TestClient, agent_token: str
) -> None:
    response = client.post(
        "/v1/submissions",
        json={"brief": "brief", "materials": [], "image_ids": []},
        headers=auth(agent_token),
    )
    submission_id = response.json()["id"]

    created = client.post(f"/content/submissions/{submission_id}/draft")
    assert created.status_code == 200
    draft_id = str(created.url).rstrip("/").rsplit("/", 1)[-1]

    submission = client.get(f"/v1/submissions/{submission_id}", headers=auth(agent_token)).json()
    assert submission["status"] == "drafted"
    assert submission["draft_id"] == draft_id


def test_submission_discard(client: TestClient, agent_token: str) -> None:
    response = client.post(
        "/v1/submissions",
        json={"brief": "brief", "materials": [], "image_ids": []},
        headers=auth(agent_token),
    )
    submission_id = response.json()["id"]
    discarded = client.post(f"/content/submissions/{submission_id}/discard")
    assert discarded.status_code == 200
    submission = client.get(f"/v1/submissions/{submission_id}", headers=auth(agent_token)).json()
    assert submission["status"] == "discarded"


# --- Drafts board and editor, every status --------------------------------


@pytest.mark.parametrize("status", DRAFT_STATUSES)
def test_editor_renders_for_every_draft_status(
    client: TestClient, services: Services, agent_token: str, status: str
) -> None:
    draft_id = make_draft(
        services,
        status,
        title=f"Draft in {status}",
        with_publish=(status in ("published", "unpublished")),
    )
    response = client.get(f"/content/drafts/{draft_id}")
    assert response.status_code == 200
    assert f"Draft in {status}" in response.text
    assert_no_token_leak(response, agent_token)

    board = client.get(f"/content/drafts?status={status}")
    assert board.status_code == 200
    assert f"Draft in {status}" in board.text
    assert_no_token_leak(board, agent_token)


def test_reserved_actions_are_labelled_editor_only(client: TestClient, services: Services) -> None:
    draft_id = make_draft(services, "in_review", title="Needs review")
    response = client.get(f"/content/drafts/{draft_id}")
    assert "(editor only)" in response.text
    assert "Scott" not in response.text
    assert "request revision" in response.text.lower()
    assert "reject" in response.text.lower()


def _retag(
    services: Services, draft_id: str, *, updated_at: str | None = None, slug: str | None = None
) -> None:
    store = services.store
    draft = store.get_draft(draft_id)
    if updated_at is not None:
        draft.updated_at = updated_at
    if slug is not None:
        draft.slug = slug
    store._write_json(store._draft_path(draft.id), draft.model_dump(mode="json"))
    store.index.upsert_draft(draft)


def _card_titles(html: str) -> list[str]:
    return re.findall(r'<h3[^>]*><a href="/content/drafts/[^"]+">([^<]*)</a></h3>', html)


def test_drafts_board_paginates_the_published_archive_at_fifty(
    client: TestClient, services: Services
) -> None:
    for i in range(55):
        make_draft(services, "published", title=f"Post {i}")

    first = client.get("/content/drafts")
    assert first.status_code == 200
    assert "Published archive (55)" in first.text
    assert "page 1 of 2" in first.text
    assert "(55 total)" in first.text
    assert "&laquo; previous</span>" in first.text  # no link: already first page
    assert 'href="/content/drafts?page=2#published"' in first.text
    assert len(_card_titles(first.text)) == 50

    second = client.get("/content/drafts?page=2")
    assert second.status_code == 200
    assert "page 2 of 2" in second.text
    assert "next &raquo;</span>" in second.text  # no link: already last page
    assert 'href="/content/drafts?page=1#published"' in second.text
    assert len(_card_titles(second.text)) == 5
    assert '<details id="published" open>' in second.text  # paging means looking at it

    beyond = client.get("/content/drafts?page=99")
    assert beyond.status_code == 200
    assert "page 2 of 2" in beyond.text  # clamped to the last real page


def test_board_splits_work_in_flight_from_the_published_archive(
    client: TestClient, services: Services
) -> None:
    make_draft(services, "drafting", title="Mid edit")
    make_draft(services, "in_review", title="Awaiting Scott")
    make_draft(services, "revision_requested", title="Sent back")
    make_draft(services, "published", title="Old race report")
    make_draft(services, "published", title="Another old one")

    html = client.get("/content/drafts").text
    in_flight, _, archive = html.partition('<details id="published"')
    assert "In flight (3)" in in_flight
    assert "Published archive (2)" in archive
    assert set(_card_titles(in_flight)) == {"Mid edit", "Awaiting Scott", "Sent back"}
    assert set(_card_titles(archive)) == {"Old race report", "Another old one"}
    # Collapsed by default: nothing asked for the archive.
    assert '<details id="published">' in html


def test_board_lists_newest_first_within_each_section(
    client: TestClient, services: Services
) -> None:
    old_live = make_draft(services, "drafting", title="Live old")
    new_live = make_draft(services, "drafting", title="Live new")
    old_pub = make_draft(services, "published", title="Pub 2005")
    new_pub = make_draft(services, "published", title="Pub 2026")
    _retag(services, old_live, updated_at="2026-09-10T08:00:00-05:00")
    _retag(services, new_live, updated_at="2026-09-18T08:00:00-05:00")
    _retag(services, old_pub, updated_at="2005-06-01T08:00:00-05:00")
    _retag(services, new_pub, updated_at="2026-09-01T08:00:00-05:00")

    html = client.get("/content/drafts").text
    in_flight, _, archive = html.partition('<details id="published"')
    assert _card_titles(in_flight) == ["Live new", "Live old"]
    assert _card_titles(archive) == ["Pub 2026", "Pub 2005"]


def test_archive_orders_by_post_date_not_by_when_the_record_was_touched(
    client: TestClient, services: Services
) -> None:
    """A digest stamps hundreds of records within seconds, so `updated_at`
    says nothing about which post is newest. The archive follows the date the
    post carries; a record with no post date falls back to `updated_at`."""
    store = services.store
    ids = {}
    for title, post_date in (
        ("Post 2011", "2011-10-12"),
        ("Post 2019", "2019-04-14 01:57:37+00:00"),
        ("Post 2026", "2026-07-24T12:00:37-05:00"),
    ):
        ids[title] = make_draft(services, "published", title=title, with_publish=True)
        draft = store.get_draft(ids[title])
        assert draft.published is not None
        draft.published["date"] = post_date
        store._write_json(store._draft_path(draft.id), draft.model_dump(mode="json"))
        store.index.upsert_draft(draft)
    # Touched in the opposite order to the post dates, one second apart.
    _retag(services, ids["Post 2026"], updated_at="2026-09-18T16:01:43+00:00")
    _retag(services, ids["Post 2019"], updated_at="2026-09-18T16:01:44+00:00")
    _retag(services, ids["Post 2011"], updated_at="2026-09-18T16:01:45+00:00")

    html = client.get("/content/drafts").text
    _, _, archive = html.partition('<details id="published"')
    assert _card_titles(archive) == ["Post 2026", "Post 2019", "Post 2011"]


def test_board_orders_by_instant_not_by_offset_text(client: TestClient, services: Services) -> None:
    """`2026-09-18T01:00:00-05:00` is 06:00 UTC, later than
    `2026-09-18T03:00:00+00:00`, though it sorts earlier as a string."""
    earlier = make_draft(services, "drafting", title="Earlier")
    later = make_draft(services, "drafting", title="Later")
    _retag(services, earlier, updated_at="2026-09-18T03:00:00+00:00")
    _retag(services, later, updated_at="2026-09-18T01:00:00-05:00")
    assert _card_titles(client.get("/content/drafts").text) == ["Later", "Earlier"]


def test_live_work_stays_on_screen_when_the_archive_is_on_a_later_page(
    client: TestClient, services: Services
) -> None:
    make_draft(services, "drafting", title="Live one")
    for i in range(60):
        make_draft(services, "published", title=f"Archived {i}")
    second = client.get("/content/drafts?page=2").text
    assert "In flight (1)" in second
    assert "Live one" in second


def test_board_search_matches_title_and_slug_case_insensitively(
    client: TestClient, services: Services
) -> None:
    by_title = make_draft(services, "drafting", title="Bonneville Salt Flats")
    by_slug = make_draft(services, "published", title="Unrelated title")
    make_draft(services, "published", title="Nothing to see")
    _retag(services, by_slug, slug="bonneville-2011-recap")
    _retag(services, by_title, slug="salt-flats")

    html = client.get("/content/drafts?q=BONNEVILLE").text
    in_flight, _, archive = html.partition('<details id="published"')
    assert _card_titles(in_flight) == ["Bonneville Salt Flats"]
    assert _card_titles(archive) == ["Unrelated title"]
    assert "In flight (1)" in html
    assert "Published archive (1)" in html
    assert '<details id="published" open>' in html  # searching opens the archive
    assert 'value="BONNEVILLE"' in html

    assert "nothing in flight." in client.get("/content/drafts?q=zzz-no-such").text


def test_board_search_and_status_filter_and_paging_combine(
    client: TestClient, services: Services
) -> None:
    for i in range(55):
        make_draft(services, "published", title=f"Race report {i}")
    make_draft(services, "published", title="Something else")
    make_draft(services, "drafting", title="Race prep")

    both = client.get("/content/drafts?status=published&q=race").text
    assert "In flight (0)" in both
    assert "Published archive (55)" in both
    assert "Race prep" not in both
    assert "Something else" not in both
    assert 'href="/content/drafts?page=2&status=published&q=race#published"' in both

    drafting_only = client.get("/content/drafts?status=drafting&q=race").text
    assert _card_titles(drafting_only) == ["Race prep"]
    assert "Published archive (0)" in drafting_only


def test_board_search_term_is_escaped(client: TestClient) -> None:
    html = client.get('/content/drafts?q="><script>alert(1)</script>').text
    assert "<script>alert(1)</script>" not in html


def test_drafts_board_status_filter_has_no_inline_handler(client: TestClient) -> None:
    """The CSP (default-src 'self', no script-src exception) blocks inline
    event handlers; the status filter must auto-submit via ui.js instead of
    an onchange="" attribute, and still work with a plain submit button when
    JS is unavailable."""
    response = client.get("/content/drafts")
    assert response.status_code == 200
    assert "onchange" not in response.text
    assert '<button type="submit" class="lat-btn">Filter</button>' in response.text
    assert '<script src="/static/ui.js"></script>' in response.text


def test_drafts_board_shows_flag_and_pr_badges(client: TestClient, services: Services) -> None:
    draft_id = make_draft(services, "published", with_publish=True)
    services.store.create_flag(
        "content_drift",
        slug=None,
        draft_id=draft_id,
        detail="main moved",
        actor="chronicle-reconcile",
    )
    board = client.get("/content/drafts?status=published")
    assert board.status_code == 200
    assert "pull/7" in board.text
    assert "content_drift" in board.text


# --- Save, conflict, and versions ------------------------------------------


def test_editor_save_forwards_base_version_and_bumps_version(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "drafting", title="Untitled")
    draft = services.store.get_draft(draft_id)
    form = {
        "base_version": str(draft.version_no),
        "title": "New Title",
        "date": "",
        "categories": "lab, homelab",
        "tags": "unifi",
        "summary": "a summary",
        "url": "",
        "featureImage": "",
        "body": "updated body",
    }
    response = client.post(f"/content/drafts/{draft_id}/save", data=form)
    assert response.status_code == 200
    assert "saved" in response.text.lower()

    updated = services.store.get_draft(draft_id)
    assert updated.version_no == draft.version_no + 1
    assert updated.title == "New Title"
    assert updated.frontmatter["categories"] == ["lab", "homelab"]
    assert updated.frontmatter["tags"] == ["unifi"]
    assert updated.body == "updated body"

    versions = services.store.list_versions(draft_id)
    assert versions[-1].author == "editor"


def _save_form(draft: Any, **overrides: str) -> dict[str, str]:
    form = {
        "base_version": str(draft.version_no),
        "title": draft.title or "Untitled",
        "date": "",
        "categories": "",
        "tags": "",
        "summary": "",
        "url": "",
        "featureImage": "",
        "body": draft.body,
    }
    form.update(overrides)
    return form


def test_editor_save_stores_lf_when_posting_over_an_lf_base(
    client: TestClient, services: Services
) -> None:
    """A browser posts every textarea line break as CRLF (#22). The record must
    hold LF, and a save that changed one line against an LF base must diff as
    one line. This does not cover a base that was itself stored with CRLF
    (an imported post, or a record written by a caller other than this
    route): see the next test."""
    draft_id = make_draft(services, "drafting", title="Endings")
    draft = services.store.get_draft(draft_id)
    services.store.save_draft(
        draft_id, "scott", draft.version_no, {"title": "Endings"}, "one\ntwo\nthree\n"
    )
    draft = services.store.get_draft(draft_id)

    posted = "one\r\nTWO\r\nthree\r\n"
    response = client.post(
        f"/content/drafts/{draft_id}/save", data=_save_form(draft, title="Endings", body=posted)
    )
    assert response.status_code == 200

    saved = services.store.get_draft(draft_id)
    assert saved.body == "one\nTWO\nthree\n"
    assert "\r" not in saved.body
    diff = services.store.diff_between(draft_id, draft.version_no, saved.version_no)
    changed = [
        line for line in diff.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---")
    ]
    assert changed == ["-two", "+TWO"]


def test_editor_save_over_a_crlf_stored_base_rewrites_every_line(
    client: TestClient, services: Services
) -> None:
    """A base stored with CRLF (an imported post, or any writer other than
    this UI route, since only this route's _crlf_to_lf normalises on the way
    in) is not the case the test above covers. This UI save normalises the
    posted body to LF, so the diff is against a CRLF base and every line
    comes out changed. This is disclosed, not fixed here: the fix is
    normalising on read or on import, which lives in store.py."""
    draft_id = make_draft(services, "drafting", title="Endings")
    draft = services.store.get_draft(draft_id)
    services.store.save_draft(
        draft_id, "scott", draft.version_no, {"title": "Endings"}, "one\r\ntwo\r\nthree\r\n"
    )
    draft = services.store.get_draft(draft_id)

    posted = "one\r\nTWO\r\nthree\r\n"
    response = client.post(
        f"/content/drafts/{draft_id}/save", data=_save_form(draft, title="Endings", body=posted)
    )
    assert response.status_code == 200

    saved = services.store.get_draft(draft_id)
    assert saved.body == "one\nTWO\nthree\n"
    diff = services.store.diff_between(draft_id, draft.version_no, saved.version_no)
    changed = [
        line for line in diff.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---")
    ]
    assert changed == ["-one", "-two", "-three", "+one", "+TWO", "+three"]


def test_a_stale_save_conflict_keeps_the_attempted_body_in_lf(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "drafting")
    draft = services.store.get_draft(draft_id)
    services.store.save_draft(draft_id, "scott", draft.version_no, {"title": "A Draft"}, "moved on")
    stale = _save_form(draft, body="mine\r\nedited\r\n")
    response = client.post(f"/content/drafts/{draft_id}/save", data=stale)
    assert response.status_code == 409
    assert "\r" not in response.text


def test_save_preserves_description_key_when_summary_field_is_unchanged(
    client: TestClient, services: Services
) -> None:
    """Adversarial review finding: the save handler always wrote the
    editor's single "summary or description" field back to `summary` and
    unconditionally dropped `description`, so an unrelated edit on a draft
    that only had `description` silently deleted it."""
    draft_id = make_draft(services, "drafting", title="Has Description")
    draft = services.store.get_draft(draft_id)
    services.store.save_draft(
        draft_id,
        "scott",
        draft.version_no,
        {**draft.frontmatter, "description": "original description"},
        draft.body,
    )
    draft = services.store.get_draft(draft_id)
    assert "description" in draft.frontmatter

    # The form's displayed value comes from `val("summary") or
    # val("description")`, so an unchanged submission echoes it back
    # unmodified under the "summary" form field.
    form = _save_form(draft, summary="original description")
    response = client.post(f"/content/drafts/{draft_id}/save", data=form)
    assert response.status_code == 200

    saved = services.store.get_draft(draft_id)
    assert saved.frontmatter.get("description") == "original description"
    assert "summary" not in saved.frontmatter


def test_save_writes_both_keys_when_both_were_present(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "drafting", title="Has Both")
    draft = services.store.get_draft(draft_id)
    services.store.save_draft(
        draft_id,
        "scott",
        draft.version_no,
        {**draft.frontmatter, "summary": "old", "description": "old"},
        draft.body,
    )
    draft = services.store.get_draft(draft_id)

    form = _save_form(draft, summary="new text")
    response = client.post(f"/content/drafts/{draft_id}/save", data=form)
    assert response.status_code == 200

    saved = services.store.get_draft(draft_id)
    assert saved.frontmatter.get("summary") == "new text"
    assert saved.frontmatter.get("description") == "new text"


def test_save_defaults_to_summary_key_when_neither_was_present(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "drafting", title="Has Neither")
    draft = services.store.get_draft(draft_id)
    assert "summary" not in draft.frontmatter
    assert "description" not in draft.frontmatter

    form = _save_form(draft, summary="brand new")
    response = client.post(f"/content/drafts/{draft_id}/save", data=form)
    assert response.status_code == 200

    saved = services.store.get_draft(draft_id)
    assert saved.frontmatter.get("summary") == "brand new"
    assert "description" not in saved.frontmatter


def test_stale_save_renders_409_with_diff_summary_and_both_panes(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "drafting", title="Original")
    draft = services.store.get_draft(draft_id)
    stale_base = draft.version_no

    # Second author (or the same session in another tab) saves first.
    services.store.save_draft(
        draft_id,
        "ghostwriter",
        draft.version_no,
        {"title": "Second author edit"},
        "second author body",
    )

    form = {
        "base_version": str(stale_base),
        "title": "My attempted title",
        "date": "",
        "categories": "",
        "tags": "",
        "summary": "",
        "url": "",
        "featureImage": "",
        "body": "my attempted body",
    }
    response = client.post(f"/content/drafts/{draft_id}/save", data=form)
    assert response.status_code == 409
    assert "conflict" in response.text.lower()
    # The server's diff summary shows what moved underneath the visitor.
    assert "Second author edit" in response.text or "second author body" in response.text
    # The reloaded form carries the current server version, ready to reapply on.
    assert 'value="Second author' in response.text
    # The visitor's own attempted text survives in a second, read-only pane.
    assert "My attempted title" in response.text
    assert "my attempted body" in response.text
    # Nothing was overwritten.
    current = services.store.get_draft(draft_id)
    assert current.title == "Second author edit"


def test_version_diff_view(client: TestClient, services: Services) -> None:
    draft_id = make_draft(services, "drafting", title="v1 title")
    draft = services.store.get_draft(draft_id)
    services.store.save_draft(draft_id, "scott", draft.version_no, {"title": "v2 title"}, "v2 body")
    response = client.get(f"/content/drafts/{draft_id}/diff?from=1&to=2")
    assert response.status_code == 200
    assert "diff-add" in response.text or "diff-del" in response.text
    assert "v2 title" in response.text or "v2 body" in response.text


# --- Reserved actions produce scott-authored records -----------------------


def test_reserved_action_via_ui_is_authored_by_the_editor(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "drafting", title="Reviewable")
    submitted = client.post(f"/content/drafts/{draft_id}/actions/submit")
    assert submitted.status_code == 200
    assert services.store.get_draft(draft_id).status == "in_review"

    revision = client.post(
        f"/content/drafts/{draft_id}/actions/request_revision",
        data={"feedback": "please add more detail"},
    )
    assert revision.status_code == 200
    draft = services.store.get_draft(draft_id)
    assert draft.status == "revision_requested"

    feedback = services.store.list_feedback(draft_id)
    assert feedback[-1].author == "editor"
    assert feedback[-1].text == "please add more detail"

    events, _cursor = services.store.events_since(0)
    reserved_events = [e for e in events if e.type == "draft.request_revision"]
    assert reserved_events and reserved_events[-1].actor == "editor"


def test_reserved_action_without_feedback_is_rejected_and_editor_re_rendered(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "drafting", title="Reviewable")
    client.post(f"/content/drafts/{draft_id}/actions/submit")
    response = client.post(f"/content/drafts/{draft_id}/actions/reject", data={})
    assert response.status_code == 422
    assert services.store.get_draft(draft_id).status == "in_review"


# --- Image upload limits mirror the API's own -------------------------------


def test_image_upload_enforces_size_and_type_like_the_api(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "drafting")

    buffer = io.BytesIO()
    PillowImage.new("RGB", (4, 4)).save(buffer, format="PNG")
    good = client.post(
        f"/content/drafts/{draft_id}/images",
        files={"file": ("a.png", buffer.getvalue(), "image/png")},
        data={"role": "inline"},
    )
    assert good.status_code == 200
    assert "a.png" in good.text

    bad_type = client.post(
        f"/content/drafts/{draft_id}/images",
        files={"file": ("a.txt", b"not an image", "text/plain")},
        data={"role": "inline"},
    )
    assert bad_type.status_code == 415

    too_big = client.post(
        f"/content/drafts/{draft_id}/images",
        files={"file": ("big.png", b"0" * (6 * 1024 * 1024), "image/png")},
        data={"role": "inline"},
    )
    assert too_big.status_code == 413

    draft = services.store.get_draft(draft_id)
    assert len(draft.images) == 1

    detach = client.post(f"/content/drafts/{draft_id}/images/{draft.images[0].image_id}/detach")
    assert detach.status_code == 200
    assert len(services.store.get_draft(draft_id).images) == 0


# --- Draft warnings flash -----------------------------------------------------


def test_draft_warnings_from_a_submission_render_on_the_editor_once(
    client: TestClient, services: Services, agent_token: str
) -> None:
    """`create_draft`'s own warnings (a dropped unknown frontmatter key here)
    ride one flash query parameter through the redirect to the editor, so an
    incomplete seed does not look identical to a complete one. The flash is
    one-shot: a plain reload shows nothing."""
    submission = client.post(
        "/v1/submissions",
        json={
            "brief": "b",
            "materials": [
                {
                    "name": "post",
                    "text": "---\ntitle: Lossy Post\nnotAnAllowedKey: surprise\n---\nbody\n",
                }
            ],
        },
        headers=auth(agent_token),
    ).json()["id"]

    created = client.post(f"/content/submissions/{submission}/draft")
    assert created.status_code == 200
    assert "warning" in created.text.lower()
    assert "notAnAllowedKey" in created.text

    draft_id = str(created.url).rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
    draft = services.store.get_draft(draft_id)
    assert "notAnAllowedKey" not in draft.frontmatter

    reload_ = client.get(f"/content/drafts/{draft_id}")
    assert "warning" not in reload_.text.lower()


# --- Preview tab and run log -------------------------------------------------


def test_preview_list_and_rebuild_and_run_log(client: TestClient, services: Services) -> None:
    draft_id = make_draft(services, "previewed")
    draft = services.store.get_draft(draft_id)
    run = services.store._queue_run(draft_id, "preview")
    services.store.start_run(run.id, "builder-1", "0.164.0", False, built_version=draft.version_no)
    services.store.finish_run(run.id, "builder-1", True, {"preview_url": f"/preview/{draft.slug}/"})

    listing = client.get("/content/previews")
    assert listing.status_code == 200
    assert f"/preview/{draft.slug}/" in listing.text
    assert "Rebuild" in listing.text

    log_page = client.get(f"/runs/{run.id}")
    assert log_page.status_code == 200
    assert "succeeded" in log_page.text

    rebuild = client.post(f"/content/previews/{draft_id}/rebuild")
    assert rebuild.status_code == 200


# --- CSRF and banner ---------------------------------------------------------


def test_cross_origin_post_is_refused(client: TestClient, services: Services) -> None:
    draft_id = make_draft(services, "drafting")
    response = client.post(
        f"/content/drafts/{draft_id}/claim", headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403


def test_null_origin_post_is_refused(client: TestClient, services: Services) -> None:
    """A sandboxed iframe sends `Origin: null`; that must not pass as same-origin.

    Adversarial review finding: `urlsplit("null").netloc` is empty, which
    used to fall through the same branch as "no header at all" and let a
    cross-origin sandboxed iframe submit every state-changing form here.
    """
    draft_id = make_draft(services, "drafting")
    response = client.post(f"/content/drafts/{draft_id}/claim", headers={"Origin": "null"})
    assert response.status_code == 403


def test_empty_origin_post_is_refused(client: TestClient, services: Services) -> None:
    """A present but empty Origin header is falsy in Python, so `origin or
    referer` used to substitute Referer (or the "absent" pass-through) for
    it; a follow-up review found the same class of bug the null-origin fix
    addressed, one layer earlier. Present-but-empty is always refused."""
    draft_id = make_draft(services, "drafting")
    response = client.post(f"/content/drafts/{draft_id}/claim", headers={"Origin": ""})
    assert response.status_code == 403


def test_garbage_origin_post_is_refused(client: TestClient, services: Services) -> None:
    draft_id = make_draft(services, "drafting")
    response = client.post(
        f"/content/drafts/{draft_id}/claim", headers={"Origin": "not a url at all"}
    )
    assert response.status_code == 403


def test_empty_origin_with_good_referer_is_still_refused(
    client: TestClient, services: Services
) -> None:
    """A present-but-unparsable Origin must never fall back to Referer, even
    a same-origin one: Origin, when sent at all, is the authoritative
    signal."""
    draft_id = make_draft(services, "drafting")
    response = client.post(
        f"/content/drafts/{draft_id}/claim",
        headers={"Origin": "", "Referer": "http://testserver/content/drafts"},
    )
    assert response.status_code == 403


def test_absent_origin_with_good_referer_is_allowed(client: TestClient, services: Services) -> None:
    draft_id = make_draft(services, "drafting")
    response = client.post(
        f"/content/drafts/{draft_id}/claim",
        headers={"Referer": "http://testserver/content/drafts"},
    )
    assert response.status_code == 200
    assert response.url.path == f"/content/drafts/{draft_id}"


def test_absent_origin_and_referer_is_allowed(client: TestClient, services: Services) -> None:
    draft_id = make_draft(services, "drafting")
    response = client.post(f"/content/drafts/{draft_id}/claim")
    assert response.status_code == 200
    assert response.url.path == f"/content/drafts/{draft_id}"


def test_rebuild_failure_notice_is_escaped(client: TestClient) -> None:
    """Adversarial review finding: the rebuild-failure page used to splice
    the draft id (verbatim, from the 404 error message) into an unescaped
    `<p>`, which is a reflected-XSS path for a nonexistent draft id."""
    from urllib.parse import quote

    response = client.post(f"/content/previews/{quote('<img src=x onerror=alert(1)>')}/rebuild")
    assert response.status_code == 404
    assert "<img src=x" not in response.text
    assert "&lt;img src=x" in response.text


def test_submission_material_javascript_url_is_not_a_live_link(
    client: TestClient, agent_token: str
) -> None:
    """Adversarial review finding: a submission's material url is
    unvalidated input; escape() alone leaves a `javascript:` scheme intact,
    so it used to render as a clickable link that runs on click."""
    response = client.post(
        "/v1/submissions",
        json={
            "brief": "brief",
            "materials": [{"name": "link", "url": "javascript:alert(document.cookie)"}],
            "image_ids": [],
        },
        headers=auth(agent_token),
    )
    submission_id = response.json()["id"]
    detail = client.get(f"/content/submissions/{submission_id}")
    assert '<a href="javascript:' not in detail.text
    assert "javascript:alert" in detail.text  # shown as inert text, not a link


def test_stale_save_conflict_preserves_full_attempted_frontmatter(
    client: TestClient, services: Services
) -> None:
    """Adversarial review finding: the conflict pane used to keep only the
    attempted title and body, silently dropping tags/date/summary edits
    from the "manual merge" pane."""
    draft_id = make_draft(services, "drafting", title="Original")
    draft = services.store.get_draft(draft_id)
    services.store.save_draft(
        draft_id, "ghostwriter", draft.version_no, {"title": "Landed"}, "landed body"
    )
    response = client.post(
        f"/content/drafts/{draft_id}/save",
        data={
            "base_version": str(draft.version_no),
            "title": "My title",
            "date": "",
            "categories": "lab",
            "tags": "unifi",
            "summary": "my summary",
            "url": "",
            "featureImage": "",
            "body": "my body",
        },
    )
    assert response.status_code == 409
    assert "lab" in response.text
    assert "unifi" in response.text
    assert "my summary" in response.text


def test_conflict_page_hands_the_posted_fields_to_the_browser_backup(
    client: TestClient, services: Services
) -> None:
    """The backup `editor.js` writes from the conflict page must carry the
    title, tags and other fields the visitor edited, not `{}`: the fields are
    the raw form values, the same shape the editor's own backup uses."""
    import html as html_lib
    import json
    import re

    draft_id = make_draft(services, "drafting", title="Original")
    draft = services.store.get_draft(draft_id)
    services.store.save_draft(
        draft_id, "ghostwriter", draft.version_no, {"title": "Landed"}, "landed body"
    )
    response = client.post(
        f"/content/drafts/{draft_id}/save",
        data={
            "base_version": str(draft.version_no),
            "title": "My title",
            "categories": "lab, home",
            "tags": "unifi",
            "summary": "my summary",
            "body": "my body",
        },
    )
    assert response.status_code == 409
    match = re.search(r'id="attempted-body"[^>]* data-fields="([^"]*)"', response.text)
    assert match is not None
    fields = json.loads(html_lib.unescape(match.group(1)))
    assert fields == {
        "title": "My title",
        "categories": "lab, home",
        "tags": "unifi",
        "summary": "my summary",
    }
    # None of the three claims about the browser's copy is made without script.
    for outcome in ("stored", "kept-other", "unavailable"):
        assert re.search(rf'id="backup-note-{outcome}" hidden', response.text)


def test_approve_button_hidden_while_publish_pr_open(
    client: TestClient, services: Services
) -> None:
    """Adversarial review finding: `_action_buttons` rendered a re-approve
    button purely off the transition table, which does not know about
    `Store.act_on_draft`'s own refusal to re-approve while a publish PR is
    already open; the button used to 409 on every click until the PR
    closed."""
    from chronicle.api.models import WatchEntry

    draft_id = make_draft(services, "approved", with_publish=True)
    services.store.record_watch(
        WatchEntry(
            draft_id=draft_id,
            kind="publish",
            branch="post/a-draft",
            pr_number=7,
            pr_url="https://github.com/o/r/pull/7",
            created_at="2026-09-17T00:00:00-05:00",
        ),
        "scott",
    )
    response = client.get(f"/content/drafts/{draft_id}")
    assert "open publish pull request" in response.text
    assert "republish" not in response.text.lower()


def test_approve_button_hidden_while_publish_run_is_queued_or_building(
    client: TestClient, services: Services
) -> None:
    """Adversarial review finding: between "approved" and a watch entry
    existing (the watch is only written once the publisher opens a PR), a
    publish run can already be `queued` or `building`; `Store.act_on_draft`
    refuses a re-approve then too (409 publish_run_in_progress), but the
    button only checked `publish_pr_open` and still rendered."""
    draft_id = make_draft(services, "approved")
    run = services.store._queue_run(draft_id, "publish")
    services.store.index.upsert_run(run)  # _queue_run alone does not index it

    response = client.get(f"/content/drafts/{draft_id}")
    assert f"/content/drafts/{draft_id}/actions/approve" not in response.text

    services.store.start_run(run.id, "publisher-1", "", False)
    response = client.get(f"/content/drafts/{draft_id}")
    assert f"/content/drafts/{draft_id}/actions/approve" not in response.text

    services.store.finish_run(run.id, "publisher-1", True, {})
    response = client.get(f"/content/drafts/{draft_id}")
    assert f"/content/drafts/{draft_id}/actions/approve" in response.text


def _save_button(html: str) -> str:
    match = re.search(r'<button[^>]*id="save-btn"[^>]*>', html)
    assert match, "no Save button in the page"
    return match.group(0)


def test_save_is_not_offered_while_a_publish_run_is_active_and_says_why(
    client: TestClient, services: Services
) -> None:
    """The store refuses a save with 409 while a publish run is queued or
    building (#45), so the editor must not offer one to fail on click."""
    draft_id = make_draft(services, "approved")
    assert "disabled" not in _save_button(client.get(f"/content/drafts/{draft_id}").text)

    run = services.store._queue_run(draft_id, "publish")
    services.store.index.upsert_run(run)
    page = client.get(f"/content/drafts/{draft_id}").text
    button = _save_button(page)
    assert " disabled" in button
    assert "data-locked=" in button
    assert "A publish run is in progress, so saving is refused until it finishes." in page

    # The refusal is the store's, so a hand-built POST still gets the 409.
    version = services.store.get_draft(draft_id).version_no
    refused = client.post(
        f"/content/drafts/{draft_id}/save",
        data={"base_version": str(version), "title": "t", "body": "b"},
    )
    assert refused.status_code == 409

    services.store.start_run(run.id, "publisher-1", "", False)
    assert " disabled" in _save_button(client.get(f"/content/drafts/{draft_id}").text)

    services.store.finish_run(run.id, "publisher-1", True, {})
    assert "disabled" not in _save_button(client.get(f"/content/drafts/{draft_id}").text)


def test_save_is_not_offered_while_a_publish_pr_is_open(
    client: TestClient, services: Services
) -> None:
    from chronicle.api.models import WatchEntry

    draft_id = make_draft(services, "approved", with_publish=True)
    services.store.record_watch(
        WatchEntry(
            draft_id=draft_id,
            kind="publish",
            branch="post/a-draft",
            pr_number=7,
            pr_url="https://github.com/o/r/pull/7",
            created_at="2026-09-17T00:00:00-05:00",
        ),
        "scott",
    )
    page = client.get(f"/content/drafts/{draft_id}").text
    assert " disabled" in _save_button(page)
    assert "A publish pull request is open, so saving is refused" in page


def test_the_save_button_is_in_a_refreshed_region(client: TestClient, services: Services) -> None:
    """A save or upload swaps `data-refresh` regions; the Save button has to be
    one, or a lock that appears (or clears) mid-session would never show."""
    draft_id = make_draft(services, "drafting")
    page = client.get(f"/content/drafts/{draft_id}").text
    assert re.search(r'<span id="save-control" data-refresh>\s*<button[^>]*id="save-btn"', page)


def test_banner_shown_by_default(client: TestClient) -> None:
    response = client.get("/content/drafts")
    assert "internal-only and unauthenticated" in response.text


def test_banner_hidden_when_disabled(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRONICLE_UI_BANNER", "0")
    from chronicle.api.main import create_app

    app = create_app()
    with TestClient(app) as scoped_client:
        response = scoped_client.get("/content/drafts")
        assert "internal-only and unauthenticated" not in response.text
    app.state.services.close()


def test_ui_token_never_appears_in_any_rendered_page(
    client: TestClient, services: Services, ui_token: str, agent_token: str
) -> None:
    draft_id = make_draft(services, "in_review", title="Leak check", with_image=True)
    pages = [
        "/content/drafts",
        f"/content/drafts/{draft_id}",
        "/content/submissions",
        "/content/previews",
    ]
    for path in pages:
        response = client.get(path)
        assert_no_token_leak(response, ui_token, agent_token)


# --- Featured image and pinned url hardening --------------------------


def test_imported_feature_image_path_survives_an_unchanged_save(
    client: TestClient, services: Services
) -> None:
    """An imported post's featureImage is a root-relative path recorded as
    the attached image's source_ref (the C2 source_ref shape), not the
    image's bare filename. A round C5+ review found the editor's select
    could only match a bare filename, so it never selected that option, an
    unchanged submit posted an empty value, and the save route dropped
    featureImage entirely."""
    store = services.store
    slug = "2026-08-01-vcf-operations-can-now-see-my-unifi-network"
    post_dir = store.site_dir / "content" / "posts"
    post_dir.mkdir(parents=True, exist_ok=True)
    (post_dir / f"{slug}.md").write_text(
        "---\n"
        "title: VCF Operations Can Now See My Unifi Network\n"
        "url: /vcf-operations-can-now-see-my-unifi-network/\n"
        "type: post\n"
        "date: 2026-08-01\n"
        "featureImage: /images/vcf-operations-can-now-see-my-unifi-network/featured.png\n"
        "---\n"
        "body text\n",
        encoding="utf-8",
    )
    image_dir = store.site_dir / "static" / "images" / "vcf-operations-can-now-see-my-unifi-network"
    image_dir.mkdir(parents=True)
    (image_dir / "featured.png").write_bytes(png_bytes())

    post = Post(
        slug=slug,
        path=f"content/posts/{slug}.md",
        title="VCF Operations Can Now See My Unifi Network",
        date="2026-08-01",
        sha="realsha",
    )
    store.posts_dir.mkdir(parents=True, exist_ok=True)
    store._write_json(store.posts_dir / f"{slug}.json", post.model_dump(mode="json"))
    store.index.upsert_post(post)

    draft, warnings = store.create_draft("ghostwriter", from_post=slug)
    assert warnings == []
    stored_feature_image = draft.frontmatter["featureImage"]
    assert stored_feature_image == (
        "/images/vcf-operations-can-now-see-my-unifi-network/featured.png"
    )

    editor = client.get(f"/content/drafts/{draft.id}")
    assert editor.status_code == 200
    # Selected by the actual attached image record (source_ref match), not
    # by the no-match fallback option: the fallback also emits
    # `value="<path>" selected` on its own, so pin the assertion to the
    # matched option's display text (the real image's filename) to prove
    # the select actually recognised the attached image.
    assert f'<option value="{stored_feature_image}" selected>featured.png</option>' in editor.text
    assert editor.text.count(" selected") == 1

    form = {
        "base_version": str(draft.version_no),
        "title": draft.frontmatter["title"],
        "date": draft.frontmatter["date"],
        "categories": "",
        "tags": "",
        "summary": "",
        "url": draft.frontmatter["url"],
        "featureImage": stored_feature_image,
        "body": draft.body,
    }
    response = client.post(f"/content/drafts/{draft.id}/save", data=form)
    assert response.status_code == 200

    updated = store.get_draft(draft.id)
    assert updated.frontmatter["featureImage"] == stored_feature_image


def test_save_preserves_pinned_url_when_form_omits_it(
    client: TestClient, services: Services
) -> None:
    """The url input is readonly once a slug is pinned: a convenience echo,
    never the source of truth. A request that leaves it out entirely (not
    just blank) must not erase the stored url."""
    draft_id = make_draft(services, "drafting", title="Pinned")
    store = services.store
    draft = store.get_draft(draft_id)
    draft.slug = "pinned-slug"
    draft.frontmatter["url"] = "/pinned-slug/"
    store._write_json(store._draft_path(draft.id), draft.model_dump(mode="json"))
    store.index.upsert_draft(draft)
    draft = store.get_draft(draft_id)

    form = {
        "base_version": str(draft.version_no),
        "title": "Pinned",
        "date": "",
        "categories": "",
        "tags": "",
        "summary": "",
        "featureImage": "",
        "body": "body text",
        # url intentionally omitted, unlike a normal browser submit.
    }
    response = client.post(f"/content/drafts/{draft_id}/save", data=form)
    assert response.status_code == 200

    updated = store.get_draft(draft_id)
    assert updated.frontmatter["url"] == "/pinned-slug/"


def test_save_never_writes_pinned_slug_into_frontmatter(
    client: TestClient, services: Services
) -> None:
    """`draft.slug` (the pinned value) must never be injected into the
    frontmatter dict on a save. A `github`-authored save
    (`Store.record_github_version`) can legitimately carry a different
    `slug` key than `draft.slug`; injecting `draft.slug` here would
    silently revert content Scott wrote on GitHub, which is
    reconciliation's job to flag, never to correct automatically. When a
    draft's frontmatter genuinely carries a stale `slug` key, `save_draft`
    itself already refuses the save (`slug_immutable`) rather than picking
    a winner, so the UI route must never smooth that over by overwriting
    one side."""
    draft_id = make_draft(services, "drafting", title="Pinned")
    store = services.store
    draft = store.get_draft(draft_id)
    draft.slug = "pinned-slug"
    draft.frontmatter["url"] = "/pinned-slug/"
    draft.frontmatter["slug"] = "drifted-from-github"
    store._write_json(store._draft_path(draft.id), draft.model_dump(mode="json"))
    store.index.upsert_draft(draft)
    draft = store.get_draft(draft_id)

    form = {
        "base_version": str(draft.version_no),
        "title": "Pinned",
        "date": "",
        "categories": "",
        "tags": "",
        "summary": "",
        "url": "/pinned-slug/",
        "featureImage": "",
        "body": "body text",
    }
    response = client.post(f"/content/drafts/{draft_id}/save", data=form)
    # save_draft's own slug_immutable check catches the drift, visibly,
    # rather than the UI route silently resolving it either direction.
    assert response.status_code == 422

    unchanged = store.get_draft(draft_id)
    assert unchanged.frontmatter["slug"] == "drifted-from-github"
    assert unchanged.slug == "pinned-slug"


def test_feature_image_select_prefers_exact_match_over_basename_collision(
    client: TestClient, services: Services
) -> None:
    """Two attached images can share a basename (one image's filename
    equals another image's source_ref basename). Only the exact match may
    ever render `selected`, so the ambiguous option keeps its own filename
    as its value and stays choosable."""
    store = services.store
    draft, _warnings = store.create_draft("scott")
    store.save_draft(
        draft.id,
        "scott",
        draft.version_no,
        {"title": "Collision", "featureImage": "featured.png"},
        "body",
    )
    image_a, _ = store.put_image(png_bytes((1, 2, 3)), "featured.png")
    store.attach_image(draft.id, image_a.image_id, "feature", "scott")
    image_b, _ = store.put_image(png_bytes((4, 5, 6)), "hero.png")
    store.attach_image(draft.id, image_b.image_id, "inline", "scott")
    draft = store.get_draft(draft.id)
    # Force a source_ref collision: image_b's recorded reference basename
    # matches image_a's filename, without touching either's own filename.
    for img in draft.images:
        if img.image_id == image_b.image_id:
            img.source_ref = "/images/elsewhere/featured.png"
    store._write_json(store._draft_path(draft.id), draft.model_dump(mode="json"))
    store.index.upsert_draft(draft)

    editor = client.get(f"/content/drafts/{draft.id}")
    assert editor.status_code == 200
    assert '<option value="featured.png" selected>featured.png</option>' in editor.text
    assert '<option value="hero.png">hero.png</option>' in editor.text
    assert editor.text.count(" selected") == 1


# --- New post ---------------------------------------------------------------


def test_board_offers_a_new_post_button_that_posts(client: TestClient) -> None:
    response = client.get("/content/drafts")
    assert '<form method="post" action="/content/drafts/new">' in response.text
    assert "New post" in response.text


def test_new_post_creates_a_blank_draft_and_redirects_into_the_editor(
    client: TestClient, services: Services
) -> None:
    assert services.store.list_drafts() == []
    response = client.post("/content/drafts/new", follow_redirects=False)
    assert response.status_code == 303
    drafts = services.store.list_drafts()
    assert len(drafts) == 1
    assert response.headers["location"] == f"/content/drafts/{drafts[0].id}"
    assert drafts[0].status == "drafting"
    assert drafts[0].title == ""
    # Acts as the ui consumer's mapped identity, like every other UI write.
    events, _cursor = services.store.events_since(0)
    assert [(e.type, e.actor, e.draft_id) for e in events if e.draft_id == drafts[0].id] == [
        ("draft.created", "editor", drafts[0].id)
    ]

    editor = client.get(response.headers["location"])
    assert editor.status_code == 200
    assert 'name="title"' in editor.text


def test_new_post_get_creates_nothing(client: TestClient, services: Services) -> None:
    response = client.get("/content/drafts/new")
    assert response.status_code == 404
    assert services.store.list_drafts() == []


@pytest.mark.parametrize("origin", ["https://evil.example", "null", ""])
def test_new_post_is_origin_guarded(client: TestClient, services: Services, origin: str) -> None:
    response = client.post("/content/drafts/new", headers={"Origin": origin})
    assert response.status_code == 403
    assert services.store.list_drafts() == []


def test_new_post_needs_a_live_ui_token(client: TestClient, services: Services) -> None:
    services.tokens.ui_token_path.unlink()
    response = client.post("/content/drafts/new")
    assert response.status_code == 503
    assert services.store.list_drafts() == []


# --- The Import tab is gone (#20) ---------------------------------------------


def test_the_import_tab_and_its_routes_are_gone(client: TestClient) -> None:
    """Digest already lands a record for every post on main, so the UI has no
    front door for `from_post`; the capability stays on the API and in the store."""
    assert client.get("/content/import").status_code == 404
    assert client.post("/content/import", data={"slug": "x"}).status_code in (404, 405)
    assert "/content/import" not in client.get("/content/drafts").text


# --- Feedback log line breaks (#26) -----------------------------------------


NOTE = "first line\r\nsecond <b>line</b>\n\n<script>alert(1)</script> & done"


def test_feedback_log_renders_line_breaks_without_opening_an_html_hole(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "in_review")
    services.store.act_on_draft(draft_id, "request_revision", "editor", True, feedback=NOTE)

    html = client.get(f"/content/drafts/{draft_id}").text
    log = html[html.index('<ul class="chr-log">') :]
    log = log[: log.index("</ul>")]
    assert "first line<br>second &lt;b&gt;line&lt;/b&gt;<br><br>&lt;script&gt;" in log
    assert "alert(1)&lt;/script&gt; &amp; done" in log
    # Only the breaks the renderer added are real tags; nothing the note wrote is.
    assert "<script>" not in log
    assert "<b>" not in log
    assert log.count("<br>") == 3
