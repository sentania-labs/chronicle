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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

from ... import backup as backup_mod
from .. import admin_templates as tpl
from .. import crypto
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
from ..admin_status import build_status
from ..deps import Services, get_services
from ..digest_runner import run as run_digest
from ..errors import ApiError
from ..github_client import GitHubApiError, build_manifest, manifest_target_url
from ..models import RECONCILE_RESOLUTIONS
from ..reconcile import run_once_logged as run_reconcile
from ..tokens import UI_TOKEN_NAME

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
            tpl.page("Connect GitHub", "<p>state did not match; start over.</p>"), status_code=400
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
            tpl.page("Connect GitHub", f"<p>GitHub exchange failed: {exc.error_class}</p>"),
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
    return HTMLResponse(tpl.page("GitHub App connected", body))


@router.post("/github/connect/org", response_class=HTMLResponse)
async def github_connect_org(
    request: Request, admin: AdminServices = Depends(require_admin_session_html)
) -> Any:
    form = await _form(request)
    org = form.get("org", "").strip()
    manifest_json = form.get("manifest", "")
    if not org:
        return HTMLResponse(
            tpl.page("Connect GitHub", "<p>an organization login is required.</p>"),
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
            tpl.page("Choose installation", "<p>connect a GitHub App first.</p>"), status_code=409
        )
    try:
        app_jwt = admin.github_client.mint_app_jwt(record.app_id, admin.github_store.pem(record))
        installations = admin.github_client.list_installations(app_jwt)
    except GitHubApiError as exc:
        admin.github_store.record_error(exc.error_class)
        return HTMLResponse(
            tpl.page(
                "Choose installation", f"<p>could not list installations: {exc.error_class}</p>"
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
            tpl.page("Choose installation", "<p>an installation id is required.</p>"),
            status_code=422,
        )
    admin.github_store.set_installation(installation_id)
    return RedirectResponse("/admin/github/repo", status_code=303)


@router.get("/github/repo", response_class=HTMLResponse)
def github_repo_form(admin: AdminServices = Depends(require_admin_session_html)) -> HTMLResponse:
    record = admin.github_store.load()
    if record is None or not record.installation_id:
        return HTMLResponse(
            tpl.page("Choose repository", "<p>choose an installation first.</p>"), status_code=409
        )
    try:
        token = _installation_token(admin, record.installation_id)
        repos = admin.github_client.list_installation_repositories(token)
    except GitHubApiError as exc:
        admin.github_store.record_error(exc.error_class)
        return HTMLResponse(
            tpl.page("Choose repository", f"<p>could not list repositories: {exc.error_class}</p>"),
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
            tpl.page("Choose repository", "<p>pick a repository.</p>"), status_code=422
        )
    owner, repo = full_name.split("/", 1)
    record = admin.github_store.load()
    if record is None or not record.installation_id:
        return HTMLResponse(
            tpl.page("Choose repository", "<p>choose an installation first.</p>"), status_code=409
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
            tpl.page("Choose repository", f"<p>verification failed: {exc.error_class}</p>"),
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
            tpl.page("Chronicle admin", "<p>a digest is already running.</p>"), status_code=409
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
    if name == UI_TOKEN_NAME:
        return HTMLResponse(
            tpl.tokens_page(
                _token_rows(services), f"{UI_TOKEN_NAME!r} is reserved for the UI backend"
            ),
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


def _data_dir(admin: AdminServices) -> Path:
    return admin.state_dir.parent


def _backup_tmp_dir(admin: AdminServices) -> Path:
    tmp_dir = admin.state_dir / BACKUP_TMP_DIR_NAME
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return tmp_dir


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


@router.get("/backup", response_class=HTMLResponse)
def backup_page(admin: AdminServices = Depends(require_admin_session_html)) -> HTMLResponse:
    return HTMLResponse(tpl.backup_page(last_backup=_read_last_backup(admin)))


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
    token = secrets.token_hex(16)
    staged_path = _backup_tmp_dir(admin) / f"upload-{token}.tar.gz"
    raw = await file.read()
    staged_path.write_bytes(raw)
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
    if not staged_path.exists():
        return HTMLResponse(
            tpl.backup_page(
                last_backup=_read_last_backup(admin), notice="upload expired, try again"
            ),
            status_code=400,
        )
    try:
        report = backup_mod.restore_backup(_data_dir(admin), staged_path)
    except backup_mod.BackupError as exc:
        return HTMLResponse(
            tpl.backup_page(last_backup=_read_last_backup(admin), notice=str(exc)), status_code=400
        )
    finally:
        staged_path.unlink(missing_ok=True)

    # The swap just replaced the data directory this process's Store,
    # TokenStore, and background threads all still hold handles into.
    # Deferred import: `main` imports this module at startup to mount the
    # router, so importing it back at module scope here would be circular.
    from .. import main as main_module

    main_module.reload_after_restore(request.app)

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
    if payload.name == UI_TOKEN_NAME:
        raise ApiError(
            422, "token_name_reserved", f"{UI_TOKEN_NAME!r} is reserved for the UI backend"
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
