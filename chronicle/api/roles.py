"""Role definitions and role-based access checks.

Chronicle has four roles arranged in a strict hierarchy (each including all
below it):

    reader      — read-only: catalog, posts, submissions, drafts, events, run logs
    contributor — + create and edit submissions, drafts, images, leases, submit and preview
    editor      — + reserved draft actions: approve, request_revision, reject, restore, unpublish
    admin       — + the admin API (/admin/api) and /admin HTML

Legacy consumer tokens are mapped implicitly:
    ui   → editor
    other → contributor

Service account tokens and personal tokens carry an explicit role from the
database.  The hierarchy check is numeric so it stays simple and auditable.
"""

from __future__ import annotations

# Ordered from least to most privileges.
ROLES: list[str] = ["reader", "contributor", "editor", "admin"]

# Numeric rank for comparison.
ROLE_HIERARCHY: dict[str, int] = {name: idx for idx, name in enumerate(ROLES)}


def role_rank(role: str) -> int:
    """Return the numeric rank of *role*.

    Raises ``ValueError`` if the name is not a known role.
    """
    return ROLE_HIERARCHY[role]


def roles_at_or_above(role: str, min_role: str) -> bool:
    """True when *role* has at least the privileges of *min_role*."""
    try:
        return role_rank(role) >= role_rank(min_role)
    except KeyError:
        return False


def is_reserved_action(action: str, role: str) -> bool:
    """Return True when *action* is reserved for editor (or above)."""
    return roles_at_or_above(role, "editor")


def validate_role(value: str) -> None:
    """Raise ``ValueError`` if *value* is not a known role name."""
    if value not in ROLE_HIERARCHY:
        raise ValueError(f"unknown role {value!r}; must be one of {ROLES}")
