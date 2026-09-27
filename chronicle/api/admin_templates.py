"""Minimal server-rendered HTML for the admin surface.

Plain string templates, not Jinja2: C5 owns the real UI (AGENTS.md, spec
section 14), so this round's bar is function over polish. Every value
interpolated into a page has already been through `html.escape`.
"""

from __future__ import annotations

from html import escape
from typing import Any

from . import ui_chrome
from .tokens import UI_TOKEN_NAME
from .ui_status import label_counts
from .ui_time import local_time

STATUS_TAB = "/admin"
GITHUB_TAB = "/admin/github/connect"
TOKENS_TAB = "/admin/tokens"
BACKUP_TAB = "/admin/backup"
TOOLCHAIN_TAB = "/admin/toolchain"
PASSWORD_TAB = "/admin/password"
NAV_LINKS = (
    (STATUS_TAB, "Status"),
    (GITHUB_TAB, "GitHub"),
    (TOKENS_TAB, "Tokens"),
    (BACKUP_TAB, "Backup"),
    (TOOLCHAIN_TAB, "Toolchain"),
    (PASSWORD_TAB, "Password"),
)

# Log out is a POST (it ends a session), so it is a real form and button, in the
# header beside the theme control on every page that has a session.
LOGOUT_FORM = (
    '<form class="inline" method="post" action="/admin/logout">'
    '<button type="submit" class="lat-btn lat-btn--ghost">Log out</button></form>'
)


def _stamp(value: object, fallback: str) -> str:
    """A stamp as local clock time, or `fallback` when there is no value.

    The fallback word ("never", "no", "?") says why nothing is shown, which
    `local_time`'s own `-` (unreadable stamp) cannot, so it is only converted
    when a value is actually present. A bare `YYYY-MM-DD` value stays a date
    on purpose: `local_time` leaves it unshifted because a date has no
    instant, and shifting it west of UTC would show the day before. Do not
    "fix" that into a clock time.
    """
    return local_time(value) if value else fallback


def page(
    title: str,
    body: str,
    notice: str | None = None,
    notice_kind: str = "error",
    *,
    active: str | None = None,
) -> str:
    """`active` is the href of the tab this page belongs to (one of the `*_TAB`
    constants). Every page reached with an admin session passes one, error and
    confirmation pages included, so the tabs and Log out are always there. Only
    a page with no session behind it (login, claim) leaves it out, and gets the
    header and theme control alone."""
    notice_html = ui_chrome.notice(notice, notice_kind) if notice else ""
    signed_in = active is not None
    header = ui_chrome.header("Chronicle admin", trailing=LOGOUT_FORM if signed_in else "")
    tabs = ui_chrome.tabs(NAV_LINKS, active, label="Admin sections") if signed_in else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{escape(title)}</title>{ui_chrome.head()}</head>
<body>
{header}
<div class="chr-page">
{tabs}
<h1 class="page-title">{escape(title)}</h1>
{notice_html}
{body}
</div>
</body></html>"""


def _cell(cell: Any) -> str:
    if isinstance(cell, int) and not isinstance(cell, bool):
        return f'<td class="lat-num">{cell}</td>'
    return f"<td>{escape(str(cell))}</td>"


def _table(rows: str, head_cells: str = "") -> str:
    """A Lattice table in its scroll wrapper; `head_cells` is the `<th>` cells,
    or empty for a plain key and value table with no header row."""
    thead = f"<thead><tr>{head_cells}</tr></thead>" if head_cells else ""
    return (
        '<div class="lat-table-scroll">'
        f'<table class="lat-table">{thead}<tbody>{rows}</tbody></table></div>'
    )


_NEW_PASSWORD = ' autocomplete="new-password"'


def _schedule_form(settings: Any, summary: dict[str, Any]) -> str:
    """The scheduled-backup settings (issue #68, ADR 024). The S3 secret is a
    write-only field: never echoed, left blank to keep the saved one."""
    from .scheduled_backup import INTERVAL_CHOICES

    def selected(flag: bool) -> str:
        return " selected" if flag else ""

    def checked(flag: bool) -> str:
        return " checked" if flag else ""

    interval_options = "".join(
        f'<option value="{hours}"{selected(settings.interval_hours == hours)}>'
        f"{escape(label)}</option>"
        for hours, label in INTERVAL_CHOICES.items()
    )
    secret_hint = "saved; leave blank to keep" if settings.s3_secret_enc else "not set"

    def field(name: str, label: str, value: str, kind: str = "text", extra: str = "") -> str:
        return (
            f'<label class="lat-label" for="{name}">{escape(label)}</label>'
            f'<input class="lat-input" type="{kind}" id="{name}" name="{name}" '
            f'value="{escape(value)}"{extra}>'
        )

    def choice(kind: str, name: str, value: str, label: str, flag: bool) -> str:
        return (
            f'<label class="lat-label"><input type="{kind}" name="{name}" '
            f'value="{value}"{checked(flag)}> {escape(label)}</label>'
        )

    status_rows = _scheduled_backup_rows(
        lambda *cells: "<tr>" + "".join(_cell(c) for c in cells) + "</tr>", summary
    )
    local_label = "Directory (a mounted volume, not under the data directory)"
    return f"""
<section class="lat-card">
<h2>Scheduled backups</h2>
{_table(status_rows)}
<form method="post" action="/admin/backup/schedule">
{choice("checkbox", "enabled", "1", "Enabled", settings.enabled)}
<label class="lat-label" for="interval_hours">How often</label>
<select class="lat-select" id="interval_hours" name="interval_hours">{interval_options}</select>
{field("time_of_day", "Time of day (HH:MM, America/Chicago)", settings.time_of_day)}
{field("retention", "Keep the newest", str(settings.retention), "number", ' min="1" max="365"')}
<fieldset class="chr-fieldset">
<legend>Target</legend>
{choice("radio", "target", "local", "Local path", settings.target == "local")}
{field("local_path", local_label, settings.local_path)}
{choice("radio", "target", "s3", "S3 bucket", settings.target == "s3")}
{field("s3_endpoint", "Endpoint URL", settings.s3_endpoint)}
{field("s3_bucket", "Bucket", settings.s3_bucket)}
{field("s3_prefix", "Prefix (optional)", settings.s3_prefix)}
{field("s3_region", "Region", settings.s3_region)}
{field("s3_access_key_id", "Access key ID", settings.s3_access_key_id)}
{field("s3_secret", f"Secret access key ({secret_hint})", "", "password", _NEW_PASSWORD)}
</fieldset>
<button type="submit" class="lat-btn lat-btn--primary">Save schedule</button>
</form>
<form method="post" action="/admin/backup/test" class="action-form">
<button type="submit" class="lat-btn">Test target</button>
</form>
<form method="post" action="/admin/backup/run" class="action-form">
<button type="submit" class="lat-btn">Run a backup now</button>
</form>
</section>
"""


def backup_page(
    *,
    last_backup: str | None,
    schedule: Any = None,
    schedule_summary: dict[str, Any] | None = None,
    notice: str | None = None,
    notice_kind: str = "error",
) -> str:
    # A bare YYYY-MM-DD stays a date (no instant to shift); see `_stamp`.
    last_html = escape(_stamp(last_backup, "never"))
    schedule_html = (
        _schedule_form(schedule, schedule_summary)
        if schedule is not None and schedule_summary is not None
        else ""
    )
    body = f"""
<p class="muted">Last backup created: {last_html}</p>
{schedule_html}
<section class="lat-card">
<h2>Create a backup</h2>
<p>Downloads a gzip tarball: repo history, images, and the encrypted
credential store. Never the instance key.</p>
<form method="get" action="/admin/backup/create">
<button type="submit" class="lat-btn lat-btn--primary">Create and download backup</button>
</form>
</section>
<section class="lat-card">
<h2>Restore from a backup</h2>
<p>Upload a bundle to see its manifest counts before anything is touched.
Restoring replaces this instance's repo, images, and credential store; the
running instance's own instance key is kept.</p>
<form method="post" action="/admin/backup/upload" enctype="multipart/form-data">
<label class="lat-label" for="file">Bundle (.tar.gz)</label>
<input type="file" class="lat-input" id="file" name="file" accept=".tar.gz,.tgz" required>
<button type="submit" class="lat-btn">Upload and preview</button>
</form>
</section>
"""
    return page("Backup", body, notice=notice, notice_kind=notice_kind, active=BACKUP_TAB)


def backup_confirm_page(*, token: str, manifest: dict[str, Any]) -> str:
    counts = manifest.get("counts", {})
    rows = "".join(
        f'<tr><td>{escape(str(key))}</td><td class="lat-num">{escape(str(value))}</td></tr>'
        for key, value in sorted(counts.items())
    )
    # A bare YYYY-MM-DD stays a date (no instant to shift); see `_stamp`.
    created = escape(_stamp(manifest.get("created_at"), "?"))
    body = f"""
<p class="muted">Bundle created {created} by Chronicle
{escape(str(manifest.get("chronicle_version", "?")))}.</p>
{_table(rows, '<th>record</th><th class="lat-num">count</th>')}
<p class="lat-banner lat-banner--warn chr-flow">
<strong>This replaces the current repo, images, and credential store.</strong>
If a builder is running against this instance, restart it after the
restore finishes: it holds its own connection to the index and will not
notice the swap on its own. Run <code>chronicle digest</code> again after
restoring, since the site checkout is not part of the bundle.
Type <code>restore</code> below to confirm.</p>
<form method="post" action="/admin/backup/restore">
<input type="hidden" name="token" value="{escape(token)}">
<label class="lat-label" for="confirm">Type "restore" to confirm</label>
<input type="text" class="lat-input" id="confirm" name="confirm" required autocomplete="off">
<button type="submit" class="lat-btn lat-btn--danger">Restore now</button>
</form>
<p><a href="/admin/backup">Cancel</a></p>
"""
    return page("Confirm restore", body, active=BACKUP_TAB)


def claim_page(notice: str | None = None) -> str:
    body = """
<p>This instance has not been claimed. Read the one-time claim code from the
file logged at startup (<code>data/state/claim-code</code>) and set a
password of at least 12 characters.</p>
<form method="post" class="lat-card chr-narrow" action="/admin/claim">
<label class="lat-label" for="code">Claim code</label>
<input type="text" class="lat-input" id="code" name="code" required autocomplete="off">
<label class="lat-label" for="password">New password (12+ characters)</label>
<input type="password" class="lat-input" id="password" name="password" required minlength="12">
<button type="submit" class="lat-btn lat-btn--primary">Claim this instance</button>
</form>
"""
    return page("Claim Chronicle", body, notice)


def login_page(notice: str | None = None) -> str:
    body = """
<form method="post" class="lat-card chr-narrow" action="/admin/login">
<label class="lat-label" for="password">Password</label>
<input type="password" class="lat-input" id="password" name="password" required
 autocomplete="current-password">
<button type="submit" class="lat-btn lat-btn--primary">Log in</button>
</form>
"""
    return page("Admin login", body, notice)


def change_password_page(notice: str | None = None, notice_kind: str = "error") -> str:
    body = """
<p>Changing the password ends every other session immediately.</p>
<form method="post" class="lat-card" action="/admin/password">
<label class="lat-label" for="current_password">Current password</label>
<input type="password" class="lat-input" id="current_password" name="current_password" required>
<label class="lat-label" for="new_password">New password (12+ characters)</label>
<input type="password" class="lat-input" id="new_password" name="new_password"
 required minlength="12">
<button type="submit" class="lat-btn lat-btn--primary">Change password</button>
</form>
"""
    return page("Change password", body, notice, notice_kind, active=PASSWORD_TAB)


def _heartbeat_rows(row: Any, heartbeat: dict[str, Any] | None, name: str) -> str:
    if not heartbeat:
        return f"<tr><td colspan=2>no heartbeat yet; the {name} has not completed a loop</td></tr>"
    # Every `*_at` key is an ISO stamp a human reads (last_loop_at, last_run_at).
    # A missing value renders as it did before; a bare date stays a date.
    return "".join(
        row(key, local_time(value) if key.endswith("_at") and value else value)
        for key, value in heartbeat.items()
    )


def _scheduled_backup_rows(row: Any, summary: dict[str, Any]) -> str:
    """The status page's scheduled-backup rows (issue #68). A last success
    more than two intervals old, or none since the schedule was saved, is a
    warning badge, the same way toolchain drift is."""
    if not summary["enabled"]:
        return row("scheduled backups", "off")
    size = summary["last_size_bytes"]
    warn = (
        "<tr><td>scheduled backups</td><td>"
        + ui_chrome.badge("overdue", "warn")
        + " no successful backup in over two intervals</td></tr>"
        if summary["overdue"]
        else ""
    )
    return (
        warn
        + row("schedule", summary["schedule"])
        + row("target", summary["target"])
        + row("last success", _stamp(summary["last_success_at"], "never"))
        + (row("last size", f"{size:,} bytes") if isinstance(size, int) else "")
        + (row("last bundle", summary["last_location"]) if summary["last_location"] else "")
        + (
            row("last failure", _stamp(summary["last_failure_at"], "-"))
            + row("last error", summary["last_error"] or "-")
            if summary["last_failure_at"]
            else ""
        )
    )


def _flag_rows(row: Any, flags: list[dict[str, Any]]) -> str:
    if not flags:
        return "<tr><td colspan=3>none</td></tr>"
    rows = []
    for flag in flags:
        buttons = "".join(
            f'<form class="inline" method="post" '
            f'action="/admin/reconcile/{escape(flag["id"])}/resolve">'
            f'<input type="hidden" name="resolution" value="{escape(resolution)}">'
            f'<button type="submit" class="lat-btn">{escape(resolution)}</button></form> '
            for resolution in flag["applicable_resolutions"]
        )
        rows.append(
            "<tr>"
            f"<td>{escape(flag['type'])}</td>"
            f"<td>{escape(flag['detail'])}</td>"
            f"<td>{buttons}</td>"
            "</tr>"
        )
    return "".join(rows)


def status_page(status: dict[str, Any], notice: str | None = None) -> str:
    def row(*cells: Any) -> str:
        # A whole number is a figure: Lattice sets those right-aligned in mono.
        return "<tr>" + "".join(_cell(cell) for cell in cells) + "</tr>"

    disk_rows = "".join(row(name, size) for name, size in status["disk_use"].items())
    submission_rows = "".join(
        row(name, count) for name, count in status["submissions_by_status"].items()
    )
    # The four words a reader sees, not the state machine's eight statuses.
    draft_rows = "".join(
        row(label, count) for label, count in label_counts(status["drafts_by_status"]).items()
    )
    theme_rows = (
        "".join(row(t["path"], t["commit"]) for t in status["toolchain"]["submodules"])
        or "<tr><td colspan=2>none</td></tr>"
    )
    builder_rows = _heartbeat_rows(row, status["builder_heartbeat"], "builder")
    publisher_rows = _heartbeat_rows(row, status.get("publisher_heartbeat"), "publisher")
    watcher_rows = _heartbeat_rows(row, status.get("watcher_heartbeat"), "watcher")
    reconcile_rows = _heartbeat_rows(row, status.get("reconcile_heartbeat"), "reconcile")
    preview_run_rows = "".join(
        row(name, count) for name, count in status["preview_runs_by_status"].items()
    )
    flag_rows = _flag_rows(row, status.get("reconcile_flags", []))
    conventions = status["toolchain"].get("conventions")
    if conventions:
        conventions_rows = (
            row("source", conventions["source"])
            + row("environment", conventions["environment"])
            + row("contentdir", conventions["contentdir"])
            + row("post directory (observed)", conventions.get("postdir") or "none observed")
            + row("staticdir", conventions["staticdir"])
            + row("mainsections", ", ".join(conventions["mainsections"]) or "none")
            + row(
                "unconfigured taxonomy keys",
                ", ".join(conventions["unconfigured_taxonomy_keys"]) or "none",
            )
        )
        if conventions["source"] == "fallback":
            conventions_rows += row("fallback reason", conventions["fallback_reason"])
    else:
        conventions_rows = row("conventions", "no digest has run yet")
    # The state sentence embeds a raw ISO stamp that /readyz must keep, so the
    # stamp travels separately and the page rebuilds the same wording from it.
    verified_at = status.get("github_app_verified_at")
    app_state = (
        f"verified at {local_time(verified_at)}" if verified_at else status["github_app_state"]
    )
    # last backup and last digest go through `_stamp`: the words "never" survive
    # and a bare YYYY-MM-DD stays a date (no instant to shift).
    toolchain = status["toolchain"]
    toolchain_row = (
        "<tr><td>toolchain</td><td>"
        + (
            ui_chrome.badge("match", "ok")
            if toolchain["match"]
            else ui_chrome.badge("drift", "warn")
        )
        + "</td></tr>"
    )
    none_row = "<tr><td colspan=2>none</td></tr>"
    body = f"""
<div class="chr-grid">
<section class="lat-card">
<h2>GitHub App</h2>
{
        _table(
            row("state", app_state)
            + row("repo", status["github_repo"] or "none chosen")
            + row("default branch", status["github_default_branch"] or "-")
        )
    }
<form method="post" action="/admin/digest">
<button type="submit" class="lat-btn">Run digest now</button>
</form>
</section>
<section class="lat-card">
<h2>Backup</h2>
{
        _table(
            row("last backup", _stamp(status["last_backup_at"], "never"))
            + _scheduled_backup_rows(row, status["scheduled_backup"])
        )
    }
<p><a href="/admin/backup">Backup and restore</a></p>
</section>
<section class="lat-card">
<h2>Digest</h2>
{
        _table(
            row("last digest", _stamp(status["last_digest_at"], "never"))
            + row("post count", status["post_count"])
            + row("hugo version (site)", toolchain["hugo_version"])
            + row("hugo version (builder)", toolchain["builder_hugo_version"])
            + toolchain_row
        )
    }
<h3>Content conventions</h3>
{_table(conventions_rows)}
<h3>Theme submodules</h3>
{_table(theme_rows, "<th>path</th><th>commit</th>")}
</section>
<section class="lat-card">
<h2>Builder</h2>
{_table(builder_rows)}
<h3>Preview runs by status</h3>
{_table(preview_run_rows or none_row)}
</section>
<section class="lat-card">
<h2>Publisher</h2>
{_table(publisher_rows)}
</section>
<section class="lat-card">
<h2>Watcher</h2>
{_table(watcher_rows)}
</section>
<section class="lat-card chr-span">
<h2>Reconciliation</h2>
{_table(reconcile_rows)}
<form method="post" action="/admin/reconcile">
<button type="submit" class="lat-btn">Run reconciliation now</button>
</form>
<h3>Open flags</h3>
{_table(flag_rows, "<th>type</th><th>detail</th><th>resolve</th>")}
</section>
<section class="lat-card">
<h2>Submissions by status</h2>
{_table(submission_rows or none_row)}
</section>
<section class="lat-card">
<h2>Posts by status</h2>
{_table(draft_rows or none_row)}
</section>
<section class="lat-card">
<h2>Disk use</h2>
{_table(disk_rows)}
</section>
<section class="lat-card">
<h2>Repository health</h2>
{
        _table(
            row("git status", status["git_health"])
            + row("index schema version", status["index_schema_version"])
        )
    }
</section>
</div>
"""
    return page("Chronicle admin", body, notice, "ok" if notice else "error", active=STATUS_TAB)


def github_connect_page(manifest_json: str, state: str, external_url: str) -> str:
    body = f"""
<p>Submitting this form opens GitHub's own "create a GitHub App" page in your
browser, pre-filled from the manifest below. GitHub creates the App and
redirects your browser back to <code>{escape(external_url)}/admin/github/callback</code>
with a one-time code; Chronicle never sees this step directly.</p>
<form method="post" action="https://github.com/settings/apps/new?state={escape(state)}">
<input type="hidden" name="manifest" value='{escape(manifest_json)}'>
<button type="submit" class="lat-btn lat-btn--primary">Create App on your personal account</button>
</form>
<form method="post" class="lat-card" action="/admin/github/connect/org">
<label class="lat-label" for="org">Organization login (optional target instead)</label>
<input type="text" class="lat-input" id="org" name="org">
<input type="hidden" name="manifest" value='{escape(manifest_json)}'>
<button type="submit" class="lat-btn">Continue to organization form</button>
</form>
<section class="lat-card">
<h2>Manifest</h2>
<pre class="lat-code">{escape(manifest_json)}</pre>
</section>
"""
    return page("Connect GitHub", body, active=GITHUB_TAB)


def github_connect_org_page(manifest_json: str, state: str, target_url: str) -> str:
    body = f"""
<p>Submitting this form opens GitHub's own "create a GitHub App" page for
this organization, pre-filled from the manifest below.</p>
<form method="post" action="{escape(target_url)}">
<input type="hidden" name="manifest" value='{escape(manifest_json)}'>
<button type="submit" class="lat-btn lat-btn--primary">Create App for this organization</button>
</form>
<section class="lat-card">
<h2>Manifest</h2>
<pre class="lat-code">{escape(manifest_json)}</pre>
</section>
"""
    return page("Connect GitHub (organization)", body, active=GITHUB_TAB)


def github_install_page(installations: list[dict[str, Any]]) -> str:
    def option(inst: dict[str, Any]) -> str:
        login = inst.get("account", {}).get("login", "?")
        return f'<option value="{escape(str(inst["id"]))}">{escape(login)} ({inst["id"]})</option>'

    options = "".join(option(inst) for inst in installations)
    body = f"""
<form method="post" class="lat-card chr-narrow" action="/admin/github/install">
<label class="lat-label" for="installation_id">Installation</label>
<select class="lat-select" id="installation_id" name="installation_id">{options}</select>
<label class="lat-label" for="pasted">...or paste an installation id</label>
<input type="text" class="lat-input" id="pasted" name="pasted_id">
<button type="submit" class="lat-btn lat-btn--primary">Use this installation</button>
</form>
"""
    return page("Choose installation", body, active=GITHUB_TAB)


def github_repo_page(repos: list[dict[str, Any]]) -> str:
    def option(repo: dict[str, Any]) -> str:
        full_name = escape(repo["full_name"])
        branch = escape(repo.get("default_branch", "main"))
        return f'<option value="{full_name}" data-branch="{branch}">{full_name}</option>'

    options = "".join(option(repo) for repo in repos)
    body = f"""
<form method="post" class="lat-card chr-narrow" action="/admin/github/repo">
<label class="lat-label" for="repo">Repository</label>
<select class="lat-select" id="repo" name="repo">{options}</select>
<button type="submit" class="lat-btn lat-btn--primary">Verify and choose this repository</button>
</form>
"""
    return page("Choose repository", body, active=GITHUB_TAB)


def tokens_page(
    tokens: list[dict[str, Any]],
    notice: str | None = None,
    minted: str | None = None,
    *,
    ui_disabled: bool = False,
) -> str:
    minted_html = (
        '<p class="notice ok lat-banner lat-banner--ok">New token (shown once): '
        f"<code>{escape(minted)}</code></p>"
        if minted
        else ""
    )

    def actions(name: str) -> str:
        # The `ui` token's kill switch survives a restart (`ui_disabled`
        # marker, chronicle/api/tokens.py); revoking it here alone is not
        # enough to bring the UI back, so its row gets a "re-enable UI"
        # button instead of (never alongside) a second revoke once already
        # disabled.
        if name == UI_TOKEN_NAME and ui_disabled:
            return (
                '<form method="post" action="/admin/tokens/ui/reenable">'
                '<button type="submit" class="lat-btn">Re-enable UI</button></form>'
            )
        return (
            f'<form method="post" action="/admin/tokens/{escape(name)}/revoke">'
            '<button type="submit" class="lat-btn">Revoke</button></form>'
        )

    # A bare YYYY-MM-DD stays a date (no instant to shift); see `_stamp`.
    rows = "".join(
        f"<tr><td>{escape(t['name'])}</td><td>{escape(_stamp(t['created_at'], '?'))}</td>"
        f"<td>{escape(_stamp(t['last_used_at'], 'never'))}</td>"
        f"<td>{escape(_stamp(t['revoked_at'], 'no'))}</td>"
        f"<td>{actions(t['name'])}</td></tr>"
        for t in tokens
    )
    body = f"""
{minted_html}
<form method="post" class="lat-card chr-narrow" action="/admin/tokens">
<label class="lat-label" for="name">New token name</label>
<input type="text" class="lat-input" id="name" name="name" required>
<button type="submit" class="lat-btn lat-btn--primary">Issue token</button>
</form>
{_table(rows, "<th>name</th><th>created</th><th>last used</th><th>revoked</th><th></th>")}
"""
    return page("Tokens", body, notice, "ok" if minted else "error", active=TOKENS_TAB)


def _short(sha: Any) -> str:
    text = str(sha or "")
    return text[:12] if text else "-"


def _post_button(action: str, label: str, fields: dict[str, str], *, danger: bool = False) -> str:
    hidden = "".join(
        f'<input type="hidden" name="{escape(k)}" value="{escape(v)}">' for k, v in fields.items()
    )
    kind = "lat-btn--danger" if danger else ""
    return (
        f'<form method="post" action="{escape(action)}" class="inline">{hidden}'
        f'<button type="submit" class="lat-btn {kind}">{escape(label)}</button></form>'
    )


def _theme_actions(row: dict[str, Any], action: dict[str, Any] | None) -> str:
    buttons = []
    if action is not None and action.get("pinned_at_open") == row.get("pinned"):
        # A PR this page opened has not landed (the pin has not moved). The
        # buttons stay: pressing one again refreshes that PR, and a PR Scott
        # closed must not leave the row with no way forward.
        url = escape(str(action.get("pr_url", "")))
        buttons.append(f'PR open: <a href="{url}">{url}</a><br>')
    tag = row.get("latest_tag") or {}
    if tag.get("sha") and tag.get("sha") != row.get("pinned"):
        buttons.append(
            _post_button(
                "/admin/toolchain/bump",
                f"PR: move to {tag.get('tag')}",
                {"path": str(row["path"]), "which": "tag"},
            )
        )
    head = row.get("head")
    if head and head != row.get("pinned") and head != tag.get("sha"):
        buttons.append(
            _post_button(
                "/admin/toolchain/bump",
                "PR: move to upstream head",
                {"path": str(row["path"]), "which": "head"},
            )
        )
    if row.get("unused"):
        buttons.append(
            _post_button(
                "/admin/toolchain/remove",
                "PR: remove this theme",
                {"path": str(row["path"])},
                danger=True,
            )
        )
    return " ".join(buttons) or "-"


def _theme_status(row: dict[str, Any]) -> str:
    if row.get("error"):
        return escape(str(row["error"]))
    pinned = row.get("pinned")
    tag = row.get("latest_tag") or {}
    if pinned and pinned == tag.get("sha"):
        state = f"at latest tag {tag.get('tag')}"
    elif pinned and pinned == row.get("head"):
        state = "at upstream head"
    elif tag:
        state = f"not at latest tag {tag.get('tag')}"
    else:
        state = "upstream has no version tags"
    return escape(state) + (
        ' <span class="lat-badge lat-badge--warn">unused</span>' if row.get("unused") else ""
    )


def toolchain_page(
    *,
    result: dict[str, Any] | None,
    actions: dict[str, Any],
    hugo_issue_url: str | None,
    hugo_release_url: str | None,
    check_running: bool,
    notice: str | None = None,
    notice_kind: str = "error",
) -> str:
    """The admin Toolchain page (issue #67): what the blog pins against what
    upstream offers. Nothing here is fetched on page load; the numbers come
    from the last check, which runs daily or on "Check now"."""
    check_button = _post_button("/admin/toolchain/check", "Check now", {})
    running = " A check is running now; reload in a moment." if check_running else ""
    if result is None:
        body = f"""
<p>No toolchain check has run yet.{running}</p>
{check_button}
"""
        return page("Toolchain", body, notice=notice, notice_kind=notice_kind, active=TOOLCHAIN_TAB)

    checked = escape(_stamp(result.get("checked_at"), "?"))
    hugo = result.get("hugo") or {}
    latest = hugo.get("latest")
    image = hugo.get("image")
    hugo_rows = (
        "<tr><td>Chronicle image (builds and previews)</td>"
        f"<td>{escape(str(image or '-'))}</td></tr>"
        f"<tr><td>Blog deploy workflow</td><td>{escape(str(hugo.get('site') or '-'))}</td></tr>"
        f"<tr><td>Latest release</td><td>{escape(str(latest or '-'))}</td></tr>"
    )
    hugo_note = ""
    if hugo_issue_url:
        links = []
        if hugo_release_url:
            links.append(f'<a href="{escape(hugo_release_url)}">release notes</a>')
        if hugo_issue_url:
            links.append(
                f'<a href="{escape(hugo_issue_url)}">file a Chronicle issue to bump it</a>'
            )
        hugo_note = (
            f"<p>Chronicle's image is behind the latest Hugo. Hugo is baked into the image, so"
            f" this page cannot change it: the bump is a Chronicle release that changes"
            f" <code>HUGO_VERSION</code>. {' | '.join(links)}</p>"
        )

    if result.get("site_missing"):
        themes_html = "<p>No site checkout yet: run a digest from the Status page first.</p>"
        modules_html = ""
    else:
        config = result.get("config")
        config_note = (
            '<p class="muted">The site\'s own <code>hugo config</code> could not be read, so no'
            " theme is marked unused.</p>"
            if config is None
            else '<p class="muted">Configured theme: '
            + escape(", ".join(config.get("themes") or []) or "none")
            + "</p>"
        )
        theme_rows = "".join(
            "<tr>"
            f"<td>{escape(str(row.get('path', '')))}</td>"
            f"<td><code>{escape(_short(row.get('pinned')))}</code></td>"
            f"<td>{escape(str((row.get('latest_tag') or {}).get('tag') or '-'))}"
            f" <code>{escape(_short((row.get('latest_tag') or {}).get('sha')))}</code></td>"
            f"<td><code>{escape(_short(row.get('head')))}</code></td>"
            f"<td>{_theme_status(row)}</td>"
            f"<td>{_theme_actions(row, actions.get(str(row.get('path'))))}</td>"
            "</tr>"
            for row in result.get("themes", [])
        )
        themes_html = config_note + (
            _table(
                theme_rows,
                "<th>submodule</th><th>pinned</th><th>latest tag</th><th>upstream head</th>"
                "<th>status</th><th>action</th>",
            )
            if theme_rows
            else "<p>The blog has no submodules.</p>"
        )
        module_rows = "".join(
            "<tr>"
            f"<td>{escape(str(m.get('module', '')))}</td>"
            f"<td>{escape(str(m.get('version', '')))}</td>"
            f"<td>{escape(str(m.get('latest') or m.get('error') or '-'))}</td>"
            "</tr>"
            for m in result.get("modules", [])
        )
        modules_html = (
            '<section class="lat-card"><h2>Hugo modules</h2>'
            '<p class="muted">Reported only: moving a module needs <code>hugo mod get</code>'
            " and a Go toolchain, which Chronicle does not carry.</p>"
            + _table(module_rows, "<th>module</th><th>pinned</th><th>latest tag</th>")
            + "</section>"
            if module_rows
            else ""
        )

    errors = result.get("errors") or []
    errors_html = (
        "<ul>" + "".join(f"<li>{escape(str(e))}</li>" for e in errors) + "</ul>" if errors else ""
    )
    body = f"""
<p class="muted">Last checked {checked}. Checks run daily.{running}</p>
{check_button}
{errors_html}
<section class="lat-card">
<h2>Hugo</h2>
{_table(hugo_rows)}
{hugo_note}
</section>
<section class="lat-card">
<h2>Themes and other submodules</h2>
<p class="muted">Each action opens a PR on the blog repo for you to review and merge. Nothing is
pushed to the default branch.</p>
{themes_html}
</section>
{modules_html}
"""
    return page("Toolchain", body, notice=notice, notice_kind=notice_kind, active=TOOLCHAIN_TAB)
