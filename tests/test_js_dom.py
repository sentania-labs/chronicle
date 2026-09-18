"""`sanitize` in ui.js, run in a real browser DOM.

Node has no `DOMParser`, so `tests/ui_sanitize.test.mjs` can only reach
`isSafeUrl`. The preview pane's sanitiser is the boundary between a draft body
(untrusted: another consumer token can write it) and the editor page's own
controls, so it is exercised here by loading the real `ui.js` into headless
Chrome and reading the serialised result back out of the page.

Skipped, with the reason stated, when no Chrome or Chromium is on PATH
(`CHRONICLE_TEST_CHROME` names one explicitly). It is not asserted by any
weaker stand-in: without a browser this file tests nothing, and says so.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).parent.parent / "chronicle" / "api" / "static"

CHROME = os.environ.get("CHRONICLE_TEST_CHROME") or next(
    (
        found
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
        if (found := shutil.which(name))
    ),
    None,
)

pytestmark = pytest.mark.skipif(
    CHROME is None, reason="no Chrome or Chromium on PATH (set CHRONICLE_TEST_CHROME)"
)

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
