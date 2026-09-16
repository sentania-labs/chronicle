"""Submission intake and triage (spec section 6)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..deps import Consumer, Services, get_services, require_consumer
from ..models import Material

router = APIRouter(prefix="/submissions", tags=["submissions"])


class SubmissionCreate(BaseModel):
    brief: str
    materials: list[Material] = []
    image_ids: list[str] = []


def _dump(record: Any) -> dict[str, Any]:
    dumped: dict[str, Any] = record.model_dump(mode="json", by_alias=True)
    return dumped


@router.post("", status_code=201)
def create_submission(
    payload: SubmissionCreate,
    consumer: Consumer = Depends(require_consumer),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    record = services.store.create_submission(
        consumer.name, payload.brief, payload.materials, payload.image_ids
    )
    return _dump(record)


@router.get("")
def list_submissions(
    status: str | None = None,
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    return {"submissions": [_dump(item) for item in services.store.list_submissions(status)]}


@router.get("/{submission_id}")
def get_submission(
    submission_id: str,
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    return _dump(services.store.get_submission(submission_id))


@router.post("/{submission_id}/claim")
def claim_submission(
    submission_id: str,
    consumer: Consumer = Depends(require_consumer),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    return _dump(services.store.act_on_submission(submission_id, "claim", consumer.name))


@router.post("/{submission_id}/discard")
def discard_submission(
    submission_id: str,
    consumer: Consumer = Depends(require_consumer),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    return _dump(services.store.act_on_submission(submission_id, "discard", consumer.name))
