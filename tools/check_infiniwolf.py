#!/usr/bin/env python3
"""Standalone CLI/GUI launcher for InfiniWolf map-plane provenance checks."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infiniwolf.watermark import main


if __name__ == "__main__":
    raise SystemExit(main())
