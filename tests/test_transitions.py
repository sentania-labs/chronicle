"""The lifecycle table itself, read as data rather than exercised by route."""

from __future__ import annotations

import pytest

from chronicle.api.errors import ApiError
from chronicle.api.models import DRAFT_STATUSES
from chronicle.api.transitions import (
    DRAFT_TRANSITIONS,
    PREVIEW_SUCCEEDED,
    RESERVED_ACTIONS,
    RUN_OUTCOME_TRANSITIONS,
    SUBMISSION_TRANSITIONS,
    UI_ACTOR,
    resolve_draft,
    resolve_save,
    resolve_submission,
)

# One action no draft in this status may take, per status in the model.
DISALLOWED = [
    ("drafting", "approve"),
    ("in_review", "restore"),
    ("revision_requested", "approve"),
    ("previewed", "reject"),
    ("approved", "submit"),
    ("published", "approve"),
    ("unpublished", "submit"),
    ("rejected", "preview"),
]


@pytest.mark.parametrize(("status", "action"), sorted(DRAFT_TRANSITIONS))
def test_every_allowed_draft_transition_resolves(status: str, action: str) -> None:
    expected = DRAFT_TRANSITIONS[(status, action)]
    resolved = resolve_draft(status, action, actor_is_ui=True)
    assert resolved is expected
    assert resolved.to_status in DRAFT_STATUSES


@pytest.mark.parametrize(("status", "action"), DISALLOWED)
def test_disallowed_draft_transitions_are_refused(status: str, action: str) -> None:
    with pytest.raises(ApiError) as caught:
        resolve_draft(status, action, actor_is_ui=True)
    assert caught.value.status_code == 409
    assert caught.value.code == "transition_not_allowed"


def test_nothing_produces_or_leaves_previewed_any_more() -> None:
    # Issue #70: a preview is a property of the draft, not a status. The
    # status stays in the model only so a legacy record loads until
    # `Store.migrate_previewed` moves it.
    assert all(transition.to_status != "previewed" for transition in DRAFT_TRANSITIONS.values())
    assert all(status != "previewed" for status, _action in DRAFT_TRANSITIONS)
    assert all(outcome != PREVIEW_SUCCEEDED for _status, outcome in RUN_OUTCOME_TRANSITIONS)


def test_every_status_in_the_model_has_a_disallowed_case_covered() -> None:
    assert {status for status, _ in DISALLOWED} == set(DRAFT_STATUSES)


@pytest.mark.parametrize("action", RESERVED_ACTIONS)
def test_reserved_actions_refuse_a_non_ui_actor(action: str) -> None:
    with pytest.raises(ApiError) as caught:
        resolve_draft("in_review", action, actor_is_ui=False)
    assert caught.value.status_code == 403
    assert caught.value.extra["action"] == action


@pytest.mark.parametrize(("status", "action"), sorted(DRAFT_TRANSITIONS))
def test_reserved_actor_rule_matches_the_table(status: str, action: str) -> None:
    reserved = DRAFT_TRANSITIONS[(status, action)].actor == UI_ACTOR
    assert reserved == (action in RESERVED_ACTIONS)


def test_save_side_effect_only_applies_to_two_statuses() -> None:
    moved = {status for status in DRAFT_STATUSES if resolve_save(status) is not None}
    assert moved == {"revision_requested", "published"}
    save = resolve_save("revision_requested")
    assert save is not None
    assert save.to_status == "drafting"


@pytest.mark.parametrize(("status", "action"), sorted(SUBMISSION_TRANSITIONS))
def test_every_allowed_submission_transition_resolves(status: str, action: str) -> None:
    assert resolve_submission(status, action) is SUBMISSION_TRANSITIONS[(status, action)]


@pytest.mark.parametrize(
    ("status", "action"),
    [("new", "draft"), ("discarded", "claim"), ("drafted", "claim"), ("claimed", "claim")],
)
def test_disallowed_submission_transitions_are_refused(status: str, action: str) -> None:
    with pytest.raises(ApiError) as caught:
        resolve_submission(status, action)
    assert caught.value.status_code == 409
