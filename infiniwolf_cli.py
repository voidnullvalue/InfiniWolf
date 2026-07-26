#!/usr/bin/env python3
"""PyInstaller entry point for the infiniwolf-cli release executable."""
from infiniwolf.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
