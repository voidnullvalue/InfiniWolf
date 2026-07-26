"""Single source of displayable InfiniWolf build identity."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

from . import __version__
from ._build import COMMIT as STAMPED_COMMIT


def _valid_commit(value: str) -> str:
    value = value.strip()
    return value.lower() if re.fullmatch(r"[0-9a-fA-F]{7,40}", value) else ""


def _source_commit() -> str:
    """Resolve a commit without making packaged applications depend on Git."""
    stamped = _valid_commit(STAMPED_COMMIT)
    if stamped:
        return stamped
    environment = _valid_commit(os.environ.get("INFINIWOLF_COMMIT", ""))
    if environment:
        return environment
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return _valid_commit(result.stdout)


COMMIT = _source_commit()


def build_label() -> str:
    """Return a compact identity suitable for CLI and window chrome."""
    commit = COMMIT[:7] if COMMIT else "unknown"
    return f"{__version__} ({commit})"
