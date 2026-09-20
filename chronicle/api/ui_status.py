"""Author-facing names for a draft's status.

The state machine's own strings (`in_review`, `revision_requested`) are the
API contract, the stored records, and the filter query values, and none of
them change. This is render layer only: `status_label` is what every UI
template calls when it shows a draft's status to a human, the same way
`ui_time.local_time` is the one place a stamp becomes a clock time.

A reader sees exactly four words: Draft, In review, Published, Rejected. The
state machine has eight statuses because it tracks facts a reader does not
think of as states (a preview was built, a reviewer asked for changes, the
post was once live). Those are shown as detail next to the four-word status,
by `status_details`, never as a status of their own.

The mapping is a plain dict so a test can hold it against `DRAFT_STATUSES`:
adding a status to the state machine fails `tests/test_ui_status.py` until it
is given a label here. At render time an unknown value is shown as it is
rather than failing a whole page over one record; the test is the guard.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .models import DRAFT_STATUSES

DRAFT = "Draft"
IN_REVIEW = "In review"
PUBLISHED = "Published"
REJECTED = "Rejected"

# The whole vocabulary a reader sees, in the order the board's filter lists it.
FOUR_WORDS: tuple[str, ...] = (DRAFT, IN_REVIEW, PUBLISHED, REJECTED)

STATUS_LABELS: dict[str, str] = {
    "drafting": DRAFT,
    "previewed": DRAFT,
    "revision_requested": DRAFT,
    "unpublished": DRAFT,
    "in_review": IN_REVIEW,
    "approved": IN_REVIEW,
    "published": PUBLISHED,
    "rejected": REJECTED,
}


# Which Lattice state colour a status carries, or "" for the neutral badge.
# A state colour carries state and nothing else: `published` is done (ok) and
# `rejected` is a refusal (bad). Draft and In review stay neutral on purpose:
# the label already says which, and colouring them apart would be decoration.
# "Waiting on a person" (warn) is no longer a status colour; it belongs to the
# `Came back from review` detail (`status_details`), which is where a draft
# that a reviewer sent back is told apart on the board.
STATUS_TONES: dict[str, str] = {
    "drafting": "",
    "previewed": "",
    "revision_requested": "",
    "unpublished": "",
    "in_review": "",
    "approved": "",
    "published": "ok",
    "rejected": "bad",
}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def status_tone(status: str) -> str:
    return STATUS_TONES.get(status, "")


def label_counts(counts: dict[str, int]) -> dict[str, int]:
    """Per-status counts summed under the four-word labels, in vocabulary
    order. A status the mapping does not know keeps its own name."""
    totals: dict[str, int] = {word: 0 for word in FOUR_WORDS}
    for status, count in counts.items():
        label = status_label(status)
        totals[label] = totals.get(label, 0) + count
    return totals


# --- The board's filter ----------------------------------------------------
#
# The filter's query values are the state machine's and stay so: `?status=` on
# the board still takes one raw status. A label covers several statuses, so
# its option carries them as a comma list, which `parse_status_filter` reads
# back. Nothing on the API side takes a list.


def statuses_for_label(label: str) -> tuple[str, ...]:
    return tuple(status for status in DRAFT_STATUSES if STATUS_LABELS.get(status) == label)


def filter_options() -> list[tuple[str, str]]:
    """(query value, text) for each of the four words, in vocabulary order."""
    return [(",".join(statuses_for_label(word)), word) for word in FOUR_WORDS]


def parse_status_filter(value: str | None) -> list[str]:
    """The raw statuses a `?status=` value names: one, or a comma list."""
    return [part for part in (value or "").split(",") if part]


# --- Detail ------------------------------------------------------------------
#
# What the eight statuses know that the four words do not say. Each is a
# (text, tone) pair for a small badge beside the status badge.

CAME_BACK = "Came back from review"
WAS_PUBLISHED = "Was published"
PREVIEW_BUILT = "Preview built"
PREVIEW_STALE = "Preview out of date"
APPROVED = "Approved"
PUBLISHING = "Publishing"
PUBLISH_PR_OPEN = "Publish PR open"
UNPUBLISH_PR_OPEN = "Unpublish PR open"

# A reviewer's request for changes is a fact about the draft that outlives the
# status: a preview or a save moves `revision_requested` on to `drafting`, and
# the request is still unanswered until the author resubmits. These are the
# feedback actions that settle it either way.
_VERDICTS = ("request_revision", "reject")


def came_back_from_review(
    status: str, feedback_actions: Iterable[str], *, published: bool = False
) -> bool:
    """True while a reviewer's request for changes is still unanswered.

    `revision_requested` is that fact directly. Once a preview or a save moves
    the draft on to `drafting` or `previewed`, the status forgets it, so it is
    read from the feedback log instead: the newest review verdict is a request
    for changes. Resubmitting is what answers it, and the draft is then In
    review, where this is not asked. A rejection recorded after the request
    settles it too (`reject` is the newest verdict, so this is false).

    A draft that has a published record is the one case the log cannot settle:
    the request may predate a publish, and nothing in the log orders the two.
    It is read as answered rather than showing a stale warning on a post that
    was revised long after; `revision_requested` itself is still shown.
    """
    if status == "revision_requested":
        return True
    if status not in ("drafting", "previewed") or published:
        return False
    verdicts = [action for action in feedback_actions if action in _VERDICTS]
    return bool(verdicts) and verdicts[-1] == "request_revision"


@dataclass(frozen=True)
class Detail:
    text: str
    tone: str = ""


def status_details(
    status: str,
    *,
    came_back: bool = False,
    has_preview: bool | None = None,
    publish_run_active: bool = False,
    publish_pr_open: bool = False,
    unpublish_pr_open: bool = False,
) -> list[Detail]:
    """The facts to show beside a status badge, most important first.

    `has_preview` is whether a preview of the draft's current text exists; the
    board does not know it and passes None, which says only that a preview was
    built. The rest are what the editor already computes for its buttons.
    """
    details: list[Detail] = []
    if came_back:
        details.append(Detail(CAME_BACK, "warn"))
    if status == "previewed":
        details.append(Detail(PREVIEW_STALE if has_preview is False else PREVIEW_BUILT))
    if status == "unpublished":
        details.append(Detail(WAS_PUBLISHED))
    if status == "approved":
        details.append(Detail(APPROVED))
    if publish_run_active:
        details.append(Detail(PUBLISHING))
    elif publish_pr_open:
        details.append(Detail(PUBLISH_PR_OPEN))
    if unpublish_pr_open:
        details.append(Detail(UNPUBLISH_PR_OPEN))
    return details
