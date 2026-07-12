#!/usr/bin/env python3
"""Debug/inspection CLI for infiniwolf maps.

Renders an ASCII view of a single map plus a metrics block covering
door-graph topology, room shape, corridor pacing, and enemy placement, so a
generated floor can be eyeballed or compared numerically against the real
Wolf3D map corpus (WDC3.1 PWADs, planes 0/1) with --compare.

Modes (pick exactly one):
  --seed SEED --floor N [--complexity 1-5]   generate a floor and inspect it
  --wad path/to/map.wad                      inspect one corpus WAD
  --pk3 path/to/pack.pk3 [--floor N]         inspect maps/iwNN.wad from a pk3
  --compare DIR                              inspect every *.wad under DIR
                                              and print a summary table
"""
from __future__ import annotations

import argparse
import json
import statistics
import struct
import sys
from collections import deque
from pathlib import Path
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infiniwolf import generator as G
from infiniwolf.config import CampaignConfig, Intensity

# Native Wolf3D patrol variants (guard/officer/ss/dog stand codes + 4), each
# repeated for the three skill tiers (+36 per tier) the translator supports.
PATROL_CODES = frozenset(
    code + 36 * tier
    for family in (G.PATROL_GUARDS, G.PATROL_OFFICERS, G.PATROL_SS, G.PATROL_DOGS)
    for code in family for tier in range(3)
)
TREASURE_LO, TREASURE_HI = 43, 56  # gold key through the native pickup block
DIRS4 = ((1, 0), (-1, 0), (0, 1), (0, -1))

# --------------------------------------------------------------------------- rendering

def cell_char(tile: int, thing: int) -> str:
    """One ASCII glyph for a tile/thing pair. Things win over base tiles."""
    if thing == G.PUSHWALL:
        return "P"
    if G.PLAYER_START <= thing <= G.PLAYER_START + 3:
        return "S"
    if thing == G.GOLD_KEY:
        return "k"
    if thing in G.ENEMY_CODES:
        return "!"
    if TREASURE_LO <= thing <= TREASURE_HI:
        return "*"
    if thing in G.STATIC_BLOCKING:
        return "o"
    if tile in G.DOORS:
        return "+"
    if tile == G.ELEVATOR_TILE:
        return "E"
    if G._is_floor(tile):
        return "."
    return "#"

def render(tiles: list[int], things: list[int]) -> list[str]:
    """ASCII rows, trimmed of all-solid border rows/columns."""
    w = h = G.GRID
    interesting = [
        (x, y) for y in range(h) for x in range(w)
        if G._is_floor(G._at(tiles, x, y)) or G._at(tiles, x, y) in G.DOORS
        or G._at(tiles, x, y) == G.ELEVATOR_TILE or things[y * w + x]
    ]
    if not interesting:
        return []
    xs = [p[0] for p in interesting]
    ys = [p[1] for p in interesting]
    x0, x1 = max(0, min(xs) - 1), min(w - 1, max(xs) + 1)
    y0, y1 = max(0, min(ys) - 1), min(h - 1, max(ys) + 1)
    return ["".join(cell_char(G._at(tiles, x, y), things[y * w + x]) for x in range(x0, x1 + 1))
            for y in range(y0, y1 + 1)]

# --------------------------------------------------------------------------- WAD/pk3 IO

def parse_wad(data: bytes) -> tuple[list[int], list[int]] | None:
    """Parse a WDC3.1 PWAD, returning (tiles, things) or None if unreadable."""
    if len(data) < 12 or data[:4] != b"PWAD":
        return None
    size = struct.unpack_from("<I", data, 8)[0]
    body = data[12:12 + size]
    if body[:6] != b"WDC3.1":
        return None
    _, numplanes, namelen = struct.unpack_from("<IHH", body, 6)
    off = 14 + namelen
    w, h = struct.unpack_from("<HH", body, off)
    off += 4
    if w * h != G.GRID * G.GRID or numplanes < 2:
        return None
    planes = []
    for _ in range(numplanes):
        planes.append(list(struct.unpack_from(f"<{w * h}H", body, off)))
        off += w * h * 2
    return planes[0], planes[1]

def load_wad_file(path: Path) -> tuple[list[int], list[int]]:
    parsed = parse_wad(path.read_bytes())
    if parsed is None:
        raise ValueError(f"{path}: not a readable WDC3.1 PWAD")
    return parsed

def load_pk3(path: Path, floor: int) -> tuple[list[int], list[int]]:
    name = f"maps/iw{floor:02d}.wad"
    with zipfile.ZipFile(path) as package:
        if name not in package.namelist():
            raise ValueError(f"{path}: no {name} inside this package")
        parsed = parse_wad(package.read(name))
    if parsed is None:
        raise ValueError(f"{path}: {name} is not a readable WDC3.1 PWAD")
    return parsed

# --------------------------------------------------------------------------- detection

def find_start(things: list[int]) -> tuple[int, int] | None:
    for index, thing in enumerate(things):
        if G.PLAYER_START <= thing <= G.PLAYER_START + 3:
            return index % G.GRID, index // G.GRID
    return None

def find_exit_stand(tiles: list[int]) -> tuple[int, int] | None:
    """Locate a floor cell standing on the east/west axis of an elevator
    switch (tile 21), the same usability check validate_map performs."""
    for y in range(G.GRID):
        for x in range(G.GRID):
            if not G._is_floor(G._at(tiles, x, y)):
                continue
            for dx in (1, -1):
                if (G._at(tiles, x + dx, y) == G.ELEVATOR_TILE
                        and G._is_floor(G._at(tiles, x - dx, y))):
                    return x, y
    return None

# --------------------------------------------------------------------------- metrics

def bfs_distances(tiles: list[int], start: tuple[int, int] | None) -> dict[tuple[int, int], int]:
    if start is None:
        return {}
    dist = {start: 0}
    queue = deque([start])
    while queue:
        x, y = queue.popleft()
        for dx, dy in DIRS4:
            nxt = x + dx, y + dy
            if nxt in dist:
                continue
            tile = G._at(tiles, *nxt)
            if G._is_floor(tile) or tile in G.DOORS:
                dist[nxt] = dist[(x, y)] + 1
                queue.append(nxt)
    return dist

def door_graph(tiles: list[int]) -> dict[str, float]:
    """Rooms-as-components/doors-as-edges graph metrics, adapted from the
    scratch topology script onto the generator's own floor/door primitives."""
    components = G._floor_components(tiles)
    comp_of = {cell: i for i, comp in enumerate(components) for cell in comp}
    edges: set[tuple[int, int]] = set()
    for y in range(G.GRID):
        for x in range(G.GRID):
            if G._at(tiles, x, y) not in G.DOORS:
                continue
            sides = {comp_of[(x + dx, y + dy)] for dx, dy in DIRS4
                     if (x + dx, y + dy) in comp_of}
            for a in sides:
                for b in sides:
                    if a < b:
                        edges.add((a, b))
    adjacency: dict[int, set[int]] = {i: set() for i in range(len(components))}
    for a, b in edges:
        adjacency[a].add(b); adjacency[b].add(a)
    seen: set[int] = set()
    graph_comps = 0
    for i in range(len(components)):
        if i in seen:
            continue
        graph_comps += 1
        queue = deque([i]); seen.add(i)
        while queue:
            node = queue.popleft()
            for nxt in adjacency[node]:
                if nxt not in seen:
                    seen.add(nxt); queue.append(nxt)
    big = [i for i, comp in enumerate(components) if len(comp) >= 12]
    degrees = [len(adjacency[i]) for i in big]
    floor_tiles = sum(1 for tile in tiles if G._is_floor(tile))
    biggest = max((len(c) for c in components), default=0)
    perfect, pillars = _room_shape(components)
    return {
        "floor_tiles": floor_tiles,
        "door_tiles": sum(1 for tile in tiles if tile in G.DOORS),
        "rooms": len(big),
        "graph_edges": len(edges),
        "graph_cycles": len(edges) - len(components) + graph_comps,
        "dead_end_ratio": _safe_div(sum(1 for d in degrees if d == 1), len(degrees)),
        "mean_degree": _safe_div(sum(degrees), len(degrees)),
        "max_degree": max(degrees, default=0),
        "biggest_room_share": _safe_div(biggest, floor_tiles),
        "perfect_rectangle_share": _safe_div(perfect, len(big)),
        "rooms_with_pillars": pillars,
    }

def _safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0

def _room_shape(components: list[set[tuple[int, int]]]) -> tuple[int, int]:
    """(perfect-rectangle count, rooms-with-interior-pillar count) among the
    door-bounded rooms with at least 12 tiles."""
    perfect = pillars = 0
    for comp in components:
        if len(comp) < 12:
            continue
        xs = [x for x, _ in comp]; ys = [y for _, y in comp]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        if (x1 - x0 + 1) * (y1 - y0 + 1) == len(comp):
            perfect += 1
            continue
        if any((x, y) not in comp and
               all((x + dx, y + dy) in comp for dx, dy in DIRS4)
               for y in range(y0 + 1, y1) for x in range(x0 + 1, x1)):
            pillars += 1
    return perfect, pillars

def corridor_share(tiles: list[int]) -> float:
    """Share of floor cells whose 3x3 neighborhood has <=5 floor cells."""
    floor_cells = [(x, y) for y in range(G.GRID) for x in range(G.GRID)
                   if G._is_floor(G._at(tiles, x, y))]
    if not floor_cells:
        return 0.0
    narrow = 0
    for x, y in floor_cells:
        count = sum(1 for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                    if G._is_floor(G._at(tiles, x + dx, y + dy)))
        if count <= 5:
            narrow += 1
    return narrow / len(floor_cells)

def longest_straight_run(tiles: list[int]) -> int:
    """Longest unobstructed floor run along any row or column; doors break
    a run just like walls do."""
    longest = 0
    for horizontal in (True, False):
        for fixed in range(G.GRID):
            run = 0
            for moving in range(G.GRID):
                x, y = (moving, fixed) if horizontal else (fixed, moving)
                if G._is_floor(G._at(tiles, x, y)):
                    run += 1
                    longest = max(longest, run)
                else:
                    run = 0
    return longest

def enemy_stats(tiles: list[int], things: list[int],
                start: tuple[int, int] | None) -> dict[str, float | int | None]:
    positions = [(index % G.GRID, index // G.GRID, thing)
                 for index, thing in enumerate(things) if thing in G.ENEMY_CODES]
    quartiles = [0, 0, 0, 0]
    if start is not None and positions:
        dist = bfs_distances(tiles, start)
        depths = [dist[p[:2]] for p in positions if p[:2] in dist]
        max_depth = max(depths, default=0) or 1
        for depth in depths:
            bucket = min(3, int(depth / max_depth * 4))
            quartiles[bucket] += 1
    return {
        "enemy_count": len(positions),
        "patrol_enemies": sum(1 for *_, thing in positions if thing in PATROL_CODES),
        "enemies_q1": quartiles[0], "enemies_q2": quartiles[1],
        "enemies_q3": quartiles[2], "enemies_q4": quartiles[3],
    }

def compute_metrics(tiles: list[int], things: list[int],
                    start: tuple[int, int] | None = None,
                    exit_stand: tuple[int, int] | None = None) -> dict[str, object]:
    start = start if start is not None else find_start(things)
    exit_stand = exit_stand if exit_stand is not None else find_exit_stand(tiles)
    metrics = door_graph(tiles)
    metrics["corridor_share"] = corridor_share(tiles)
    metrics["longest_straight_run"] = longest_straight_run(tiles)
    tortuosity = None
    if start is not None and exit_stand is not None:
        manhattan = abs(start[0] - exit_stand[0]) + abs(start[1] - exit_stand[1])
        walked = bfs_distances(tiles, start).get(exit_stand)
        if manhattan and walked is not None:
            tortuosity = walked / manhattan
    metrics["tortuosity"] = tortuosity
    metrics.update(enemy_stats(tiles, things, start))
    return metrics

# --------------------------------------------------------------------------- printing

HUMAN_LABELS = (
    ("floor_tiles", "Floor tiles"), ("door_tiles", "Door tiles"),
    ("rooms", "Door-bounded rooms (>=12 tiles)"),
    ("graph_edges", "Door-graph edges"), ("graph_cycles", "Door-graph cycles"),
    ("dead_end_ratio", "Dead-end ratio"), ("mean_degree", "Mean room degree"),
    ("max_degree", "Max room degree"), ("biggest_room_share", "Biggest-room share of floor"),
    ("perfect_rectangle_share", "Perfect-rectangle room share"),
    ("rooms_with_pillars", "Rooms with interior pillars"),
    ("corridor_share", "Corridor-cell share"), ("longest_straight_run", "Longest straight run"),
    ("tortuosity", "Tortuosity (start->exit)"), ("enemy_count", "Enemies"),
    ("patrol_enemies", "Patrol enemies"),
    ("enemies_q1", "Enemies in depth Q1"), ("enemies_q2", "Enemies in depth Q2"),
    ("enemies_q3", "Enemies in depth Q3"), ("enemies_q4", "Enemies in depth Q4"),
)

def print_human(tiles: list[int], things: list[int], metrics: dict[str, object]) -> None:
    for line in render(tiles, things):
        print(line)
    print()
    for key, label in HUMAN_LABELS:
        value = metrics[key]
        if isinstance(value, float):
            value = f"{value:.3f}"
        elif value is None:
            value = "n/a"
        print(f"  {label:34s} {value}")

def inspect_single(tiles: list[int], things: list[int], as_json: bool,
                   start: tuple[int, int] | None = None,
                   exit_stand: tuple[int, int] | None = None) -> None:
    metrics = compute_metrics(tiles, things, start, exit_stand)
    if as_json:
        print(json.dumps(metrics, indent=2, sort_keys=True))
    else:
        print_human(tiles, things, metrics)

def compare_corpus(directory: Path) -> int:
    wads = sorted(directory.rglob("*.wad"))
    rows = []
    for wad in wads:
        parsed = parse_wad(wad.read_bytes())
        if parsed is None:
            continue
        tiles, things = parsed
        rows.append(compute_metrics(tiles, things))
    if not rows:
        print(f"no readable *.wad files under {directory}", file=sys.stderr)
        return 1
    print(f"Compared {len(rows)} maps under {directory} ({len(wads)} *.wad files found):")
    for key, label in HUMAN_LABELS:
        values = [row[key] for row in rows if row[key] is not None]
        if not values:
            print(f"  {label:34s} n/a")
            continue
        mean = statistics.mean(values)
        median = statistics.median(values)
        print(f"  {label:34s} mean={mean:8.3f} median={median:8.3f}")
    return 0

# --------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect infiniwolf/corpus Wolf3D maps.")
    parser.add_argument("--seed", help="deterministic seed for --floor generation")
    parser.add_argument("--floor", type=int, help="floor number (1-10)")
    parser.add_argument("--complexity", type=int, choices=range(1, 6),
                        help="layout_complexity intensity (1-5) for --seed generation")
    parser.add_argument("--wad", type=Path, help="path to a corpus WDC3.1 PWAD")
    parser.add_argument("--pk3", type=Path, help="path to a generated infiniwolf .pk3 package")
    parser.add_argument("--compare", type=Path, help="recursively inspect every *.wad under DIR")
    parser.add_argument("--json", action="store_true", help="emit metrics as JSON, suppress render")
    return parser

def generate_floor(seed: str, floor: int, complexity: int | None) -> G.GeneratedMap:
    settings: dict[str, object] = {}
    if complexity is not None:
        settings["layout_complexity"] = Intensity(complexity)
    config = CampaignConfig.with_seed(seed, **settings)
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            return G.generate_map(config, floor, attempt)
        except ValueError as error:
            last_error = error
    raise SystemExit(f"floor {floor} failed to generate after retries: {last_error}")

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    modes = [bool(args.compare), bool(args.wad), bool(args.pk3), args.seed is not None]
    if sum(modes) != 1:
        print("error: specify exactly one of --seed/--floor, --wad, --pk3, or --compare",
              file=sys.stderr)
        return 2
    if args.compare:
        if not args.compare.is_dir():
            print(f"error: {args.compare} is not a directory", file=sys.stderr)
            return 2
        return compare_corpus(args.compare)
    if args.wad:
        if not args.wad.is_file():
            print(f"error: {args.wad} does not exist", file=sys.stderr)
            return 2
        tiles, things = load_wad_file(args.wad)
        inspect_single(tiles, things, args.json)
        return 0
    if args.pk3:
        if not args.pk3.is_file():
            print(f"error: {args.pk3} does not exist", file=sys.stderr)
            return 2
        floor = args.floor if args.floor is not None else 1
        tiles, things = load_pk3(args.pk3, floor)
        inspect_single(tiles, things, args.json)
        return 0
    if args.seed is None or args.floor is None:
        print("error: --seed and --floor must both be given for floor generation",
              file=sys.stderr)
        return 2
    if not 1 <= args.floor <= 10:
        print("error: --floor must be between 1 and 10", file=sys.stderr)
        return 2
    level = generate_floor(args.seed, args.floor, args.complexity)
    inspect_single(level.tiles, level.things, args.json, level.start,
                   getattr(level, "exit_stand", None))
    return 0

if __name__ == "__main__":
    sys.exit(main())
