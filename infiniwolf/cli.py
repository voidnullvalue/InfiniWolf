"""Command-line interface for reproducible campaign generation."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from .build_info import build_label
from .config import CampaignConfig, Intensity, ThemeBias
from .generator import generate_campaign


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="infiniwolf",
        description=f"InfiniWolf {build_label()} — generate a WL6 campaign for ECWolf.",
    )
    result.add_argument("--version", action="version",
                        version=f"%(prog)s {build_label()}")
    result.add_argument("--seed", help="Integer, 0x-prefixed integer, or stable text seed; blank uses time")
    result.add_argument("--output", type=Path, default=Path.cwd() / "infiniwolf.pk3")
    for name in ("guard-density", "enemy-toughness", "supplies", "treasure",
                 "secrets", "locked-doors", "layout-complexity", "decoration-amount",
                 "room-shape-variation", "patrol-activity", "atmosphere",
                 "secret-reward-quality"):
        result.add_argument(f"--{name}", type=int, choices=range(1, 6), default=3,
                            metavar="1..5")
    result.add_argument("--theme-bias", choices=[bias.value for bias in ThemeBias],
                        default=ThemeBias.MIXED.value)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    print(f"InfiniWolf {build_label()}")
    config = CampaignConfig.with_seed(
        args.seed,
        guard_density=Intensity(args.guard_density),
        enemy_toughness=Intensity(args.enemy_toughness),
        supplies=Intensity(args.supplies),
        treasure=Intensity(args.treasure),
        secrets=Intensity(args.secrets),
        locked_doors=Intensity(args.locked_doors),
        layout_complexity=Intensity(args.layout_complexity),
        decoration_amount=Intensity(args.decoration_amount),
        room_shape_variation=Intensity(args.room_shape_variation),
        patrol_activity=Intensity(args.patrol_activity),
        atmosphere=Intensity(args.atmosphere),
        secret_reward_quality=Intensity(args.secret_reward_quality),
        theme_bias=ThemeBias(args.theme_bias),
    )
    output = generate_campaign(config, args.output)
    print(f"Generated {output}")
    print(f"Seed: {config.seed}")
    return 0
