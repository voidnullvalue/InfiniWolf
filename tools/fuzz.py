#!/usr/bin/env python3
"""Generate many maps across setting extremes and report deterministic retries."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infiniwolf.config import CampaignConfig, Intensity
from infiniwolf.campaign import resolve_schedule
from infiniwolf.generator import generate_map


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
            # Mirror generate_campaign's own per-floor options. Hand-rolled kwargs
            # never passed the floor-9 boss or the aesthetic phase, so the fuzzer
            # was exercising a default path production no longer uses.
            schedule = resolve_schedule(config)
            for floor in range(1, 11):
                for attempt in range(args.attempts):
                    try:
                        generate_map(config, floor, attempt,
                                     **schedule.floor_options(floor))
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

