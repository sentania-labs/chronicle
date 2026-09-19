"""Run the node-only tests (`tests/*.test.mjs`) as part of `make check`.

The JavaScript under `chronicle/api/static/` is original code with a
DOM-free core that `node --test` can exercise. Where node is not installed
the test is skipped, not passed silently: the reason says so.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

TESTS = Path(__file__).parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_node_tests_pass() -> None:
    files = sorted(str(p) for p in TESTS.glob("*.test.mjs"))
    assert files
    result = subprocess.run(
        ["node", "--test", *files], capture_output=True, text=True, check=False, timeout=120
    )
    assert result.returncode == 0, result.stdout + result.stderr
