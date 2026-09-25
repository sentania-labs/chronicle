"""The admin front door: claim, login, GitHub App connection, digest, tokens.

Mounted separately from `build_v1_router()` (main.py) and carries none of
its dependencies: a bearer token never gets in here, and a session cookie
never gets into `/v1` (spec section 11). HTML routes redirect an
unauthenticated visitor to claim or login; `/admin/api` routes answer 401
like `/v1` does, via the same `ApiError` envelope.
"""

from __future__ import annotations

import json
import secrets
import tarfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

from ... import backup as backup_mod
from .. import admin_templates as tpl
from .. import crypto, scheduled_backup, toolchain_check
from ..admin_auth import (
    GITHUB_STATE_COOKIE_NAME,
    AlreadyClaimed,
    InvalidClaimCode,
    PasswordTooShort,
    WrongPassword,
)
from ..admin_deps import (
    AdminServices,
    InstallationToken,
    clear_session_cookie,
    get_admin_services,
    require_admin_session_html,
    require_admin_session_json,
    set_github_state_cookie,
    set_session_cookie,
)
from ..admin_status import build_status, scheduled_backup_summary
from ..deps import Services, get_services
from ..digest_runner import run as run_digest
from ..errors import ApiError
from ..github_client import GitHubApiError, build_manifest, manifest_target_url
from ..models import RECONCILE_RESOLUTIONS
from ..publisher import PublishFailed, build_repo_target
from ..reconcile import run_once_logged as run_reconcile
from ..tokens import RESERVED_TOKEN_NAMES
from ..ui_deps import check_same_origin

router = APIRouter(prefix="/admin", tags=["admin"])
api_router = APIRouter(prefix="/admin/api", tags=["admin-api"])

MAX_FORM_BYTES = 8192


async def _form(request: Request) -> dict[str, str]:
    """Read and parse a form body with its own small ceiling.

    Claim and login are the two unauthenticated POST routes here, so nothing
    upstream limits their body size except the global stream guard in
    main.py; this ceiling is tighter because a form field has no business
    being large.
    """
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        raise ApiError(413, "request_too_large", "form body is larger than expected")
    from urllib.parse import parse_qsl

    return {key: value for key, value in parse_qsl(body.decode("utf-8", "replace"))}


# --- Claim and login -------------------------------------------------------


@router.get("/claim", response_class=HTMLResponse)
def claim_form(admin: AdminServices = Depends(get_admin_services)) -> HTMLResponse:
    if admin.credentials.is_claimed():
        return RedirectResponse("/admin/login", status_code=303)  # type: ignore[return-value]
    return HTMLResponse(tpl.claim_page())


@router.post("/claim", response_class=HTMLResponse)
async def claim_submit(
    request: Request, admin: AdminServices = Depends(get_admin_services)
) -> HTMLResponse:
    form = await _form(request)
    try:
        admin.credentials.claim(form.get("code", ""), form.get("password", ""))
    except AlreadyClaimed:
        return HTMLResponse(tpl.login_page("this instance is already claimed"), status_code=409)
    except InvalidClaimCode:
        return HTMLResponse(tpl.claim_page("that claim code is not valid"), status_code=422)
    except PasswordTooShort:
        return HTMLResponse(
            tpl.claim_page("the password must be at least 12 characters"), status_code=422
        )
    return RedirectResponse("/admin/login", status_code=303)  # type: ignore[return-value]


@router.get("/login", response_class=HTMLResponse)
def login_form(admin: AdminServices = Depends(get_admin_services)) -> HTMLResponse:
    if not admin.credentials.is_claimed():
        return RedirectResponse("/admin/claim", status_code=303)  # type: ignore[return-value]
    return HTMLResponse(tpl.login_page())


@router.post("/login")
async def login_submit(request: Request, admin: AdminServices = Depends(get_admin_services)) -> Any:
    if not admin.credentials.is_claimed():
        return RedirectResponse("/admin/claim", status_code=303)
    form = await _form(request)
    try:
        admin.credentials.verify_password(form.get("password", ""))
    except WrongPassword:
        return HTMLResponse(tpl.login_page("wrong password"), status_code=401)
    response = RedirectResponse("/admin", status_code=303)
    set_session_cookie(response, admin, request)
    return response


@router.get("/password", response_class=HTMLResponse)
def change_password_form(
    admin: AdminServices = Depends(require_admin_session_html),
) -> HTMLResponse:
    return HTMLResponse(tpl.change_password_page())


@router.post("/password", response_class=HTMLResponse)
async def change_password_submit(
    request: Request, admin: AdminServices = Depends(require_admin_session_html)
) -> Any:
    form = await _form(request)
    try:
        admin.credentials.change_password(
            form.get("current_password", ""), form.get("new_password", "")
        )
    except WrongPassword:
        return HTMLResponse(tpl.change_password_page("wrong current password"), status_code=401)
    except PasswordTooShort:
        return HTMLResponse(
            tpl.change_password_page("the new password must be at least 12 characters"),
            status_code=422,
        )
    response = HTMLResponse(
        tpl.change_password_page(
            "password changed; every other session (including this one) has been signed out",
            "ok",
        )
    )
    clear_session_cookie(response)
    return response


@router.post("/logout")
def logout(admin: AdminServices = Depends(get_admin_services)) -> Any:
    response = RedirectResponse("/admin/login", status_code=303)
    clear_session_cookie(response)
    return response


# --- Status ------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def status_html(
    admin: AdminServices = Depends(require_admin_session_html),
    services: Services = Depends(get_services),
) -> HTMLResponse:
    return HTMLResponse(tpl.status_page(build_status(admin, services)))


@api_router.get("/status")
def status_json(
    admin: AdminServices = Depends(require_admin_session_json),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    return build_status(admin, services)


# --- GitHub App manifest flow -------------------------------------------


@router.get("/github/connect", response_class=HTMLResponse)
def github_connect(
    request: Request, admin: AdminServices = Depends(require_admin_session_html)
) -> Any:
    app_name = f"{admin.settings.app_name_prefix}-{crypto.instance_id(admin.instance_key)}"
    manifest = build_manifest(admin.settings, app_name)
    state = secrets.token_urlsafe(24)
    response = HTMLResponse(
        tpl.github_connect_page(json.dumps(manifest), state, admin.settings.external_url)
    )
    set_github_state_cookie(response, admin, request, state)
    return response


@router.get("/github/callback", response_class=HTMLResponse)
def github_callback(
    request: Request,
    code: str,
    state: str,
    admin: AdminServices = Depends(require_admin_session_html),
) -> HTMLResponse:
    cookie_state = request.cookies.get(GITHUB_STATE_COOKIE_NAME, "")
    if not cookie_state or not secrets.compare_digest(cookie_state, state):
        return HTMLResponse(
            tpl.page(
                "Connect GitHub", "<p>state did not match; start over.</p>", active=tpl.GITHUB_TAB
            ),
            status_code=400,
        )
    try:
        conversion = admin.github_client.exchange_manifest_code(code)
    except GitHubApiError as exc:
        # There is no App record yet on this, the very first exchange, so
        # record_error() would raise instead of returning; only record an
        # error once a record actually exists to record it against.
        if admin.github_store.load() is not None:
            admin.github_store.record_error(exc.error_class)
        return HTMLResponse(
            tpl.page(
                "Connect GitHub",
                f"<p>GitHub exchange failed: {exc.error_class}</p>",
                active=tpl.GITHUB_TAB,
            ),
            status_code=502,
        )
    admin.github_store.store_new_app(
        app_id=conversion.app_id,
        slug=conversion.slug,
        client_id=conversion.client_id,
        client_secret=conversion.client_secret,
        webhook_secret=conversion.webhook_secret,
        pem=conversion.pem,
        html_url=conversion.html_url,
    )
    body = (
        f"<p>App <code>{conversion.slug}</code> created.</p>"
        f'<p><a href="{conversion.html_url}/installations/new">Install this App</a>, '
        f'then come back to <a href="/admin/github/install">choose the installation</a>.</p>'
    )
    return HTMLResponse(tpl.page("GitHub App connected", body, active=tpl.GITHUB_TAB))


@router.post("/github/connect/org", response_class=HTMLResponse)
async def github_connect_org(
    request: Request, admin: AdminServices = Depends(require_admin_session_html)
) -> Any:
    form = await _form(request)
    org = form.get("org", "").strip()
    manifest_json = form.get("manifest", "")
    if not org:
        return HTMLResponse(
            tpl.page(
                "Connect GitHub", "<p>an organization login is required.</p>", active=tpl.GITHUB_TAB
            ),
            status_code=422,
        )
    state = secrets.token_urlsafe(24)
    target_url = manifest_target_url(admin.settings, state, org=org)
    response = HTMLResponse(tpl.github_connect_org_page(manifest_json, state, target_url))
    set_github_state_cookie(response, admin, request, state)
    return response


@router.get("/github/install", response_class=HTMLResponse)
def github_install_form(admin: AdminServices = Depends(require_admin_session_html)) -> HTMLResponse:
    record = admin.github_store.load()
    if record is None:
        return HTMLResponse(
            tpl.page(
                "Choose installation", "<p>connect a GitHub App first.</p>", active=tpl.GITHUB_TAB
            ),
            status_code=409,
        )
    try:
        app_jwt = admin.github_client.mint_app_jwt(record.app_id, admin.github_store.pem(record))
        installations = admin.github_client.list_installations(app_jwt)
    except GitHubApiError as exc:
        admin.github_store.record_error(exc.error_class)
        return HTMLResponse(
            tpl.page(
                "Choose installation",
                f"<p>could not list installations: {exc.error_class}</p>",
                active=tpl.GITHUB_TAB,
            ),
            status_code=502,
        )
    return HTMLResponse(tpl.github_install_page(installations))


@router.post("/github/install")
async def github_install_submit(
    request: Request, admin: AdminServices = Depends(require_admin_session_html)
) -> Any:
    form = await _form(request)
    installation_id = form.get("pasted_id") or form.get("installation_id") or ""
    if not installation_id:
        return HTMLResponse(
            tpl.page(
                "Choose installation",
                "<p>an installation id is required.</p>",
                active=tpl.GITHUB_TAB,
            ),
            status_code=422,
        )
    admin.github_store.set_installation(installation_id)
    return RedirectResponse("/admin/github/repo", status_code=303)


@router.get("/github/repo", response_class=HTMLResponse)
def github_repo_form(admin: AdminServices = Depends(require_admin_session_html)) -> HTMLResponse:
    record = admin.github_store.load()
    if record is None or not record.installation_id:
        return HTMLResponse(
            tpl.page(
                "Choose repository", "<p>choose an installation first.</p>", active=tpl.GITHUB_TAB
            ),
            status_code=409,
        )
    try:
        token = _installation_token(admin, record.installation_id)
        repos = admin.github_client.list_installation_repositories(token)
    except GitHubApiError as exc:
        admin.github_store.record_error(exc.error_class)
        return HTMLResponse(
            tpl.page(
                "Choose repository",
                f"<p>could not list repositories: {exc.error_class}</p>",
                active=tpl.GITHUB_TAB,
            ),
            status_code=502,
        )
    return HTMLResponse(tpl.github_repo_page(repos))


@router.post("/github/repo")
async def github_repo_submit(
    request: Request, admin: AdminServices = Depends(require_admin_session_html)
) -> Any:
    form = await _form(request)
    full_name = form.get("repo", "")
    if "/" not in full_name:
        return HTMLResponse(
            tpl.page("Choose repository", "<p>pick a repository.</p>", active=tpl.GITHUB_TAB),
            status_code=422,
        )
    owner, repo = full_name.split("/", 1)
    record = admin.github_store.load()
    if record is None or not record.installation_id:
        return HTMLResponse(
            tpl.page(
                "Choose repository", "<p>choose an installation first.</p>", active=tpl.GITHUB_TAB
            ),
            status_code=409,
        )
    try:
        token = _installation_token(admin, record.installation_id)
        repos = admin.github_client.list_installation_repositories(token)
        default_branch = next(
            (r.get("default_branch", "main") for r in repos if r["full_name"] == full_name), "main"
        )
        contents_ok = (
            admin.github_client.get_file(token, owner, repo, "README.md", "HEAD") is not None
        )
        prs = admin.github_client.list_pull_requests(token, owner, repo)
        pulls_ok = isinstance(prs, list)
    except GitHubApiError as exc:
        admin.github_store.record_error(exc.error_class)
        return HTMLResponse(
            tpl.page(
                "Choose repository",
                f"<p>verification failed: {exc.error_class}</p>",
                active=tpl.GITHUB_TAB,
            ),
            status_code=502,
        )
    admin.github_store.set_repo(full_name, default_branch)
    admin.github_store.record_verification(
        {
            "contents": "read" if contents_ok else "unverified",
            "pull_requests": "read" if pulls_ok else "unverified",
        }
    )
    return RedirectResponse("/admin", status_code=303)


def _installation_token(admin: AdminServices, installation_id: str) -> str:
    cached = admin.cached_installation_token(installation_id)
    if cached is not None:
        expires = datetime.fromisoformat(cached.expires_at)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires > datetime.now(tz=UTC):
            return cached.token
    record = admin.github_store.load()
    assert record is not None
    app_jwt = admin.github_client.mint_app_jwt(record.app_id, admin.github_store.pem(record))
    minted = admin.github_client.mint_installation_token(app_jwt, installation_id)
    admin.cache_installation_token(
        installation_id, InstallationToken(token=minted["token"], expires_at=minted["expires_at"])
    )
    return str(minted["token"])


# --- Digest --------------------------------------------------------------


@router.post("/digest")
def trigger_digest(
    admin: AdminServices = Depends(require_admin_session_html),
    services: Services = Depends(get_services),
) -> Any:
    # Acquired synchronously, before the worker even starts, so a second
    # request that arrives before the thread has set digest_running still
    # sees the lock held instead of racing it (spec: one digest at a time).
    if not admin._digest_lock.acquire(blocking=False):
        return HTMLResponse(
            tpl.page(
                "Chronicle admin", "<p>a digest is already running.</p>", active=tpl.STATUS_TAB
            ),
            status_code=409,
        )
    admin.digest_running = True

    def worker() -> None:
        try:
            run_digest(services.store, "scott", admin)
        except Exception:  # noqa: BLE001 - best-effort background job, status page shows the result
            pass
        finally:
            admin.digest_running = False
            admin._digest_lock.release()

    threading.Thread(target=worker, daemon=True).start()
    return RedirectResponse("/admin", status_code=303)


class DigestResult(BaseModel):
    created: int
    updated: int
    unchanged: int


@api_router.post("/digest")
def trigger_digest_json(
    admin: AdminServices = Depends(require_admin_session_json),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    if not admin._digest_lock.acquire(blocking=False):
        raise ApiError(409, "digest_already_running", "a digest is already running")
    admin.digest_running = True
    try:
        summary = run_digest(services.store, "scott", admin)
    finally:
        admin.digest_running = False
        admin._digest_lock.release()
    return summary.as_dict()


# --- Reconciliation (spec section 12) ---------------------------------------


@router.post("/reconcile")
def trigger_reconcile(
    admin: AdminServices = Depends(require_admin_session_html),
    services: Services = Depends(get_services),
) -> Any:
    # A manual run outside the hourly loop, so an admin can act on a flag
    # right after fixing what caused it rather than waiting for the clock;
    # run_once_logged already swallows "not configured" rather than
    # raising, so this is a plain redirect either way.
    run_reconcile(services.store, admin)
    return RedirectResponse("/admin", status_code=303)


@router.post("/reconcile/{flag_id}/resolve", response_class=HTMLResponse)
async def resolve_flag(
    flag_id: str,
    request: Request,
    admin: AdminServices = Depends(require_admin_session_html),
    services: Services = Depends(get_services),
) -> Any:
    form = await _form(request)
    services.store.resolve_flag(flag_id, form.get("resolution", ""), "admin")
    return RedirectResponse("/admin", status_code=303)


class FlagResolve(BaseModel):
    resolution: str


@api_router.post("/reconcile/{flag_id}/resolve")
def resolve_flag_json(
    flag_id: str,
    payload: FlagResolve,
    admin: AdminServices = Depends(require_admin_session_json),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    if payload.resolution not in RECONCILE_RESOLUTIONS:
        raise ApiError(
            422,
            "resolution_unknown",
            f"resolution must be one of {', '.join(RECONCILE_RESOLUTIONS)}",
        )
    flag = services.store.resolve_flag(flag_id, payload.resolution, "admin")
    return flag.model_dump(mode="json")


# --- Tokens ----------------------------------------------------------------


def _token_rows(services: Services) -> list[dict[str, Any]]:
    return [record.model_dump(mode="json") for record in services.tokens.load()]


@router.get("/tokens", response_class=HTMLResponse)
def tokens_html(
    admin: AdminServices = Depends(require_admin_session_html),
    services: Services = Depends(get_services),
) -> HTMLResponse:
    ui_disabled = services.tokens.ui_disabled_path.exists()
    return HTMLResponse(tpl.tokens_page(_token_rows(services), ui_disabled=ui_disabled))


@router.post("/tokens", response_class=HTMLResponse)
async def tokens_issue(
    request: Request,
    admin: AdminServices = Depends(require_admin_session_html),
    services: Services = Depends(get_services),
) -> HTMLResponse:
    form = await _form(request)
    name = form.get("name", "").strip()
    if not name:
        return HTMLResponse(
            tpl.tokens_page(_token_rows(services), "a name is required"), status_code=422
        )
    if name in RESERVED_TOKEN_NAMES:
        return HTMLResponse(
            tpl.tokens_page(_token_rows(services), f"{name!r} is reserved for the UI backend"),
            status_code=422,
        )
    token = services.tokens.issue(name)
    return HTMLResponse(tpl.tokens_page(_token_rows(services), minted=token))


@router.post("/tokens/{name}/revoke", response_class=HTMLResponse)
def tokens_revoke(
    name: str,
    admin: AdminServices = Depends(require_admin_session_html),
    services: Services = Depends(get_services),
) -> HTMLResponse:
    services.tokens.revoke(name)
    ui_disabled = services.tokens.ui_disabled_path.exists()
    return HTMLResponse(tpl.tokens_page(_token_rows(services), ui_disabled=ui_disabled))


@router.post("/tokens/ui/reenable", response_class=HTMLResponse)
def tokens_reenable_ui(
    admin: AdminServices = Depends(require_admin_session_html),
    services: Services = Depends(get_services),
) -> HTMLResponse:
    token = services.tokens.reenable_ui_token()
    return HTMLResponse(tpl.tokens_page(_token_rows(services), minted=token))


# --- Backup and restore (spec section 13, ADR 016) -------------------------

LAST_BACKUP_FILE_NAME = "last_backup.json"
BACKUP_TMP_DIR_NAME = "backup-tmp"
UPLOAD_CHUNK_BYTES = 1 << 20
# Long enough to fill in the confirm-page form, short enough that an upload
# abandoned by a Cancel, a closed tab, or a lost session does not sit under
# state/backup-tmp/ forever. A file older than this is stale by definition:
# nothing in this flow re-uses a token past the confirm step that follows
# the upload directly.
UPLOAD_TTL_SECONDS = 3600


def _data_dir(admin: AdminServices) -> Path:
    return admin.state_dir.parent


def _backup_tmp_dir(admin: AdminServices) -> Path:
    tmp_dir = admin.state_dir / BACKUP_TMP_DIR_NAME
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return tmp_dir


def cleanup_stale_backup_uploads(admin: AdminServices, now: float | None = None) -> None:
    """Remove every staged upload older than UPLOAD_TTL_SECONDS.

    Called at api startup (main.py:_start_services) so a restart bounds
    however much an operator's abandoned uploads have piled up, and again
    on every visit to the backup page and every new upload, so the sweep
    also runs without a restart in between.
    """
    cutoff = (now if now is not None else time.time()) - UPLOAD_TTL_SECONDS
    for path in _backup_tmp_dir(admin).glob("upload-*.tar.gz"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            continue


def _read_last_backup(admin: AdminServices) -> str | None:
    path = admin.state_dir / LAST_BACKUP_FILE_NAME
    if not path.exists():
        return None
    try:
        return str(json.loads(path.read_text(encoding="utf-8"))["created_at"])
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def _record_last_backup(admin: AdminServices) -> None:
    path = admin.state_dir / LAST_BACKUP_FILE_NAME
    payload = {"created_at": datetime.now(tz=UTC).isoformat(timespec="seconds")}
    path.write_text(json.dumps(payload), encoding="utf-8")


def _backup_page(
    admin: AdminServices,
    *,
    notice: str | None = None,
    notice_kind: str = "error",
    status_code: int = 200,
) -> HTMLResponse:
    return HTMLResponse(
        tpl.backup_page(
            last_backup=_read_last_backup(admin),
            schedule=scheduled_backup.load_settings(admin.state_dir),
            schedule_summary=scheduled_backup_summary(admin),
            notice=notice,
            notice_kind=notice_kind,
        ),
        status_code=status_code,
    )


@router.get("/backup", response_class=HTMLResponse)
def backup_page(admin: AdminServices = Depends(require_admin_session_html)) -> HTMLResponse:
    cleanup_stale_backup_uploads(admin)
    return _backup_page(admin)


def _int_field(form: dict[str, str], name: str, default: int) -> int:
    try:
        return int(form.get(name, "").strip())
    except ValueError:
        return default


@router.post("/backup/schedule", response_class=HTMLResponse)
async def backup_schedule_save(
    request: Request,
    _origin: None = Depends(check_same_origin),
    admin: AdminServices = Depends(require_admin_session_html),
) -> HTMLResponse:
    """Save the scheduled-backup settings (issue #68). Same-origin only: the
    target decides where a copy of the whole data directory goes, so a
    same-site page riding the admin cookie must not be able to change it and
    then press Run now (ADR 024). A blank secret keeps
    the saved one; a new one is encrypted with the instance key before it
    touches disk, and is never echoed back or logged."""
    form = await _form(request)
    current = scheduled_backup.load_settings(admin.state_dir)
    secret = form.get("s3_secret", "")
    candidate = scheduled_backup.BackupSettings(
        enabled=form.get("enabled") == "1",
        interval_hours=_int_field(form, "interval_hours", current.interval_hours),
        time_of_day=form.get("time_of_day", "").strip(),
        retention=min(max(_int_field(form, "retention", current.retention), 1), 365),
        target=form.get("target", "local"),
        local_path=form.get("local_path", "").strip(),
        s3_endpoint=form.get("s3_endpoint", "").strip(),
        s3_bucket=form.get("s3_bucket", "").strip(),
        s3_prefix=form.get("s3_prefix", "").strip(),
        s3_region=form.get("s3_region", "").strip(),
        s3_access_key_id=form.get("s3_access_key_id", "").strip(),
        s3_secret_enc=(
            crypto.encrypt(admin.instance_key, secret) if secret else current.s3_secret_enc
        ),
        updated_at=datetime.now(tz=UTC).isoformat(timespec="seconds"),
    )
    problems = scheduled_backup.settings_problems(candidate, _data_dir(admin))
    if problems and candidate.enabled:
        return _backup_page(
            admin, notice="Not saved: " + "; ".join(problems) + ".", status_code=400
        )
    scheduled_backup.save_settings(admin.state_dir, candidate)
    note = "Schedule saved." if not problems else "Saved (disabled): " + "; ".join(problems) + "."
    return _backup_page(admin, notice=note, notice_kind="ok" if not problems else "error")


@router.post("/backup/test", response_class=HTMLResponse)
def backup_schedule_test(
    _origin: None = Depends(check_same_origin),
    admin: AdminServices = Depends(require_admin_session_html),
) -> HTMLResponse:
    """Write and delete a small probe at the saved target."""
    settings = scheduled_backup.load_settings(admin.state_dir)
    problem = scheduled_backup.test_target(settings, _data_dir(admin), admin.instance_key)
    if problem:
        return _backup_page(admin, notice=f"Target test failed: {problem}", status_code=502)
    return _backup_page(
        admin, notice="Target test passed: wrote and removed a probe.", notice_kind="ok"
    )


@router.post("/backup/run", response_class=HTMLResponse)
def backup_schedule_run(
    _origin: None = Depends(check_same_origin),
    admin: AdminServices = Depends(require_admin_session_html),
) -> HTMLResponse:
    """Start one scheduled-style backup now, in the background: a bundle can
    take longer than a request should. Its outcome shows on this page."""
    if not scheduled_backup.start_run_now(_data_dir(admin), admin.state_dir, admin.instance_key):
        return _backup_page(
            admin,
            notice="A backup (or a restore) is already running; try again when it finishes.",
            status_code=409,
        )
    return _backup_page(
        admin,
        notice="Backup started; reload this page to see its outcome.",
        notice_kind="ok",
    )


@router.get("/backup/create")
def backup_create_download(admin: AdminServices = Depends(require_admin_session_html)) -> Any:
    # Written under state/backup-tmp/, never a web-served path (spec
    # section 10): the file exists only long enough to stream, and the
    # generator's `finally` deletes it whether the download completes,
    # fails partway, or the client disconnects.
    bundle_path = backup_mod.create_backup(_data_dir(admin), _backup_tmp_dir(admin))
    _record_last_backup(admin)

    def stream() -> Any:
        try:
            with bundle_path.open("rb") as handle:
                while True:
                    chunk = handle.read(1 << 20)
                    if not chunk:
                        break
                    yield chunk
        finally:
            bundle_path.unlink(missing_ok=True)

    return StreamingResponse(
        stream(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{bundle_path.name}"'},
    )


@router.post("/backup/upload", response_class=HTMLResponse)
async def backup_upload(
    admin: AdminServices = Depends(require_admin_session_html),
    file: UploadFile = File(...),
) -> HTMLResponse:
    """Save the upload and show its manifest counts, without restoring
    anything yet: `backup_restore` below re-validates checksums and member
    safety from scratch, so this preview only ever reads `manifest.json`."""
    # Deferred import: see the same note on the circular import in
    # backup_restore below.
    from .. import main as main_module

    cleanup_stale_backup_uploads(admin)
    token = secrets.token_hex(16)
    staged_path = _backup_tmp_dir(admin) / f"upload-{token}.tar.gz"
    total = 0
    # Copied in bounded chunks, never `await file.read()`: reading the
    # whole upload into one `bytes` object risks OOMing the api process,
    # which the reference deployment caps at 512 MiB of memory
    # (examples/k8s/deployment.yaml) while this route allows uploads
    # nearly that large. BodySizeLimitMiddleware already rejects an
    # oversized request before it reaches this route; this loop enforces
    # the same ceiling again on what actually gets written to disk.
    with staged_path.open("wb") as out:
        while True:
            chunk = await file.read(UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > main_module.MAX_BACKUP_UPLOAD_BYTES:
                out.close()
                staged_path.unlink(missing_ok=True)
                return HTMLResponse(
                    tpl.backup_page(
                        last_backup=_read_last_backup(admin), notice="upload too large"
                    ),
                    status_code=413,
                )
            out.write(chunk)
    try:
        with tarfile.open(staged_path, "r:gz") as tar:
            manifest_bytes = tar.extractfile(tar.getmember(backup_mod.MANIFEST_NAME))
            if manifest_bytes is None:
                raise KeyError(backup_mod.MANIFEST_NAME)
            manifest = json.loads(manifest_bytes.read())
    except (tarfile.TarError, KeyError, json.JSONDecodeError) as exc:
        staged_path.unlink(missing_ok=True)
        return HTMLResponse(
            tpl.backup_page(
                last_backup=_read_last_backup(admin), notice=f"not a valid bundle: {exc}"
            ),
            status_code=400,
        )
    return HTMLResponse(tpl.backup_confirm_page(token=token, manifest=manifest))


def _is_upload_token(token: str) -> bool:
    # Must match exactly what backup_upload mints (secrets.token_hex(16)):
    # 32 lowercase hex characters, never a path segment taken from the form
    # value as-is. `staged_path` below is built from this token, so an
    # unvalidated value (a `..` segment, an absolute path) would let the
    # confirm step read and then delete an arbitrary file on disk.
    return len(token) == 32 and all(c in "0123456789abcdef" for c in token)


@router.post("/backup/restore", response_class=HTMLResponse)
def backup_restore(
    request: Request,
    token: str = Form(...),
    confirm: str = Form(...),
    admin: AdminServices = Depends(require_admin_session_html),
) -> HTMLResponse:
    if confirm.strip() != "restore":
        return HTMLResponse(
            tpl.backup_page(
                last_backup=_read_last_backup(admin), notice='type "restore" to confirm'
            ),
            status_code=400,
        )
    if not _is_upload_token(token):
        return HTMLResponse(
            tpl.backup_page(
                last_backup=_read_last_backup(admin), notice="upload expired, try again"
            ),
            status_code=400,
        )
    staged_path = _backup_tmp_dir(admin) / f"upload-{token}.tar.gz"
    if not staged_path.exists() or (time.time() - staged_path.stat().st_mtime > UPLOAD_TTL_SECONDS):
        staged_path.unlink(missing_ok=True)
        return HTMLResponse(
            tpl.backup_page(
                last_backup=_read_last_backup(admin), notice="upload expired, try again"
            ),
            status_code=400,
        )

    # Deferred import: `main` imports this module at startup to mount the
    # router, so importing it back at module scope here would be circular.
    from .. import main as main_module

    data_dir = _data_dir(admin)
    # restore_backup's own contract is that the api process is stopped
    # first: its tree swap is a bare shutil.move, not taken under any lock,
    # on the single-writer assumption ADR 013 makes for the publisher. This
    # route can't stop the process, so it gets the same effect by stopping
    # the publisher, watcher, reconcile threads, and the store's own SQLite
    # connection before the swap runs, never after; a write racing the swap
    # would otherwise land in the pre-restore tree this same call deletes.
    main_module.quiesce_for_restore(request.app)
    try:
        # A scheduled or "Run now" backup still reading the tree would ship a
        # torn bundle that counts toward retention; wait for it to finish,
        # and hold off any other until the swap is done (ADR 024).
        with scheduled_backup.run_lock():
            report = backup_mod.restore_backup(data_dir, staged_path)
    except backup_mod.BackupError as exc:
        return HTMLResponse(
            tpl.backup_page(last_backup=_read_last_backup(admin), notice=str(exc)), status_code=400
        )
    finally:
        staged_path.unlink(missing_ok=True)
        main_module.resume_after_restore(request.app)

    notice = f"restored: before={report.before} after={report.after}"
    if report.credentials_decryptable is False:
        notice += (
            "; github-app.json could not be decrypted under this instance's key"
            " (reconnect GitHub through the manifest flow)"
        )
    return HTMLResponse(
        tpl.backup_page(last_backup=_read_last_backup(admin), notice=notice, notice_kind="ok")
    )


@api_router.get("/tokens")
def tokens_json(
    admin: AdminServices = Depends(require_admin_session_json),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    return {"tokens": _token_rows(services)}


class TokenIssue(BaseModel):
    name: str


@api_router.post("/tokens")
def tokens_issue_json(
    payload: TokenIssue,
    admin: AdminServices = Depends(require_admin_session_json),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    if payload.name in RESERVED_TOKEN_NAMES:
        raise ApiError(
            422, "token_name_reserved", f"{payload.name!r} is reserved for the UI backend"
        )
    token = services.tokens.issue(payload.name)
    return {"name": payload.name, "token": token}


@api_router.post("/tokens/{name}/revoke")
def tokens_revoke_json(
    name: str,
    admin: AdminServices = Depends(require_admin_session_json),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    revoked = services.tokens.revoke(name)
    return {"revoked": revoked}


# --- Toolchain (issue #67, ADR 026) ------------------------------------------


def _toolchain_page(
    admin: AdminServices,
    *,
    notice: str | None = None,
    notice_kind: str = "error",
    status_code: int = 200,
) -> HTMLResponse:
    result = toolchain_check.load_result(admin.state_dir)
    hugo = (result or {}).get("hugo") or {}
    latest, image = hugo.get("latest"), hugo.get("image")
    return HTMLResponse(
        tpl.toolchain_page(
            result=result,
            actions=toolchain_check.load_actions(admin.state_dir),
            hugo_issue_url=(
                toolchain_check.hugo_issue_url(str(image), str(latest))
                if toolchain_check.hugo_behind(image, latest)
                else None
            ),
            hugo_release_url=(
                toolchain_check.HUGO_RELEASE_URL.format(version=latest) if latest else None
            ),
            check_running=toolchain_check.check_running(),
            notice=notice,
            notice_kind=notice_kind,
        ),
        status_code=status_code,
    )


@router.get("/toolchain", response_class=HTMLResponse)
def toolchain_page(admin: AdminServices = Depends(require_admin_session_html)) -> HTMLResponse:
    return _toolchain_page(admin)


@router.post("/toolchain/check", response_class=HTMLResponse)
def toolchain_check_now(
    _origin: None = Depends(check_same_origin),
    admin: AdminServices = Depends(require_admin_session_html),
    services: Services = Depends(get_services),
) -> HTMLResponse:
    if not toolchain_check.start_check_now(services.store, admin):
        return _toolchain_page(admin, notice="A check is already running.", status_code=409)
    return _toolchain_page(
        admin, notice="Check started; reload this page to see the result.", notice_kind="ok"
    )


def _toolchain_action(admin: AdminServices, act: Any) -> HTMLResponse:
    """Run one PR-opening action against the configured blog repo. Same
    GitHub App (or test-token repo) the publisher uses; nothing configured
    is a refusal, not a crash."""
    try:
        target = build_repo_target(admin)
    except PublishFailed as exc:
        return _toolchain_page(admin, notice=str(exc), status_code=409)
    if target is None:
        return _toolchain_page(
            admin, notice="No GitHub App or test-token repo is configured.", status_code=409
        )
    try:
        record = act(target)
    except toolchain_check.ToolchainActionError as exc:
        return _toolchain_page(admin, notice=str(exc), status_code=409)
    except GitHubApiError as exc:
        return _toolchain_page(admin, notice=f"GitHub refused it: {exc}", status_code=502)
    return _toolchain_page(admin, notice=f"PR opened: {record['pr_url']}", notice_kind="ok")


@router.post("/toolchain/bump", response_class=HTMLResponse)
async def toolchain_bump(
    request: Request,
    _origin: None = Depends(check_same_origin),
    admin: AdminServices = Depends(require_admin_session_html),
) -> HTMLResponse:
    form = await _form(request)
    path, which = form.get("path", ""), form.get("which", "")
    return _toolchain_action(
        admin,
        lambda t: toolchain_check.bump_submodule(
            admin.state_dir, t.ops, t.default_branch, path, which
        ),
    )


@router.post("/toolchain/remove", response_class=HTMLResponse)
async def toolchain_remove(
    request: Request,
    _origin: None = Depends(check_same_origin),
    admin: AdminServices = Depends(require_admin_session_html),
) -> HTMLResponse:
    form = await _form(request)
    path = form.get("path", "")
    return _toolchain_action(
        admin,
        lambda t: toolchain_check.remove_theme(admin.state_dir, t.ops, t.default_branch, path),
    )
