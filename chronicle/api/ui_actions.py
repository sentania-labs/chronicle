"""What the editor offers for a draft, and why some offers are disabled.

`transitions.py` decides what is legal. This module only decides how the
editor presents it: which actions get a button, which of those are clickable,
and what a disabled one says. Nothing here names a status or a legal move of
its own: an offer is available only if `transitions.plan_action` finds a path
for it, so an offer the table would refuse cannot be rendered as available.

Two offers are special. Preview is always present, because the click stages
whatever it needs to be legal (a published post is revised first, as a save
would). Publish is the primary action only once a preview of the current text
exists, and is disabled with "Preview first" until then.

The page and the staged action route read the same offers. `staged_refusal`
is how the route asks: a click whose plan runs more than one step is refused
when the offer for it is disabled or absent, so a POST cannot do what the
page said it would not (this UI has no login, so the offer is the guard).
"""

from __future__ import annotations

from dataclasses import dataclass

from .transitions import DRAFT_TRANSITIONS, RESERVED_ACTIONS, plan_action

AVAILABLE = "available"
DISABLED = "disabled"

# The actions rendered from their own rules below rather than as a plain
# button per table entry. `revise` is a save's side effect, never a button.
_SPECIAL = ("preview", "approve", "revise")


@dataclass(frozen=True)
class Offer:
    action: str
    label: str
    state: str = AVAILABLE
    primary: bool = False
    # Why a disabled offer is disabled; empty for an available one.
    reason: str = ""
    feedback_required: bool = False
    reserved: bool = False
    # The steps a click runs (`transitions.plan_action`); empty when disabled.
    steps: tuple[str, ...] = ()


def offers_for(
    status: str,
    *,
    has_preview: bool,
    republish: bool = False,
    publish_pr_open: bool = False,
    publish_run_active: bool = False,
    unpublish_pr_open: bool = False,
) -> list[Offer]:
    """The editor's action offers for a draft in `status`, in display order.

    `has_preview` means a preview of the draft's current text has been built.
    `republish` is a draft that already has a published record (the approve
    button then reads "Republish"). `publish_pr_open` and `publish_run_active`
    are the two things `Store.act_on_draft` refuses a re-approve for that the
    table cannot see. `unpublish_pr_open` blocks a Preview that would first
    revise the post: the unpublish PR is about to remove it from main, and a
    revise moves the record to `drafting`, where the merge has no transition.
    """
    offers: list[Offer] = []

    preview_plan = plan_action(status, "preview", True)
    if preview_plan is not None and unpublish_pr_open and len(preview_plan) > 1:
        offers.append(Offer("preview", "Preview", DISABLED, reason="Unpublish PR open"))
    elif preview_plan is not None:
        offers.append(
            Offer("preview", "Preview", primary=not has_preview, steps=preview_plan),
        )
    else:
        offers.append(Offer("preview", "Preview", DISABLED, reason=_preview_reason(status)))

    publish = _publish_offer(
        status,
        has_preview=has_preview,
        republish=republish,
        publish_pr_open=publish_pr_open,
        publish_run_active=publish_run_active,
    )
    if publish is not None:
        offers.append(publish)

    for (from_status, action), transition in DRAFT_TRANSITIONS.items():
        if from_status != status or action in _SPECIAL:
            continue
        offers.append(
            Offer(
                action,
                action.replace("_", " ").capitalize(),
                feedback_required=transition.feedback_required,
                reserved=action in RESERVED_ACTIONS,
                steps=(action,),
            )
        )
    return offers


def _preview_reason(status: str) -> str:
    if status in ("rejected", "unpublished"):
        return "Restore first"
    return "Already approved"


def _publish_offer(
    status: str,
    *,
    has_preview: bool,
    republish: bool,
    publish_pr_open: bool,
    publish_run_active: bool,
) -> Offer | None:
    label = "Republish" if republish else "Publish"
    if status in ("rejected", "unpublished"):
        # Restore is the way back, and it is offered on its own; a disabled
        # Publish here would promise a path that starts with a different button.
        return None
    if publish_pr_open or publish_run_active:
        # Already in flight, and `Store.act_on_draft` would 409 it. Nothing
        # to offer, and nothing to wait for on this page.
        return None
    if status != "approved" and not has_preview:
        # An approved draft is the retry case (its earlier publish run failed
        # or was never observed); it was allowed through review without a
        # preview gate and stays retryable.
        return Offer("approve", label, DISABLED, reason="Preview first", reserved=True)
    plan = plan_action(status, "approve", True)
    if plan is None:
        reason = (
            "Save an edit first" if status in ("published", "revision_requested") else "Not yet"
        )
        return Offer("approve", label, DISABLED, reason=reason, reserved=True)
    return Offer("approve", label, primary=True, reserved=True, steps=plan)


def staged_refusal(
    status: str,
    action: str,
    actor_is_ui: bool,
    *,
    has_preview: bool,
    republish: bool = False,
    publish_pr_open: bool = False,
    publish_run_active: bool = False,
    unpublish_pr_open: bool = False,
) -> str | None:
    """Why a click on `action` must be refused, or None when it may run.

    Only a click that stages (its plan is more than the action itself) is
    judged here: a single legal step stays a plain table lookup in the store,
    as it always was. A staged click is allowed only if the editor offers it
    as available for this same state, so the page and the route cannot
    disagree about what a click does.
    """
    plan = plan_action(status, action, actor_is_ui)
    if plan is None or len(plan) < 2:
        return None
    offer = next(
        (
            candidate
            for candidate in offers_for(
                status,
                has_preview=has_preview,
                republish=republish,
                publish_pr_open=publish_pr_open,
                publish_run_active=publish_run_active,
                unpublish_pr_open=unpublish_pr_open,
            )
            if candidate.action == action
        ),
        None,
    )
    if offer is None:
        return "That action is not available right now."
    if offer.state == DISABLED:
        return f"{offer.label} is not available right now ({offer.reason})."
    return None
