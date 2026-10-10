"""Image body reference rewrite for rename operations.

Uses the same ``_FINAL_IMAGE_REF_RE`` from ``chronicle.api.lint`` so body
reference rewriting matches the same refs that ``lint_staged_images`` checks.
"""

from __future__ import annotations

import re


def rewrite_body_image_ref(
    body: str, old_filename: str, new_filename: str, slug: str
) -> tuple[str, list[str]]:
    """Rewrite every reference to ``old_filename`` in ``body`` as
    ``new_filename``, leaving code spans untouched.

    Handles both the bare markdown form ``![alt](old_filename)`` and the
    site-path form ``/images/<slug>/old_filename``.  Returns
    ``(new_body, list_of_replaced_refs)`` so the caller can verify the
    rewrite touched every expected reference.
    """
    from .lint import _FINAL_IMAGE_REF_RE, _apply_outside_code

    replaced: list[str] = []

    def _fn(seg: str) -> str:
        # Rewrite /images/<slug>/<old> references.
        # The regex captures the full segment after the second slash (which may
        # include trailing text due to the original character class), so we
        # only match if the captured segment STARTS with old_filename followed
        # by a word boundary or end-of-string.
        def _dir_repl(m: re.Match[str]) -> str:
            ref_slug = m.group(1)
            full_capture = m.group(2)
            if ref_slug != slug:
                return m.group(0)
            # The captured filename segment may include trailing text
            # (e.g. "photo.png after") due to the regex character class.
            # We match only when old_filename appears at the START of the
            # captured segment, followed by a space, period, or end.
            prefix = re.escape(old_filename)
            pat = rf"^{prefix}(?:\s|\.|$)"
            if re.match(pat, full_capture):
                # Replace only the old_filename portion, preserving any
                # trailing text.
                trailing = full_capture[len(old_filename) :]
                replacement = f"/images/{ref_slug}/{new_filename}{trailing}"
                replaced.append(m.group(0))
                return replacement
            return m.group(0)

        seg = _FINAL_IMAGE_REF_RE.sub(_dir_repl, seg)

        # Rewrite bare markdown references: ![alt](old).
        bare_pat = r"!\[([^\]]*)\]\(\s*" + re.escape(old_filename) + r"\s*\)"

        def _bare_repl(m: re.Match[str]) -> str:
            alt_text = m.group(1)
            replacement = f"![{alt_text}]({new_filename})"
            replaced.append(m.group(0))
            return replacement

        seg = re.sub(bare_pat, _bare_repl, seg)
        return seg

    new_body = _apply_outside_code(body, _fn)
    return new_body, replaced


def lint_staged_images_from_attachments(
    body: str,
    *,
    slug: str,
    attachments: set[str],
    attachments_lower: dict[str, str],
) -> list[str]:
    """Warn (never fix) on any ``/images/<slug>/<file>`` reference in the
    body that does not match an attached image name.

    Unlike ``lint_staged_images`` this does not read the filesystem; it
    validates against a set of known attachment filenames (the body may be
    checked before conversion places files).
    """
    from .lint import _check_image_refs_exist, _scan_outside_code

    return _scan_outside_code(
        body,
        lambda seg: _check_image_refs_exist(seg, slug, attachments, attachments_lower),
    )
