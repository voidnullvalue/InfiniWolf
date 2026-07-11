"""Command-line interface for reproducible campaign generation."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from .config import CampaignConfig, Intensity
from .generator import generate_campaign


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="randomwolf", description="Generate a WL6 campaign for ECWolf.")
    result.add_argument("--seed", help="Integer, 0x-prefixed integer, or stable text seed; blank uses time")
    result.add_argument("--output", type=Path, default=Path.cwd() / "randomwolf.pk3")
    for name in ("guard-density", "enemy-toughness", "supplies", "treasure",
                 "secrets", "locked-doors", "layout-complexity"):
        result.add_argument(f"--{name}", type=int, choices=range(1, 6), default=3,
                            metavar="1..5")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config = CampaignConfig.with_seed(
        args.seed,
        guard_density=Intensity(args.guard_density),
        enemy_toughness=Intensity(args.enemy_toughness),
        supplies=Intensity(args.supplies),
        treasure=Intensity(args.treasure),
        secrets=Intensity(args.secrets),
        locked_doors=Intensity(args.locked_doors),
        layout_complexity=Intensity(args.layout_complexity),
    )
    output = generate_campaign(config, args.output)
    print(f"Generated {output}")
    print(f"Seed: {config.seed}")
    return 0

