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
    # Asking for a preview queues a build and changes nothing else, and so
    # does the build finishing (issue #70): having a preview is a property of
    # the draft (`store.preview_is_current`), not a stage of its lifecycle.
    # `previewed` is no longer produced; `Store.migrate_previewed` moves any
    # record still carrying it back to the status it had before the build.
    ("drafting", "preview"): Transition("drafting", run_kind="preview"),
    ("in_review", "preview"): Transition("in_review", run_kind="preview"),
    ("in_review", "approve"): Transition("approved", actor=UI_ACTOR, run_kind="publish"),
    # A re-approve: the previous publish run failed (see PUBLISH_RUN_FAILED
    # below) or the draft is simply approved again with no publish PR open
    # and no run in flight. `Store.act_on_draft` enforces the "no open PR,
    # no run in flight" half of that (not expressible as a status-keyed
    # table lookup), this table only says the status itself allows it.
    ("approved", "approve"): Transition("approved", actor=UI_ACTOR, run_kind="publish"),
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
# moved by a build that finished afterwards. A succeeded preview has no entry
# at all: it never changes a status (issue #70).
PREVIEW_SUCCEEDED = "preview_succeeded"
# A publish run failed after `approve` already moved the draft to `approved`
# (a transient GitHub error, a conversion failure): this is what makes the
# draft retryable again, since `approved` otherwise has no outgoing action
# left once its one publish run has failed (round C4 review, P1).
PUBLISH_RUN_FAILED = "publish_failed"

RUN_OUTCOME_TRANSITIONS: dict[tuple[str, str], Transition] = {
    ("approved", PUBLISH_RUN_FAILED): Transition("in_review"),
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
    # A revision of the submission's own content (brief, materials, images).
    # It changes no status: like a draft save it is an action so that the
    # frozen states (`drafted`, `discarded`) are decided here, in the table,
    # and not in a route handler.
    ("new", "revise"): Transition("new"),
    ("claimed", "revise"): Transition("claimed"),
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


# The actions a click may run first, per requested action, so that the click
# is legal from where the draft actually is. This is what lets the editor
# offer Preview on a published post without a second lifecycle table: the
# steps are found by walking DRAFT_TRANSITIONS, and only through the actions
# named here. Anything not named stays an explicit, separate decision
# (`restore` of a rejected post, a `revise` on the way to publish).
#
# - preview may `revise`: that is exactly what a save already does to a
#   `published` or `revision_requested` draft, so asking for a preview is a
#   save without the edit.
# - approve may `submit`: the UI token is the reviewer, so a draft with a
#   current preview goes to review and is approved in one click, each step
#   still a recorded event.
STAGING_ACTIONS: dict[str, tuple[str, ...]] = {
    "preview": ("revise",),
    "approve": ("submit",),
}


def plan_action(status: str, action: str, actor_is_ui: bool) -> tuple[str, ...] | None:
    """The actions to run, in order, for `action` to happen from `status`.

    `(action,)` when it is legal as it stands, a longer tuple ending in
    `action` when only staging steps (STAGING_ACTIONS) stand in the way, and
    None when there is no such path. Every step is checked against
    DRAFT_TRANSITIONS, including the reserved-actor rule, so a plan is a
    sequence `resolve_draft` will accept one step at a time; an offer built
    from it can never be one the table would refuse.
    """
    if action in RESERVED_ACTIONS and not actor_is_ui:
        return None
    allowed = STAGING_ACTIONS.get(action, ())
    frontier: list[tuple[str, tuple[str, ...]]] = [(status, ())]
    seen = {status}
    while frontier:
        current, steps = frontier.pop(0)
        if (current, action) in DRAFT_TRANSITIONS:
            return (*steps, action)
        for stage in allowed:
            transition = DRAFT_TRANSITIONS.get((current, stage))
            if transition is None or transition.feedback_required or transition.run_kind:
                continue
            if stage in RESERVED_ACTIONS and not actor_is_ui:
                continue
            if transition.to_status in seen:
                continue
            seen.add(transition.to_status)
            frontier.append((transition.to_status, (*steps, stage)))
    return None


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


def resolve_submission_revise(status: str, submission_id: str) -> Transition:
    """A submission is editable while `new` or `claimed`; after that its
    content belongs to the draft made from it (or is gone with the discard)."""
    transition = SUBMISSION_TRANSITIONS.get((status, "revise"))
    if transition is None:
        raise ApiError(
            409,
            "submission_frozen",
            f"submission {submission_id} is {status} and can no longer be edited:"
            " once a draft exists from a submission, edit the draft instead",
            status=status,
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
