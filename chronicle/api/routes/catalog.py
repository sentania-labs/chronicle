"""Read paths over posts, runs, and the event log.

Posts stay empty until the digest of main lands in C2, and a run has no log
until the builder lands in C3; both read paths are real now so their
consumers can be written against them.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ..deps import Services, get_services

router = APIRouter(tags=["catalog"])


@router.get("/posts")
def list_posts(services: Services = Depends(get_services)) -> dict[str, Any]:
    return {"posts": [post.model_dump(mode="json") for post in services.store.list_posts()]}


@router.get("/posts/{slug}")
def get_post(slug: str, services: Services = Depends(get_services)) -> dict[str, Any]:
    return services.store.get_post(slug).model_dump(mode="json")


@router.get("/runs/{run_id}")
def get_run(run_id: str, services: Services = Depends(get_services)) -> dict[str, Any]:
    return services.store.get_run(run_id).model_dump(mode="json")


@router.get("/runs/{run_id}/log")
def get_run_log(run_id: str, services: Services = Depends(get_services)) -> dict[str, Any]:
    run = services.store.get_run(run_id)
    return {"run_id": run.id, "status": run.status, "log": ""}


@router.get("/events")
def get_events(since: int = 0, services: Services = Depends(get_services)) -> dict[str, Any]:
    events, cursor = services.store.events_since(since)
    return {
        "events": [event.model_dump(mode="json") for event in events],
        "next_cursor": cursor,
    }
