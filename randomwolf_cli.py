#!/usr/bin/env python3
"""PyInstaller entry point for the randomwolf-cli release executable."""
from randomwolf.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
