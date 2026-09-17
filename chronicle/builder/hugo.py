"""The Hugo binary: what version it is, and one build of a scratch tree.

Flags mirror the blog's own Pages workflow (`.github/workflows/hugo.yml` in
the blog repo) so a preview is the same build the public site gets:
`--minify`, `HUGO_ENVIRONMENT=production` and `HUGO_ENV=production`. Three
flags are Chronicle's own and not the workflow's:

- `--baseURL` points at the preview path for this slug, which is what makes
  every generated link resolve under `/preview/<slug>/` (spec section 8).
- `--gc` cleans the resource cache during the build, which matters here and
  not in CI because Chronicle's cache directory is long-lived.
- `--cacheDir` and `HUGO_RESOURCEDIR` put Hugo's caches beside the scratch
  tree instead of inside it. Inside would mean Hugo writing into a directory
  copied from `data/site/`, and the copy is made with hardlinks.

The second reason for an external resource directory is speed: the blog has
396 processed images, and reprocessing them from scratch is the difference
between a 23 second build and a 2 second one.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# "hugo v0.164.0-ce2470e+extended linux/amd64 BuildDate=..." -> "0.164.0"
_VERSION = re.compile(r"hugo v(\d+\.\d+\.\d+)")

UNKNOWN_VERSION = "unknown"


@dataclass(frozen=True)
class BuildResult:
    returncode: int
    output: str
    timed_out: bool


def version_line(hugo_bin: str) -> str:
    try:
        result = subprocess.run(
            [hugo_bin, "version"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"hugo version unavailable: {exc}"
    text = (result.stdout or result.stderr).strip()
    return text.splitlines()[0] if text else ""


def installed_version(hugo_bin: str) -> str:
    match = _VERSION.search(version_line(hugo_bin))
    return match.group(1) if match else UNKNOWN_VERSION


def build(
    hugo_bin: str,
    source: Path,
    destination: Path,
    base_url: str,
    cache_dir: Path,
    resource_dir: Path,
    timeout_seconds: float,
) -> BuildResult:
    """One full build. Output is captured, never streamed to the builder's log."""
    command = [
        hugo_bin,
        "--source",
        str(source),
        "--destination",
        str(destination),
        "--baseURL",
        base_url,
        "--cacheDir",
        str(cache_dir),
        "--minify",
        "--gc",
    ]
    environment = {
        **os.environ,
        "HUGO_ENVIRONMENT": "production",
        "HUGO_ENV": "production",
        "HUGO_RESOURCEDIR": str(resource_dir),
        # Hugo reads no global git config, but it does shell out to git for
        # `--enableGitInfo`, which nothing here enables; pinning the two
        # config paths keeps the builder container from inheriting a host
        # config the same way `gitrepo.py` does for the api.
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        captured = _decode(exc.stdout) + _decode(exc.stderr)
        return BuildResult(
            returncode=124,
            output=f"$ {' '.join(command)}\n{captured}\nhugo timed out after"
            f" {timeout_seconds:.0f}s\n",
            timed_out=True,
        )
    except OSError as exc:
        return BuildResult(
            returncode=127, output=f"could not run {hugo_bin}: {exc}\n", timed_out=False
        )
    return BuildResult(
        returncode=completed.returncode,
        output=f"$ {' '.join(command)}\n{completed.stdout}{completed.stderr}",
        timed_out=False,
    )


def _decode(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
