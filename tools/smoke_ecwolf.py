#!/usr/bin/env python3
"""Generate a package and ask a local ECWolf installation to load floor one."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from randomwolf import CampaignConfig, generate_campaign
from randomwolf.paths import validate_ecwolf, validate_wl6_data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ecwolf", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--seed", default="engine-smoke")
    parser.add_argument("--timeout", type=float, default=5)
    args = parser.parse_args()
    errors = validate_ecwolf(args.ecwolf) + validate_wl6_data(args.data)
    if errors:
        parser.error("; ".join(errors))
    with tempfile.TemporaryDirectory(prefix="randomwolf-smoke-") as directory:
        root = Path(directory)
        package = generate_campaign(CampaignConfig.with_seed(args.seed), root / "randomwolf.pk3")
        environment = os.environ.copy()
        environment.setdefault("SDL_VIDEODRIVER", "dummy")
        environment.setdefault("SDL_AUDIODRIVER", "dummy")
        command = [str(args.ecwolf), "--data", "wl6", "--file", str(package),
                   "--tedlevel", "0", "--normal", "--nowait", "--config", str(root / "ecwolf.cfg"),
                   "--savedir", str(root / "saves")]
        try:
            completed = subprocess.run(command, cwd=args.data, env=environment, timeout=args.timeout,
                                       capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            print("ECWolf remained running through the smoke window; package load succeeded.")
            return 0
        if completed.returncode:
            sys.stderr.write(completed.stdout + completed.stderr)
            return completed.returncode
        print("ECWolf loaded the generated campaign successfully.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

