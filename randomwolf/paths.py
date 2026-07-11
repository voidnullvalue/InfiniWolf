"""Runtime path validation without reading or packaging WL6 assets."""

from __future__ import annotations

from pathlib import Path
import os
import sys


REQUIRED_WL6_FILES = (
    "AUDIOHED.WL6", "AUDIOT.WL6", "GAMEMAPS.WL6", "MAPHEAD.WL6",
    "VGADICT.WL6", "VGAGRAPH.WL6", "VGAHEAD.WL6", "VSWAP.WL6",
)


def validate_ecwolf(path: Path) -> list[str]:
    if not path.is_file():
        return ["ECWolf executable does not exist"]
    if sys.platform != "win32" and not os.access(path, os.X_OK):
        return ["ECWolf file is not executable"]
    return []


def validate_wl6_data(path: Path) -> list[str]:
    if not path.is_dir():
        return ["WL6 data directory does not exist"]
    names = {item.name.upper() for item in path.iterdir() if item.is_file()}
    missing = [name for name in REQUIRED_WL6_FILES if name not in names]
    return [f"Missing registered data file: {name}" for name in missing]
