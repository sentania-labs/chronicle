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
    if payload.from_post is not None:
        raise ApiError(
            501,
            "import_not_implemented",
            "importing a published post from main arrives in C2",
        )
    return _dump(services.store.create_draft(consumer.name, payload.from_submission))


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


@router.get("/{draft_id}/status")
def get_status(draft_id: str, services: Services = Depends(get_services)) -> dict[str, Any]:
    draft = services.store.get_draft(draft_id)
    last_run = services.store.last_run(draft.id)
    return {
        "status": draft.status,
        "slug": draft.slug,
        "last_run": last_run.model_dump(mode="json") if last_run else None,
        # The builder and the GitHub client arrive in C3 and C2; until then
        # these are honestly null rather than a guessed URL.
        "preview_url": None,
        "branch": None,
        "pr_url": None,
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
