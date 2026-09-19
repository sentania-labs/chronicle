"""THIRD_PARTY.md records the sha256 of each vendored file; hold it to the files.

A claim that a vendored asset is "not modified" is only checkable if the
repository says what the bytes were. This fails when a vendored file changes
without the record changing with it, and when a bundled component's licence
text is missing from `vendor/LICENSES/`.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
VENDOR = ROOT / "chronicle" / "api" / "static" / "vendor"


def recorded_checksums() -> dict[str, str]:
    text = (ROOT / "THIRD_PARTY.md").read_text(encoding="utf-8")
    return dict(re.findall(r"^\| `([^`]+)` \| `([0-9a-f]{64})` \|$", text, flags=re.MULTILINE))


def test_every_vendored_file_has_a_matching_recorded_checksum() -> None:
    recorded = recorded_checksums()
    vendored = sorted(p for p in VENDOR.iterdir() if p.is_file())
    assert vendored
    for path in vendored:
        key = path.relative_to(ROOT).as_posix()
        assert key in recorded, f"{key} has no checksum in THIRD_PARTY.md"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == recorded[key], key
    assert set(recorded) == {p.relative_to(ROOT).as_posix() for p in vendored}


def test_a_licence_text_is_carried_for_everything_the_bundle_contains() -> None:
    expected = {
        "easymde.txt": "Jeroen Akkerman",
        "codemirror.txt": "Marijn Haverbeke",
        "marked.txt": "Christopher Jeffrey",
        "codemirror-spell-checker.txt": "Wes Cossick",
        "typo-js.txt": "Christopher Finke",
    }
    for name, holder in expected.items():
        text = (VENDOR / "LICENSES" / name).read_text(encoding="utf-8")
        assert holder in text, name
        assert "Permission" in text or "Redistribution" in text, name
