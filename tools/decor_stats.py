#!/usr/bin/env python3
"""Measure decoration density and composition, hand-authored corpus vs generated.

Turns "does it look authored?" into a number. Reports the same rows for both
corpora so a generated floor can be held against the hand-authored band
directly:

  decor/map              total static decorations placed
  blocking/map           the +SOLID subset (furniture the player collides with)
  decor per floor cell   density, normalised for map size
  blocking wall-adj      share of solid props with a wall orthogonally behind
                         them -- free-standing furniture is the strongest
                         "procedural" tell
  clustering             share of decorations that have another decoration as
                         an orthogonal neighbour
  distinct item types    vocabulary breadth
  lit maps / rooms       share carrying any ceiling/lamp fixture

Method note: floor cells are `tile >= 106` (Wolf3D area codes; InfiniWolf's
FLOOR is 108). Inferring a single floor code per map instead badly inflates
wall-adjacency for both corpora.

Usage:
  tools/decor_stats.py --corpus                       # hand-authored reference
  tools/decor_stats.py --generated --seeds 8          # freshly generated
  tools/decor_stats.py --corpus --generated           # side by side (the gate)
"""
from __future__ import annotations

import argparse
from collections import Counter
import glob
import json
import statistics
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from infiniwolf import wl6 as T
from infiniwolf.config import CampaignConfig
from inspect_map import parse_wad

GRID = T.GRID
# Wolf3D area codes start at 106; every value at or above it is walkable floor
# (107 is the secret-exit zone, 108-143 the sound zones).
FLOORMIN = 106
DIRS4 = ((1, 0), (-1, 0), (0, 1), (0, -1))
BLOCKING = frozenset(T.STATIC_BLOCKING)
OPEN = frozenset(T.STATIC_OPEN)
DECOR = BLOCKING | OPEN
# Non-solid fixtures plus the solid floor lamp: "is this room lit at all".
FIXTURES = frozenset(T.LIGHTING_ITEMS)
DEFAULT_CORPUS = str(ROOT.parent / "installed" / "*" / "*.pk3")


def at(plane: list[int], x: int, y: int) -> int:
    return plane[y * GRID + x] if 0 <= x < GRID and 0 <= y < GRID else -1


def is_floor(value: int) -> bool:
    return value >= FLOORMIN


def measure(tiles: list[int], things: list[int]) -> dict:
    """One map's decoration profile."""
    cells = [(i % GRID, i // GRID) for i, v in enumerate(things) if v in DECOR]
    occupied = {c for c in cells}
    floor_cells = sum(1 for v in tiles if is_floor(v))

    blocking_cells = [c for c in cells if at(things, *c) in BLOCKING]
    wall_backed = 0
    for x, y in blocking_cells:
        for dx, dy in DIRS4:
            neighbour = at(tiles, x + dx, y + dy)
            # -1 is off-map, which reads as backed by the map edge.
            if neighbour == -1 or not is_floor(neighbour):
                wall_backed += 1
                break

    clustered = sum(1 for x, y in cells
                    if any((x + dx, y + dy) in occupied for dx, dy in DIRS4))

    return {
        "decor": len(cells),
        "blocking": len(blocking_cells),
        "floor_cells": floor_cells,
        "density": len(cells) / floor_cells if floor_cells else 0.0,
        "wall_adj": wall_backed / len(blocking_cells) if blocking_cells else None,
        "clustered": clustered / len(cells) if cells else None,
        "types": len({at(things, *c) for c in cells}),
        "fixtures": sum(1 for c in cells if at(things, *c) in FIXTURES),
        "items": [at(things, *c) for c in cells],
        "pillars": sum(at(things, *c) == 30 for c in cells),
        "pillar_density": sum(at(things, *c) == 30 for c in cells) / floor_cells if floor_cells else 0.0,
    }


def load_corpus(pattern: str) -> list[tuple[str, dict]]:
    out = []
    for path in sorted(glob.glob(pattern)):
        if "infiniwolf" in path:
            continue  # never measure our own output as if it were authored
        mod = Path(path).parent.name
        with zipfile.ZipFile(path) as package:
            for name in package.namelist():
                if not name.lower().endswith(".wad"):
                    continue
                parsed = parse_wad(package.read(name))
                if parsed is None:
                    continue
                # Match the miner's floor: a map with almost no decoration is
                # not evidence about decoration style.
                if sum(1 for v in parsed[1] if v in DECOR) < 5:
                    continue
                out.append((f"{mod}/{name}", measure(*parsed)))
    return out


def room_coverage(result) -> tuple[int, int, int, int]:
    """(rooms, rooms with a light fixture, rooms with any decor, decor in rooms).

    Per-map fixture counts hide the actual complaint: a floor can carry 15
    ceiling lights and still have most of its rooms pitch dark, because whole
    concepts have no lighting entry at all. Only a per-room tally shows that.
    """
    lit = furnished = decor_in_rooms = 0
    for room in result.rooms:
        fixtures = decor = 0
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                thing = at(result.things, x, y)
                if thing in FIXTURES:
                    fixtures += 1
                if thing in DECOR:
                    decor += 1
        lit += fixtures > 0
        furnished += decor > 0
        decor_in_rooms += decor
    return len(result.rooms), lit, furnished, decor_in_rooms


def load_generated(seeds: int, floors: tuple[int, ...]) -> list[tuple[str, dict]]:
    from infiniwolf import generator as G

    out = []
    for index in range(seeds):
        config = CampaignConfig(seed=str(index))
        for floor in floors:
            for attempt in range(50):
                try:
                    result = G.generate_map(config, floor, attempt)
                except ValueError:
                    continue
                stats = measure(result.tiles, result.things)
                rooms, lit, furnished, _ = room_coverage(result)
                stats["rooms"] = rooms
                stats["rooms_lit"] = lit
                stats["rooms_furnished"] = furnished
                stats["pillar_rooms"] = [sum(at(result.things, x, y) == 30
                    for y in range(room.y, room.y + room.h)
                    for x in range(room.x, room.x + room.w)) for room in result.rooms]
                stats["pillar_sources"] = dict(Counter(
                    placement.source for placement in result.pillar_placements
                    for _ in placement.cells))
                out.append((f"seed{index}/floor{floor}", stats))
                break
    return out


def summarise(rows: list[tuple[str, dict]]) -> dict:
    def mean(key):
        vals = [r[key] for _, r in rows if r[key] is not None]
        return statistics.fmean(vals) if vals else float("nan")

    items = [code for _, r in rows for code in r["items"]]
    freq = {}
    for code in items:
        freq[code] = freq.get(code, 0) + 1
    # Room-level coverage is only knowable for generated floors (the authored
    # corpus has no room list), so it stays absent rather than faked.
    total_rooms = sum(r.get("rooms", 0) for _, r in rows)
    pillar_counts = [r["pillars"] for _, r in rows]
    pillar_rooms = [count for _, r in rows for count in r.get("pillar_rooms", ())]
    pillar_sources = Counter(source for _, r in rows
                             for source, count in r.get("pillar_sources", {}).items()
                             for _ in range(count))
    return {
        "maps": len(rows),
        "rooms_lit": (sum(r.get("rooms_lit", 0) for _, r in rows) / total_rooms
                      if total_rooms else None),
        "rooms_furnished": (sum(r.get("rooms_furnished", 0) for _, r in rows) / total_rooms
                            if total_rooms else None),
        "decor_per_map": mean("decor"),
        "blocking_per_map": mean("blocking"),
        "density": mean("density"),
        "pillars_per_map": mean("pillars"),
        "pillar_density": mean("pillar_density"),
        "pillar_median": statistics.median(pillar_counts) if pillar_counts else 0.0,
        "pillar_max": max(pillar_counts, default=0),
        "pillar_p95": (sorted(pillar_counts)[max(0, round(0.95 * (len(pillar_counts) - 1)))] if pillar_counts else 0),
        "pillars_per_room": statistics.fmean(pillar_rooms) if pillar_rooms else None,
        "pillar_sources": dict(pillar_sources),
        "wall_adj": mean("wall_adj"),
        "clustered": mean("clustered"),
        "types_per_map": mean("types"),
        "fixtures_per_map": mean("fixtures"),
        "unlit_maps": sum(1 for _, r in rows if r["fixtures"] == 0) / len(rows) if rows else 0,
        "top_items": sorted(freq.items(), key=lambda kv: -kv[1])[:16],
        "per_map_freq": {code: n / len(rows) for code, n in freq.items()} if rows else {},
    }


ROW_SPECS = (
    ("decorations / map", "decor_per_map", "{:.1f}"),
    ("blocking / map", "blocking_per_map", "{:.1f}"),
    ("decor per floor cell", "density", "{:.3f}"),
    ("WhitePillar / map", "pillars_per_map", "{:.1f}"),
    ("WhitePillar / floor cell", "pillar_density", "{:.4f}"),
    ("WhitePillar median", "pillar_median", "{:.1f}"),
    ("blocking wall-adjacency", "wall_adj", "{:.1%}"),
    ("clustering", "clustered", "{:.1%}"),
    ("distinct item types", "types_per_map", "{:.1f}"),
    ("light fixtures / map", "fixtures_per_map", "{:.1f}"),
    ("maps with no fixture", "unlit_maps", "{:.1%}"),
    ("rooms with a fixture", "rooms_lit", "{:.1%}"),
    ("rooms with any decor", "rooms_furnished", "{:.1%}"),
)


def print_table(columns: list[tuple[str, dict]]) -> None:
    label_w = max(len(label) for label, _, _ in ROW_SPECS) + 2
    head = "".join(f"{name:>18}" for name, _ in columns)
    print(f"{'metric':<{label_w}}{head}")
    print("-" * (label_w + 18 * len(columns)))
    print(f"{'maps measured':<{label_w}}" + "".join(f"{s['maps']:>18}" for _, s in columns))
    for label, key, fmt in ROW_SPECS:
        cells = "".join(f"{fmt.format(s[key]) if s.get(key) is not None else '-':>18}"
                        for _, s in columns)
        print(f"{label:<{label_w}}{cells}")

    print("\nper-item frequency (instances per map)")
    codes = sorted({c for _, s in columns for c, _ in s["top_items"]},
                   key=lambda c: -max(s["per_map_freq"].get(c, 0) for _, s in columns))
    name_w = 16
    print(f"{'item':<{name_w}}" + "".join(f"{n:>18}" for n, _ in columns))
    for code in codes[:20]:
        label = f"{ITEM_NAMES.get(code, code)}({code})"
        cells = "".join(f"{s['per_map_freq'].get(code, 0):>18.1f}" for _, s in columns)
        print(f"{label:<{name_w}}{cells}")


ITEM_NAMES = {
    23: "Puddle", 24: "GreenBarrel", 25: "TableChairs", 26: "FloorLamp", 27: "Chandelier",
    28: "HangedMan", 30: "WhitePillar", 31: "GreenPlant", 32: "SkeletonFlat", 33: "Sink",
    34: "BrownPlant", 35: "Vase", 36: "BareTable", 37: "CeilingLight", 38: "KitchenStuff",
    39: "SuitOfArmor", 40: "HangingCage", 41: "SkeletonCage", 42: "Bones1", 45: "BunkBed",
    46: "Basket", 57: "Gibs", 58: "Barrel", 59: "Well", 60: "EmptyWell", 61: "Blood",
    62: "Flag", 64: "Bones2", 65: "Bones3", 66: "Bones4", 67: "Pots", 68: "Stove",
    69: "Spears", 70: "Vines",
}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--corpus", action="store_true", help="measure the hand-authored corpus")
    ap.add_argument("--corpus-glob", default=DEFAULT_CORPUS,
                    help="where the authored .pk3 files live (default: %(default)s)")
    ap.add_argument("--generated", action="store_true", help="measure freshly generated floors")
    ap.add_argument("--seeds", type=int, default=4, help="generated seeds (default: %(default)s)")
    ap.add_argument("--floors", default="1,2,4,6,8,10",
                    help="generated floors (default: %(default)s)")
    ap.add_argument("--json", action="store_true", help="emit raw numbers instead of a table")
    args = ap.parse_args(argv)

    if not args.corpus and not args.generated:
        args.corpus = args.generated = True

    columns = []
    if args.corpus:
        rows = load_corpus(args.corpus_glob)
        if not rows:
            raise SystemExit(f"no authored maps matched {args.corpus_glob!r}")
        columns.append(("hand-authored", summarise(rows)))
    if args.generated:
        floors = tuple(int(f) for f in args.floors.split(","))
        rows = load_generated(args.seeds, floors)
        if not rows:
            raise SystemExit("no floors generated")
        columns.append(("InfiniWolf", summarise(rows)))

    if args.json:
        print(json.dumps({name: {k: v for k, v in s.items() if k != "per_map_freq"}
                          for name, s in columns}, indent=2, default=str))
    else:
        print_table(columns)


if __name__ == "__main__":
    main()
