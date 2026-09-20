"""Issue 39: a filename whose stem keeps only a fragment must not collide.

`写真2.png` and `猫2.png` both used to clean to `2.png`; the second was then
refused with `image_filename_conflict`. A fragment of a word now takes a short
content hash, and the result is still a name a markdown reference and
`convert.py` both carry (`images.is_plain_filename`).
"""

from __future__ import annotations

import pytest

from chronicle.api.images import is_plain_filename, safe_upload_filename
from chronicle.api.store import Store

from .conftest import png_bytes


def test_the_two_filenames_from_the_issue_no_longer_reduce_to_the_same_name() -> None:
    first = safe_upload_filename("写真2.png", b"one")
    second = safe_upload_filename("猫2.png", b"two")
    assert first != second
    assert first.startswith("2-") and first.endswith(".png")
    assert second.startswith("2-") and second.endswith(".png")
    assert is_plain_filename(first) and is_plain_filename(second)


def test_the_same_content_under_the_same_name_gets_the_same_filename() -> None:
    assert safe_upload_filename("写真2.png", b"one") == safe_upload_filename("写真2.png", b"one")


def test_a_fragment_stem_keeps_its_extension_and_reads_as_a_name() -> None:
    assert safe_upload_filename("café.JPG", b"x").startswith("caf-")
    assert safe_upload_filename("写真2.png", b"x").rsplit(".", 1)[1] == "png"


@pytest.mark.parametrize(
    "name, expected",
    [
        # Whole ASCII words are what the author typed: unchanged.
        ("My Photo (1).PNG", "My-Photo-1.PNG"),
        ("rack-photo_2.png", "rack-photo_2.png"),
        ("../../etc/passwd", "passwd"),
        # A whole non-ASCII word next to a whole ASCII word leaves the ASCII
        # word alone: nothing was cut in half.
        ("写真 a.png", "a.png"),
    ],
)
def test_a_name_that_lost_no_partial_word_is_left_alone(name: str, expected: str) -> None:
    assert safe_upload_filename(name, b"x") == expected


def test_a_name_with_no_content_to_hash_is_still_cleaned_not_hashed() -> None:
    # The route always passes the bytes; this is the no-argument default.
    assert safe_upload_filename("写真2.png") == "2.png"
    assert safe_upload_filename("写真.png") == "upload"


def test_two_such_images_attach_to_one_draft_without_a_conflict(store: Store) -> None:
    draft, _ = store.create_draft("scott")
    names = []
    for original, color in (("写真2.png", (1, 2, 3)), ("猫2.png", (4, 5, 6))):
        raw = png_bytes(color)
        record, _ = store.put_and_attach_image(
            draft.id, raw, safe_upload_filename(original, raw), "inline", "scott"
        )
        names.append(record.filename)
    assert len(set(names)) == 2
    assert [item.filename for item in store.get_draft(draft.id).images] == names
