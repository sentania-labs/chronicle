"""`sanitize` in ui.js, run in a real browser DOM.

Node has no `DOMParser`, so `tests/ui_sanitize.test.mjs` can only reach
`isSafeUrl`. The preview pane's sanitiser is the boundary between a draft body
(untrusted: another consumer token can write it) and the editor page's own
controls, so it is exercised here by loading the real `ui.js` into headless
Chrome and reading the serialised result back out of the page.

Skipped, with the reason stated, when no Chrome or Chromium is on PATH
(`CHRONICLE_TEST_CHROME` names one explicitly), and a failure instead under
CHRONICLE_REQUIRE_TEST_TOOLS=1, which CI sets (`conftest.py`). It is not
asserted by any weaker stand-in: without a browser this file tests nothing,
and says so.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from .conftest import find_chrome

STATIC = Path(__file__).parent.parent / "chronicle" / "api" / "static"

CHROME = find_chrome()

pytestmark = pytest.mark.requires_tool("chrome")

PAGE = """<!doctype html><meta charset="utf-8"><base href="http://localhost/">
<script src="file://{ui_js}"></script>
<pre id="results"></pre>
<script>
var cases = {cases};
var resolve = function (src) {{
  return src === "rack.png" ? "/content/drafts/d/images/i1/file" : src;
}};
document.getElementById("results").textContent = JSON.stringify(
  cases.map(function (html) {{ return sanitize(html, resolve); }})
);
</script>
"""


def sanitize_all(tmp_path: Path, cases: list[str]) -> list[str]:
    page = tmp_path / "page.html"
    page.write_text(
        PAGE.format(
            ui_js=(STATIC / "ui.js").as_posix(), cases=json.dumps(cases).replace("</", "<\\/")
        ),
        encoding="utf-8",
    )
    assert CHROME is not None
    result = subprocess.run(
        [
            CHROME,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            f"--user-data-dir={tmp_path / 'profile'}",
            "--dump-dom",
            page.as_uri(),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    marker = '<pre id="results">'
    start = result.stdout.index(marker) + len(marker)
    text = result.stdout[start : result.stdout.index("</pre>", start)]
    import html

    return json.loads(html.unescape(text))


# A payload, and what must not survive it.
HOSTILE = [
    (
        '<button formaction="/x/actions/approve" formmethod="post">go</button>',
        ["button", "formaction", "formmethod"],
    ),
    ('<input type="image" formaction="/x/actions/approve" src="a.png">', ["<input", "formaction"]),
    (
        '<form action="/x/actions/approve" method="post"><button>go</button></form>',
        ["<form", "action=", "button"],
    ),
    (
        '<a href="/ok" formaction="/x" formtarget="_blank" form="edit-form">t</a>',
        ["formaction", "formtarget", "form="],
    ),
    ('<div id="save-btn" name="body">x</div>', ["id=", "name="]),
    ('<label for="save-btn">click me</label>', ["for="]),
    ('<select name="role"><option>inline</option></select>', ["<select", "<option"]),
    (
        "<textarea>x</textarea><fieldset>y</fieldset><output>z</output>",
        ["<textarea", "<fieldset", "<output"],
    ),
    (
        '<template><button formaction="/x">go</button></template>',
        ["<template", "button", "formaction"],
    ),
    ('<img src="x" onerror="alert(1)">', ["onerror"]),
    ('<a href="javascript:alert(1)">t</a>', ["javascript"]),
    ('<a href="java&#x0A;script:alert(1)">t</a>', ["script:"]),
    ("<svg><script>alert(1)</script></svg>", ["<script"]),
    (
        '<iframe src="/x"></iframe><object data="/x"></object><embed src="/x">',
        ["<iframe", "<object", "<embed"],
    ),
    ('<img src="a.png" srcset="b.png 2x">', ["srcset"]),
]


def test_hostile_markup_loses_every_control_and_submit_attribute(tmp_path: Path) -> None:
    outputs = sanitize_all(tmp_path, [case for case, _ in HOSTILE])
    for (case, banned), output in zip(HOSTILE, outputs, strict=True):
        for fragment in banned:
            assert fragment not in output.lower(), f"{fragment!r} survived {case!r}: {output!r}"


def test_ordinary_markup_and_links_survive(tmp_path: Path) -> None:
    html = (
        "<p><strong>hi</strong> <em>there</em> <code>x</code></p><ul><li>a</li></ul>"
        '<a href="https://example.com/x">link</a> <a href="/rel">rel</a>'
        "<table><tr><td>c</td></tr></table>"
    )
    [output] = sanitize_all(tmp_path, [html])
    assert "<strong>hi</strong>" in output and "<ul><li>a</li></ul>" in output
    assert '<a href="https://example.com/x">link</a>' in output
    assert '<a href="/rel">rel</a>' in output
    assert "<table>" in output


def test_an_attached_image_reference_resolves_and_an_unsafe_one_does_not(tmp_path: Path) -> None:
    resolved, remote, unsafe = sanitize_all(
        tmp_path,
        [
            '<img src="rack.png" alt="rack">',
            '<img src="https://example.com/x.png">',
            '<img src="javascript:alert(1)" alt="x">',
        ],
    )
    assert 'src="/content/drafts/d/images/i1/file"' in resolved
    assert 'src="https://example.com/x.png"' in remote
    assert "javascript" not in unsafe and "src=" not in unsafe


DRAFT_PREVIEW_PAGE = """<!doctype html><meta charset="utf-8"><base href="http://localhost/">
<script src="file://{marked_js}"></script>
<script src="file://{ui_js}"></script>
<script src="file://{editor_js}"></script>
<pre id="results"></pre>
<script>
var body = {body};
var images = [{{ filename: "image.png", src: "/content/drafts/d1/images/abc123/file" }}];
var imageDir = {image_dir};
var html = sanitize(marked.parse(body), function (src) {{
  return lookupImageSrc(src, images, imageDir);
}});
document.getElementById("results").textContent = JSON.stringify({{ html: html, body: body }});
</script>
"""


def render_draft_preview(tmp_path: Path, body: str, image_dir: str) -> dict:
    page = tmp_path / "preview.html"
    page.write_text(
        DRAFT_PREVIEW_PAGE.format(
            marked_js=(STATIC / "vendor" / "marked.min.js").as_posix(),
            ui_js=(STATIC / "ui.js").as_posix(),
            editor_js=(STATIC / "editor.js").as_posix(),
            body=json.dumps(body).replace("</", "<\\/"),
            image_dir=json.dumps(image_dir),
        ),
        encoding="utf-8",
    )
    assert CHROME is not None
    result = subprocess.run(
        [
            CHROME,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            f"--user-data-dir={tmp_path / 'profile'}",
            "--dump-dom",
            page.as_uri(),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    marker = '<pre id="results">'
    start = result.stdout.index(marker) + len(marker)
    text = result.stdout[start : result.stdout.index("</pre>", start)]
    import html

    return json.loads(html.unescape(text))


def test_a_digested_posts_site_path_image_renders_in_the_editor_preview(tmp_path: Path) -> None:
    body = "![The relationships!](/images/vcf-operations-can-now-see-my-unifi-network/image.png)"
    result = render_draft_preview(tmp_path, body, "vcf-operations-can-now-see-my-unifi-network")
    assert 'src="/content/drafts/d1/images/abc123/file"' in result["html"]
    # The live render never rewrites the source markdown itself.
    assert result["body"] == body


def test_a_site_path_image_from_a_different_posts_directory_stays_broken(tmp_path: Path) -> None:
    body = "![x](/images/some-other-post/image.png)"
    result = render_draft_preview(tmp_path, body, "vcf-operations-can-now-see-my-unifi-network")
    assert 'src="/images/some-other-post/image.png"' in result["html"]


# The feature image at the top of the preview: composed the same way
# editor.js's renderMarkdown does (featureImageSrc off the selected <option>'s
# data attribute, run through sanitize, prepended ahead of the body's own
# sanitized markdown). This exercises featureImageSrc, sanitize, and their
# combination in a real DOM the same way DRAFT_PREVIEW_PAGE exercises
# lookupImageSrc, without needing EasyMDE itself in headless Chrome.
FEATURE_PREVIEW_PAGE = """<!doctype html><meta charset="utf-8"><base href="http://localhost/">
<script src="file://{marked_js}"></script>
<script src="file://{ui_js}"></script>
<script src="file://{editor_js}"></script>
<select id="featureImage">
<option value="">none</option>
<option value="feature.png" data-image-src="/content/drafts/d1/images/abc123/file"
{selected}>feature.png</option>
</select>
<textarea id="body">{body}</textarea>
<pre id="results"></pre>
<script>
var select = document.getElementById("featureImage");
var textarea = document.getElementById("body");
var src = featureImageSrc(select);
var featureHtml = src
  ? sanitize('<img src="' + src.replace(/"/g, "&quot;") + '" alt="feature image">')
  : "";
var body = sanitize(marked.parse(textarea.value), function (s) {{
  return lookupImageSrc(s, [], "");
}});
var html = featureHtml + body;
var out = {{ html: html, textarea: textarea.value }};
document.getElementById("results").textContent = JSON.stringify(out);
</script>
"""


def render_feature_preview(tmp_path: Path, body: str, *, selected: bool) -> dict:
    page = tmp_path / "feature-preview.html"
    page.write_text(
        FEATURE_PREVIEW_PAGE.format(
            marked_js=(STATIC / "vendor" / "marked.min.js").as_posix(),
            ui_js=(STATIC / "ui.js").as_posix(),
            editor_js=(STATIC / "editor.js").as_posix(),
            body=body,
            selected="selected" if selected else "",
        ),
        encoding="utf-8",
    )
    assert CHROME is not None
    result = subprocess.run(
        [
            CHROME,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            f"--user-data-dir={tmp_path / 'profile'}",
            "--dump-dom",
            page.as_uri(),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    marker = '<pre id="results">'
    start = result.stdout.index(marker) + len(marker)
    text = result.stdout[start : result.stdout.index("</pre>", start)]
    import html

    return json.loads(html.unescape(text))


def test_the_selected_feature_image_renders_at_the_top_of_the_preview(tmp_path: Path) -> None:
    result = render_feature_preview(tmp_path, "the body text", selected=True)
    assert result["html"].startswith(
        '<img src="/content/drafts/d1/images/abc123/file" alt="feature image">'
    )
    assert "the body text" in result["html"]
    # renderMarkdown never touches the textarea itself: only the preview pane
    # (EasyMDE's own innerHTML assignment) is built from it.
    assert result["textarea"] == "the body text"


def test_no_feature_image_selected_renders_nothing_at_the_top(tmp_path: Path) -> None:
    result = render_feature_preview(tmp_path, "the body text", selected=False)
    assert "<img" not in result["html"]
    assert "the body text" in result["html"]


# A freshly uploaded feature image: editor.js's addFeatureOption creates the
# new <option> client-side (the frontmatter panel does not refresh after an
# upload, ui_templates._panel's own `refresh=False`), and a round of review
# on this change found it left that option with no `data-image-src`, so the
# thumbnail and the preview stayed blank for an image just uploaded and
# selected as the feature image, until the next full page load. This drives
# the real upload path (a mocked fetch, a real DataTransfer-backed file
# input, the actual `#image-form` submit handler) in a full editor.js load,
# not a hand-rolled replica, since the bug was in wiring between two
# functions rather than in either one alone.
UPLOAD_PAGE = """<!doctype html><meta charset="utf-8"><base href="http://localhost/">
<script>
window.__error = null;
window.onerror = function (msg) {{ window.__error = String(msg); }};
window.fetch = function (url, opts) {{
  if (String(url).indexOf("/images") !== -1 && opts && opts.method === "POST") {{
    return Promise.resolve({{
      json: function () {{
        return Promise.resolve({{
          ok: true,
          filename: "new-feature.png",
          role: "feature",
          markdown: null,
          url: "/content/drafts/d1/images/newsha/file",
        }});
      }},
    }});
  }}
  return Promise.resolve({{ text: function () {{ return Promise.resolve("<html></html>"); }} }});
}};
</script>
<div id="editor-app" data-draft-id="d1">
<form id="edit-form" method="post" action="/content/drafts/d1/save">
<input type="hidden" id="base_version" value="1">
<textarea id="body"></textarea>
</form>
</div>
<button id="save-btn">Save</button>
<span id="save-state"></span>
<span id="upload-state"></span>
<select id="featureImage">
<option value="">none</option>
</select>
<img id="featureImageThumb" alt="" hidden>
<div id="images-panel"></div>
<form id="image-form">
<input type="file" id="file">
<select id="role"><option value="feature" selected>feature</option></select>
</form>
<pre id="results"></pre>
<script src="file://{ui_js}"></script>
<script src="file://{editor_js}"></script>
<script>
var input = document.getElementById("file");
var dt = new DataTransfer();
dt.items.add(new File(["x"], "new-feature.png", {{ type: "image/png" }}));
input.files = dt.files;
var submitEvent = new Event("submit", {{ bubbles: true, cancelable: true }});
document.getElementById("image-form").dispatchEvent(submitEvent);
setTimeout(function () {{
  var select = document.getElementById("featureImage");
  var thumb = document.getElementById("featureImageThumb");
  var option = select.options[select.selectedIndex];
  document.getElementById("results").textContent = JSON.stringify({{
    selectValue: select.value,
    optionHasSrc: !!(option && option.dataset.imageSrc),
    optionSrc: option && option.dataset.imageSrc,
    thumbHidden: thumb.hidden,
    thumbSrc: thumb.src,
    error: window.__error,
  }});
}}, 50);
</script>
"""


def test_a_freshly_uploaded_feature_image_shows_in_the_thumbnail_without_reload(
    tmp_path: Path,
) -> None:
    page = tmp_path / "upload.html"
    page.write_text(
        UPLOAD_PAGE.format(
            ui_js=(STATIC / "ui.js").as_posix(),
            editor_js=(STATIC / "editor.js").as_posix(),
        ),
        encoding="utf-8",
    )
    assert CHROME is not None
    result = subprocess.run(
        [
            CHROME,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            f"--user-data-dir={tmp_path / 'profile'}",
            "--virtual-time-budget=2000",
            "--dump-dom",
            page.as_uri(),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    marker = '<pre id="results">'
    start = result.stdout.index(marker) + len(marker)
    text = result.stdout[start : result.stdout.index("</pre>", start)]
    import html

    out = json.loads(html.unescape(text))
    assert out["selectValue"] == "new-feature.png"
    assert out["optionHasSrc"] is True
    assert out["optionSrc"] == "/content/drafts/d1/images/newsha/file"
    assert out["thumbHidden"] is False
    assert out["thumbSrc"].endswith("/content/drafts/d1/images/newsha/file")
