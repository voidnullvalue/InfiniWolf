#!/usr/bin/env python3
"""Render all ten campaign floors into one self-contained SVG contact sheet.

Fresh ``--seed`` generation retains every overlay. A ``--pk3`` can derive the
critical route (as a shortest start/exit walk), encounters (enemy sprites), and
sound zones from its planes; authored sightlines require an InfiniWolf manifest.
Landmark coordinates are generation-only because package metadata records their
room indices but not room origins, so use ``--seed`` for that overlay.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infiniwolf.config import CampaignConfig, GenerationQuality
from infiniwolf.generator import generate_campaign
from infiniwolf.preview import (
    OVERLAY_KINDS,
    load_previews,
    preview_generated,
    write_contact_sheet,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--seed",
                        help="generate from an integer, 0x integer, or stable text seed")
    source.add_argument("--pk3", type=Path,
                        help="read maps and retained metadata from an existing package")
    parser.add_argument("--out", type=Path, default=Path("contact-sheet.svg"),
                        help="SVG destination (default: %(default)s)")
    parser.add_argument("--overlay", choices=OVERLAY_KINDS,
                        help="diagnostic layer to draw above every floor")
    parser.add_argument(
        "--generation-quality",
        choices=[quality.value for quality in GenerationQuality],
        default=GenerationQuality.THOROUGH.value,
        help="candidate pool policy for --seed (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    if args.pk3 is not None:
        previews = load_previews(args.pk3.expanduser())
        source_label = str(args.pk3)
        if args.overlay == "landmarks":
            print("warning: landmark coordinates require fresh --seed generation; "
                  "the package panels will have no landmark marks", file=sys.stderr)
    else:
        quality = GenerationQuality(args.generation_quality)
        config = CampaignConfig.with_seed(args.seed, generation_quality=quality)
        levels = []
        with tempfile.TemporaryDirectory(prefix="infiniwolf-contact-") as directory:
            generate_campaign(
                config, Path(directory) / "campaign.pk3",
                level_collector=levels,
            )
        previews = tuple(preview_generated(level) for level in levels)
        source_label = f"seed {config.seed} ({quality.value})"

    if len(previews) != 10:
        raise SystemExit(f"expected ten floor panels, got {len(previews)}")
    output = write_contact_sheet(args.out.expanduser(), previews, args.overlay)
    size = output.stat().st_size
    print(f"Wrote {output}")
    print(f"Source: {source_label}")
    print(f"Panels: {len(previews)}  Overlay: {args.overlay or 'none'}  Bytes: {size}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
