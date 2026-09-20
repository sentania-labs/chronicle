"""The one status-label helper, held against the state machine.

The labels are render layer only: the API's status values, the stored
records, and the board's filter query values keep the state machine's own
strings. A reader sees four words; the finer statuses are detail.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from chronicle.api.deps import Services
from chronicle.api.models import DRAFT_STATUSES, FeedbackEntry
from chronicle.api.ui_status import (
    FOUR_WORDS,
    STATUS_LABELS,
    Detail,
    came_back_from_review,
    filter_options,
    label_counts,
    parse_status_filter,
    status_details,
    status_label,
)

from .conftest import auth
from .test_ui import make_draft

EXPECTED = {
    "drafting": "Draft",
    "previewed": "Draft",
    "revision_requested": "Draft",
    "unpublished": "Draft",
    "in_review": "In review",
    "approved": "In review",
    "published": "Published",
    "rejected": "Rejected",
}


def test_every_status_has_a_label_and_the_labels_are_exactly_four_words() -> None:
    # A status added to the state machine fails here until it is given a label.
    assert set(STATUS_LABELS) == set(DRAFT_STATUSES)
    assert FOUR_WORDS == ("Draft", "In review", "Published", "Rejected")
    for status in DRAFT_STATUSES:
        assert status_label(status) == EXPECTED[status]
    assert set(STATUS_LABELS.values()) == set(FOUR_WORDS)


def test_expected_table_covers_the_whole_state_machine() -> None:
    assert set(EXPECTED) == set(DRAFT_STATUSES)


def _feedback(services: Services, draft_id: str, action: str, text: str = "x") -> None:
    services.store._append_feedback(
        FeedbackEntry(
            draft_id=draft_id,
            author="editor",
            created_at="2026-09-01T10:00:00-05:00",
            action=action,
            version_no=1,
            text=text,
        )
    )


# --- Detail --------------------------------------------------------------


def test_the_facts_the_finer_statuses_carried_are_detail() -> None:
    assert status_details("drafting") == []
    assert status_details("in_review") == []
    assert status_details("published") == []
    assert status_details("rejected") == []
    assert status_details("previewed") == [Detail("Preview built")]
    assert status_details("previewed", has_preview=False) == [Detail("Preview out of date")]
    assert status_details("unpublished") == [Detail("Was published")]
    assert status_details("approved") == [Detail("Approved")]
    assert status_details("drafting", came_back=True) == [Detail("Came back from review", "warn")]
    # Two facts at once: both show.
    assert [d.text for d in status_details("previewed", came_back=True)] == [
        "Came back from review",
        "Preview built",
    ]
    # A publish in flight is detail on whichever word the status has.
    assert status_details("approved", publish_run_active=True)[-1] == Detail("Publishing")
    assert status_details("approved", publish_pr_open=True)[-1] == Detail("Publish PR open")
    assert status_details("published", unpublish_pr_open=True) == [Detail("Unpublish PR open")]


def test_came_back_is_read_from_the_log_once_the_status_has_moved_on() -> None:
    assert came_back_from_review("revision_requested", [])
    # A save moved it to drafting: the request stands.
    assert came_back_from_review("drafting", ["request_revision"])
    assert not came_back_from_review("drafting", [])
    assert not came_back_from_review("drafting", ["material", "publish_failed"])
    # A later rejection is the newest verdict, so the request is settled.
    assert not came_back_from_review("drafting", ["request_revision", "reject"])
    assert came_back_from_review("drafting", ["reject", "request_revision"])
    # In review is a status that already says so, and published/rejected are done.
    for status in ("in_review", "approved", "published", "rejected", "unpublished"):
        assert not came_back_from_review(status, ["request_revision"])
    # previewed never claims the fact: it cannot be told apart from a resubmit
    # that has already answered the request (see the docstring).
    assert not came_back_from_review("previewed", ["request_revision"])
    # A draft with a published record cannot be ordered against its request.
    assert not came_back_from_review("drafting", ["request_revision"], published=True)
    assert came_back_from_review("revision_requested", [], published=True)


def test_came_back_does_not_reappear_after_a_resubmit_and_preview() -> None:
    # The reviewer's scenario: request_revision, then a preview (drafting),
    # then a resubmit (in_review), then a further preview success lands back
    # on previewed. submit and approve write no feedback entry, so the log
    # still ends on request_revision even though the request was answered.
    assert not came_back_from_review("previewed", ["request_revision"])


def test_board_shows_the_four_word_status_with_the_detail_beside_it(
    client: TestClient, services: Services
) -> None:
    make_draft(services, "revision_requested", title="Sent back")
    make_draft(services, "previewed", title="Built")
    make_draft(services, "unpublished", title="Pulled", with_publish=True)
    make_draft(services, "approved", title="Going out")
    board = client.get("/content/drafts").text
    assert "Needs revision" not in board
    assert "Previewed<" not in board
    assert "Unpublished<" not in board
    assert ">Approved</span>" in board
    for detail in ("Came back from review", "Preview built", "Was published"):
        assert board.count(f">{detail}</span>") == 1
    # The warn tone moved with the signal.
    assert 'lat-badge--warn">Came back from review</span>' in board
    assert board.count(">Draft</span>") == 3
    assert board.count(">In review</span>") == 1


def test_a_preview_does_not_erase_that_a_reviewer_sent_it_back(
    client: TestClient, services: Services
) -> None:
    # Issue #37: Preview on a revision_requested draft stages a revise, which
    # used to clear `Needs revision` and with it the reviewer's request.
    draft_id = make_draft(services, "in_review", title="Reviewed")
    client.post(
        f"/content/drafts/{draft_id}/actions/request_revision", data={"feedback": "tighten it"}
    )
    assert services.store.get_draft(draft_id).status == "revision_requested"
    assert ">Came back from review</span>" in client.get("/content/drafts").text

    previewed = client.post(f"/content/drafts/{draft_id}/actions/preview")
    assert previewed.status_code == 200
    assert services.store.get_draft(draft_id).status == "drafting"
    board = client.get("/content/drafts").text
    assert ">Came back from review</span>" in board
    editor = client.get(f"/content/drafts/{draft_id}").text
    assert '<span id="status-detail" data-refresh>' in editor
    assert ">Came back from review</span>" in editor

    # Resubmitting answers it: the draft is In review and the board says so.
    client.post(f"/content/drafts/{draft_id}/actions/submit")
    assert services.store.get_draft(draft_id).status == "in_review"
    board = client.get("/content/drafts").text
    assert "Came back from review" not in board
    assert ">In review</span>" in board


def test_a_rejected_then_restored_draft_did_not_come_back(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "in_review")
    client.post(f"/content/drafts/{draft_id}/actions/reject", data={"feedback": "no"})
    client.post(f"/content/drafts/{draft_id}/actions/restore")
    assert services.store.get_draft(draft_id).status == "drafting"
    assert "Came back from review" not in client.get("/content/drafts").text


def test_a_revised_published_post_does_not_show_a_stale_request(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "drafting", with_publish=True)
    _feedback(services, draft_id, "request_revision")
    assert "Came back from review" not in client.get("/content/drafts").text


def test_editor_details_come_from_what_the_page_already_knows(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "previewed")
    editor = client.get(f"/content/drafts/{draft_id}").text
    # `make_draft` records no preview run, so there is no current preview.
    assert ">Preview out of date</span>" in editor
    assert re.search(r'id="status-pill"[^>]*>Draft</span>', editor)
    published = make_draft(services, "approved", with_publish=True)
    editor = client.get(f"/content/drafts/{published}").text
    assert ">In review</span>" in editor
    assert ">Approved</span>" in editor


# --- The filter ------------------------------------------------------------


def test_the_filter_lists_four_words_that_map_onto_all_eight_values() -> None:
    options = filter_options()
    assert [text for _value, text in options] == list(FOUR_WORDS)
    assert len({text for _value, text in options}) == 4
    covered = [status for value, _text in options for status in parse_status_filter(value)]
    assert sorted(covered) == sorted(DRAFT_STATUSES)
    values = dict((text, value) for value, text in options)
    assert values["Published"] == "published"
    assert values["Rejected"] == "rejected"
    assert parse_status_filter(values["In review"]) == ["in_review", "approved"]
    assert parse_status_filter("previewed") == ["previewed"]
    assert parse_status_filter(None) == []


def test_board_filter_has_each_word_once_and_no_raw_status_text(
    client: TestClient, services: Services
) -> None:
    board = client.get("/content/drafts").text
    select = board[board.index('<select class="lat-select" id="status"') :]
    select = select[: select.index("</select>")]
    texts = re.findall(r">([^<]+)</option>", select)
    assert texts == ["all", "Draft", "In review", "Published", "Rejected"]
    assert '<option value="published">Published</option>' in select
    assert '<option value="rejected">Rejected</option>' in select
    draft_value = dict((text, value) for value, text in filter_options())["Draft"]
    assert sorted(draft_value.split(",")) == sorted(
        ["drafting", "previewed", "revision_requested", "unpublished"]
    )
    assert f'<option value="{draft_value}">Draft</option>' in select


def test_a_word_filter_finds_every_status_it_covers(client: TestClient, services: Services) -> None:
    make_draft(services, "drafting", title="Alpha")
    make_draft(services, "previewed", title="Bravo")
    make_draft(services, "revision_requested", title="Charlie")
    make_draft(services, "unpublished", title="Delta", with_publish=True)
    make_draft(services, "in_review", title="Echo")
    make_draft(services, "approved", title="Foxtrot")
    make_draft(services, "rejected", title="Golf")

    draft_value = dict((text, value) for value, text in filter_options())["Draft"]
    found = client.get("/content/drafts", params={"status": draft_value}).text
    for shown in ("Alpha", "Bravo", "Charlie", "Delta"):
        assert shown in found
    for hidden in ("Echo", "Foxtrot", "Golf"):
        assert hidden not in found
    assert f'<option value="{draft_value}" selected>Draft</option>' in found

    review = client.get("/content/drafts", params={"status": "in_review,approved"}).text
    assert "Echo" in review and "Foxtrot" in review and "Alpha" not in review


def test_a_raw_status_query_value_still_works_and_is_not_mislabelled(
    client: TestClient, services: Services
) -> None:
    make_draft(services, "previewed", title="Bravo")
    make_draft(services, "drafting", title="Alpha")
    found = client.get("/content/drafts?status=previewed").text
    assert "Bravo" in found and "Alpha" not in found
    # It is not one of the four options, so it is not shown as the wider word.
    assert '<option value="previewed" selected>previewed</option>' in found
    assert "selected>Draft<" not in found
    raw_review = client.get("/content/drafts?status=in_review")
    assert raw_review.status_code == 200
    published = client.get("/content/drafts?status=published").text
    assert '<option value="published" selected>Published</option>' in published


def test_admin_status_counts_are_summed_under_the_four_words(
    admin_client: TestClient, services: Services
) -> None:
    for status in ("drafting", "previewed", "revision_requested", "in_review", "approved"):
        make_draft(services, status)
    assert label_counts({"drafting": 1, "previewed": 2, "in_review": 1, "approved": 1}) == {
        "Draft": 3,
        "In review": 2,
        "Published": 0,
        "Rejected": 0,
    }
    page = admin_client.get("/admin").text
    assert page.count("<td>Draft</td>") == 1
    assert page.count("<td>In review</td>") == 1
    assert "<td>Previewed</td>" not in page and "<td>Needs revision</td>" not in page


def test_the_api_still_speaks_the_raw_status_values(
    client: TestClient, services: Services, agent_token: str
) -> None:
    draft_id = make_draft(services, "in_review")
    response = client.get(f"/v1/drafts/{draft_id}", headers=auth(agent_token))
    assert response.status_code == 200
    assert response.json()["status"] == "in_review"


def test_board_labels_render_and_filter_values_stay_raw(
    client: TestClient, services: Services
) -> None:
    make_draft(services, "revision_requested", title="Needs work")
    board = client.get("/content/drafts")
    assert ">Draft</span>" in board.text
    assert "status: revision_requested" not in board.text
    # The filter's option values are the API's, only the text is friendly.
    assert '<option value="published">Published</option>' in board.text
    assert "Needs revision" not in board.text

    filtered = client.get("/content/drafts?status=revision_requested")
    assert "Needs work" in filtered.text


def test_editor_pill_uses_the_label(client: TestClient, services: Services) -> None:
    draft_id = make_draft(services, "revision_requested")
    editor = client.get(f"/content/drafts/{draft_id}")
    assert re.search(r'id="status-pill"[^>]*>Draft</span>', editor.text)
    assert ">Came back from review</span>" in editor.text
    assert ">revision_requested<" not in editor.text


def test_admin_status_page_uses_labels(admin_client: TestClient, services: Services) -> None:
    make_draft(services, "in_review")
    response = admin_client.get("/admin")
    assert response.status_code == 200
    assert "In review" in response.text
    assert "<td>in_review</td>" not in response.text
