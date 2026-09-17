"""Minimal server-rendered HTML for the admin surface.

Plain string templates, not Jinja2: C5 owns the real UI (AGENTS.md, spec
section 14), so this round's bar is function over polish. Every value
interpolated into a page has already been through `html.escape`.
"""

from __future__ import annotations

from html import escape
from typing import Any

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
        '<a href="/admin/tokens">Tokens</a><a href="/admin/password">Password</a>'
        '<form style="display:inline" method="post" action="/admin/logout">'
        '<button type="submit">Log out</button></form></nav>'
    )


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
<h2>Digest</h2>
<table>
{row("last digest", status["last_digest_at"] or "never")}
{row("post count", status["post_count"])}
{row("hugo version (site)", status["toolchain"]["hugo_version"])}
{row("hugo version (builder)", status["toolchain"]["builder_hugo_version"])}
{row("toolchain", "match" if status["toolchain"]["match"] else "drift")}
</table>
<h3>Theme submodules</h3>
<table><tr><th>path</th><th>commit</th></tr>{theme_rows}</table>
<h2>Submissions by status</h2>
<table>{submission_rows or "<tr><td colspan=2>none</td></tr>"}</table>
<h2>Drafts by status</h2>
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
    tokens: list[dict[str, Any]], notice: str | None = None, minted: str | None = None
) -> str:
    minted_html = (
        f'<p class="notice ok">New token (shown once): <code>{escape(minted)}</code></p>'
        if minted
        else ""
    )
    rows = "".join(
        f"<tr><td>{escape(t['name'])}</td><td>{escape(t['created_at'])}</td>"
        f"<td>{escape(t['last_used_at'] or 'never')}</td><td>{escape(t['revoked_at'] or 'no')}</td>"
        f'<td><form method="post" action="/admin/tokens/{escape(t["name"])}/revoke">'
        f'<button type="submit">Revoke</button></form></td></tr>'
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
