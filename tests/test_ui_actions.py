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
from chronicle.api.models import DRAFT_STATUSES, WatchEntry
from chronicle.api.transitions import DRAFT_TRANSITIONS, plan_action, resolve_draft
from chronicle.api.ui_actions import AVAILABLE, DISABLED, Offer, offers_for, staged_refusal

from .test_ui import make_draft


def build_preview(services: Services, draft_id: str) -> None:
    """A succeeded preview run of the draft's current version."""
    store = services.store
    draft = store.get_draft(draft_id)
    run = store._queue_run(draft_id, "preview")
    store.index.upsert_run(run)
    store.start_run(run.id, "builder-1", "0.164.0", False, built_version=draft.version_no)
    store.finish_run(run.id, "builder-1", True, {"preview_url": f"/preview/{draft.slug}/"})


def preview_in_review(services: Services, draft_id: str) -> None:
    """An `in_review` draft whose current text has a built preview (a build
    never changes a status, issue #70, so it stays in review)."""
    assert services.store.get_draft(draft_id).status == "in_review"
    build_preview(services, draft_id)
    assert services.store.get_draft(draft_id).status == "in_review"


def open_watch(services: Services, draft_id: str, kind: str) -> None:
    services.store.record_watch(
        WatchEntry(
            draft_id=draft_id,
            kind=kind,
            branch="post/a-draft",
            pr_number=7,
            pr_url="https://github.com/o/r/pull/7",
            created_at="2026-09-17T00:00:00-05:00",
        ),
        "scott",
    )


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
    # `previewed` is not a working status any more (issue #70): it is never
    # produced, and `Store.migrate_previewed` moves a legacy record off it.
    for status in ("drafting", "in_review", "revision_requested", "published"):
        assert by_action(offers_for(status, has_preview=True))["preview"].state == AVAILABLE


@pytest.mark.parametrize("status", ["drafting", "in_review", "revision_requested", "published"])
def test_publish_is_disabled_with_preview_first_until_a_preview_exists(status: str) -> None:
    publish = by_action(offers_for(status, has_preview=False))["approve"]
    assert publish.state == DISABLED
    assert publish.reason == "Preview first"
    assert not publish.primary


def test_publish_becomes_the_primary_action_once_a_preview_exists() -> None:
    offers = by_action(offers_for("drafting", has_preview=True))
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
    assert "approve" not in by_action(
        offers_for("approved", has_preview=True, publish_pr_open=True)
    )
    assert "approve" not in by_action(
        offers_for("approved", has_preview=True, publish_run_active=True)
    )


def test_republish_label_only_for_a_draft_with_a_published_record() -> None:
    assert by_action(offers_for("drafting", has_preview=True))["approve"].label == "Publish"
    assert (
        by_action(offers_for("drafting", has_preview=True, republish=True))["approve"].label
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
    assert plan_action("drafting", "approve", False) is None
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
    assert re.search(r'<button type="button" class="lat-btn offer-approve" disabled', html)
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


def test_clicking_publish_on_a_drafting_post_with_a_current_preview_submits_then_approves(
    client: TestClient, services: Services
) -> None:
    # Issue #70: the preview leaves the draft at `drafting`, so Publish stages
    # submit then approve, each a recorded status change.
    draft_id = make_draft(services, "drafting", title="Staged publish")
    build_preview(services, draft_id)
    assert services.store.get_draft(draft_id).status == "drafting"
    response = client.post(f"/content/drafts/{draft_id}/actions/approve")
    assert response.status_code == 200
    assert services.store.get_draft(draft_id).status == "approved"
    run = services.store.last_run(draft_id, kind="publish")
    assert run is not None and run.status == "queued"
    events, _cursor = services.store.events_since(0)
    path = [
        (e.from_status, e.to_status)
        for e in events
        if e.draft_id == draft_id and e.type in ("draft.submit", "draft.approve")
    ]
    assert path == [("drafting", "in_review"), ("in_review", "approved")]


def test_a_refused_click_writes_nothing(client: TestClient, services: Services) -> None:
    draft_id = make_draft(services, "rejected")
    before = services.store.get_draft(draft_id).version_no
    response = client.post(f"/content/drafts/{draft_id}/actions/preview")
    assert response.status_code == 409
    draft = services.store.get_draft(draft_id)
    assert draft.status == "rejected" and draft.version_no == before
    assert services.store.last_run(draft_id, kind="preview") is None


def test_a_staged_publish_with_no_current_preview_is_refused_and_writes_nothing(
    client: TestClient, services: Services
) -> None:
    # The editor renders Publish disabled ("Preview first"); the route the
    # button posts to must agree, or a same-origin POST publishes what the
    # page said it would not (this UI has no login, so the offer is the guard).
    draft_id = make_draft(services, "drafting")
    version = services.store.get_draft(draft_id).version_no
    refused = client.post(f"/content/drafts/{draft_id}/actions/approve")
    assert refused.status_code == 409
    assert "Preview first" in refused.text
    draft = services.store.get_draft(draft_id)
    assert draft.status == "drafting" and draft.version_no == version
    assert services.store.last_run(draft_id, kind="publish") is None
    events, _cursor = services.store.events_since(0)
    assert not [e for e in events if e.type == "draft.submit" and e.draft_id == draft_id]


def test_a_staged_publish_on_a_stale_preview_is_refused(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "drafting")
    build_preview(services, draft_id)
    draft = services.store.get_draft(draft_id)
    services.store.save_draft(draft_id, "scott", draft.version_no, draft.frontmatter, "edited")
    refused = client.post(f"/content/drafts/{draft_id}/actions/approve")
    assert refused.status_code == 409
    assert services.store.get_draft(draft_id).status == "drafting"


def test_a_staged_publish_with_a_current_preview_still_runs_both_steps(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "drafting")
    build_preview(services, draft_id)
    response = client.post(f"/content/drafts/{draft_id}/actions/approve")
    assert response.status_code == 200
    assert services.store.get_draft(draft_id).status == "approved"


def test_staging_a_preview_still_works(client: TestClient, services: Services) -> None:
    for status in ("published", "revision_requested"):
        draft_id = make_draft(services, status, with_publish=status == "published")
        response = client.post(f"/content/drafts/{draft_id}/actions/preview")
        assert response.status_code == 200, status
        assert services.store.get_draft(draft_id).status == "drafting"


def test_an_action_with_no_path_is_still_refused_untouched(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "drafting")
    refused = client.post(f"/content/drafts/{draft_id}/actions/reject", data={"feedback": "no"})
    assert refused.status_code == 409
    assert services.store.get_draft(draft_id).status == "drafting"


def test_the_route_and_the_page_read_one_offer(client: TestClient, services: Services) -> None:
    # Whatever the page renders disabled, a staged POST of that action refuses,
    # for every status and both preview states.
    for status in DRAFT_STATUSES:
        for with_preview in (False, True):
            for action in ("approve", "preview"):
                # A fresh draft per click, with its own title: a click that goes
                # through changes the status and pins a slug, and two drafts may
                # not pin the same one.
                draft_id = make_draft(
                    services, status, title=f"Draft {status} {with_preview} {action}"
                )
                if with_preview:
                    build_preview(services, draft_id)  # never moves the status
                actual = services.store.get_draft(draft_id).status
                if plan_action(actual, action, True) is None:
                    continue
                html = client.get(f"/content/drafts/{draft_id}").text
                offered = f"/content/drafts/{draft_id}/actions/{action}" in html
                response = client.post(f"/content/drafts/{draft_id}/actions/{action}")
                assert (response.status_code == 200) == offered, (status, action, with_preview)


# --- The one-step publish is held to the offer too -----------------------------


def test_a_one_step_publish_with_no_current_preview_is_refused_and_writes_nothing(
    client: TestClient, services: Services
) -> None:
    # `in_review` -> `approve` is a single step, so it never staged, and the
    # offer check used to skip it: a POST published what the page rendered
    # disabled ("Preview first").
    draft_id = make_draft(services, "in_review")
    assert plan_action("in_review", "approve", True) == ("approve",)
    html = client.get(f"/content/drafts/{draft_id}").text
    assert "Preview first" in html and f"/content/drafts/{draft_id}/actions/approve" not in html
    version = services.store.get_draft(draft_id).version_no
    refused = client.post(f"/content/drafts/{draft_id}/actions/approve")
    assert refused.status_code == 409
    assert "Preview first" in refused.text
    draft = services.store.get_draft(draft_id)
    assert draft.status == "in_review" and draft.version_no == version
    assert services.store.last_run(draft_id, kind="publish") is None


def test_a_one_step_publish_on_a_stale_preview_is_refused(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "in_review")
    preview_in_review(services, draft_id)
    draft = services.store.get_draft(draft_id)
    services.store.save_draft(draft_id, "scott", draft.version_no, draft.frontmatter, "edited")
    assert services.store.get_draft(draft_id).status == "in_review"
    refused = client.post(f"/content/drafts/{draft_id}/actions/approve")
    assert refused.status_code == 409
    assert services.store.last_run(draft_id, kind="publish") is None


def test_a_one_step_publish_with_a_current_preview_still_runs(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "in_review")
    preview_in_review(services, draft_id)
    response = client.post(f"/content/drafts/{draft_id}/actions/approve")
    assert response.status_code == 200
    assert services.store.get_draft(draft_id).status == "approved"
    assert services.store.last_run(draft_id, kind="publish") is not None


def test_a_publish_retry_on_an_approved_post_still_needs_no_preview(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "approved", with_publish=True)
    response = client.post(f"/content/drafts/{draft_id}/actions/approve")
    assert response.status_code == 200


# --- The offer check runs under the store lock -----------------------------------


@pytest.mark.parametrize("status", ["drafting", "in_review"])
def test_a_save_landing_after_the_click_is_read_cannot_publish_an_unpreviewed_version(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """The window the review found: the route checked the offer, then took the
    store lock, and a save landing in between was published unpreviewed.

    The save is made to land at exactly that point, between the route's call
    into the store and the store taking its lock (the only place a writer could
    always slip in), by wrapping `act_on_draft_staged` on the instance. With the
    offer checked before the call, the click saw preview N current and then ran
    against version N+1; checked under the lock it sees version N+1 and refuses.
    The wrapper saves before the lock is held, so it cannot deadlock the guard
    that now runs inside it."""
    # The route's store is the app's own, not the fixture's: patch that one.
    store = client.app.state.services.store  # type: ignore[attr-defined]
    draft_id = make_draft(services, status)
    build_preview(services, draft_id)
    assert store.get_draft(draft_id).status == status
    version = store.get_draft(draft_id).version_no
    real = store.act_on_draft_staged

    def save_then_act(*args: object, **kwargs: object) -> object:
        current = store.get_draft(draft_id)
        store.save_draft(draft_id, "ghostwriter", current.version_no, current.frontmatter, "raced")
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "act_on_draft_staged", save_then_act)
    refused = client.post(f"/content/drafts/{draft_id}/actions/approve")
    assert refused.status_code == 409
    draft = store.get_draft(draft_id)
    assert draft.version_no == version + 1 and draft.status == status
    assert store.last_run(draft_id, kind="publish") is None
    events, _cursor = store.events_since(0)
    assert not [e for e in events if e.type == "draft.approve" and e.draft_id == draft_id]


def test_the_guard_runs_under_the_store_lock(services: Services) -> None:
    store = services.store
    draft_id = make_draft(services, "in_review")
    held: list[bool] = []

    def guard(_draft: object) -> str | None:
        held.append(store._lock._thread_lock.locked())
        return "no"

    from chronicle.api.errors import ApiError

    with pytest.raises(ApiError) as caught:
        store.act_on_draft_staged(draft_id, "approve", "scott", True, guard=guard)
    assert caught.value.status_code == 409 and caught.value.code == "offer_unavailable"
    assert held == [True]
    assert store.get_draft(draft_id).status == "in_review"


# --- An open unpublish PR blocks the staged Preview ---------------------------


def test_preview_is_disabled_when_it_would_revise_under_an_open_unpublish_pr() -> None:
    preview = by_action(offers_for("published", has_preview=False, unpublish_pr_open=True))[
        "preview"
    ]
    assert preview.state == DISABLED
    assert preview.reason == "Unpublish PR open"
    assert preview.steps == ()
    # No staging needed, nothing to protect: a draft under review still previews.
    assert (
        by_action(offers_for("in_review", has_preview=False, unpublish_pr_open=True))[
            "preview"
        ].state
        == AVAILABLE
    )


def test_a_staged_preview_on_a_post_with_an_open_unpublish_pr_is_refused(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "published", with_publish=True)
    open_watch(services, draft_id, "unpublish")
    html = client.get(f"/content/drafts/{draft_id}").text
    assert "/actions/preview" not in html and "Unpublish PR open" in html
    refused = client.post(f"/content/drafts/{draft_id}/actions/preview")
    assert refused.status_code == 409
    assert services.store.get_draft(draft_id).status == "published"
    assert services.store.last_run(draft_id, kind="preview") is None


def test_staged_refusal_is_none_for_an_action_the_page_does_not_disable() -> None:
    assert staged_refusal("in_review", "approve", True, has_preview=True) is None
    assert staged_refusal("drafting", "reject", True, has_preview=False) is None
    # No offer at all for a one-step click (an open publish PR): the store's own
    # 409, with its own code, stays the answer.
    assert (
        staged_refusal("approved", "approve", True, has_preview=False, publish_pr_open=True) is None
    )
    assert staged_refusal("in_review", "approve", True, has_preview=False) == (
        "Publish is not available right now (Preview first)."
    )
    assert staged_refusal("drafting", "approve", True, has_preview=False) == (
        "Publish is not available right now (Preview first)."
    )
