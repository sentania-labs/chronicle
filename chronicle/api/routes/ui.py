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
from itertools import zip_longest
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from .. import ui_templates as tpl
from ..deps import Consumer, Services
from ..errors import ApiError
from ..images import MAX_IMAGE_BYTES, alt_text_for, safe_upload_filename
from ..models import Draft, Material, Post, Submission
from ..pagination import Page, paginate
from ..store import Store
from ..ui_deps import banner_enabled, check_same_origin, get_services, require_ui_consumer
from ..ui_time import sort_key

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


def _has_current_preview(run: Any, version_no: int) -> bool:
    """A preview of the draft's current text exists: the last preview run
    succeeded and built the version the draft is at now (a run with no
    recorded `built_version` predates the field and counts). A save after the
    build makes it stale, which is what keeps Publish behind "Preview first"
    after an edit to a `previewed` draft, where a save leaves the status alone."""
    if _preview_url(run) is None:
        return False
    return run.built_version is None or run.built_version == version_no


# `create_draft` reports dropped frontmatter keys and failed image imports
# as warnings rather than a 422, because the import itself still succeeded
# (AGENTS.md: "Store.create_draft returns (Draft, warnings), not a bare
# Draft"). A redirect can't carry the response body that came back from the
# store, so the warnings ride one flash query parameter instead, joined on a
# separator no warning message produces (they are built from f-strings over
# key/filename reprs, never a raw unit separator byte) and read back once by
# the editor GET that follows.
_WARNING_SEP = "\x1f"


def _redirect_to_draft(draft_id: str, warnings: list[str]) -> RedirectResponse:
    if not warnings:
        return RedirectResponse(f"/content/drafts/{draft_id}", status_code=303)
    query = urlencode({"warnings": _WARNING_SEP.join(warnings)})
    return RedirectResponse(f"/content/drafts/{draft_id}?{query}", status_code=303)


@router.get("/", response_class=HTMLResponse)
def home() -> RedirectResponse:
    return RedirectResponse("/content/drafts", status_code=303)


# --- Submissions ------------------------------------------------------------


@router.get("/content/submissions", response_class=HTMLResponse)
def submissions_list(
    request: Request, page: int = Query(1, ge=1), services: Services = Depends(get_services)
) -> HTMLResponse:
    submissions = [_dump(s) for s in services.store.list_submissions()]
    pg = paginate(submissions, page)
    return HTMLResponse(tpl.submissions_list_page(pg, banner=banner_enabled(request)))


def _submission_response(
    services: Services,
    submission_id: str,
    request: Request,
    *,
    notice: str | None = None,
    notice_kind: str | None = None,
    status_code: int = 200,
    conflict_diff: str | None = None,
    attempted: dict[str, Any] | None = None,
    submission: Submission | None = None,
) -> HTMLResponse:
    # A caller that already read the record passes it in, so the page renders
    # the same snapshot it computed anything else from.
    if submission is None:
        submission = services.store.get_submission(submission_id)
    images = [_dump(services.store.get_image(image_id)) for image_id in submission.image_ids]
    html = tpl.submission_detail_page(
        _dump(submission),
        images,
        banner=banner_enabled(request),
        notice=notice,
        notice_kind=notice_kind,
        conflict_diff=conflict_diff,
        attempted=attempted,
    )
    return HTMLResponse(html, status_code=status_code)


@router.get("/content/submissions/{submission_id}", response_class=HTMLResponse)
def submission_detail(
    submission_id: str, request: Request, services: Services = Depends(get_services)
) -> HTMLResponse:
    return _submission_response(services, submission_id, request)


def _crlf_to_lf(value: str) -> str:
    # A browser submits every textarea line break as CRLF. Left alone, that
    # would rewrite every line of a pasted post in the submission's diff and
    # stop a `---` frontmatter fence from matching when the draft is seeded.
    return value.replace("\r\n", "\n")


@router.post("/content/submissions/{submission_id}/edit", response_class=HTMLResponse)
async def submission_edit(
    submission_id: str,
    request: Request,
    _origin: None = Depends(check_same_origin),
    consumer: Consumer = Depends(require_ui_consumer),
    services: Services = Depends(get_services),
) -> HTMLResponse:
    form = await request.form()
    try:
        base_version = int(str(form.get("base_version", "0")) or "0")
    except ValueError:
        return _submission_response(
            services,
            submission_id,
            request,
            notice="base_version must be a whole number",
            notice_kind="error",
            status_code=422,
        )
    # A browser always sends every field (an emptied one arrives as ""); a
    # request that omits one is not the edit form, and defaulting it would
    # write an empty brief or clear every material. The API route refuses the
    # same omission.
    missing = [
        field
        for field in ("brief", "material_name", "material_url", "material_text")
        if field not in form
    ]
    if missing:
        return _submission_response(
            services,
            submission_id,
            request,
            notice=f"missing form fields: {', '.join(missing)}",
            notice_kind="error",
            status_code=422,
        )
    brief = _crlf_to_lf(str(form.get("brief", "")))
    materials: list[Material] = []
    rows = zip_longest(
        form.getlist("material_name"),
        form.getlist("material_url"),
        form.getlist("material_text"),
        fillvalue="",
    )
    for number, (raw_name, raw_url, raw_text) in enumerate(rows, start=1):
        name, url, text = (
            str(raw_name).strip(),
            str(raw_url).strip(),
            _crlf_to_lf(str(raw_text)),
        )
        if not (name or url or text.strip()):
            continue
        materials.append(
            Material(name=name or f"material {number}", url=url or None, text=text or None)
        )
    image_ids = [str(value) for value in form.getlist("image_id")]
    attempted = {"brief": brief, "materials": [m.model_dump() for m in materials]}
    try:
        services.store.revise_submission(
            submission_id, consumer.name, base_version, brief, materials, image_ids
        )
    except ApiError as exc:
        if exc.code != "stale_base_version":
            # Carried here too: a submission that turned frozen underneath
            # the visitor (drafted or discarded in another tab) must not eat
            # what they typed.
            return _submission_response(
                services,
                submission_id,
                request,
                notice=exc.message,
                notice_kind="error",
                status_code=exc.status_code,
                attempted=attempted,
            )
        # Recomputed against the record's actual current version, not
        # `exc.extra`: a save landing between the conflict and this handler
        # would leave that summary describing an older version (the same
        # reasoning as `draft_save`). The record is read once and both the
        # diff and the rendered form come from that snapshot, so a revision
        # landing mid-handler cannot leave the form newer than the diff.
        current = services.store.get_submission(submission_id)
        return _submission_response(
            services,
            submission_id,
            request,
            notice=exc.message,
            notice_kind="conflict",
            status_code=409,
            conflict_diff=services.store.submission_diff(current, base_version),
            attempted=attempted,
            submission=current,
        )
    return _submission_response(services, submission_id, request, notice="saved", notice_kind="ok")


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
    draft, warnings = services.store.create_draft(consumer.name, from_submission=submission_id)
    return _redirect_to_draft(draft.id, warnings)


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


@router.post("/content/drafts/new")
def draft_new(
    _origin: None = Depends(check_same_origin),
    consumer: Consumer = Depends(require_ui_consumer),
    services: Services = Depends(get_services),
) -> RedirectResponse:
    """Start a post from nothing. POST only: a GET on this path is the
    editor route with `new` as a draft id, which 404s and creates nothing."""
    draft, warnings = services.store.create_draft(consumer.name)
    return _redirect_to_draft(draft.id, warnings)


def _board_row(
    services: Services, draft: Draft, flags_by_draft: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    store = services.store
    versions = store.list_versions(draft.id)
    last_author = versions[-1].author if versions else "-"
    last_preview = store.last_run(draft.id, kind="preview")
    run_info = {"preview_url": _preview_url(last_preview)} if last_preview else None
    return {
        "draft": _dump(draft),
        "last_author": last_author,
        "run_info": run_info,
        "flags": flags_by_draft.get(draft.id, []),
    }


def _archive_sort_key(draft: Draft) -> tuple[datetime, datetime]:
    post_date = (draft.published or {}).get("date")
    return (sort_key(post_date or draft.updated_at), sort_key(draft.updated_at))


@router.get("/content/drafts", response_class=HTMLResponse)
def drafts_board(
    request: Request,
    status: str | None = None,
    q: str = "",
    page: int = Query(1, ge=1),
    services: Services = Depends(get_services),
) -> HTMLResponse:
    store = services.store
    flags_by_draft: dict[str, list[dict[str, Any]]] = {}
    for flag in store.list_flags(resolved=False):
        if flag.draft_id:
            flags_by_draft.setdefault(flag.draft_id, []).append(_dump(flag))

    drafts = store.list_drafts(status or None)
    needle = q.strip().lower()
    if needle:
        drafts = [
            d
            for d in drafts
            if needle in (d.title or "").lower() or needle in (d.slug or "").lower()
        ]
    # Newest first, here and not in `Index.draft_ids`: that ordering is the
    # API's own list contract (oldest first) and must not move under it.
    drafts.sort(key=lambda d: sort_key(d.updated_at), reverse=True)
    active = [d for d in drafts if d.status != "published"]
    archive = [d for d in drafts if d.status == "published"]
    # The archive is ordered by the date the post itself carries, not by when
    # its record was last touched: a digest stamps hundreds of records within
    # seconds of each other, so `updated_at` alone left a freshly loaded
    # archive in processing order (2026, 2026, then 2011 ascending). Found in
    # the live pass. A record with no post date falls back to `updated_at`.
    archive.sort(key=_archive_sort_key, reverse=True)
    # Only the archive is paged, and its rows (versions, last preview run) are
    # only loaded for the visible page: after a digest the archive is hundreds
    # of records and the board should not read every one to show fifty.
    visible = paginate(archive, page)
    active_rows = [_board_row(services, d, flags_by_draft) for d in active]
    archive_pg = Page(
        items=[_board_row(services, d, flags_by_draft) for d in visible.items],
        page=visible.page,
        page_size=visible.page_size,
        total=visible.total,
    )
    return HTMLResponse(
        tpl.drafts_board_page(
            active_rows, archive_pg, status_filter=status, q=q, banner=banner_enabled(request)
        )
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
    preview_run = store.last_run(draft_id, kind="preview")
    preview_url = _preview_url(preview_run)
    watch = store.get_watch(draft_id)
    publish_run = store.last_run(draft_id, kind="publish")
    html = tpl.editor_page(
        _dump(draft),
        versions,
        feedback,
        _dump(last_run) if last_run else None,
        preview_url,
        banner=banner,
        has_preview=_has_current_preview(preview_run, draft.version_no),
        publish_pr_open=watch is not None and watch.kind == "publish",
        # `Store.act_on_draft` separately refuses a re-approve while a
        # publish run is still queued or building (409
        # publish_run_in_progress), the gap between "approved" and a watch
        # entry existing (the watch is only created once the publisher has
        # actually opened a PR). The transition table and the
        # publish_pr_open check above can't see a run with no PR yet, so a
        # round C5 review found this button rendering and 409ing on every
        # click for exactly that window.
        publish_run_active=publish_run is not None and publish_run.status in ("queued", "building"),
        notice=notice,
        notice_kind=notice_kind,
    )
    return HTMLResponse(html, status_code=status_code)


@router.get("/content/drafts/{draft_id}", response_class=HTMLResponse)
def draft_editor(
    draft_id: str, request: Request, services: Services = Depends(get_services)
) -> HTMLResponse:
    raw_warnings = request.query_params.get("warnings")
    notice = None
    notice_kind = "error"
    if raw_warnings:
        notice = "Import warnings: " + "; ".join(raw_warnings.split(_WARNING_SEP))
        notice_kind = "warning"
    return _editor_response(
        services, draft_id, banner=banner_enabled(request), notice=notice, notice_kind=notice_kind
    )


def _apply_summary_or_description(
    frontmatter: dict[str, Any], existing: dict[str, Any], value: str
) -> None:
    # The editor shows one field labelled "summary or description"
    # (`val("summary") or val("description")`), but they are two separate
    # allowed Hugo frontmatter keys, and an imported or API-authored draft
    # can carry either or both. A round C5 review found this used to always
    # write "summary" and unconditionally drop "description", so saving an
    # unrelated edit silently deleted a `description` an unchanged form
    # never touched. Writing back to whichever key(s) the draft originally
    # had (both, if both existed) preserves that shape instead of collapsing
    # it to one key chosen by the UI.
    present = [key for key in ("summary", "description") if key in existing]
    keys = present or ["summary"]
    for key in keys:
        if value:
            frontmatter[key] = value
        else:
            frontmatter.pop(key, None)


def _build_frontmatter(
    existing: dict[str, Any], form: dict[str, str], pinned_slug: str | None
) -> dict[str, Any]:
    frontmatter = dict(existing)
    frontmatter["title"] = form.get("title", "").strip()
    value = form.get("date", "").strip()
    if value:
        frontmatter["date"] = value
    else:
        frontmatter.pop("date", None)
    _apply_summary_or_description(frontmatter, existing, form.get("summary", "").strip())
    if pinned_slug:
        # The url field is readonly once a slug is pinned (`draft.slug`,
        # not any `slug` key in frontmatter): the form's own value is a
        # convenience echo, never the source of truth, so a request that
        # omits it (or sends it blank) must not erase the stored url. Never
        # write `pinned_slug` into frontmatter here: a `github`-authored
        # save (`Store.record_github_version`) can legitimately carry a
        # different `slug` key than `draft.slug`, and overwriting it would
        # silently revert content Scott wrote on GitHub, which is exactly
        # what reconciliation exists to flag, not correct automatically.
        if existing.get("url"):
            frontmatter["url"] = existing["url"]
    else:
        value = form.get("url", "").strip()
        if value:
            frontmatter["url"] = value
        else:
            frontmatter.pop("url", None)
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
    frontmatter = _build_frontmatter(draft.frontmatter, form, draft.slug)
    body_text = form.get("body", "")
    try:
        services.store.save_draft(draft_id, consumer.name, base_version, frontmatter, body_text)
    except ApiError as exc:
        # Only a stale base_version is the conflict this view exists for; a
        # draft with an open publish PR also saves as a 409
        # ("publish_pr_open", chronicle/api/store.py's save_draft), and a
        # round C5 review found that rendering the conflict page for that
        # case showed an empty diff summary against wording that claims
        # something changed underneath the visitor, which it did not.
        if exc.code != "stale_base_version":
            return _editor_response(
                services,
                draft_id,
                banner=banner_enabled(request),
                notice=exc.message,
                notice_kind="error",
                status_code=exc.status_code,
            )
        current = services.store.get_draft(draft_id)
        # Recomputed fresh against the draft's actual current version rather
        # than trusting `exc.extra["diff_summary"]`: that value was computed
        # inside `save_draft`'s lock at the moment of the conflict, and a
        # review found that a second save landing between then and this
        # handler running could leave it describing an older version than
        # the one `current` (and the reloaded form) now shows.
        try:
            diff_summary = services.store.diff_between(draft_id, base_version, current.version_no)
        except ApiError:
            # base_version named no real version to begin with (0, or ahead
            # of everything): save_draft's own best-effort summary already
            # covers that case without raising.
            diff_summary = str(exc.extra.get("diff_summary", ""))
        attempted = {
            "frontmatter": frontmatter,
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
        # Staged: the editor offers Preview on a published post and Publish
        # on a previewed one, and the store runs the steps
        # `transitions.plan_action` says make that legal (nothing decided here).
        services.store.act_on_draft_staged(
            draft_id, action, consumer.name, consumer.is_ui, feedback
        )
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


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


@router.post("/content/drafts/{draft_id}/images", response_class=HTMLResponse)
async def draft_image_upload(
    draft_id: str,
    request: Request,
    _origin: None = Depends(check_same_origin),
    consumer: Consumer = Depends(require_ui_consumer),
    services: Services = Depends(get_services),
    file: UploadFile = File(...),
    role: str = Form("inline"),
) -> Any:
    """Upload and attach an image.

    A plain form post gets the editor page back, as ever. `editor.js` asks for
    `Accept: application/json` instead and gets what it needs to put the
    reference in the body without re-rendering anything: the attached image's
    stored `filename` (the reference is by filename, and a dedup can keep an
    earlier upload's name), the `markdown` to insert at the cursor (None for a
    `feature` image, which the body does not reference), and the `url` the live
    render loads the bytes from.
    """
    try:
        raw = _read_capped(file)
        record, created = services.store.put_and_attach_image(
            draft_id, raw, safe_upload_filename(file.filename or "upload", raw), role, consumer.name
        )
    except ApiError as exc:
        if _wants_json(request):
            return JSONResponse(
                {"ok": False, "code": exc.code, "message": exc.message},
                status_code=exc.status_code,
            )
        return _editor_response(
            services,
            draft_id,
            banner=banner_enabled(request),
            notice=exc.message,
            notice_kind="error",
            status_code=exc.status_code,
        )
    if _wants_json(request):
        return JSONResponse(
            {
                "ok": True,
                "image_id": record.image_id,
                "filename": record.filename,
                "role": role,
                "created": created,
                "markdown": (
                    f"![{alt_text_for(record.filename)}]({record.filename})"
                    if role == "inline"
                    else None
                ),
                "url": tpl.image_url(draft_id, record.image_id),
            }
        )
    return _editor_response(services, draft_id, banner=banner_enabled(request))


@router.get("/content/drafts/{draft_id}/images/{image_id}/file")
def draft_image_file(
    draft_id: str, image_id: str, services: Services = Depends(get_services)
) -> FileResponse:
    """The bytes of an image attached to this draft, for the editor's live
    render (the body refers to it by bare filename, which no page can load).
    Only an image the draft has attached, and only the four decoded types the
    store ever keeps (no SVG), served inline with no sniffing."""
    draft = services.store.get_draft(draft_id)
    if not any(item.image_id == image_id for item in draft.images):
        raise ApiError(404, "image_not_attached", f"draft {draft_id} has no image {image_id}")
    record = services.store.get_image(image_id)
    return FileResponse(
        services.store.image_blob(image_id),
        media_type=record.mime,
        headers={"X-Content-Type-Options": "nosniff", "Content-Disposition": "inline"},
    )


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


def _untracked_posts(store: Store) -> tuple[list[Post], list[Post]]:
    """(every post, the posts no draft record tracks yet).

    Same rule as `Store._create_published_drafts_from_digest`, which holds
    the other copy of it (store.py is owned elsewhere, so the two are kept in
    step by hand: change one, change both). A post is tracked when its slug is
    some draft's slug, or its path is some draft's `source_post` path or
    `published` post path. Digest lands a record for each post it sees, so on
    a digested blog this is usually empty; what is left is the recovery case
    (a post digest could not import).
    """
    posts = store.list_posts()
    drafts = store.list_drafts()
    tracked_slugs = {d.slug for d in drafts if d.slug}
    tracked_paths = {(d.source_post or {}).get("path") for d in drafts if d.source_post} | {
        (d.published or {}).get("post_path") for d in drafts if d.published
    }
    untracked = [p for p in posts if p.slug not in tracked_slugs and p.path not in tracked_paths]
    return posts, untracked


def _import_listing(services: Services, q: str, page: int) -> tuple[Page[dict[str, Any]], int, int]:
    """(the page to render, how many posts exist, how many are untracked)."""
    posts, untracked = _untracked_posts(services.store)
    needle = q.strip().lower()
    shown = untracked
    if needle:
        shown = [p for p in shown if needle in p.slug.lower() or needle in p.title.lower()]
    posts_dump = [_dump(p) for p in sorted(shown, key=lambda p: p.date, reverse=True)]
    return paginate(posts_dump, page), len(posts), len(untracked)


@router.get("/content/import", response_class=HTMLResponse)
def import_search(
    request: Request,
    q: str = "",
    page: int = Query(1, ge=1),
    services: Services = Depends(get_services),
) -> HTMLResponse:
    pg, posts_total, untracked_total = _import_listing(services, q, page)
    return HTMLResponse(
        tpl.import_page(
            pg,
            q,
            banner=banner_enabled(request),
            posts_total=posts_total,
            untracked_total=untracked_total,
        )
    )


def _import_refusal(
    services: Services, request: Request, message: str, status_code: int, q: str, page: int
) -> Any:
    pg, posts_total, untracked_total = _import_listing(services, q, page)
    return HTMLResponse(
        tpl.import_page(
            pg,
            q,
            banner=banner_enabled(request),
            notice=message,
            posts_total=posts_total,
            untracked_total=untracked_total,
        ),
        status_code=status_code,
    )


@router.post("/content/import")
async def import_create(
    request: Request,
    _origin: None = Depends(check_same_origin),
    consumer: Consumer = Depends(require_ui_consumer),
    services: Services = Depends(get_services),
) -> Any:
    form = await request.form()
    slug = str(form.get("slug", ""))
    q = str(form.get("q", ""))
    try:
        page = max(1, int(str(form.get("page", "1"))))
    except ValueError:
        page = 1
    posts, untracked = _untracked_posts(services.store)
    if any(p.slug == slug for p in posts) and not any(p.slug == slug for p in untracked):
        # A stale tab or a hand-built POST: the list no longer offers this
        # post, because a record for it already exists.
        return _import_refusal(
            services,
            request,
            f"{slug} is already a post on the Posts tab, so there is nothing to import. "
            "Open it from there.",
            409,
            q,
            page,
        )
    try:
        draft, warnings = services.store.create_draft(consumer.name, from_post=slug)
    except ApiError as exc:
        message = exc.message
        if exc.code == "image_dir_collision":
            folder = exc.extra.get("image_dir") or slug
            message = (
                f"Could not import {slug}: its image folder (static/images/{folder}/) belongs "
                "to another post on this board. Nothing was created. If that post is this "
                "one, it is already on the Posts tab."
            )
        return _import_refusal(services, request, message, exc.status_code, q, page)
    return _redirect_to_draft(draft.id, warnings)


# --- Preview tab ------------------------------------------------------------


def _wall_seconds(run: Any) -> float | None:
    if not run.started_at or not run.finished_at:
        return None
    started = datetime.fromisoformat(run.started_at)
    finished = datetime.fromisoformat(run.finished_at)
    return round((finished - started).total_seconds(), 2)


@router.get("/content/previews", response_class=HTMLResponse)
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


@router.post("/content/previews/{draft_id}/rebuild", response_class=HTMLResponse)
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
        # `tpl.page`'s own `notice` argument already escapes; the body here
        # must never repeat that message unescaped (a round C5 review found
        # this reflecting an unescaped draft_id straight back in the page).
        return HTMLResponse(
            tpl.page("Rebuild failed", "", banner=banner_enabled(request), notice=exc.message),
            status_code=exc.status_code,
        )
    return RedirectResponse("/content/previews", status_code=303)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_log(
    run_id: str, request: Request, services: Services = Depends(get_services)
) -> HTMLResponse:
    run = services.store.get_run(run_id)
    log_text = services.store.run_log(run_id)
    return HTMLResponse(tpl.run_log_page(_dump(run), log_text, banner=banner_enabled(request)))
