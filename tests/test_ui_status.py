"""The one status-label helper, held against the state machine.

The labels are render layer only: the API's status values, the stored
records, and the board's filter query values keep the state machine's own
strings.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from chronicle.api.deps import Services
from chronicle.api.models import DRAFT_STATUSES
from chronicle.api.ui_status import STATUS_LABELS, status_label

from .conftest import auth
from .test_ui import make_draft

EXPECTED = {
    "drafting": "Draft",
    "in_review": "In review",
    "revision_requested": "Needs revision",
    "previewed": "Previewed",
    "approved": "Approved",
    "published": "Published",
    "unpublished": "Unpublished",
    "rejected": "Rejected",
}


def test_every_status_has_a_label_and_no_label_is_a_raw_status() -> None:
    # A status added to the state machine fails here until it is given a label.
    assert set(STATUS_LABELS) == set(DRAFT_STATUSES)
    for status in DRAFT_STATUSES:
        label = status_label(status)
        assert label == EXPECTED[status]
        assert "_" not in label
        assert label != status


def test_expected_table_covers_the_whole_state_machine() -> None:
    assert set(EXPECTED) == set(DRAFT_STATUSES)


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
    assert "Needs revision" in board.text
    assert "status: revision_requested" not in board.text
    # The filter's option values are the API's, only the text is friendly.
    assert '<option value="revision_requested">Needs revision</option>' in board.text
    assert '<option value="in_review">In review</option>' in board.text

    filtered = client.get("/content/drafts?status=revision_requested")
    assert "Needs work" in filtered.text


def test_editor_pill_uses_the_label(client: TestClient, services: Services) -> None:
    draft_id = make_draft(services, "revision_requested")
    editor = client.get(f"/content/drafts/{draft_id}")
    assert ">Needs revision</span>" in editor.text
    assert ">revision_requested<" not in editor.text


def test_admin_status_page_uses_labels(admin_client: TestClient, services: Services) -> None:
    make_draft(services, "in_review")
    response = admin_client.get("/admin")
    assert response.status_code == 200
    assert "In review" in response.text
    assert "<td>in_review</td>" not in response.text
