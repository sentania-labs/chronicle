"""Server-rendered HTML for the content and preview tabs (ADR 014).

Plain string templates in the same hand-written style as
`admin_templates.py`: C5's own bar is function over polish, and pulling in a
templating engine buys nothing a small set of f-strings does not already do
for a UI this size. Every value interpolated into a page has already been
through `html.escape`; the one exception, the live markdown preview pane, is
never populated from a template at all (`ui.js` fills it from the textarea in
the visitor's own browser, not from anything the server renders).
"""

from __future__ import annotations

from html import escape
from posixpath import basename
from typing import Any

from .pagination import Page
from .transitions import DRAFT_TRANSITIONS, RESERVED_ACTIONS

STYLE_LINKS = (
    '<link rel="stylesheet" href="/static/style.css">'
    '<script src="/static/vendor/marked.min.js"></script>'
)

NAV_LINKS = (
    ("/content/submissions", "Submissions"),
    ("/content/drafts", "Drafts"),
    ("/content/import", "Import"),
    ("/content/previews", "Preview"),
)

BANNER_TEXT = (
    "This Chronicle instance is internal-only and unauthenticated. "
    "Anyone who can reach it on the network can create, edit, and act on content."
)


def _nav() -> str:
    links = "".join(f'<a href="{href}">{escape(label)}</a>' for href, label in NAV_LINKS)
    return f"<nav>{links}</nav>"


def page(
    title: str, body: str, *, banner: bool, notice: str | None = None, notice_kind: str = "error"
) -> str:
    banner_html = f'<p class="banner">{escape(BANNER_TEXT)}</p>' if banner else ""
    notice_html = f'<p class="notice {notice_kind}">{escape(notice)}</p>' if notice else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{escape(title)}</title>{STYLE_LINKS}</head>
<body>
{banner_html}
{_nav()}
<h1>{escape(title)}</h1>
{notice_html}
{body}
</body></html>"""


def _pagination_links(pg: Page[Any], base_url: str, *, extra: str = "") -> str:
    """Previous/next links and the total count, `?page=N` on `base_url`,
    with whatever other query string (`extra`, already a leading `&...`)
    the current listing carries (a status filter, a search term)."""
    prev_html = (
        f'<a href="{base_url}?page={pg.page - 1}{extra}">&laquo; previous</a>'
        if pg.has_previous
        else "<span>&laquo; previous</span>"
    )
    next_html = (
        f'<a href="{base_url}?page={pg.page + 1}{extra}">next &raquo;</a>'
        if pg.has_next
        else "<span>next &raquo;</span>"
    )
    return (
        f'<p class="pagination">{prev_html} &nbsp; '
        f"page {pg.page} of {pg.total_pages} &nbsp; ({pg.total} total) &nbsp; "
        f"{next_html}</p>"
    )


def _status_options(current: str | None) -> str:
    from .models import DRAFT_STATUSES

    options = ['<option value="">all</option>']
    for status in DRAFT_STATUSES:
        selected = " selected" if status == current else ""
        options.append(f'<option value="{status}"{selected}>{escape(status)}</option>')
    return "".join(options)


# --- Submissions -------------------------------------------------------


def submissions_list_page(pg: Page[dict[str, Any]], *, banner: bool) -> str:
    if not pg.items:
        rows = "<tr><td colspan=6>none</td></tr>"
    else:
        rows = "".join(
            "<tr>"
            f'<td><a href="/content/submissions/{escape(s["id"])}">{escape(s["brief"][:80])}</a></td>'
            f"<td>{escape(s['status'])}</td>"
            f"<td>{escape(s['from_'])}</td>"
            f"<td>{escape(s['created_at'])}</td>"
            f"<td>{len(s['image_ids'])}</td>"
            f"<td>{escape(s['claimed_by'] or '-')}</td>"
            "</tr>"
            for s in pg.items
        )
    body = f"""
<table>
<tr><th>brief</th><th>status</th><th>from</th><th>created</th><th>images</th><th>claimed by</th></tr>
{rows}
</table>
{_pagination_links(pg, "/content/submissions")}
"""
    return page("Submissions", body, banner=banner)


def submission_detail_page(
    submission: dict[str, Any],
    images: list[dict[str, Any]],
    *,
    banner: bool,
    notice: str | None = None,
) -> str:
    def material_link(url: str) -> str:
        # A submission's material url is unvalidated input (chronicle.api.
        # models.Material.url is a bare str); escape() alone leaves the
        # scheme untouched, so a `javascript:` value would still render as
        # a clickable link that runs on Scott's click (found in a round C5
        # review). Only ever emit an anchor for a scheme a browser will
        # navigate to, not execute.
        if url.lower().startswith(("http://", "https://")):
            return f' (<a href="{escape(url)}">{escape(url)}</a>)'
        return f" ({escape(url)})"

    materials = "".join(
        f"<li><strong>{escape(m['name'])}</strong>"
        + (f": {escape(m['text'])}" if m.get("text") else "")
        + (material_link(m["url"]) if m.get("url") else "")
        + "</li>"
        for m in submission["materials"]
    )
    image_rows = "".join(
        f"<li>{escape(img['filename'])} ({img['bytes']} bytes)</li>" for img in images
    )
    can_draft = submission["status"] in ("new", "claimed")
    can_discard = submission["status"] in ("new", "claimed")
    actions = []
    if can_draft:
        actions.append(
            f'<form method="post" action="/content/submissions/{escape(submission["id"])}/draft">'
            '<button type="submit">Create draft from this submission</button></form>'
        )
    if can_discard:
        actions.append(
            f'<form method="post" action="/content/submissions/{escape(submission["id"])}/discard">'
            '<button type="submit">Discard</button></form>'
        )
    body = f"""
<p class="muted">status: {escape(submission["status"])}, from: {escape(submission["from_"])},
created: {escape(submission["created_at"])}, claimed by: {escape(submission["claimed_by"] or "-")}</p>
<h2>Brief</h2>
<p>{escape(submission["brief"])}</p>
<h2>Materials</h2>
<ul>{materials or "<li>none</li>"}</ul>
<h2>Images ({len(images)})</h2>
<ul>{image_rows or "<li>none</li>"}</ul>
<div class="actions">{"".join(actions)}</div>
"""
    return page(
        f"Submission {submission['id']}",
        body,
        banner=banner,
        notice=notice,
        notice_kind="ok" if notice else "error",
    )


# --- Drafts board --------------------------------------------------------


def _flag_badges(flags: list[dict[str, Any]]) -> str:
    if not flags:
        return ""
    return "".join(f'<span class="tag">flag: {escape(f["type"])}</span>' for f in flags)


def _draft_card(
    draft: dict[str, Any],
    last_author: str,
    run_info: dict[str, Any] | None,
    flags: list[dict[str, Any]],
) -> str:
    published = draft.get("published") or {}
    pr_link = (
        f'<a href="{escape(published["pr_url"])}">PR #{published["pr_number"]}</a>'
        if published.get("pr_url")
        else "-"
    )
    preview_url = (run_info or {}).get("preview_url")
    preview_link = f'<a href="{escape(preview_url)}">preview</a>' if preview_url else "-"
    claim = draft.get("claim")
    claim_text = f"held by {escape(claim['author'])}" if claim else "unclaimed"
    return f"""
<div class="card">
<h3><a href="/content/drafts/{escape(draft["id"])}">{escape(draft["title"] or "(untitled)")}</a></h3>
<p class="muted">slug: {escape(draft["slug"] or "-")} | status: {escape(draft["status"])} |
author: {escape(last_author)} | updated: {escape(draft["updated_at"])} | claim: {claim_text}</p>
<p class="muted">PR: {pr_link} | preview: {preview_link} {_flag_badges(flags)}</p>
</div>
"""


def drafts_board_page(pg: Page[dict[str, Any]], *, status_filter: str | None, banner: bool) -> str:
    cards = (
        "".join(
            _draft_card(row["draft"], row["last_author"], row["run_info"], row["flags"])
            for row in pg.items
        )
        or "<p>no drafts.</p>"
    )
    extra = f"&status={escape(status_filter)}" if status_filter else ""
    body = f"""
<form method="get" action="/content/drafts">
<label for="status">Filter by status</label>
<select id="status" name="status" onchange="this.form.submit()">{_status_options(status_filter)}</select>
</form>
{cards}
{_pagination_links(pg, "/content/drafts", extra=extra)}
"""
    return page("Drafts", body, banner=banner)


# --- Import ---------------------------------------------------------------


def import_page(
    pg: Page[dict[str, Any]], q: str, *, banner: bool, notice: str | None = None
) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{escape(p['slug'])}</td>"
        f"<td>{escape(p['title'])}</td>"
        f"<td>{escape(p['date'])}</td>"
        '<td><form method="post" action="/content/import">'
        f'<input type="hidden" name="slug" value="{escape(p["slug"])}">'
        '<button type="submit">Import as draft</button></form></td>'
        "</tr>"
        for p in pg.items
    )
    extra = f"&q={escape(q)}" if q else ""
    body = f"""
<form method="get" action="/content/import">
<label for="q">Search published posts</label>
<input type="text" id="q" name="q" value="{escape(q)}" placeholder="title or slug">
<button type="submit">Search</button>
</form>
<table>
<tr><th>slug</th><th>title</th><th>date</th><th></th></tr>
{rows or "<tr><td colspan=4>no posts match.</td></tr>"}
</table>
{_pagination_links(pg, "/content/import", extra=extra)}
"""
    return page(
        "Import published post",
        body,
        banner=banner,
        notice=notice,
        notice_kind="ok" if notice else "error",
    )


# --- Editor ---------------------------------------------------------------


def _frontmatter_fields(
    frontmatter: dict[str, Any], images: list[dict[str, Any]], slug: str | None
) -> str:
    def val(key: str) -> str:
        value = frontmatter.get(key, "")
        return escape(value if isinstance(value, str) else "")

    def list_val(key: str) -> str:
        value = frontmatter.get(key) or []
        return escape(", ".join(str(v) for v in value))

    url_field = (
        f'<input type="text" id="url" name="url" value="{val("url")}" readonly>'
        if slug
        else f'<input type="text" id="url" name="url" value="{val("url")}">'
    )
    stored_feature = frontmatter.get("featureImage")
    stored_feature = stored_feature if isinstance(stored_feature, str) else ""

    def _feature_rank(img: dict[str, Any]) -> int | None:
        # An imported post's featureImage is a root-relative path recorded
        # as the image's source_ref (the C2 source_ref shape), not the bare
        # filename, so a bare-filename match alone would never select it.
        # Ranked so an exact match (filename or source_ref) always wins over
        # a basename-only match: two images can share a basename (a
        # dedup-kept filename versus another image's source_ref), and only
        # one option may ever come back `selected`.
        if not stored_feature:
            return None
        if stored_feature == img["filename"]:
            return 0
        source_ref = img.get("source_ref")
        if not source_ref:
            return None
        if stored_feature == source_ref:
            return 0
        if basename(stored_feature) == basename(source_ref):
            return 1
        return None

    ranked = [(img, r) for img in images for r in [_feature_rank(img)] if r is not None]
    matched: set[str] = set()
    if ranked:
        best_rank = min(r for _, r in ranked)
        # Still ambiguous at the best rank (e.g. two images sharing a
        # filename): keep only the first, so at most one option ever
        # renders `selected`.
        best_img = next(img for img, r in ranked if r == best_rank)
        matched = {best_img["image_id"]}
    feature_options = "".join(
        # A matched option's value is the draft's own stored string, not the
        # bare filename, so resubmitting the form unchanged round-trips the
        # original path byte for byte instead of collapsing it to a filename.
        f'<option value="{escape(stored_feature if img["image_id"] in matched else img["filename"])}"'
        + (" selected" if img["image_id"] in matched else "")
        + f">{escape(img['filename'])}</option>"
        for img in images
    )
    if stored_feature and not matched:
        # No attached image represents this value (e.g. detached since
        # import): keep it selectable and preserved rather than silently
        # falling back to "none" and losing it on the next save.
        feature_options += (
            f'<option value="{escape(stored_feature)}" selected>{escape(stored_feature)}</option>'
        )
    return f"""
<label for="title">Title</label>
<input type="text" id="title" name="title" value="{val("title")}" required>
<label for="date">Date</label>
<input type="text" id="date" name="date" value="{val("date")}" placeholder="YYYY-MM-DD">
<label for="categories">Categories (comma-separated)</label>
<input type="text" id="categories" name="categories" value="{list_val("categories")}">
<label for="tags">Tags (comma-separated)</label>
<input type="text" id="tags" name="tags" value="{list_val("tags")}">
<label for="summary">Summary / description</label>
<input type="text" id="summary" name="summary" value="{val("summary") or val("description")}">
<label for="url">URL{" (read-only, slug is pinned)" if slug else ""}</label>
{url_field}
<label for="featureImage">Feature image</label>
<select id="featureImage" name="featureImage"><option value="">none</option>{feature_options}</select>
"""


def _image_list(draft_id: str, images: list[dict[str, Any]]) -> str:
    if not images:
        return "<p>no attached images.</p>"
    rows = "".join(
        "<tr>"
        f"<td>{escape(img['filename'])}</td>"
        f"<td>{escape(img['role'])}</td>"
        f'<td><form method="post" action="/content/drafts/{escape(draft_id)}/images/{escape(img["image_id"])}/detach">'
        '<button type="submit">Detach</button></form></td>'
        "</tr>"
        for img in images
    )
    return f"<table><tr><th>filename</th><th>role</th><th></th></tr>{rows}</table>"


def _action_buttons(
    draft_id: str,
    status: str,
    published: dict[str, Any] | None,
    publish_pr_open: bool,
    publish_run_active: bool = False,
) -> str:
    buttons = []
    for (from_status, action), transition in DRAFT_TRANSITIONS.items():
        if from_status != status or action == "revise":
            continue
        if action == "approve" and status == "approved" and publish_pr_open:
            # `Store.act_on_draft` itself refuses a re-approve while a
            # publish PR is already open (409 publish_pr_open); the table
            # alone can't see that, so a round C5 review found this button
            # rendering and then 409ing on every click until the PR closes.
            continue
        if action == "approve" and status == "approved" and publish_run_active:
            # The narrower window before that PR exists: a publish run
            # already `queued` or `building` for this draft (409
            # publish_run_in_progress). A round C5 review found the button
            # still rendered and 409ed on every click through this gap.
            continue
        label = action.replace("_", " ")
        if action == "approve" and published:
            label = "republish"
        reserved = (
            ' <span class="reserved">(Scott only)</span>' if action in RESERVED_ACTIONS else ""
        )
        if transition.feedback_required:
            buttons.append(
                f'<form method="post" action="/content/drafts/{escape(draft_id)}/actions/{action}">'
                f"<label>{label}{reserved}</label>"
                '<textarea name="feedback" required placeholder="feedback text (required)"></textarea>'
                f'<button type="submit">{escape(label)}</button></form>'
            )
        else:
            buttons.append(
                f'<form method="post" action="/content/drafts/{escape(draft_id)}/actions/{action}">'
                f'<button type="submit">{escape(label)}{reserved}</button></form>'
            )
    return "".join(buttons) or "<p>no actions available from this status.</p>"


def _feedback_log(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "<p>no feedback yet.</p>"
    rows = "".join(
        f"<li><strong>v{e['version_no']} {escape(e['action'])}</strong> by {escape(e['author'])} "
        f"at {escape(e['created_at'])}: {escape(e['text'])}</li>"
        for e in entries
    )
    return f"<ul>{rows}</ul>"


def _version_history(draft_id: str, versions: list[dict[str, Any]]) -> str:
    if not versions:
        return "<p>no versions yet.</p>"
    rows = "".join(
        f"<li>v{v['version_no']} by {escape(v['author'])} at {escape(v['created_at'])}"
        + (f": {escape(v['message'])}" if v.get("message") else "")
        + f' (<a href="/content/drafts/{escape(draft_id)}/diff?from={v["version_no"] - 1}&to={v["version_no"]}">diff vs previous</a>)</li>'
        for v in versions
    )
    return f"<ul>{rows}</ul>"


def _run_status(run: dict[str, Any] | None) -> str:
    if run is None:
        return "<p>no runs yet.</p>"
    return (
        f"<p>last run: {escape(run['kind'])}: {escape(run['status'])} at "
        f"{escape(run.get('finished_at') or run.get('started_at') or run['created_at'])} "
        f'(<a href="/runs/{escape(run["id"])}">log</a>)</p>'
    )


def editor_page(
    draft: dict[str, Any],
    versions: list[dict[str, Any]],
    feedback: list[dict[str, Any]],
    last_run: dict[str, Any] | None,
    preview_url: str | None,
    *,
    banner: bool,
    publish_pr_open: bool = False,
    publish_run_active: bool = False,
    notice: str | None = None,
    notice_kind: str = "error",
) -> str:
    claim = draft.get("claim")
    if claim:
        claim_html = (
            f"<p>Claimed by {escape(claim['author'])} since {escape(claim['since'])}. "
            f'<form style="display:inline" method="post" action="/content/drafts/{escape(draft["id"])}/release">'
            '<button type="submit">Release claim</button></form></p>'
        )
    else:
        claim_html = (
            "<p>Unclaimed. "
            f'<form style="display:inline" method="post" action="/content/drafts/{escape(draft["id"])}/claim">'
            '<button type="submit">Claim</button></form></p>'
        )
    preview_link = (
        f'<p><a href="{escape(preview_url)}">Last built preview</a></p>' if preview_url else ""
    )
    body_text = escape(draft["body"])
    pr_open_notice = (
        '<p class="notice conflict">This draft has an open publish pull request; '
        "saving is refused until it merges or closes.</p>"
        if publish_pr_open
        else ""
    )
    body = f"""
{claim_html}
{preview_link}
{_run_status(last_run)}
{pr_open_notice}
<h2>Edit</h2>
<form method="post" action="/content/drafts/{escape(draft["id"])}/save">
<input type="hidden" name="base_version" value="{draft["version_no"]}">
{_frontmatter_fields(draft["frontmatter"], draft["images"], draft["slug"])}
<div class="columns">
<div>
<label for="body">Body (markdown)</label>
<textarea id="body" name="body">{body_text}</textarea>
</div>
<div>
<label>Live preview</label>
<div id="preview-pane" class="card"></div>
</div>
</div>
<button type="submit">Save</button>
</form>
<script src="/static/ui.js"></script>
<h2>Images</h2>
{_image_list(draft["id"], draft["images"])}
<form method="post" action="/content/drafts/{escape(draft["id"])}/images" enctype="multipart/form-data">
<label for="file">Upload image</label>
<input type="file" id="file" name="file" required>
<label for="role">Role</label>
<select id="role" name="role"><option value="inline">inline</option><option value="feature">feature</option></select>
<button type="submit">Upload and attach</button>
</form>
<h2>Actions</h2>
<div class="actions">{_action_buttons(draft["id"], draft["status"], draft.get("published"), publish_pr_open, publish_run_active)}</div>
<h2>Version history</h2>
{_version_history(draft["id"], versions)}
<h2>Feedback</h2>
{_feedback_log(feedback)}
"""
    return page(
        f"Draft: {draft['title'] or '(untitled)'}",
        body,
        banner=banner,
        notice=notice,
        notice_kind=notice_kind,
    )


def conflict_page(
    draft: dict[str, Any],
    attempted: dict[str, Any],
    diff_summary: str,
    *,
    banner: bool,
) -> str:
    """A stale save: never overwrites. Shows the current server version in a
    form ready to reapply on top of, and the visitor's own attempted text in
    a second, read-only pane so nothing they wrote is silently lost."""
    body = f"""
<p class="notice conflict">Someone else saved draft {escape(draft["id"])} to version
{draft["version_no"]} while you were editing version {attempted["base_version"]}. Nothing was
overwritten. Review the diff below, then use the reloaded form (now at the current version) to
reapply anything from your attempted text on the right.</p>
<h2>What changed underneath you</h2>
<pre>{escape(diff_summary) or "(no diff available)"}</pre>
<div class="columns">
<div>
<h2>Current version (reloaded, ready to reapply)</h2>
<form method="post" action="/content/drafts/{escape(draft["id"])}/save">
<input type="hidden" name="base_version" value="{draft["version_no"]}">
{_frontmatter_fields(draft["frontmatter"], draft["images"], draft["slug"])}
<label for="body">Body (markdown)</label>
<textarea id="body" name="body">{escape(draft["body"])}</textarea>
<button type="submit">Save (reapply from here)</button>
</form>
</div>
<div>
<h2>Your attempted text (not saved, for manual merging)</h2>
{_attempted_frontmatter_summary(attempted.get("frontmatter", {}))}
<pre>{escape(attempted.get("body", ""))}</pre>
</div>
</div>
"""
    return page("Save conflict", body, banner=banner, notice_kind="conflict")


def _attempted_frontmatter_summary(frontmatter: dict[str, Any]) -> str:
    # Every frontmatter field the visitor attempted, not just title and
    # body: a round C5 review found that a conflict on a save that also
    # changed tags, date, or summary silently dropped those from the
    # "your attempted text" pane, leaving nothing to manually merge from.
    def render(value: Any) -> str:
        if isinstance(value, list):
            return escape(", ".join(str(v) for v in value))
        return escape(str(value))

    rows = "".join(f"<li>{escape(key)}: {render(value)}</li>" for key, value in frontmatter.items())
    return f"<ul class='muted'>{rows}</ul>" if rows else ""


def diff_page(
    draft_id: str, from_version: int, to_version: int, diff_text: str, *, banner: bool
) -> str:
    lines = []
    for line in diff_text.splitlines():
        cls = ""
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            cls = "diff-hunk"
        elif line.startswith("+"):
            cls = "diff-add"
        elif line.startswith("-"):
            cls = "diff-del"
        span = f'<span class="{cls}">{escape(line)}</span>' if cls else escape(line)
        lines.append(span)
    rendered = "\n".join(lines) or "(no differences)"
    body = f"""
<p><a href="/content/drafts/{escape(draft_id)}">back to draft</a></p>
<pre>{rendered}</pre>
"""
    return page(f"Diff v{from_version} to v{to_version}", body, banner=banner)


# --- Preview tab ------------------------------------------------------------


def preview_list_page(rows: list[dict[str, Any]], *, banner: bool) -> str:
    if not rows:
        table = "<tr><td colspan=6>no built previews yet.</td></tr>"
    else:
        table = "".join(
            "<tr>"
            f'<td><a href="/content/drafts/{escape(r["draft_id"])}">{escape(r["title"])}</a></td>'
            f'<td><a href="{escape(r["preview_url"])}">{escape(r["preview_url"])}</a></td>'
            f"<td>{escape(r['built_at'] or '-')}</td>"
            f"<td>{escape(str(r['wall_seconds']) if r['wall_seconds'] is not None else '-')}</td>"
            f"<td>{'drift' if r['toolchain_drift'] else 'match'}</td>"
            f'<td><form method="post" action="/content/previews/{escape(r["draft_id"])}/rebuild">'
            '<button type="submit">Rebuild</button></form></td>'
            "</tr>"
            for r in rows
        )
    body = f"""
<table>
<tr><th>draft</th><th>preview</th><th>built</th><th>wall seconds</th><th>toolchain</th><th></th></tr>
{table}
</table>
"""
    return page("Preview", body, banner=banner)


def run_log_page(run: dict[str, Any], log_text: str, *, banner: bool) -> str:
    body = f"""
<p class="muted">kind: {escape(run["kind"])} | status: {escape(run["status"])} |
started: {escape(run.get("started_at") or "-")} | finished: {escape(run.get("finished_at") or "-")} |
builder: {escape(run.get("builder_id") or "-")} | hugo: {escape(run.get("hugo_version") or "-")} |
toolchain drift: {run.get("toolchain_drift")}</p>
<pre>{escape(log_text) or "(no log captured yet)"}</pre>
"""
    return page(f"Run {run['id']}", body, banner=banner)
