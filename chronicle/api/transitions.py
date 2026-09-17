"""The only place a status change is decided (spec section 5).

The lifecycle is a table, not a chain of conditionals in route handlers: a
transition that is not in the table cannot happen, and reading the table is
reading the lifecycle. Everything a transition implies travels with it, so
callers ask one question and get the actor rule, the feedback rule, and
whether a run is queued in the same answer.

Saving a draft is not one of the API's named actions, but it can still move
a draft's status (a save against a draft in `revision_requested` or
`published` puts it back in `drafting`). That side effect is modelled here
as the `revise` action so it is decided in exactly one place and writes an
event like every other transition.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ApiError

ANY_ACTOR = "any"
UI_ACTOR = "ui"

RESERVED_ACTIONS = ("approve", "request_revision", "reject", "restore", "unpublish")
DRAFT_ACTIONS = ("submit", "preview", *RESERVED_ACTIONS)


@dataclass(frozen=True)
class Transition:
    to_status: str
    actor: str = ANY_ACTOR
    feedback_required: bool = False
    run_kind: str | None = None


DRAFT_TRANSITIONS: dict[tuple[str, str], Transition] = {
    ("drafting", "submit"): Transition("in_review"),
    ("previewed", "submit"): Transition("in_review"),
    # Asking for a preview queues a build and changes nothing else: a draft is
    # `previewed` when a preview exists, which is the builder's answer on a
    # succeeded run (RUN_OUTCOME_TRANSITIONS below), not the API's answer at
    # enqueue time. A failed build therefore leaves the draft where it was.
    ("drafting", "preview"): Transition("drafting", run_kind="preview"),
    ("in_review", "preview"): Transition("in_review", run_kind="preview"),
    ("previewed", "preview"): Transition("previewed", run_kind="preview"),
    ("in_review", "approve"): Transition("approved", actor=UI_ACTOR, run_kind="publish"),
    ("in_review", "request_revision"): Transition(
        "revision_requested", actor=UI_ACTOR, feedback_required=True
    ),
    ("in_review", "reject"): Transition("rejected", actor=UI_ACTOR, feedback_required=True),
    ("rejected", "restore"): Transition("drafting", actor=UI_ACTOR),
    ("unpublished", "restore"): Transition("drafting", actor=UI_ACTOR),
    # `unpublished` is set only on an observed merge, which arrives with the
    # GitHub client, so this round's unpublish queues the run and leaves the
    # status where it is.
    ("published", "unpublish"): Transition("published", actor=UI_ACTOR, run_kind="unpublish"),
    ("revision_requested", "revise"): Transition("drafting"),
    ("published", "revise"): Transition("drafting"),
}

# What a finished run does to the draft it was queued for. Separate from
# DRAFT_TRANSITIONS because the actor is the builder, not a consumer: no token
# check applies, and an outcome that has no entry for the draft's current
# status leaves the status alone instead of raising. A draft that was
# rejected, or saved back into `drafting`, while its build ran must not be
# dragged into `previewed` by a build that finished afterwards.
PREVIEW_SUCCEEDED = "preview_succeeded"

RUN_OUTCOME_TRANSITIONS: dict[tuple[str, str], Transition] = {
    ("drafting", PREVIEW_SUCCEEDED): Transition("previewed"),
    ("in_review", PREVIEW_SUCCEEDED): Transition("previewed"),
    ("previewed", PREVIEW_SUCCEEDED): Transition("previewed"),
}

# What an observed PR outcome does to the draft that opened it (spec section
# 9's merge watch and close-without-merge). Keyed by (from_status, event), not
# by run kind: `approve` only ever opens a PR from `approved`, and `unpublish`
# only ever opens one from `published`, so the two publish/unpublish cases
# never collide on the same from_status even though both events are named
# "merged". Same shape as RUN_OUTCOME_TRANSITIONS: no actor check (the
# watcher is not a consumer token), and a status this table has no entry for
# is left exactly as it is (a draft revised or rejected again while its PR
# was still open must not be dragged back by a merge the watcher only now
# noticed).
WATCH_TRANSITIONS: dict[tuple[str, str], Transition] = {
    ("approved", "merged"): Transition("published"),
    ("published", "merged"): Transition("unpublished"),
    ("approved", "closed"): Transition("in_review"),
    ("published", "closed"): Transition("in_review"),
}

# A reconciliation resolution (spec section 12) is an explicit admin
# override of a data-integrity mismatch, not a normal action gated by the
# draft's current status, so it names the resulting status directly rather
# than keying off (from_status, action) the way DRAFT_TRANSITIONS does.
# `import_as_draft` and `ignore` are not status changes at all: the first
# creates a new draft (Store.create_draft), the second touches nothing.
RECONCILE_STATUS: dict[str, str] = {
    "mark_published": "published",
    "mark_unpublished": "unpublished",
}

SUBMISSION_TRANSITIONS: dict[tuple[str, str], Transition] = {
    ("new", "claim"): Transition("claimed"),
    ("new", "discard"): Transition("discarded"),
    ("claimed", "discard"): Transition("discarded"),
    ("claimed", "draft"): Transition("drafted"),
}


def resolve_draft(status: str, action: str, actor_is_ui: bool) -> Transition:
    if action in RESERVED_ACTIONS and not actor_is_ui:
        raise ApiError(
            403,
            "action_reserved",
            f"action {action!r} is reserved for the ui token",
            action=action,
        )
    transition = DRAFT_TRANSITIONS.get((status, action))
    if transition is None:
        raise ApiError(
            409,
            "transition_not_allowed",
            f"a draft in status {status!r} cannot {action}",
            status=status,
            action=action,
        )
    return transition


def resolve_submission(status: str, action: str) -> Transition:
    transition = SUBMISSION_TRANSITIONS.get((status, action))
    if transition is None:
        raise ApiError(
            409,
            "transition_not_allowed",
            f"a submission in status {status!r} cannot {action}",
            status=status,
            action=action,
        )
    return transition


def resolve_run_outcome(status: str, outcome: str) -> Transition | None:
    """The status change a finished run implies, or None to leave it alone."""
    return RUN_OUTCOME_TRANSITIONS.get((status, outcome))


def resolve_watch(status: str, event: str) -> Transition | None:
    """The status change an observed PR outcome implies, or None to leave it alone."""
    return WATCH_TRANSITIONS.get((status, event))


def resolve_save(status: str) -> Transition | None:
    """The status change a save implies, or None when a save leaves it alone."""
    return DRAFT_TRANSITIONS.get((status, "revise"))
