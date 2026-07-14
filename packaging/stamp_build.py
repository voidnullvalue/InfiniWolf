#!/usr/bin/env python3
"""Stamp a Git commit into the package immediately before freezing it."""

from __future__ import annotations

import argparse
from pathlib import Path
import re


def stamp(commit: str, output: Path) -> None:
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit):
        raise ValueError("commit must be a 7-40 character hexadecimal Git object ID")
    output.write_text(
        '"""Build-time values stamped into packaged executables by CI."""\n\n'
        f'COMMIT = "{commit.lower()}"\n',
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path,
                        default=Path("infiniwolf/_build.py"))
    args = parser.parse_args()
    stamp(args.commit, args.output)


if __name__ == "__main__":
    main()
