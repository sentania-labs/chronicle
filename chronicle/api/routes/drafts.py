"""Drafts: create, read, save with conflict detection, claim, act (spec section 6).

`PUT` is the only write that can lose someone's work, so it refuses to guess:
a stale `base_version` returns 409 with the current version and a unified
diff of what moved underneath the caller, never a silent overwrite.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..deps import Consumer, Services, get_services, require_consumer
from ..errors import ApiError
from ..models import Draft
from ..transitions import DRAFT_ACTIONS

router = APIRouter(prefix="/drafts", tags=["drafts"])


class DraftCreate(BaseModel):
    from_submission: str | None = None
    from_post: str | None = None


class DraftSave(BaseModel):
    base_version: int
    frontmatter: dict[str, Any] = {}
    body: str = ""
    message: str = ""
    # Omitted (None) keeps whatever the draft already carries; a mapping,
    # `{}` included, replaces it in full (ADR 021). Typed loosely here so an
    # unknown channel or a non-string value is refused by
    # `store.check_announcements` in the same 422 envelope as every other
    # domain refusal, rather than as a framework validation error.
    announcements: dict[str, Any] | None = None


class ActionBody(BaseModel):
    feedback: str | None = None


class ImageAttach(BaseModel):
    role: str


def _dump(draft: Draft) -> dict[str, Any]:
    return draft.model_dump(mode="json")


@router.post("", status_code=201)
def create_draft(
    payload: DraftCreate,
    consumer: Consumer = Depends(require_consumer),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    draft, warnings = services.store.create_draft(
        consumer.name, payload.from_submission, payload.from_post
    )
    body = _dump(draft)
    body["warnings"] = warnings
    return body


@router.get("")
def list_drafts(
    status: str | None = None,
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    return {"drafts": [_dump(draft) for draft in services.store.list_drafts(status)]}


@router.get("/{draft_id}")
def get_draft(draft_id: str, services: Services = Depends(get_services)) -> dict[str, Any]:
    return _dump(services.store.get_draft(draft_id))


@router.put("/{draft_id}")
def save_draft(
    draft_id: str,
    payload: DraftSave,
    consumer: Consumer = Depends(require_consumer),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    draft = services.store.save_draft(
        draft_id,
        consumer.name,
        payload.base_version,
        payload.frontmatter,
        payload.body,
        payload.message,
        announcements=payload.announcements,
    )
    return _dump(draft)


@router.post("/{draft_id}/claim")
def claim_draft(
    draft_id: str,
    consumer: Consumer = Depends(require_consumer),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    return _dump(services.store.set_claim(draft_id, consumer.name, held=True))


@router.post("/{draft_id}/release")
def release_draft(
    draft_id: str,
    consumer: Consumer = Depends(require_consumer),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    return _dump(services.store.set_claim(draft_id, consumer.name, held=False))


@router.get("/{draft_id}/versions")
def list_versions(draft_id: str, services: Services = Depends(get_services)) -> dict[str, Any]:
    versions = services.store.list_versions(draft_id)
    return {"versions": [version.model_dump(mode="json") for version in versions]}


@router.get("/{draft_id}/versions/{version_no}")
def get_version(
    draft_id: str,
    version_no: int,
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    return services.store.get_version(draft_id, version_no).model_dump(mode="json")


@router.get("/{draft_id}/changes")
def get_changes(
    draft_id: str,
    since: int = 0,
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    return services.store.changes_since(draft_id, since)


def _preview_url(last_preview_run: Any) -> str | None:
    if last_preview_run is None or last_preview_run.status != "succeeded":
        return None
    result = last_preview_run.result or {}
    url = result.get("preview_url")
    return url if isinstance(url, str) else None


@router.get("/{draft_id}/status")
def get_status(draft_id: str, services: Services = Depends(get_services)) -> dict[str, Any]:
    draft = services.store.get_draft(draft_id)
    last_run = services.store.last_run(draft.id)
    last_preview_run = services.store.last_run(draft.id, kind="preview")
    published = draft.published or {}
    return {
        "status": draft.status,
        "slug": draft.slug,
        "last_run": last_run.model_dump(mode="json") if last_run else None,
        "preview_url": _preview_url(last_preview_run),
        # Set once a publish or unpublish run has actually recorded a
        # result (Store.record_publish_result); null before that, not a
        # guessed value.
        "branch": published.get("branch"),
        "pr_url": published.get("pr_url"),
    }


@router.get("/{draft_id}/preview")
def get_preview(draft_id: str, services: Services = Depends(get_services)) -> dict[str, Any]:
    """Same data `status` already carries, narrowed to what a preview link needs.

    Kept cheap and consistent with `get_status` on purpose: both read the same
    `last_run(kind="preview")` call, so the two routes can never disagree
    about whether a preview is ready.
    """
    draft = services.store.get_draft(draft_id)
    last_preview_run = services.store.last_run(draft.id, kind="preview")
    return {
        "preview_url": _preview_url(last_preview_run),
        "last_run": last_preview_run.model_dump(mode="json") if last_preview_run else None,
    }


@router.post("/{draft_id}/actions/{action}")
def act_on_draft(
    draft_id: str,
    action: str,
    payload: ActionBody | None = None,
    consumer: Consumer = Depends(require_consumer),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    if action not in DRAFT_ACTIONS:
        raise ApiError(
            404, "action_unknown", f"{action!r} is not one of {', '.join(DRAFT_ACTIONS)}"
        )
    draft, run = services.store.act_on_draft(
        draft_id,
        action,
        consumer.name,
        consumer.is_ui,
        payload.feedback if payload else None,
    )
    return {"draft": _dump(draft), "run_id": run.id if run else None}


@router.put("/{draft_id}/images/{image_id}")
def attach_image(
    draft_id: str,
    image_id: str,
    payload: ImageAttach,
    consumer: Consumer = Depends(require_consumer),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    return _dump(services.store.attach_image(draft_id, image_id, payload.role, consumer.name))


@router.delete("/{draft_id}/images/{image_id}")
def detach_image(
    draft_id: str,
    image_id: str,
    consumer: Consumer = Depends(require_consumer),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    return _dump(services.store.detach_image(draft_id, image_id, consumer.name))
