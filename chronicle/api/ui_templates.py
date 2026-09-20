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

import json
from html import escape
from posixpath import basename
from typing import Any
from urllib.parse import quote

from . import ui_chrome
from .pagination import Page
from .ui_actions import DISABLED, Offer, offers_for
from .ui_chrome import badge
from .ui_status import (
    Detail,
    came_back_from_review,
    filter_options,
    parse_status_filter,
    status_details,
    status_label,
    status_tone,
)
from .ui_time import local_time

# `marked` rides in the head of every page; the stylesheets (Lattice, then
# Chronicle's own) come from `ui_chrome.head`, shared with Admin.
HEAD_SCRIPTS = '<script src="/static/vendor/marked.min.js"></script>'

# Only the editor page loads EasyMDE (and its stylesheet); every asset is
# vendored under /static/vendor and none reaches a CDN (THIRD_PARTY.md).
EDITOR_HEAD = '<link rel="stylesheet" href="/static/vendor/easymde.min.css">'
EDITOR_SCRIPTS = (
    '<script src="/static/vendor/easymde.min.js"></script>'
    '<script src="/static/ui.js"></script>'
    '<script src="/static/editor.js"></script>'
)

# User-facing label only: "Drafts" reads "Posts" everywhere the editor sees it
# (ADR 017), since the editor intends to hold other content types here too and most
# working records now start life already `published` by digest rather than
# hand-drafted. The route path, `/content/drafts`, is unchanged this round;
# see ADR 017 for why a storage/route rename is deferred.
SUBMISSIONS_TAB = "/content/submissions"
POSTS_TAB = "/content/drafts"
PREVIEW_TAB = "/content/previews"
NAV_LINKS = (
    (SUBMISSIONS_TAB, "Submissions"),
    (POSTS_TAB, "Posts"),
    (PREVIEW_TAB, "Preview"),
)

BANNER_TEXT = (
    "This Chronicle instance is internal-only and unauthenticated. "
    "Anyone who can reach it on the network can create, edit, and act on content."
)


def _nav(active: str | None) -> str:
    return ui_chrome.tabs(NAV_LINKS, active, label="Sections")


def page(
    title: str,
    body: str,
    *,
    banner: bool,
    active: str | None = None,
    notice: str | None = None,
    notice_kind: str = "error",
    editor: bool = False,
    editor_backup: bool = False,
) -> str:
    """`active` is the href of the tab this page belongs to (one of the four
    `*_TAB` constants), which is what marks it current in the tab strip.
    `editor` is the full editor page (EasyMDE and editor.js); `editor_backup`
    is the conflict page, which only needs editor.js to keep the visitor's
    attempted text in the browser."""
    # The internal-only warning is about exposure, so it is a `warn` banner,
    # placed directly under the header as Lattice asks for a whole-screen one.
    banner_html = (
        f'<p class="banner {ui_chrome.banner_class("warn")}">{escape(BANNER_TEXT)}</p>'
        if banner
        else ""
    )
    notice_html = ui_chrome.notice(notice, notice_kind) if notice else ""
    # EasyMDE's own sheet sits between Lattice and `style.css`, so Chronicle's
    # overrides of it win on order as well as on specificity.
    head = ui_chrome.head(extra_stylesheets=EDITOR_HEAD if editor else "", scripts=HEAD_SCRIPTS)
    if editor:
        scripts = EDITOR_SCRIPTS
    elif editor_backup:
        scripts = '<script src="/static/ui.js"></script><script src="/static/editor.js"></script>'
    else:
        scripts = '<script src="/static/ui.js"></script>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{escape(title)}</title>{head}</head>
<body{' class="wide"' if editor else ""}>
{ui_chrome.header("Chronicle")}
<div class="chr-page">
{banner_html}
{_nav(active)}
<h1 class="page-title">{escape(title)}</h1>
{notice_html}
{body}
</div>
{scripts}
</body></html>"""


def _pagination_links(pg: Page[Any], base_url: str, *, extra: str = "", anchor: str = "") -> str:
    """Previous/next links and the total count, `?page=N` on `base_url`,
    with whatever other query string (`extra`, already a leading `&...`)
    the current listing carries (a status filter, a search term), and an
    optional `#anchor` so a listing that shares a page with other sections
    lands back on itself."""
    suffix = f"#{anchor}" if anchor else ""
    prev_html = (
        f'<a href="{base_url}?page={pg.page - 1}{extra}{suffix}">&laquo; previous</a>'
        if pg.has_previous
        else "<span>&laquo; previous</span>"
    )
    next_html = (
        f'<a href="{base_url}?page={pg.page + 1}{extra}{suffix}">next &raquo;</a>'
        if pg.has_next
        else "<span>next &raquo;</span>"
    )
    return (
        f'<p class="pagination">{prev_html} &nbsp; '
        f"page {pg.page} of {pg.total_pages} &nbsp; ({pg.total} total) &nbsp; "
        f"{next_html}</p>"
    )


def _status_options(current: str | None) -> str:
    """The board's filter: four words, each carrying the raw statuses it
    covers as a comma list (`ui_status.filter_options`). A hand-typed
    `?status=` that is not one of those (a single raw status) is kept as its
    own selected option rather than shown as the wider word that contains it."""
    options = ['<option value="">all</option>']
    known = filter_options()
    selected_value = ",".join(parse_status_filter(current))
    for value, text in known:
        selected = " selected" if value == selected_value else ""
        options.append(f'<option value="{escape(value)}"{selected}>{escape(text)}</option>')
    if selected_value and selected_value not in {value for value, _ in known}:
        options.append(
            f'<option value="{escape(selected_value)}" selected>{escape(selected_value)}</option>'
        )
    return "".join(options)


def _detail_badges(details: list[Detail]) -> str:
    return "".join(badge(d.text, d.tone) for d in details)


# --- Submissions -------------------------------------------------------


def _table(head_cells: str, rows: str) -> str:
    """A Lattice table in its scroll wrapper: an overflowing row scrolls the
    table rather than the page. `head_cells` is the `<th>` cells, or empty for
    a table with no header row."""
    thead = f"<thead><tr>{head_cells}</tr></thead>" if head_cells else ""
    return (
        '<div class="lat-table-scroll">'
        f'<table class="lat-table">{thead}<tbody>{rows}</tbody></table></div>'
    )


def submissions_list_page(pg: Page[dict[str, Any]], *, banner: bool) -> str:
    if not pg.items:
        rows = "<tr><td colspan=6>none</td></tr>"
    else:
        rows = "".join(
            "<tr>"
            f'<td><a href="/content/submissions/{escape(s["id"])}">{escape(s["brief"][:80])}</a></td>'
            f"<td>{badge(s['status'])}</td>"
            f"<td>{escape(s['from_'])}</td>"
            f"<td>{escape(local_time(s['created_at']))}</td>"
            f'<td class="lat-num">{len(s["image_ids"])}</td>'
            f"<td>{escape(s['claimed_by'] or '-')}</td>"
            "</tr>"
            for s in pg.items
        )
    heads = (
        "<th>brief</th><th>status</th><th>from</th><th>created</th>"
        '<th class="lat-num">images</th><th>claimed by</th>'
    )
    body = f"""
{_table(heads, rows)}
{_pagination_links(pg, "/content/submissions")}
"""
    return page("Submissions", body, banner=banner, active=SUBMISSIONS_TAB)


def _submission_edit_form(submission: dict[str, Any]) -> str:
    """Plain edit form for a `new` or `claimed` submission: the brief, and one
    row per material plus a blank row to add one. Clearing a row's three
    fields removes that material. Image ids ride along hidden so a save
    keeps them; `base_version` is what makes a stale save a 409."""
    rows = list(submission["materials"]) + [{"name": "", "url": None, "text": None}]
    material_rows = "".join(
        '<fieldset class="chr-fieldset">'
        '<div class="chr-row">'
        f'<label class="lat-label">Name <input class="lat-input" name="material_name" value="{escape(m["name"])}"></label>'
        f'<label class="lat-label">URL <input class="lat-input" name="material_url" value="{escape(m.get("url") or "")}"></label>'
        "</div>"
        f'<label class="lat-label">Text<textarea class="lat-textarea" name="material_text" rows="6">\n'
        f"{escape(m.get('text') or '')}</textarea></label>"
        "</fieldset>"
        for m in rows
    )
    image_fields = "".join(
        f'<input type="hidden" name="image_id" value="{escape(image_id)}">'
        for image_id in submission["image_ids"]
    )
    return f"""
<form method="post" class="lat-card" action="/content/submissions/{escape(submission["id"])}/edit">
<h2>Edit</h2>
<input type="hidden" name="base_version" value="{submission["version_no"]}">
{image_fields}
<label class="lat-label" for="brief">Brief</label>
<textarea class="lat-textarea" id="brief" name="brief" rows="4">
{escape(submission["brief"])}</textarea>
{material_rows}
<button type="submit" class="lat-btn">Save changes (version {submission["version_no"]})</button>
</form>
"""


def submission_detail_page(
    submission: dict[str, Any],
    images: list[dict[str, Any]],
    *,
    banner: bool,
    missing_image_ids: list[str] | None = None,
    notice: str | None = None,
    notice_kind: str | None = None,
    conflict_diff: str | None = None,
    attempted: dict[str, Any] | None = None,
) -> str:
    def material_link(url: str) -> str:
        # A submission's material url is unvalidated input (chronicle.api.
        # models.Material.url is a bare str); escape() alone leaves the
        # scheme untouched, so a `javascript:` value would still render as
        # a clickable link that runs on the editor's click (found in a round C5
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
    # An id the image store does not hold is named in the list where the image
    # should be, not only in the notice above the page (#45). It is not counted
    # in the heading: that number is what the page can actually show.
    image_rows += "".join(
        f'<li class="chr-missing-image"><code>{escape(missing)}</code> '
        f"{badge('Missing', 'warn')} not in the image store</li>"
        for missing in missing_image_ids or []
    )
    can_draft = submission["status"] in ("new", "claimed")
    can_discard = submission["status"] in ("new", "claimed")
    actions = []
    if can_draft:
        actions.append(
            f'<form method="post" action="/content/submissions/{escape(submission["id"])}/draft">'
            '<button type="submit" class="lat-btn lat-btn--primary">'
            "Create post from this submission</button></form>"
        )
    if can_discard:
        actions.append(
            f'<form method="post" action="/content/submissions/{escape(submission["id"])}/discard">'
            '<button type="submit" class="lat-btn lat-btn--danger">Discard</button></form>'
        )
    conflict_html = ""
    if conflict_diff is not None:
        # A stale edit: nothing was overwritten. The form below is already
        # reloaded at the current version, and what the visitor tried to save
        # is shown below so nothing they wrote is lost.
        conflict_html += f"""
<section class="lat-card">
<h2>What changed underneath you</h2>
<pre class="lat-code">{escape(conflict_diff) or "(no diff available)"}</pre>
</section>
"""
    if attempted is not None:
        # Also shown when the edit was refused for another reason (the
        # submission was drafted or discarded in another tab).
        attempted_materials = "".join(
            f"<li><strong>{escape(m['name'])}</strong>"
            + (f" ({escape(m['url'])})" if m.get("url") else "")
            + (f'<pre class="lat-code">{escape(m["text"])}</pre>' if m.get("text") else "")
            + "</li>"
            for m in attempted.get("materials", [])
        )
        conflict_html += f"""
<section class="lat-card">
<h2>Your attempted text (not saved, for manual merging)</h2>
<pre class="lat-code">{escape(attempted.get("brief", ""))}</pre>
<ul>{attempted_materials}</ul>
</section>
"""
    body = f"""
<p class="muted">status: {badge(submission["status"])}, version: {submission["version_no"]}, from: {escape(submission["from_"])},
created: {escape(local_time(submission["created_at"]))}, claimed by: {escape(submission["claimed_by"] or "-")}</p>
<section class="lat-card">
<h2>Brief</h2>
<p class="chr-prose">{escape(submission["brief"])}</p>
</section>
<section class="lat-card">
<h2>Materials</h2>
<ul>{materials or "<li>none</li>"}</ul>
</section>
<section class="lat-card">
<h2>Images ({len(images)})</h2>
<ul>{image_rows or "<li>none</li>"}</ul>
</section>
<div class="actions">{"".join(actions)}</div>
{conflict_html}
{_submission_edit_form(submission) if can_draft else ""}
"""
    return page(
        f"Submission {submission['id']}",
        body,
        banner=banner,
        active=SUBMISSIONS_TAB,
        notice=notice,
        notice_kind=notice_kind or ("ok" if notice else "error"),
    )


# --- Drafts board --------------------------------------------------------


def _flag_badges(flags: list[dict[str, Any]]) -> str:
    if not flags:
        return ""
    # A reconciliation flag is something to look at before carrying on: warn.
    return "".join(badge(f"flag: {f['type']}", "warn") for f in flags)


def _draft_card(
    draft: dict[str, Any],
    last_author: str,
    run_info: dict[str, Any] | None,
    flags: list[dict[str, Any]],
    came_back: bool,
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
<div class="lat-card card">
<div class="chr-card-head">
<h3 class="subhead"><a href="/content/drafts/{escape(draft["id"])}">{escape(draft["title"] or "(untitled)")}</a></h3>
{badge(status_label(draft["status"]), status_tone(draft["status"]))}
{_detail_badges(status_details(draft["status"], came_back=came_back))}
{_flag_badges(flags)}
</div>
<p class="muted">slug: {escape(draft["slug"] or "-")} |
author: {escape(last_author)} | updated: {escape(local_time(draft["updated_at"]))} | claim: {claim_text}</p>
<p class="muted">PR: {pr_link} | preview: {preview_link}</p>
</div>
"""


def _cards(rows: list[dict[str, Any]]) -> str:
    return "".join(
        _draft_card(
            row["draft"], row["last_author"], row["run_info"], row["flags"], row["came_back"]
        )
        for row in rows
    )


def drafts_board_page(
    active: list[dict[str, Any]],
    archive: Page[dict[str, Any]],
    *,
    status_filter: str | None,
    q: str,
    banner: bool,
) -> str:
    """Work in flight (every status but `published`) in full above, then the
    published archive in its own collapsed section. Only the archive is paged:
    live work is never pushed off screen by a long archive, and the archive's
    own count and page links say how much is folded away."""
    filters = {"status": status_filter or "", "q": q}
    extra = "".join(f"&{k}={quote(v)}" for k, v in filters.items() if v)
    # A search, a status filter of `published`, or a page past the first all
    # mean the visitor is looking for something in the archive, so it opens.
    archive_open = " open" if (q or status_filter == "published" or archive.page > 1) else ""
    active_html = _cards(active) or '<p class="lat-banner">nothing in flight.</p>'
    archive_html = _cards(archive.items) or '<p class="lat-banner">no published posts.</p>'
    body = f"""
<form method="post" action="/content/drafts/new">
<button type="submit" class="lat-btn lat-btn--primary">New post</button>
</form>
<form method="get" action="/content/drafts" class="chr-filter">
<div class="chr-field">
<label class="lat-label" for="status">Filter by status</label>
<select class="lat-select" id="status" name="status">{_status_options(status_filter)}</select>
</div>
<div class="chr-field chr-grow">
<label class="lat-label" for="q">Search</label>
<input type="text" class="lat-input" id="q" name="q" value="{escape(q)}" placeholder="title or slug">
</div>
<button type="submit" class="lat-btn">Filter</button>
</form>
<h2>In flight ({len(active)})</h2>
<div class="chr-cards">{active_html}</div>
<details id="published"{archive_open}>
<summary>Published archive ({archive.total})</summary>
<div class="chr-cards">{archive_html}</div>
{_pagination_links(archive, "/content/drafts", extra=extra, anchor="published")}
</details>
"""
    return page("Posts", body, banner=banner, active=POSTS_TAB)


# --- Editor ---------------------------------------------------------------

# The edit form is `<form id="edit-form">` and everything that belongs to it
# but sits beside the editor (the frontmatter panel in the sidebar, the sticky
# Save button) joins it with `form="edit-form"`. The sidebar's own forms
# (claim, detach, upload) cannot nest inside it, so the sidebar is a sibling.
EDIT_FORM_ID = "edit-form"


def _frontmatter_fields(
    frontmatter: dict[str, Any],
    images: list[dict[str, Any]],
    slug: str | None,
    *,
    include_title: bool = True,
    form_id: str | None = None,
) -> str:
    join = f' form="{form_id}"' if form_id else ""

    def val(key: str) -> str:
        value = frontmatter.get(key, "")
        return escape(value if isinstance(value, str) else "")

    def list_val(key: str) -> str:
        value = frontmatter.get(key) or []
        return escape(", ".join(str(v) for v in value))

    url_field = (
        f'<input type="text" class="lat-input" id="url" name="url" value="{val("url")}" readonly{join}>'
        if slug
        else f'<input type="text" class="lat-input" id="url" name="url" value="{val("url")}"{join}>'
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
    title_field = (
        f'<label class="lat-label" for="title">Title</label>\n'
        f'<input type="text" class="lat-input" id="title" name="title" value="{val("title")}" required{join}>\n'
        if include_title
        else ""
    )
    return f"""{title_field}<label class="lat-label" for="date">Date</label>
<input type="text" class="lat-input" id="date" name="date" value="{val("date")}" placeholder="YYYY-MM-DD"{join}>
<label class="lat-label" for="categories">Categories (comma-separated)</label>
<input type="text" class="lat-input" id="categories" name="categories" value="{list_val("categories")}"{join}>
<label class="lat-label" for="tags">Tags (comma-separated)</label>
<input type="text" class="lat-input" id="tags" name="tags" value="{list_val("tags")}"{join}>
<label class="lat-label" for="summary">Summary / description</label>
<input type="text" class="lat-input" id="summary" name="summary" value="{val("summary") or val("description")}"{join}>
<label class="lat-label" for="url">URL{" (read-only, slug is pinned)" if slug else ""}</label>
{url_field}
<label class="lat-label" for="featureImage">Feature image</label>
<select class="lat-select" id="featureImage" name="featureImage"{join}><option value="">none</option>{feature_options}</select>
"""


def image_url(draft_id: str, image_id: str) -> str:
    return f"/content/drafts/{quote(draft_id, safe='')}/images/{quote(image_id, safe='')}/file"


def _image_list(draft_id: str, images: list[dict[str, Any]]) -> str:
    if not images:
        return '<p class="lat-banner">no attached images.</p>'
    # data-image-filename / data-image-src are how the editor's live render
    # finds the bytes for a body reference like `![](feature.png)`: the
    # reference is a bare filename (what convert.py rewrites), which is not a
    # URL this page can load.
    rows = "".join(
        f'<tr data-image-filename="{escape(img["filename"])}" '
        f'data-image-src="{escape(image_url(draft_id, img["image_id"]))}">'
        f'<td class="chr-mono">{escape(img["filename"])}</td>'
        f"<td>{escape(img['role'])}</td>"
        f'<td><form method="post" action="/content/drafts/{escape(draft_id)}/images/{escape(img["image_id"])}/detach">'
        '<button type="submit" class="lat-btn lat-btn--ghost">Detach</button></form></td>'
        "</tr>"
        for img in images
    )
    return _table("<th>filename</th><th>role</th><th></th>", rows)


STAGE_HINTS = {
    "revise": "moves it back to Draft first",
    "submit": "submits it for review first",
}


def _offer_button(draft_id: str, offer: Offer) -> str:
    if offer.state == DISABLED:
        return (
            f'<span class="offer"><button type="button" class="lat-btn offer-{escape(offer.action)}" '
            f'disabled title="{escape(offer.reason)}">{escape(offer.label)}</button> '
            f'<small class="offer-reason">{escape(offer.reason)}</small></span>'
        )
    reserved = ' <span class="reserved">(editor only)</span>' if offer.reserved else ""
    action_url = f"/content/drafts/{escape(draft_id)}/actions/{offer.action}"
    # Lattice allows one primary per screen; `offers_for` marks at most one.
    css = "lat-btn lat-btn--primary" if offer.primary else "lat-btn"
    # Steps the click runs beyond the action itself (a revise before a
    # preview, a submit before an approve) are said out loud, not hidden.
    staged = [STAGE_HINTS.get(step, step) for step in offer.steps[:-1]]
    hint = f' <small class="offer-reason">{escape("; ".join(staged))}</small>' if staged else ""
    if offer.feedback_required:
        return (
            f'<form method="post" action="{action_url}" class="action-form">'
            f'<label class="lat-label">{escape(offer.label)}{reserved}</label>'
            '<textarea class="lat-textarea" name="feedback" required placeholder="feedback text (required)"></textarea>'
            f'<button type="submit" class="{css}">{escape(offer.label)}</button></form>'
        )
    return (
        f'<form method="post" action="{action_url}" class="action-form">'
        f'<button type="submit" class="{css} offer-{escape(offer.action)}">'
        f"{escape(offer.label)}{reserved}</button>{hint}</form>"
    )


def _action_buttons(draft_id: str, offers: list[Offer]) -> str:
    return "".join(_offer_button(draft_id, offer) for offer in offers)


def _feedback_log(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return '<p class="lat-banner">no feedback yet.</p>'
    rows = "".join(
        f"<li><strong>v{e['version_no']} {escape(e['action'])}</strong> by {escape(e['author'])} "
        f"at {escape(local_time(e['created_at']))}: {escape(e['text'])}</li>"
        for e in entries
    )
    return f'<ul class="chr-log">{rows}</ul>'


def _version_history(draft_id: str, versions: list[dict[str, Any]]) -> str:
    if not versions:
        return '<p class="lat-banner">no versions yet.</p>'
    rows = "".join(
        "<tr>"
        f'<td class="lat-num">v{v["version_no"]}</td>'
        f"<td>{escape(v['author'])}</td>"
        f"<td>{escape(local_time(v['created_at']))}</td>"
        f'<td><a href="/content/drafts/{escape(draft_id)}/diff?from={v["version_no"] - 1}&to={v["version_no"]}">diff vs previous</a></td>'
        "</tr>"
        + (
            f'<tr class="chr-note"><td></td><td colspan="3">{escape(v["message"])}</td></tr>'
            if v.get("message")
            else ""
        )
        for v in versions
    )
    return _table('<th class="lat-num">v</th><th>author</th><th>saved</th><th></th>', rows)


def _run_status(run: dict[str, Any] | None) -> str:
    if run is None:
        return '<p class="lat-banner">no runs yet.</p>'
    return (
        f"<p>last run: {escape(run['kind'])}: {escape(run['status'])} at "
        f"{escape(local_time(run.get('finished_at') or run.get('started_at') or run['created_at']))} "
        f'(<a href="/runs/{escape(run["id"])}">log</a>)</p>'
    )


def _status_pill(status: str, details: list[Detail]) -> str:
    """The four-word status, then what the finer statuses know as detail. Two
    regions, both `data-refresh`, so a save or an action swaps in the server's
    fresh copy of each."""
    pill = badge(
        status_label(status),
        status_tone(status),
        attrs=f' id="status-pill" data-refresh data-status="{escape(status)}"',
    )
    return f'{pill}<span id="status-detail" data-refresh>{_detail_badges(details)}</span>'


def _post_info(
    draft: dict[str, Any], last_run: dict[str, Any] | None, preview_url: str | None
) -> str:
    claim = draft.get("claim")
    if claim:
        claim_html = (
            f"<p>Claimed by {escape(claim['author'])} since {escape(local_time(claim['since']))}. "
            f'<form class="inline" method="post" action="/content/drafts/{escape(draft["id"])}/release">'
            '<button type="submit" class="lat-btn">Release claim</button></form></p>'
        )
    else:
        claim_html = (
            "<p>Unclaimed. "
            f'<form class="inline" method="post" action="/content/drafts/{escape(draft["id"])}/claim">'
            '<button type="submit" class="lat-btn">Claim</button></form></p>'
        )
    preview_link = (
        f'<p><a href="{escape(preview_url)}">Last built preview</a></p>' if preview_url else ""
    )
    return (
        '<section id="post-info" data-refresh class="lat-card panel">'
        f"{claim_html}{preview_link}{_run_status(last_run)}</section>"
    )


def _panel(
    panel_id: str,
    title: str,
    inner: str,
    *,
    open_: bool,
    count: int | None = None,
    refresh: bool = True,
) -> str:
    """A collapsible sidebar panel. `data-refresh` marks it as one the editor
    swaps for the server's fresh copy after a save or an upload, so what the
    panel shows never lags the record. The frontmatter panel opts out: it
    holds form fields the visitor may be typing into."""
    badge = f' <span class="count lat-pill"><b>{count}</b></span>' if count is not None else ""
    marker = " data-refresh" if refresh else ""
    return (
        f'<details id="{panel_id}"{marker} class="lat-card panel"{" open" if open_ else ""}>'
        f"<summary>{escape(title)}{badge}</summary>{inner}</details>"
    )


def _image_upload_form(draft_id: str, images: list[dict[str, Any]]) -> str:
    # Works with no script at all (a plain multipart post that re-renders the
    # page); editor.js upgrades it to upload-in-place and insert-at-cursor.
    return f"""{_image_list(draft_id, images)}
<form id="image-form" method="post" action="/content/drafts/{escape(draft_id)}/images" enctype="multipart/form-data">
<div id="dropzone" class="dropzone">Drop an image on the editor or here, or choose one.</div>
<label class="lat-label" for="file">Upload image</label>
<input type="file" class="lat-input" id="file" name="file" accept="image/png,image/jpeg,image/gif,image/webp" required>
<label class="lat-label" for="role">Role</label>
<select class="lat-select" id="role" name="role"><option value="inline">inline (insert in body)</option><option value="feature">feature</option></select>
<button type="submit" id="upload-btn" class="lat-btn">Upload and attach</button>
</form>"""


def _save_control(publish_run_active: bool, publish_pr_open: bool) -> str:
    """The Save button, or the reason it is not offered. `Store.save_draft`
    refuses while a publish run is queued or building and while a publish PR is
    open (both 409), so the button is disabled with the reason beside it rather
    than rendered to fail on click. It is a `data-refresh` region so a swap after
    an upload or a save keeps it true; `data-locked` carries the reason for
    `editor.js`, which must refuse Ctrl+S the same way."""
    reason = (
        "A publish run is in progress, so saving is refused until it finishes."
        if publish_run_active
        else "A publish pull request is open, so saving is refused until it merges or closes."
        if publish_pr_open
        else ""
    )
    if not reason:
        return (
            '<span id="save-control" data-refresh>'
            f'<button type="submit" id="save-btn" class="lat-btn" form="{EDIT_FORM_ID}">Save</button>'
            "</span>"
        )
    return (
        '<span id="save-control" data-refresh>'
        f'<button type="submit" id="save-btn" class="lat-btn" form="{EDIT_FORM_ID}" '
        f'disabled data-locked="{escape(reason)}" title="{escape(reason)}">Save</button> '
        f'<small class="offer-reason">{escape(reason)}</small></span>'
    )


def editor_page(
    draft: dict[str, Any],
    versions: list[dict[str, Any]],
    feedback: list[dict[str, Any]],
    last_run: dict[str, Any] | None,
    preview_url: str | None,
    *,
    banner: bool,
    has_preview: bool = False,
    publish_pr_open: bool = False,
    publish_run_active: bool = False,
    unpublish_pr_open: bool = False,
    notice: str | None = None,
    notice_kind: str = "error",
) -> str:
    draft_id = escape(draft["id"])
    pr_open_notice = (
        f'<p id="pr-open-notice" data-refresh class="notice conflict {ui_chrome.banner_class("warn")}">This post has an open '
        "publish pull request; saving is refused until it merges or closes.</p>"
        if publish_pr_open
        else '<p id="pr-open-notice" data-refresh hidden></p>'
    )
    offers = offers_for(
        draft["status"],
        has_preview=has_preview,
        republish=bool(draft.get("published")),
        publish_pr_open=publish_pr_open,
        publish_run_active=publish_run_active,
        unpublish_pr_open=unpublish_pr_open,
    )
    details = status_details(
        draft["status"],
        came_back=came_back_from_review(
            draft["status"],
            [entry["action"] for entry in feedback],
            published=bool(draft.get("published")),
        ),
        has_preview=has_preview,
        publish_run_active=publish_run_active,
        publish_pr_open=publish_pr_open,
        unpublish_pr_open=unpublish_pr_open,
    )
    frontmatter = draft["frontmatter"]
    title_value = frontmatter.get("title", "")
    title_value = title_value if isinstance(title_value, str) else ""
    body = f"""
<div id="editor-app" data-draft-id="{draft_id}" data-version="{draft["version_no"]}">
<div id="backup-banner" class="backup-banner {ui_chrome.banner_class("warn")}" role="alert" hidden>
<span id="backup-banner-text"></span>
<button type="button" id="backup-restore" class="lat-btn">Restore</button>
<button type="button" id="backup-discard" class="lat-btn lat-btn--ghost">Discard</button>
</div>
{pr_open_notice}
<div class="editor-bar" id="editor-bar">
{_status_pill(draft["status"], details)}
{_save_control(publish_run_active, publish_pr_open)}
<span id="save-state" class="save-state" data-state="idle" role="status" aria-live="polite">No unsaved changes</span>
<span id="upload-state" class="upload-state" role="status" aria-live="polite"></span>
</div>
<div class="editor-layout">
<div class="editor-main">
<div id="action-panel" data-refresh class="actions">{_action_buttons(draft["id"], offers)}</div>
<form id="{EDIT_FORM_ID}" method="post" action="/content/drafts/{draft_id}/save">
<input type="hidden" id="base_version" name="base_version" value="{draft["version_no"]}">
<label class="lat-label" for="title">Title</label>
<input type="text" class="lat-input" id="title" name="title" value="{escape(title_value)}" required>
<label class="lat-label" for="body">Body (markdown)</label>
<textarea class="lat-textarea" id="body" name="body" data-editor="markdown">{escape(draft["body"])}</textarea>
</form>
</div>
<aside class="editor-side">
{_post_info(draft, last_run, preview_url)}
{_panel("frontmatter-panel", "Frontmatter", _frontmatter_fields(frontmatter, draft["images"], draft["slug"], include_title=False, form_id=EDIT_FORM_ID), open_=False, refresh=False)}
{_panel("feedback-panel", "Feedback", _feedback_log(feedback), open_=True, count=len(feedback))}
{_panel("images-panel", "Images", _image_upload_form(draft["id"], draft["images"]), open_=True, count=len(draft["images"]))}
{_panel("versions-panel", "Version history", _version_history(draft["id"], versions), open_=False, count=len(versions))}
</aside>
</div>
</div>
"""
    return page(
        f"Post: {draft['title'] or '(untitled)'}",
        body,
        banner=banner,
        active=POSTS_TAB,
        notice=notice,
        notice_kind=notice_kind,
        editor=True,
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
    a second, read-only pane so nothing they wrote is silently lost.

    `editor.js` reads `#attempted-body` on this page and, when the browser's
    local backup holds nothing else the visitor has not answered for, writes
    the attempted text there (with the frontmatter fields from `data-fields`,
    the raw form values as posted) so the editor's restore banner offers it
    back. It then shows whichever of the three `backup-note-*` sentences is
    true; with no script none of them is claimed."""
    body = f"""
<p class="notice conflict {ui_chrome.banner_class("warn")} chr-flow">Someone else saved post {escape(draft["id"])} to version
{draft["version_no"]} while you were editing version {attempted["base_version"]}. Nothing was
overwritten. Review the diff below, then use the reloaded form (now at the current version) to
reapply anything from your attempted text on the right.
<span id="backup-note-stored" hidden>Your text is also kept in this browser: open the post again
and choose Restore.</span>
<span id="backup-note-kept-other" hidden>This browser already holds earlier unsaved work for this
post, and it was left as it was: the attempted text on the right is not stored there, so copy
anything you need from it now.</span>
<span id="backup-note-unavailable" hidden>This browser could not store your text: it is only in the
pane on the right, so copy anything you need from it now.</span></p>
<section class="lat-card">
<h2>What changed underneath you</h2>
<pre class="lat-code">{escape(diff_summary) or "(no diff available)"}</pre>
</section>
<div class="columns">
<div>
<form method="post" class="lat-card" action="/content/drafts/{escape(draft["id"])}/save">
<h2>Current version (reloaded, ready to reapply)</h2>
<input type="hidden" name="base_version" value="{draft["version_no"]}">
{_frontmatter_fields(draft["frontmatter"], draft["images"], draft["slug"])}
<label class="lat-label" for="body">Body (markdown)</label>
<textarea class="lat-textarea" id="body" name="body">{escape(draft["body"])}</textarea>
<button type="submit" class="lat-btn lat-btn--primary">Save (reapply from here)</button>
</form>
</div>
<div>
<section class="lat-card">
<h2>Your attempted text (not saved, for manual merging)</h2>
{_attempted_frontmatter_summary(attempted.get("frontmatter", {}))}
<pre class="lat-code" id="attempted-body" data-draft-id="{escape(draft["id"])}" data-base-version="{attempted["base_version"]}" data-fields="{escape(json.dumps(attempted.get("fields", {})))}">{escape(attempted.get("body", ""))}</pre>
</section>
</div>
</div>
"""
    return page(
        "Save conflict",
        body,
        banner=banner,
        active=POSTS_TAB,
        notice_kind="conflict",
        editor_backup=True,
    )


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
<p><a href="/content/drafts/{escape(draft_id)}">back to post</a></p>
<pre class="lat-code">{rendered}</pre>
"""
    return page(f"Diff v{from_version} to v{to_version}", body, banner=banner, active=POSTS_TAB)


# --- Preview tab ------------------------------------------------------------


def _toolchain_badge(drift: bool) -> str:
    """The builder's Hugo against the site's: a mismatch is worth a look (warn),
    a match is the ordinary case and says so quietly (ok)."""
    return badge("drift", "warn") if drift else badge("match", "ok")


def preview_list_page(rows: list[dict[str, Any]], *, banner: bool) -> str:
    if not rows:
        table = "<tr><td colspan=6>no built previews yet.</td></tr>"
    else:
        table = "".join(
            "<tr>"
            f'<td><a href="/content/drafts/{escape(r["draft_id"])}">{escape(r["title"])}</a></td>'
            f'<td><a href="{escape(r["preview_url"])}">{escape(r["preview_url"])}</a></td>'
            f"<td>{escape(local_time(r['built_at']))}</td>"
            f'<td class="lat-num">{escape(str(r["wall_seconds"]) if r["wall_seconds"] is not None else "-")}</td>'
            f"<td>{_toolchain_badge(r['toolchain_drift'])}</td>"
            f'<td><form method="post" action="/content/previews/{escape(r["draft_id"])}/rebuild">'
            '<button type="submit" class="lat-btn">Rebuild</button></form></td>'
            "</tr>"
            for r in rows
        )
    heads = (
        "<th>post</th><th>preview</th><th>built</th>"
        '<th class="lat-num">wall seconds</th><th>toolchain</th><th></th>'
    )
    body = f"""
{_table(heads, table)}
"""
    return page("Preview", body, banner=banner, active=PREVIEW_TAB)


def run_log_page(run: dict[str, Any], log_text: str, *, banner: bool) -> str:
    body = f"""
<p class="muted">kind: {escape(run["kind"])} | status: {escape(run["status"])} |
started: {escape(local_time(run.get("started_at")))} | finished: {escape(local_time(run.get("finished_at")))} |
builder: {escape(run.get("builder_id") or "-")} | hugo: {escape(run.get("hugo_version") or "-")} |
toolchain drift: {run.get("toolchain_drift")}</p>
<pre class="lat-code">{escape(log_text) or "(no log captured yet)"}</pre>
"""
    return page(f"Run {run['id']}", body, banner=banner, active=PREVIEW_TAB)
