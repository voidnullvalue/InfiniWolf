#!/usr/bin/env python3
"""Byte-identity gate for behaviour-preserving refactors.

A refactor that moves code between modules is only correct if it produces the
same maps. That is a cheap thing to check and an expensive thing to debug, so
check it after *every* move rather than once per stage: a hash mismatch found
after fifteen moves means searching thousands of relocated lines, while one
found after a single move means `git revert`.

This is deliberately not the test suite. The suite takes ~50 minutes and mostly
covers code a given move does not touch; this takes ~2 minutes and proves the
one property the extraction stages promise.

    tools/fingerprint.py --record     # write tests/fingerprints.json
    tools/fingerprint.py --check      # compare against it (exit 1 on drift)
    tools/fingerprint.py --check -v   # name every combination as it runs

Hashes cover the tile plane, the thing plane, the packed WAD bytes, and a
canonical projection of the metadata that feeds the manifest -- so a change that
preserves geometry but perturbs, say, encounter provenance still trips.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infiniwolf.campaign import resolve_schedule  # noqa: E402
from infiniwolf.config import CampaignConfig, Intensity  # noqa: E402
from infiniwolf.generator import _wad_bytes, generate_map  # noqa: E402

STORE = ROOT / "tests" / "fingerprints.json"

# Floors chosen for structural distinctness rather than coverage breadth:
# 1 starts inside the castle with no arrival car, 5 is an ordinary mid-campaign
# floor, 9 carries a boss arena, 10 is the secret reward expedition.
FLOORS = (1, 5, 9, 10)

# Seeds are fixed strings so the corpus is identical on every machine. The
# extremes matter: layout_complexity and decoration_amount at 1 and 5 exercise
# the smallest and largest room programs, where off-by-one geometry lives.
CASES: tuple[tuple[str, dict], ...] = (
    ("plain", {}),
    ("aardwolf", {"say_aardwolf": True}),
    ("sparse", {"layout_complexity": Intensity.VERY_LOW,
                "decoration_amount": Intensity.VERY_LOW,
                "guard_density": Intensity.VERY_LOW}),
    ("dense", {"layout_complexity": Intensity.VERY_HIGH,
               "decoration_amount": Intensity.VERY_HIGH,
               "guard_density": Intensity.VERY_HIGH}),
)
SEEDS = ("castle", "42")


def _metadata(level) -> str:
    """Canonical projection of the decisions the manifest records.

    Sorted keys and plain types only: this must be stable across Python
    versions and must not depend on dataclass repr, which is free to change.
    """
    return json.dumps({
        "number": level.number,
        "seed": level.seed,
        "variant": level.variant,
        "skeleton": level.circulation_skeleton,
        "grammar": level.progression_grammar,
        "roles": list(level.room_roles),
        "tiers": list(level.room_tiers),
        "concepts": list(level.room_concepts),
        "shapes": list(level.room_shapes),
        "lighting": list(level.lighting_families),
        "rooms": [[r.x, r.y, r.w, r.h] for r in level.rooms],
        "edges": [list(e) for e in level.edges],
        "start": list(level.start),
        "exit_stand": list(level.exit_stand) if level.exit_stand else None,
        "key_order": list(level.key_order),
        "locked_doors": level.locked_doors,
        "boss": level.boss,
        "special_family": level.special_family,
        "critical_route": list(level.critical_route),
        "encounters": [[e.template, e.room_index, e.family,
                        [i for _, _, i in e.cells]] for e in level.encounters],
        "pickups": [[p.reason, p.template, p.room_index,
                     [i for _, _, i in p.cells]] for p in level.pickup_placements],
        "secrets": [[d.shape, d.reward_count, d.host_room] for d in level.secret_details],
        "landmarks": [[p.room_index, p.rank, p.purpose, p.approach_room]
                      for p in level.landmarks],
        "critique": list(level.critique),
    }, sort_keys=True, separators=(",", ":"))


def _digest(level) -> str:
    sponge = hashlib.sha256()
    sponge.update(struct.pack(f"<{len(level.tiles)}H", *level.tiles))
    sponge.update(struct.pack(f"<{len(level.things)}H", *level.things))
    sponge.update(_wad_bytes(f"IW{level.number:02d}", level.tiles, level.things))
    sponge.update(_metadata(level).encode("utf-8"))
    return sponge.hexdigest()[:32]


def _generate(config: CampaignConfig, floor: int):
    """Mirror generate_campaign's retry loop.

    A single (seed, floor, attempt) may legitimately fail validate_map, and
    production retries. Hashing attempt 0 unconditionally would make every
    such combination collapse to the same "failed" marker and silently stop
    testing anything -- the exact bug the throwaway version of this tool had.
    """
    # Use the campaign's own per-floor options rather than hand-rolled kwargs, so
    # the corpus exercises exactly what generate_campaign does. Hand-rolling them
    # once meant the floor-9 boss was never passed, and a change to boss selection
    # showed up as "identical" because the corpus silently took the default path.
    options = resolve_schedule(config).floor_options(floor)
    last = None
    for attempt in range(50):
        try:
            return generate_map(config, floor, attempt, **options)
        except ValueError as error:
            last = error
    raise RuntimeError(f"floor {floor} never validated in 50 attempts: {last}")


def corpus():
    for seed in SEEDS:
        for name, settings in CASES:
            config = CampaignConfig.with_seed(seed, **settings)
            for floor in FLOORS:
                yield f"{seed}/{name}/{floor}", config, floor


def collect(verbose: bool = False) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, config, floor in corpus():
        started = time.monotonic()
        result[key] = _digest(_generate(config, floor))
        if verbose:
            print(f"  {key:<28} {result[key]}  {time.monotonic()-started:.1f}s")
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--record", action="store_true",
                      help="write the fingerprint store from current code")
    mode.add_argument("--check", action="store_true",
                      help="compare current code against the store")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    started = time.monotonic()
    current = collect(args.verbose)
    elapsed = time.monotonic() - started

    if args.record:
        from infiniwolf import __version__
        STORE.parent.mkdir(parents=True, exist_ok=True)
        STORE.write_text(json.dumps(
            {"version": __version__, "combinations": len(current),
             "fingerprints": current}, indent=2, sort_keys=True) + "\n")
        print(f"recorded {len(current)} fingerprints for {__version__} "
              f"in {elapsed:.0f}s -> {STORE.relative_to(ROOT)}")
        return 0

    if not STORE.exists():
        print(f"no store at {STORE.relative_to(ROOT)}; run --record first")
        return 1
    stored = json.loads(STORE.read_text())
    baseline = stored["fingerprints"]
    drift = [key for key in sorted(set(baseline) | set(current))
             if baseline.get(key) != current.get(key)]
    if drift:
        print(f"DRIFT in {len(drift)} of {len(current)} combinations "
              f"(baseline recorded for {stored.get('version')}):")
        for key in drift:
            print(f"  {key:<28} {baseline.get(key, '(absent)')} -> "
                  f"{current.get(key, '(absent)')}")
        return 1
    print(f"identical: {len(current)} combinations in {elapsed:.0f}s "
          f"(baseline {stored.get('version')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
