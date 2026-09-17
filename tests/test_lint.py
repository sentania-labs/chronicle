"""lint.py: ported from the vault publisher's test_publisher.py lint suite."""

from __future__ import annotations

from pathlib import Path

from chronicle.api import lint


def test_autofix_relative_image_ref() -> None:
    body = (
        "See ![a](images/dash-slug/one.png) and "
        "![b](drafts/images/dash-slug/two.jpg) but leave "
        "![c](/images/other-slug/three.gif) alone."
    )
    out, warnings = lint.lint_and_normalize_body(body, slug="final-slug")

    assert "/images/final-slug/one.png" in out
    assert "/images/final-slug/two.jpg" in out
    assert "/images/other-slug/three.gif" in out, "already-rooted ref must be left untouched"
    assert "images/dash-slug" not in out
    fixes = [w for w in warnings if "normalized relative image ref" in w]
    assert len(fixes) == 2, warnings


def test_warn_malformed_links() -> None:
    body = (
        "Broken bracket link: [click here][https://example.com/x].\n"
        "Missing parens entirely: [click here]https://example.com/y.\n"
        "Empty target: [click here]().\n"
        "A normal [fine link](https://example.com/z) must not warn."
    )
    out, warnings = lint.lint_and_normalize_body(body, slug="s")

    assert out == body, "warn-only rules must never rewrite the body"
    assert any("][" in w for w in warnings)
    assert any("missing its opening parenthesis" in w for w in warnings)
    assert any("empty link target" in w for w in warnings)
    assert not any("example.com/z" in w for w in warnings)


def test_clean_body_byte_identical() -> None:
    body = (
        "# Title\n\nNormal text with a [fine link](https://example.com) "
        "and an already-correct ![image](/images/my-slug/pic.png).\n"
    )
    out, warnings = lint.lint_and_normalize_body(body, slug="my-slug")
    assert out == body
    assert warnings == []


def test_never_touches_code_spans() -> None:
    body = (
        "Inline example: `![x](images/slug/file.png)` shows the bug.\n\n"
        "```markdown\n"
        "![x](images/slug/file.png)\n"
        "[broken]https://example.com/one\n"
        "[also broken][https://example.com/two]\n"
        "```\n\n"
        "Prose after the fence stays linted: ![y](images/slug/real.png)\n"
    )
    out, warnings = lint.lint_and_normalize_body(body, slug="final")

    assert "`![x](images/slug/file.png)`" in out, "inline code span was rewritten"
    assert (
        "![x](images/slug/file.png)\n[broken]https://example.com/one\n"
        "[also broken][https://example.com/two]"
    ) in out, "fenced code block content was rewritten"
    assert "/images/final/real.png" in out, "prose outside the fence must still be linted"
    fixes = [w for w in warnings if "normalized relative image ref" in w]
    assert len(fixes) == 1, warnings
    assert not any(w.startswith("malformed link") or w.startswith("empty link") for w in warnings)


def test_staged_images_warnings(tmp_path: Path) -> None:
    dest_dir = tmp_path
    (dest_dir / "Pic.png").write_bytes(b"x")
    (dest_dir / "ok.png").write_bytes(b"x")

    body = (
        "![a](/images/my-slug/ok.png) "
        "![b](/images/other-slug/ok.png) "
        "![c](/images/my-slug/pic.png) "
        "![d](/images/my-slug/missing.png) "
        "![e](/images/my-slug/has space.png)"
    )
    warnings = lint.lint_staged_images(body, slug="my-slug", dest_dir=dest_dir)

    assert any("other-slug" in w for w in warnings)
    assert any("case-mismatch" in w for w in warnings)
    assert any("no matching file" in w for w in warnings)
    assert any("contains spaces" in w for w in warnings)
    assert not any("'/images/my-slug/ok.png'" in w for w in warnings)
