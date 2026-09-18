"""What the editor offers per status: available, disabled, or absent.

`transitions.py` decides what is legal; `ui_actions.offers_for` only presents
it. The invariant this file holds is that nothing rendered as available is
something `resolve_draft` would refuse, however many staging steps the click
runs.
"""

from __future__ import annotations

import itertools
import re

import pytest
from fastapi.testclient import TestClient

from chronicle.api.deps import Services
from chronicle.api.models import DRAFT_STATUSES
from chronicle.api.transitions import DRAFT_TRANSITIONS, plan_action, resolve_draft
from chronicle.api.ui_actions import AVAILABLE, DISABLED, Offer, offers_for

from .test_ui import make_draft


def by_action(offers: list[Offer]) -> dict[str, Offer]:
    return {offer.action: offer for offer in offers}


COMBINATIONS = list(
    itertools.product(DRAFT_STATUSES, (False, True), (False, True), (False, True), (False, True))
)


@pytest.mark.parametrize(
    "status,has_preview,republish,pr_open,run_active",
    COMBINATIONS,
)
def test_no_available_offer_is_one_the_table_would_refuse(
    status: str, has_preview: bool, republish: bool, pr_open: bool, run_active: bool
) -> None:
    offers = offers_for(
        status,
        has_preview=has_preview,
        republish=republish,
        publish_pr_open=pr_open,
        publish_run_active=run_active,
    )
    for offer in offers:
        if offer.state == DISABLED:
            assert offer.reason, f"{offer.action} is disabled with no reason given"
            assert offer.steps == ()
            continue
        assert offer.state == AVAILABLE
        assert offer.steps and offer.steps[-1] == offer.action
        current = status
        for step in offer.steps:
            transition = resolve_draft(current, step, True)  # raises if refused
            current = transition.to_status


def test_every_status_has_exactly_one_preview_offer_and_no_bare_revise() -> None:
    for status in DRAFT_STATUSES:
        offers = offers_for(status, has_preview=False)
        assert [o.action for o in offers].count("preview") == 1
        assert "revise" not in [o.action for o in offers]


def test_preview_is_present_on_published_and_stages_a_revise() -> None:
    preview = by_action(offers_for("published", has_preview=False))["preview"]
    assert preview.state == AVAILABLE
    assert preview.steps == ("revise", "preview")
    assert preview.primary


def test_preview_is_present_from_every_working_status() -> None:
    for status in ("drafting", "in_review", "revision_requested", "previewed", "published"):
        assert by_action(offers_for(status, has_preview=True))["preview"].state == AVAILABLE


@pytest.mark.parametrize("status", ["drafting", "in_review", "revision_requested", "published"])
def test_publish_is_disabled_with_preview_first_until_a_preview_exists(status: str) -> None:
    publish = by_action(offers_for(status, has_preview=False))["approve"]
    assert publish.state == DISABLED
    assert publish.reason == "Preview first"
    assert not publish.primary


def test_publish_becomes_the_primary_action_once_a_preview_exists() -> None:
    offers = by_action(offers_for("previewed", has_preview=True))
    assert offers["approve"].state == AVAILABLE
    assert offers["approve"].primary
    assert offers["approve"].steps == ("submit", "approve")
    assert not offers["preview"].primary  # a preview exists: it is secondary now

    in_review = by_action(offers_for("in_review", has_preview=True))
    assert in_review["approve"].state == AVAILABLE
    assert in_review["approve"].steps == ("approve",)


def test_a_published_post_with_a_stale_status_cannot_publish_without_an_edit() -> None:
    # published -> approve has no path in the table: the way in is Preview,
    # which stages the revise. Publish must not render available.
    assert plan_action("published", "approve", True) is None
    publish = by_action(offers_for("published", has_preview=True))["approve"]
    assert publish.state == DISABLED


def test_approved_keeps_a_retryable_publish_and_a_disabled_preview() -> None:
    offers = by_action(offers_for("approved", has_preview=False))
    assert offers["approve"].state == AVAILABLE
    assert offers["preview"].state == DISABLED
    assert offers["preview"].reason == "Already approved"


@pytest.mark.parametrize("status", ["rejected", "unpublished"])
def test_rejected_and_unpublished_offer_restore_not_publish(status: str) -> None:
    offers = by_action(offers_for(status, has_preview=True))
    assert "approve" not in offers
    assert offers["restore"].state == AVAILABLE
    assert offers["preview"].state == DISABLED
    assert offers["preview"].reason == "Restore first"


def test_publish_is_absent_while_a_publish_pr_or_run_is_in_flight() -> None:
    assert "approve" not in by_action(offers_for("approved", has_preview=True, publish_pr_open=True))
    assert "approve" not in by_action(
        offers_for("approved", has_preview=True, publish_run_active=True)
    )


def test_republish_label_only_for_a_draft_with_a_published_record() -> None:
    assert by_action(offers_for("previewed", has_preview=True))["approve"].label == "Publish"
    assert (
        by_action(offers_for("previewed", has_preview=True, republish=True))["approve"].label
        == "Republish"
    )


def test_feedback_actions_and_reserved_flags_come_from_the_table() -> None:
    offers = by_action(offers_for("in_review", has_preview=False))
    assert offers["request_revision"].feedback_required
    assert offers["reject"].feedback_required and offers["reject"].reserved
    assert not offers["submit"].reserved if "submit" in offers else True
    for (from_status, action), transition in DRAFT_TRANSITIONS.items():
        if from_status == "in_review" and action in offers:
            assert offers[action].feedback_required == transition.feedback_required


def test_plan_action_refuses_what_staging_cannot_reach() -> None:
    assert plan_action("rejected", "preview", True) is None
    assert plan_action("approved", "preview", True) is None
    assert plan_action("drafting", "approve", True) == ("submit", "approve")
    assert plan_action("revision_requested", "approve", True) is None
    assert plan_action("drafting", "reject", True) is None
    # A reserved action is never planned for a non-UI actor, staged or not.
    assert plan_action("previewed", "approve", False) is None
    assert plan_action("in_review", "approve", False) is None


# --- Rendered ---------------------------------------------------------------


def _action_urls(html: str) -> set[str]:
    return set(re.findall(r'action="/content/drafts/[^"/]+/actions/([a-z_]+)"', html))


def test_editor_renders_preview_on_published_and_publish_disabled(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "published", with_publish=True)
    html = client.get(f"/content/drafts/{draft_id}").text
    assert "preview" in _action_urls(html)
    assert "approve" not in _action_urls(html)
    assert re.search(r'<button type="button" class="offer-approve" disabled', html)
    assert "Preview first" in html


def test_editor_renders_only_offers_the_table_allows(
    client: TestClient, services: Services
) -> None:
    for status in DRAFT_STATUSES:
        draft_id = make_draft(services, status)
        html = client.get(f"/content/drafts/{draft_id}").text
        for action in _action_urls(html):
            assert plan_action(status, action, True) is not None, (status, action)


def test_clicking_preview_on_a_published_post_revises_it_then_queues_a_preview(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "published", with_publish=True)
    response = client.post(f"/content/drafts/{draft_id}/actions/preview")
    assert response.status_code == 200
    assert services.store.get_draft(draft_id).status == "drafting"
    run = services.store.last_run(draft_id, kind="preview")
    assert run is not None and run.status == "queued"


def test_clicking_publish_on_a_previewed_post_submits_then_approves(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "previewed")
    response = client.post(f"/content/drafts/{draft_id}/actions/approve")
    assert response.status_code == 200
    assert services.store.get_draft(draft_id).status == "approved"
    run = services.store.last_run(draft_id, kind="publish")
    assert run is not None and run.status == "queued"


def test_a_refused_click_writes_nothing(client: TestClient, services: Services) -> None:
    draft_id = make_draft(services, "rejected")
    before = services.store.get_draft(draft_id).version_no
    response = client.post(f"/content/drafts/{draft_id}/actions/preview")
    assert response.status_code == 409
    draft = services.store.get_draft(draft_id)
    assert draft.status == "rejected" and draft.version_no == before
    assert services.store.last_run(draft_id, kind="preview") is None


def test_the_preview_gate_is_presentation_and_the_route_stays_a_table_lookup(
    client: TestClient, services: Services
) -> None:
    # "Preview first" disables the button; it is not a second lifecycle rule.
    # A hand-built approve from `drafting` runs the same two legal steps a
    # person could POST one at a time (submit, then approve), and an action
    # with no path at all is refused untouched.
    draft_id = make_draft(services, "drafting")
    refused = client.post(
        f"/content/drafts/{draft_id}/actions/reject", data={"feedback": "no"}
    )
    assert refused.status_code == 409
    assert services.store.get_draft(draft_id).status == "drafting"

    staged = client.post(f"/content/drafts/{draft_id}/actions/approve")
    assert staged.status_code == 200
    assert services.store.get_draft(draft_id).status == "approved"
