"""Content and preview tabs: server-rendered HTML at the root path (ADR 014).

Every mutating route here calls the same `Store` methods the `/v1` routes
call, through a `Consumer` built from the ui token the same way a real bearer
call would be (`require_ui_consumer`), so every version, event, and commit
this surface produces is authored `scott` exactly as C1 defines. Nothing here
touches a session cookie or `/admin`; nothing under `/admin` is reachable
from here either.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import ui_templates as tpl
from ..deps import Consumer, Services
from ..errors import ApiError
from ..images import MAX_IMAGE_BYTES
from ..ui_deps import banner_enabled, check_same_origin, get_services, require_ui_consumer

router = APIRouter(tags=["ui"])

_READ_CHUNK_BYTES = 64 * 1024


def _read_capped(file: UploadFile) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = file.file.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_IMAGE_BYTES:
            raise ApiError(
                413, "image_too_large", f"image exceeds the {MAX_IMAGE_BYTES} byte ceiling"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _dump(model: Any) -> dict[str, Any]:
    result: dict[str, Any] = model.model_dump(mode="json")
    return result


def _preview_url(run: Any) -> str | None:
    if run is None or run.status != "succeeded":
        return None
    url = (run.result or {}).get("preview_url")
    return url if isinstance(url, str) else None


@router.get("/", response_class=HTMLResponse)
def home() -> RedirectResponse:
    return RedirectResponse("/content/drafts", status_code=303)


# --- Submissions ------------------------------------------------------------


@router.get("/content/submissions", response_class=HTMLResponse)
def submissions_list(request: Request, services: Services = Depends(get_services)) -> HTMLResponse:
    submissions = [_dump(s) for s in services.store.list_submissions()]
    return HTMLResponse(tpl.submissions_list_page(submissions, banner=banner_enabled(request)))


@router.get("/content/submissions/{submission_id}", response_class=HTMLResponse)
def submission_detail(
    submission_id: str, request: Request, services: Services = Depends(get_services)
) -> HTMLResponse:
    submission = services.store.get_submission(submission_id)
    images = [_dump(services.store.get_image(image_id)) for image_id in submission.image_ids]
    return HTMLResponse(
        tpl.submission_detail_page(_dump(submission), images, banner=banner_enabled(request))
    )


@router.post("/content/submissions/{submission_id}/draft")
def submission_to_draft(
    submission_id: str,
    _origin: None = Depends(check_same_origin),
    consumer: Consumer = Depends(require_ui_consumer),
    services: Services = Depends(get_services),
) -> RedirectResponse:
    submission = services.store.get_submission(submission_id)
    if submission.status == "new":
        # `create_draft`'s own transition check only allows a "claimed"
        # submission through; the button on the detail page offers both
        # "new" and "claimed" submissions, so claiming here is the one step
        # that makes that button work without a second click.
        services.store.act_on_submission(submission_id, "claim", consumer.name)
    draft, _warnings = services.store.create_draft(consumer.name, from_submission=submission_id)
    return RedirectResponse(f"/content/drafts/{draft.id}", status_code=303)


@router.post("/content/submissions/{submission_id}/discard")
def submission_discard(
    submission_id: str,
    _origin: None = Depends(check_same_origin),
    consumer: Consumer = Depends(require_ui_consumer),
    services: Services = Depends(get_services),
) -> RedirectResponse:
    services.store.act_on_submission(submission_id, "discard", consumer.name)
    return RedirectResponse("/content/submissions", status_code=303)


# --- Drafts board -------------------------------------------------------


@router.get("/content/drafts", response_class=HTMLResponse)
def drafts_board(
    request: Request, status: str | None = None, services: Services = Depends(get_services)
) -> HTMLResponse:
    store = services.store
    flags_by_draft: dict[str, list[dict[str, Any]]] = {}
    for flag in store.list_flags(resolved=False):
        if flag.draft_id:
            flags_by_draft.setdefault(flag.draft_id, []).append(_dump(flag))

    rows = []
    for draft in store.list_drafts(status or None):
        versions = store.list_versions(draft.id)
        last_author = versions[-1].author if versions else "-"
        last_preview = store.last_run(draft.id, kind="preview")
        run_info = {"preview_url": _preview_url(last_preview)} if last_preview else None
        rows.append(
            {
                "draft": _dump(draft),
                "last_author": last_author,
                "run_info": run_info,
                "flags": flags_by_draft.get(draft.id, []),
            }
        )
    return HTMLResponse(
        tpl.drafts_board_page(rows, status_filter=status, banner=banner_enabled(request))
    )


def _editor_response(
    services: Services,
    draft_id: str,
    *,
    banner: bool,
    notice: str | None = None,
    notice_kind: str = "error",
    status_code: int = 200,
) -> HTMLResponse:
    store = services.store
    draft = store.get_draft(draft_id)
    versions = [_dump(v) for v in store.list_versions(draft_id)]
    feedback = [_dump(f) for f in store.list_feedback(draft_id)]
    last_run = store.last_run(draft_id)
    preview_url = _preview_url(store.last_run(draft_id, kind="preview"))
    html = tpl.editor_page(
        _dump(draft),
        versions,
        feedback,
        _dump(last_run) if last_run else None,
        preview_url,
        banner=banner,
        notice=notice,
        notice_kind=notice_kind,
    )
    return HTMLResponse(html, status_code=status_code)


@router.get("/content/drafts/{draft_id}", response_class=HTMLResponse)
def draft_editor(
    draft_id: str, request: Request, services: Services = Depends(get_services)
) -> HTMLResponse:
    return _editor_response(services, draft_id, banner=banner_enabled(request))


def _build_frontmatter(existing: dict[str, Any], form: dict[str, str]) -> dict[str, Any]:
    frontmatter = dict(existing)
    frontmatter["title"] = form.get("title", "").strip()
    for key, form_key in (("date", "date"), ("url", "url"), ("summary", "summary")):
        value = form.get(form_key, "").strip()
        if value:
            frontmatter[key] = value
        else:
            frontmatter.pop(key, None)
    frontmatter.pop("description", None)
    for key in ("categories", "tags"):
        raw = form.get(key, "").strip()
        items = [item.strip() for item in raw.split(",") if item.strip()]
        if items:
            frontmatter[key] = items
        else:
            frontmatter.pop(key, None)
    feature = form.get("featureImage", "").strip()
    if feature:
        frontmatter["featureImage"] = feature
    else:
        frontmatter.pop("featureImage", None)
    if existing.get("slug"):
        frontmatter["slug"] = existing["slug"]
    return frontmatter


@router.post("/content/drafts/{draft_id}/save", response_class=HTMLResponse)
async def draft_save(
    draft_id: str,
    request: Request,
    _origin: None = Depends(check_same_origin),
    consumer: Consumer = Depends(require_ui_consumer),
    services: Services = Depends(get_services),
) -> HTMLResponse:
    form_data = await request.form()
    form = {key: str(value) for key, value in form_data.items()}
    base_version = int(form.get("base_version", "0") or "0")
    draft = services.store.get_draft(draft_id)
    frontmatter = _build_frontmatter(draft.frontmatter, form)
    body_text = form.get("body", "")
    try:
        services.store.save_draft(draft_id, consumer.name, base_version, frontmatter, body_text)
    except ApiError as exc:
        if exc.status_code != 409:
            raise
        current = services.store.get_draft(draft_id)
        diff_summary = str(exc.extra.get("diff_summary", ""))
        attempted = {
            "title": frontmatter.get("title", ""),
            "body": body_text,
            "base_version": base_version,
        }
        html = tpl.conflict_page(
            _dump(current), attempted, diff_summary, banner=banner_enabled(request)
        )
        return HTMLResponse(html, status_code=409)
    return _editor_response(
        services, draft_id, banner=banner_enabled(request), notice="saved", notice_kind="ok"
    )


@router.post("/content/drafts/{draft_id}/claim")
def draft_claim(
    draft_id: str,
    _origin: None = Depends(check_same_origin),
    consumer: Consumer = Depends(require_ui_consumer),
    services: Services = Depends(get_services),
) -> RedirectResponse:
    services.store.set_claim(draft_id, consumer.name, held=True)
    return RedirectResponse(f"/content/drafts/{draft_id}", status_code=303)


@router.post("/content/drafts/{draft_id}/release")
def draft_release(
    draft_id: str,
    _origin: None = Depends(check_same_origin),
    consumer: Consumer = Depends(require_ui_consumer),
    services: Services = Depends(get_services),
) -> RedirectResponse:
    services.store.set_claim(draft_id, consumer.name, held=False)
    return RedirectResponse(f"/content/drafts/{draft_id}", status_code=303)


@router.post("/content/drafts/{draft_id}/actions/{action}", response_class=HTMLResponse)
async def draft_action(
    draft_id: str,
    action: str,
    request: Request,
    _origin: None = Depends(check_same_origin),
    consumer: Consumer = Depends(require_ui_consumer),
    services: Services = Depends(get_services),
) -> Any:
    form_data = await request.form()
    feedback = str(form_data.get("feedback") or "") or None
    try:
        services.store.act_on_draft(draft_id, action, consumer.name, consumer.is_ui, feedback)
    except ApiError as exc:
        return _editor_response(
            services,
            draft_id,
            banner=banner_enabled(request),
            notice=exc.message,
            notice_kind="error",
            status_code=exc.status_code,
        )
    return RedirectResponse(f"/content/drafts/{draft_id}", status_code=303)


@router.get("/content/drafts/{draft_id}/diff", response_class=HTMLResponse)
def draft_diff(
    draft_id: str,
    request: Request,
    from_: int = Query(0, alias="from"),
    to: int = 0,
    services: Services = Depends(get_services),
) -> HTMLResponse:
    diff_text = services.store.diff_between(draft_id, from_, to)
    return HTMLResponse(
        tpl.diff_page(draft_id, from_, to, diff_text, banner=banner_enabled(request))
    )


@router.post("/content/drafts/{draft_id}/images", response_class=HTMLResponse)
async def draft_image_upload(
    draft_id: str,
    request: Request,
    _origin: None = Depends(check_same_origin),
    consumer: Consumer = Depends(require_ui_consumer),
    services: Services = Depends(get_services),
    file: UploadFile = File(...),
    role: str = Form("inline"),
) -> HTMLResponse:
    try:
        raw = _read_capped(file)
        record, _created = services.store.put_image(raw, file.filename or "upload")
        services.store.attach_image(draft_id, record.image_id, role, consumer.name)
    except ApiError as exc:
        return _editor_response(
            services,
            draft_id,
            banner=banner_enabled(request),
            notice=exc.message,
            notice_kind="error",
            status_code=exc.status_code,
        )
    return _editor_response(services, draft_id, banner=banner_enabled(request))


@router.post("/content/drafts/{draft_id}/images/{image_id}/detach")
def draft_image_detach(
    draft_id: str,
    image_id: str,
    _origin: None = Depends(check_same_origin),
    consumer: Consumer = Depends(require_ui_consumer),
    services: Services = Depends(get_services),
) -> RedirectResponse:
    services.store.detach_image(draft_id, image_id, consumer.name)
    return RedirectResponse(f"/content/drafts/{draft_id}", status_code=303)


# --- Import ---------------------------------------------------------------


@router.get("/content/import", response_class=HTMLResponse)
def import_search(
    request: Request, q: str = "", services: Services = Depends(get_services)
) -> HTMLResponse:
    posts = services.store.list_posts()
    needle = q.strip().lower()
    if needle:
        posts = [p for p in posts if needle in p.slug.lower() or needle in p.title.lower()]
    posts_dump = [_dump(p) for p in sorted(posts, key=lambda p: p.date, reverse=True)]
    return HTMLResponse(tpl.import_page(posts_dump, q, banner=banner_enabled(request)))


@router.post("/content/import")
async def import_create(
    request: Request,
    _origin: None = Depends(check_same_origin),
    consumer: Consumer = Depends(require_ui_consumer),
    services: Services = Depends(get_services),
) -> Any:
    form = await request.form()
    slug = str(form.get("slug", ""))
    try:
        draft, _warnings = services.store.create_draft(consumer.name, from_post=slug)
    except ApiError as exc:
        return HTMLResponse(
            tpl.import_page([], slug, banner=banner_enabled(request), notice=exc.message),
            status_code=exc.status_code,
        )
    return RedirectResponse(f"/content/drafts/{draft.id}", status_code=303)


# --- Preview tab ------------------------------------------------------------


def _wall_seconds(run: Any) -> float | None:
    if not run.started_at or not run.finished_at:
        return None
    started = datetime.fromisoformat(run.started_at)
    finished = datetime.fromisoformat(run.finished_at)
    return round((finished - started).total_seconds(), 2)


@router.get("/preview", response_class=HTMLResponse)
def preview_list(request: Request, services: Services = Depends(get_services)) -> HTMLResponse:
    store = services.store
    rows = []
    for draft in store.list_drafts():
        run = store.last_run(draft.id, kind="preview")
        if run is None or run.status != "succeeded":
            continue
        url = _preview_url(run)
        if not url:
            continue
        rows.append(
            {
                "draft_id": draft.id,
                "title": draft.title or "(untitled)",
                "preview_url": url,
                "built_at": run.finished_at,
                "wall_seconds": _wall_seconds(run),
                "toolchain_drift": run.toolchain_drift,
            }
        )
    return HTMLResponse(tpl.preview_list_page(rows, banner=banner_enabled(request)))


@router.post("/preview/{draft_id}/rebuild", response_class=HTMLResponse)
def preview_rebuild(
    draft_id: str,
    request: Request,
    _origin: None = Depends(check_same_origin),
    consumer: Consumer = Depends(require_ui_consumer),
    services: Services = Depends(get_services),
) -> Any:
    try:
        services.store.act_on_draft(draft_id, "preview", consumer.name, consumer.is_ui)
    except ApiError as exc:
        return HTMLResponse(
            tpl.page(
                "Rebuild failed",
                f"<p>{exc.message}</p>",
                banner=banner_enabled(request),
                notice=exc.message,
            ),
            status_code=exc.status_code,
        )
    return RedirectResponse("/preview", status_code=303)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_log(
    run_id: str, request: Request, services: Services = Depends(get_services)
) -> HTMLResponse:
    run = services.store.get_run(run_id)
    log_text = services.store.run_log(run_id)
    return HTMLResponse(tpl.run_log_page(_dump(run), log_text, banner=banner_enabled(request)))
