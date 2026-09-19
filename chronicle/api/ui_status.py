"""Author-facing names for a draft's status.

The state machine's own strings (`in_review`, `revision_requested`) are the
API contract, the stored records, and the filter query values, and none of
them change. This is render layer only: one function, `status_label`, is what
every UI template calls when it shows a draft's status to a human, the same
way `ui_time.local_time` is the one place a stamp becomes a clock time.

The mapping is a plain dict so a test can hold it against `DRAFT_STATUSES`:
adding a status to the state machine fails `tests/test_ui_status.py` until it
is given a label here. At render time an unknown value is shown as it is
rather than failing a whole page over one record; the test is the guard.
"""

from __future__ import annotations

STATUS_LABELS: dict[str, str] = {
    "drafting": "Draft",
    "in_review": "In review",
    "revision_requested": "Needs revision",
    "previewed": "Previewed",
    "approved": "Approved",
    "published": "Published",
    "unpublished": "Unpublished",
    "rejected": "Rejected",
}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)
