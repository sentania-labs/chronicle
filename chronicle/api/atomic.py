"""Whole-file writes that a reader, a backup, or a crash cannot catch halfway.

Every JSON record in the data directory goes through here: written to a temp
file beside the target and renamed onto it, so the file on disk is always
either the previous record or the new one, never a partial line of either.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

RECORD_MODE = 0o644


def write_atomic(path: Path, text: str, mode: int = RECORD_MODE) -> None:
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        # A temp file is 0600 by default, so the mode is set explicitly rather
        # than inherited from tempfile's choice.
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
