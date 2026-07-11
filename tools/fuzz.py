#!/usr/bin/env python3
"""Generate many maps across setting extremes and report deterministic retries."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from randomwolf.config import CampaignConfig, Intensity
from randomwolf.generator import generate_map


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, default=1000)
    parser.add_argument("--attempts", type=int, default=50)
    args = parser.parse_args()
    maps = retries = 0
    for seed in range(args.seeds):
        for intensity in (Intensity.VERY_LOW, Intensity.NORMAL, Intensity.VERY_HIGH):
            config = CampaignConfig(
                seed=seed, guard_density=intensity, enemy_toughness=intensity,
                supplies=intensity, treasure=intensity, secrets=intensity,
                locked_doors=intensity, layout_complexity=intensity,
            )
            for floor in range(1, 11):
                for attempt in range(args.attempts):
                    try:
                        generate_map(config, floor, attempt, secret_exit=floor == 4)
                        maps += 1; retries += attempt
                        break
                    except ValueError:
                        continue
                else:
                    raise RuntimeError(f"failed seed={seed} intensity={intensity} floor={floor}")
    print(f"Validated {maps} maps across {args.seeds} seeds; deterministic retries: {retries}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

