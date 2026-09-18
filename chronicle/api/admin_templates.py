"""Minimal server-rendered HTML for the admin surface.

Plain string templates, not Jinja2: C5 owns the real UI (AGENTS.md, spec
section 14), so this round's bar is function over polish. Every value
interpolated into a page has already been through `html.escape`.
"""

from __future__ import annotations

from html import escape
from typing import Any

from .tokens import UI_TOKEN_NAME
from .ui_time import local_time

STYLE = """
body { font-family: system-ui, sans-serif; max-width: 42rem; margin: 2rem auto; color: #1a1a1a; }
h1 { font-size: 1.4rem; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
td, th { border: 1px solid #ccc; padding: 0.4rem 0.6rem; text-align: left; font-size: 0.9rem; }
.notice { padding: 0.6rem; border-radius: 4px; margin-bottom: 1rem; }
.notice.error { background: #fde8e8; }
.notice.ok { background: #e6f6e6; }
code, pre { background: #f2f2f2; padding: 0.1rem 0.3rem; border-radius: 3px; }
form { margin: 1rem 0; }
label { display: block; margin: 0.5rem 0 0.2rem; }
input[type=text], input[type=password] { width: 100%; padding: 0.4rem; box-sizing: border-box; }
button { padding: 0.5rem 1rem; margin-top: 0.6rem; }
nav a { margin-right: 1rem; }
"""


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


def page(title: str, body: str, notice: str | None = None, notice_kind: str = "error") -> str:
    notice_html = f'<p class="notice {notice_kind}">{escape(notice)}</p>' if notice else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{escape(title)}</title><style>{STYLE}</style></head>
<body>
<h1>{escape(title)}</h1>
{notice_html}
{body}
</body></html>"""


def nav() -> str:
    return (
        '<nav><a href="/admin">Status</a><a href="/admin/github/connect">GitHub</a>'
        '<a href="/admin/tokens">Tokens</a><a href="/admin/backup">Backup</a>'
        '<a href="/admin/password">Password</a>'
        '<form style="display:inline" method="post" action="/admin/logout">'
        '<button type="submit">Log out</button></form></nav>'
    )


def backup_page(
    *, last_backup: str | None, notice: str | None = None, notice_kind: str = "error"
) -> str:
    # A bare YYYY-MM-DD stays a date (no instant to shift); see `_stamp`.
    last_html = escape(_stamp(last_backup, "never"))
    body = f"""
{nav()}
<p>Last backup created: {last_html}</p>
<h2>Create a backup</h2>
<p>Downloads a gzip tarball: repo history, images, and the encrypted
credential store. Never the instance key.</p>
<form method="get" action="/admin/backup/create">
<button type="submit">Create and download backup</button>
</form>
<h2>Restore from a backup</h2>
<p>Upload a bundle to see its manifest counts before anything is touched.
Restoring replaces this instance's repo, images, and credential store; the
running instance's own instance key is kept.</p>
<form method="post" action="/admin/backup/upload" enctype="multipart/form-data">
<label for="file">Bundle (.tar.gz)</label>
<input type="file" id="file" name="file" accept=".tar.gz,.tgz" required>
<button type="submit">Upload and preview</button>
</form>
"""
    return page("Backup", body, notice=notice, notice_kind=notice_kind)


def backup_confirm_page(*, token: str, manifest: dict[str, Any]) -> str:
    counts = manifest.get("counts", {})
    rows = "".join(
        f"<tr><td>{escape(str(key))}</td><td>{escape(str(value))}</td></tr>"
        for key, value in sorted(counts.items())
    )
    # A bare YYYY-MM-DD stays a date (no instant to shift); see `_stamp`.
    created = escape(_stamp(manifest.get("created_at"), "?"))
    body = f"""
{nav()}
<p>Bundle created {created} by Chronicle
{escape(str(manifest.get("chronicle_version", "?")))}.</p>
<table><tr><th>record</th><th>count</th></tr>{rows}</table>
<p><strong>This replaces the current repo, images, and credential store.</strong>
If a builder is running against this instance, restart it after the
restore finishes: it holds its own connection to the index and will not
notice the swap on its own. Run <code>chronicle digest</code> again after
restoring, since the site checkout is not part of the bundle.
Type <code>restore</code> below to confirm.</p>
<form method="post" action="/admin/backup/restore">
<input type="hidden" name="token" value="{escape(token)}">
<label for="confirm">Type "restore" to confirm</label>
<input type="text" id="confirm" name="confirm" required autocomplete="off">
<button type="submit">Restore now</button>
</form>
<p><a href="/admin/backup">Cancel</a></p>
"""
    return page("Confirm restore", body)


def claim_page(notice: str | None = None) -> str:
    body = """
<p>This instance has not been claimed. Read the one-time claim code from the
file logged at startup (<code>data/state/claim-code</code>) and set a
password of at least 12 characters.</p>
<form method="post" action="/admin/claim">
<label for="code">Claim code</label>
<input type="text" id="code" name="code" required autocomplete="off">
<label for="password">New password (12+ characters)</label>
<input type="password" id="password" name="password" required minlength="12">
<button type="submit">Claim this instance</button>
</form>
"""
    return page("Claim Chronicle", body, notice)


def login_page(notice: str | None = None) -> str:
    body = """
<form method="post" action="/admin/login">
<label for="password">Password</label>
<input type="password" id="password" name="password" required autocomplete="current-password">
<button type="submit">Log in</button>
</form>
"""
    return page("Admin login", body, notice)


def change_password_page(notice: str | None = None, notice_kind: str = "error") -> str:
    body = f"""
{nav()}
<p>Changing the password ends every other session immediately.</p>
<form method="post" action="/admin/password">
<label for="current_password">Current password</label>
<input type="password" id="current_password" name="current_password" required>
<label for="new_password">New password (12+ characters)</label>
<input type="password" id="new_password" name="new_password" required minlength="12">
<button type="submit">Change password</button>
</form>
"""
    return page("Change password", body, notice, notice_kind)


def _heartbeat_rows(row: Any, heartbeat: dict[str, Any] | None, name: str) -> str:
    if not heartbeat:
        return f"<tr><td colspan=2>no heartbeat yet; the {name} has not completed a loop</td></tr>"
    # Every `*_at` key is an ISO stamp a human reads (last_loop_at, last_run_at).
    # A missing value renders as it did before; a bare date stays a date.
    return "".join(
        row(key, local_time(value) if key.endswith("_at") and value else value)
        for key, value in heartbeat.items()
    )


def _flag_rows(row: Any, flags: list[dict[str, Any]]) -> str:
    if not flags:
        return "<tr><td colspan=3>none</td></tr>"
    rows = []
    for flag in flags:
        buttons = "".join(
            f'<form style="display:inline" method="post" '
            f'action="/admin/reconcile/{escape(flag["id"])}/resolve">'
            f'<input type="hidden" name="resolution" value="{escape(resolution)}">'
            f'<button type="submit">{escape(resolution)}</button></form> '
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
        return "<tr>" + "".join(f"<td>{escape(str(cell))}</td>" for cell in cells) + "</tr>"

    disk_rows = "".join(row(name, size) for name, size in status["disk_use"].items())
    submission_rows = "".join(
        row(name, count) for name, count in status["submissions_by_status"].items()
    )
    draft_rows = "".join(row(name, count) for name, count in status["drafts_by_status"].items())
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
    # last backup and last digest go through `_stamp`: the words "never" survive
    # and a bare YYYY-MM-DD stays a date (no instant to shift).
    body = f"""
{nav()}
<h2>GitHub App</h2>
<table>
{row("state", status["github_app_state"])}
{row("repo", status["github_repo"] or "none chosen")}
{row("default branch", status["github_default_branch"] or "-")}
</table>
<form method="post" action="/admin/digest">
<button type="submit">Run digest now</button>
</form>
<h2>Backup</h2>
<table>
{row("last backup", _stamp(status["last_backup_at"], "never"))}
</table>
<p><a href="/admin/backup">Backup and restore</a></p>
<h2>Digest</h2>
<table>
{row("last digest", _stamp(status["last_digest_at"], "never"))}
{row("post count", status["post_count"])}
{row("hugo version (site)", status["toolchain"]["hugo_version"])}
{row("hugo version (builder)", status["toolchain"]["builder_hugo_version"])}
{row("toolchain", "match" if status["toolchain"]["match"] else "drift")}
</table>
<h3>Content conventions</h3>
<table>{conventions_rows}</table>
<h3>Theme submodules</h3>
<table><tr><th>path</th><th>commit</th></tr>{theme_rows}</table>
<h2>Builder</h2>
<table>{builder_rows}</table>
<h3>Preview runs by status</h3>
<table>{preview_run_rows}</table>
<h2>Publisher</h2>
<table>{publisher_rows}</table>
<h2>Watcher</h2>
<table>{watcher_rows}</table>
<h2>Reconciliation</h2>
<table>{reconcile_rows}</table>
<form method="post" action="/admin/reconcile">
<button type="submit">Run reconciliation now</button>
</form>
<h3>Open flags</h3>
<table><tr><th>type</th><th>detail</th><th>resolve</th></tr>{flag_rows}</table>
<h2>Submissions by status</h2>
<table>{submission_rows or "<tr><td colspan=2>none</td></tr>"}</table>
<h2>Posts by status</h2>
<table>{draft_rows or "<tr><td colspan=2>none</td></tr>"}</table>
<h2>Disk use</h2>
<table>{disk_rows}</table>
<h2>Repository health</h2>
<table>
{row("git status", status["git_health"])}
{row("index schema version", status["index_schema_version"])}
</table>
"""
    return page("Chronicle admin", body, notice, "ok" if notice else "error")


def github_connect_page(manifest_json: str, state: str, external_url: str) -> str:
    body = f"""
{nav()}
<p>Submitting this form opens GitHub's own "create a GitHub App" page in your
browser, pre-filled from the manifest below. GitHub creates the App and
redirects your browser back to <code>{escape(external_url)}/admin/github/callback</code>
with a one-time code; Chronicle never sees this step directly.</p>
<form method="post" action="https://github.com/settings/apps/new?state={escape(state)}">
<input type="hidden" name="manifest" value='{escape(manifest_json)}'>
<button type="submit">Create App on your personal account</button>
</form>
<form method="post" action="/admin/github/connect/org">
<label for="org">Organization login (optional target instead)</label>
<input type="text" id="org" name="org">
<input type="hidden" name="manifest" value='{escape(manifest_json)}'>
<button type="submit">Continue to organization form</button>
</form>
<h2>Manifest</h2>
<pre>{escape(manifest_json)}</pre>
"""
    return page("Connect GitHub", body)


def github_connect_org_page(manifest_json: str, state: str, target_url: str) -> str:
    body = f"""
{nav()}
<p>Submitting this form opens GitHub's own "create a GitHub App" page for
this organization, pre-filled from the manifest below.</p>
<form method="post" action="{escape(target_url)}">
<input type="hidden" name="manifest" value='{escape(manifest_json)}'>
<button type="submit">Create App for this organization</button>
</form>
<h2>Manifest</h2>
<pre>{escape(manifest_json)}</pre>
"""
    return page("Connect GitHub (organization)", body)


def github_install_page(installations: list[dict[str, Any]]) -> str:
    def option(inst: dict[str, Any]) -> str:
        login = inst.get("account", {}).get("login", "?")
        return f'<option value="{escape(str(inst["id"]))}">{escape(login)} ({inst["id"]})</option>'

    options = "".join(option(inst) for inst in installations)
    body = f"""
{nav()}
<form method="post" action="/admin/github/install">
<label for="installation_id">Installation</label>
<select id="installation_id" name="installation_id">{options}</select>
<label for="pasted">...or paste an installation id</label>
<input type="text" id="pasted" name="pasted_id">
<button type="submit">Use this installation</button>
</form>
"""
    return page("Choose installation", body)


def github_repo_page(repos: list[dict[str, Any]]) -> str:
    def option(repo: dict[str, Any]) -> str:
        full_name = escape(repo["full_name"])
        branch = escape(repo.get("default_branch", "main"))
        return f'<option value="{full_name}" data-branch="{branch}">{full_name}</option>'

    options = "".join(option(repo) for repo in repos)
    body = f"""
{nav()}
<form method="post" action="/admin/github/repo">
<label for="repo">Repository</label>
<select id="repo" name="repo">{options}</select>
<button type="submit">Verify and choose this repository</button>
</form>
"""
    return page("Choose repository", body)


def tokens_page(
    tokens: list[dict[str, Any]],
    notice: str | None = None,
    minted: str | None = None,
    *,
    ui_disabled: bool = False,
) -> str:
    minted_html = (
        f'<p class="notice ok">New token (shown once): <code>{escape(minted)}</code></p>'
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
                '<button type="submit">Re-enable UI</button></form>'
            )
        return (
            f'<form method="post" action="/admin/tokens/{escape(name)}/revoke">'
            '<button type="submit">Revoke</button></form>'
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
{nav()}
{minted_html}
<form method="post" action="/admin/tokens">
<label for="name">New token name</label>
<input type="text" id="name" name="name" required>
<button type="submit">Issue token</button>
</form>
<table>
<tr><th>name</th><th>created</th><th>last used</th><th>revoked</th><th></th></tr>
{rows}
</table>
"""
    return page("Tokens", body, notice, "ok" if minted else "error")
