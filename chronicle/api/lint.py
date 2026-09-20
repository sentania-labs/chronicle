"""Publish-time lint and normalize pass, ported from the vault publisher.

Runs at publish (spec section 9: "The lint and normalize pass the vault
publisher had runs at publish time, not at save time"), right before the
converted post's body is written to a blob. It never raises and never
blocks a publish: rule 1 silently auto-fixes a relative image reference
that is missing its leading slash, and rules 2 through 4 only warn, both
tiers returning their findings as plain strings instead of logging directly
so the caller (the publisher) decides where a warning goes (the run's log
and its result, not a bare log line the way the original vault script used).

Ported from the vault's blog lint script with two changes to fit
Chronicle's shape: `log()` calls become returned warning strings, and
`lint_and_normalize_body` takes only `slug` (Chronicle has one slug per post
throughout, not a separate dashboard slug and blog slug).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

# --- code-span protection --------------------------------------------------
# Every lint/fix rule below must leave fenced code blocks and inline code
# spans untouched - a body showing markdown syntax as a worked example must
# not get rewritten.

_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,}).*?^[ \t]*\1[ \t]*$", re.MULTILINE | re.DOTALL)
_INLINE_CODE_RE = re.compile(r"(`+)(?:(?!\1).)*?\1", re.DOTALL)


def _code_spans(text: str) -> list[tuple[int, int]]:
    spans = [m.span() for m in _FENCE_RE.finditer(text)]
    for m in _INLINE_CODE_RE.finditer(text):
        s, e = m.span()
        if not any(fs <= s and e <= fe for fs, fe in spans):
            spans.append((s, e))
    spans.sort()
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _apply_outside_code(text: str, fn: Callable[[str], str]) -> str:
    """Apply `fn` to every segment of `text` that is NOT inside a fenced
    code block or inline code span, leaving code segments byte-identical."""
    spans = _code_spans(text)
    if not spans:
        return fn(text)
    out = []
    pos = 0
    for s, e in spans:
        out.append(fn(text[pos:s]))
        out.append(text[s:e])
        pos = e
    out.append(fn(text[pos:]))
    return "".join(out)


def _scan_outside_code(text: str, fn: Callable[[str], list[str]]) -> list[str]:
    """Read-only counterpart to _apply_outside_code: runs `fn` over every
    non-code segment and concatenates the warning lists it returns."""
    spans = _code_spans(text)
    if not spans:
        return fn(text)
    results: list[str] = []
    pos = 0
    for s, e in spans:
        results.extend(fn(text[pos:s]))
        pos = e
    results.extend(fn(text[pos:]))
    return results


# --- rule 1 (auto-fix): relative image ref missing its leading slash -------
_RELATIVE_IMAGE_RE = re.compile(r'(?<![/\w])(?:drafts/)?images/([^/\s"\')]+)/([^/\s"\')]+)')


def _fix_relative_image_refs(segment: str, slug: str, fixes: list[tuple[str, str]]) -> str:
    def _sub(m: re.Match[str]) -> str:
        filename = m.group(2)
        replacement = f"/images/{slug}/{filename}"
        fixes.append((m.group(0), replacement))
        return replacement

    return _RELATIVE_IMAGE_RE.sub(_sub, segment)


# --- rule 3 (warn): malformed markdown links -------------------------------
_BROKEN_BRACKET_LINK_RE = re.compile(r"\[([^\]\n]+)\]\[((?:https?://|/)[^\]\n]*)\]")
_MISSING_PAREN_URL_RE = re.compile(r"\[([^\]\n]+)\](?!\(|\[)\s*(https?://\S+)")
_EMPTY_LINK_TARGET_RE = re.compile(r"\[([^\]\n]*)\]\(\s*\)")


def _find_malformed_links(segment: str) -> list[str]:
    warnings: list[str] = []
    for m in _BROKEN_BRACKET_LINK_RE.finditer(segment):
        warnings.append(f"malformed link {m.group(0)!r} uses '][' where '](' was likely intended")
    for m in _MISSING_PAREN_URL_RE.finditer(segment):
        warnings.append(f"malformed link {m.group(0)!r} is missing its opening parenthesis")
    for m in _EMPTY_LINK_TARGET_RE.finditer(segment):
        warnings.append(f"empty link target in {m.group(0)!r}")
    return warnings


def lint_and_normalize_body(body: str, *, slug: str) -> tuple[str, list[str]]:
    """Rule 1 (auto-fix) and rule 3 (warn) over a post body, skipping code
    spans. Returns (possibly rewritten body, warnings). Call before images
    are copied: it only touches refs the conversion's own rewrite doesn't
    already handle (a missing leading slash), so the two never fight over
    the same text.
    """
    fixes: list[tuple[str, str]] = []
    fixed = _apply_outside_code(body, lambda seg: _fix_relative_image_refs(seg, slug, fixes))
    warnings = [f"normalized relative image ref {orig!r} -> {repl!r}" for orig, repl in fixes]
    warnings.extend(_scan_outside_code(fixed, _find_malformed_links))
    return fixed, warnings


# --- rules 2 + 4 (warn): staged-image sanity, run after images are placed --
_FINAL_IMAGE_REF_RE = re.compile(r'/images/([^/\s"\')<>]+)/([^"\')<>\n]+)')


def _check_image_refs_exist(
    segment: str, slug: str, existing: set[str], existing_lower: dict[str, str]
) -> list[str]:
    warnings: list[str] = []
    seen: set[tuple[str, str]] = set()
    for m in _FINAL_IMAGE_REF_RE.finditer(segment):
        ref_slug, filename = m.group(1), m.group(2)
        key = (ref_slug, filename)
        if key in seen:
            continue
        seen.add(key)

        if ref_slug != slug:
            warnings.append(
                f"image ref {m.group(0)!r} points at slug '{ref_slug}', not this post's "
                f"slug '{slug}' - will 404"
            )
            continue
        if " " in filename:
            warnings.append(
                f"image ref {m.group(0)!r} filename contains spaces - will likely 404 once served"
            )
        if filename in existing:
            continue
        if filename.lower() in existing_lower:
            warnings.append(
                f"image ref {m.group(0)!r} case-mismatches the file on disk "
                f"('{existing_lower[filename.lower()]}') - case-sensitive server will 404"
            )
        else:
            warnings.append(
                f"image ref {m.group(0)!r} has no matching file in static/images/{slug}/ - will 404"
            )
    return warnings


def lint_staged_images(body: str, *, slug: str, dest_dir: Path) -> list[str]:
    """Warn (never fix) on any `/images/<slug>/<file>` reference in the
    already-rewritten body that won't resolve against what's actually in
    `dest_dir`: wrong slug, a filename with spaces, a case mismatch against
    the file on disk, or no matching file at all. Call after images are
    copied into `dest_dir`.
    """
    try:
        existing = (
            {f.name for f in dest_dir.iterdir() if f.is_file()} if dest_dir.is_dir() else set()
        )
    except OSError:
        existing = set()
    existing_lower = {name.lower(): name for name in existing}
    return _scan_outside_code(
        body, lambda seg: _check_image_refs_exist(seg, slug, existing, existing_lower)
    )
