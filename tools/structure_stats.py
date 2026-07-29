#!/usr/bin/env python3
"""Structural corpus-vs-generated comparison.

decor_stats.py did this for decoration and the decoration systems were tuned
into parity as a result. Nothing did it for layout: DESIGN.md advertised
`inspect_map.py --compare` for the job, but that mode globbed loose *.wad and
the corpus ships inside .pk3, so it measured zero maps and the structural gap
went unwatched. Generated floors carry roughly two thirds of an authored map's
walkable area.

Metrics are reported as median and p90 rather than mean: the authored corpus is
heavily right-tailed and its grand halls -- the thing generation most obviously
lacks -- live entirely in that tail, where a mean hides them.

  tools/structure_stats.py --corpus --generated --seeds 12
  tools/structure_stats.py --gate            # exit 1 on an out-of-band metric
"""
from __future__ import annotations

import argparse
from collections import deque
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from infiniwolf import wl6 as T
from infiniwolf.config import CampaignConfig
import inspect_map as I
from corpus_io import DEFAULT_CORPUS, iter_corpus_maps

DIRS4 = ((1, 0), (-1, 0), (0, 1), (0, -1))
DOORS = frozenset(T.DOORS)
DOORS_PLAIN = frozenset({90, 91})
DOORS_LOCKED = frozenset({92, 93, 94, 95})
DOORS_ELEVATOR = frozenset({100, 101})
TREASURE_LO, TREASURE_HI = 43, 56
ROOM_MIN = 12  # a door-bounded component below this reads as connective space

DEFAULT_FLOORS = (1, 2, 3, 5, 7, 9, 10)


# --------------------------------------------------------------------------- helpers

def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * (len(ordered) - 1)))]


def _components(tiles: list[int], is_floor, grid: int) -> list[set[tuple[int, int]]]:
    return I.floor_components(tiles, is_floor, grid)


def _walkable(value: int) -> bool:
    return value in DOORS


def _bbox(cells) -> tuple[int, int, int, int]:
    xs = [x for x, _ in cells]
    ys = [y for _, y in cells]
    return min(xs), min(ys), max(xs), max(ys)


# --------------------------------------------------------------------------- metrics

def _enclosed_rock(tiles: list[int], is_floor, grid: int,
                   box: tuple[int, int, int, int]) -> int:
    """Solid cells inside the played bounding box that no outside rock reaches.

    Rock the layout wraps around is deliberate negative space; rock that merely
    fills the gap between two rooms nobody could pack tighter is wasted plane,
    and this separates them."""
    x0, y0, x1, y1 = box
    solid = {(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)
             if not is_floor(I.at(tiles, x, y, grid))
             and not _walkable(I.at(tiles, x, y, grid))}
    border = deque(cell for cell in solid
                   if cell[0] in (x0, x1) or cell[1] in (y0, y1))
    reached = set(border)
    while border:
        x, y = border.popleft()
        for dx, dy in DIRS4:
            nxt = (x + dx, y + dy)
            if nxt in solid and nxt not in reached:
                reached.add(nxt)
                border.append(nxt)
    return len(solid) - len(reached)


def _loop_detours(components: list[set[tuple[int, int]]],
                  adjacency: dict[int, set[int]]) -> list[float]:
    """For every door-graph edge that sits on a cycle, the length of the best
    route between its endpoints that avoids it.

    A loop is only worth building if going the other way is a real decision. Two
    parallel hallways between the same pair of rooms score 2 and read as
    redundancy; a genuine circuit scores much higher."""
    detours: list[float] = []
    for a, neighbours in adjacency.items():
        for b in neighbours:
            if b <= a:
                continue
            dist = {a: 0}
            queue = deque([a])
            while queue:
                node = queue.popleft()
                for nxt in adjacency[node]:
                    if node == a and nxt == b:
                        continue  # the edge under test
                    if nxt == a and node == b:
                        continue
                    if nxt not in dist:
                        dist[nxt] = dist[node] + 1
                        queue.append(nxt)
            if b in dist:
                detours.append(float(dist[b]))
    return detours


def _route_bend_rate(tiles: list[int], is_floor, grid: int,
                     start: tuple[int, int] | None,
                     goal: tuple[int, int] | None) -> float | None:
    """Direction changes per ten tiles along the shortest start->goal walk."""
    if start is None or goal is None:
        return None
    prev: dict[tuple[int, int], tuple[int, int]] = {start: start}
    queue = deque([start])
    while queue:
        x, y = queue.popleft()
        if (x, y) == goal:
            break
        for dx, dy in DIRS4:
            nxt = (x + dx, y + dy)
            if nxt in prev:
                continue
            tile = I.at(tiles, *nxt, grid)
            if is_floor(tile) or tile in DOORS:
                prev[nxt] = (x, y)
                queue.append(nxt)
    if goal not in prev:
        return None
    path = [goal]
    while path[-1] != start:
        path.append(prev[path[-1]])
    path.reverse()
    if len(path) < 3:
        return None
    headings = [(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:])]
    bends = sum(one != two for one, two in zip(headings, headings[1:]))
    return 10.0 * bends / len(path)


def measure(tiles: list[int], things: list[int], is_floor, grid: int,
            start: tuple[int, int] | None = None,
            exit_stand: tuple[int, int] | None = None) -> dict:
    floor_cells = [(x, y) for y in range(grid) for x in range(grid)
                   if is_floor(I.at(tiles, x, y, grid))]
    door_cells = [(x, y) for y in range(grid) for x in range(grid)
                  if I.at(tiles, x, y, grid) in DOORS]
    if not floor_cells:
        return {}
    open_cells = floor_cells + door_cells
    x0, y0, x1, y1 = _bbox(open_cells)
    bbox_w, bbox_h = x1 - x0 + 1, y1 - y0 + 1
    bbox_area = bbox_w * bbox_h

    components = _components(tiles, is_floor, grid)
    rooms = [comp for comp in components if len(comp) >= ROOM_MIN]
    areas = [len(comp) for comp in rooms]
    aspects, majors = [], []
    for comp in rooms:
        cx0, cy0, cx1, cy1 = _bbox(comp)
        w, h = cx1 - cx0 + 1, cy1 - cy0 + 1
        aspects.append(max(w, h) / max(1, min(w, h)))
        majors.append(max(w, h))

    graph = I.door_graph(tiles, is_floor, grid)
    comp_of = {cell: i for i, comp in enumerate(components) for cell in comp}
    adjacency: dict[int, set[int]] = {i: set() for i in range(len(components))}
    for x, y in door_cells:
        sides = {comp_of[(x + dx, y + dy)] for dx, dy in DIRS4
                 if (x + dx, y + dy) in comp_of}
        for a in sides:
            for b in sides:
                if a != b:
                    adjacency[a].add(b)
    big = [i for i, comp in enumerate(components) if len(comp) >= ROOM_MIN]
    degrees = [len(adjacency[i]) for i in big]
    detours = _loop_detours(components, adjacency)

    connective = sum(len(comp) for comp in components if len(comp) < ROOM_MIN)
    destination = sum(areas)

    start = start if start is not None else I.find_start(things, grid)
    exit_stand = (exit_stand if exit_stand is not None
                  else I.find_exit_stand(tiles, is_floor, grid))

    # A dead end that pays is authored; a dead end that does not is filler.
    payoff = paid = 0
    for index in big:
        if len(adjacency[index]) > 1:
            continue
        paid += 1
        if any(TREASURE_LO <= things[y * grid + x] <= TREASURE_HI
               for x, y in components[index]):
            payoff += 1

    return {
        "floor_cells": len(floor_cells),
        "doors": len(door_cells),
        "doors_plain": sum(1 for x, y in door_cells
                           if I.at(tiles, x, y, grid) in DOORS_PLAIN),
        "doors_locked": sum(1 for x, y in door_cells
                            if I.at(tiles, x, y, grid) in DOORS_LOCKED),
        "doors_elevator": sum(1 for x, y in door_cells
                              if I.at(tiles, x, y, grid) in DOORS_ELEVATOR),
        "bbox_w": bbox_w,
        "bbox_h": bbox_h,
        "fill_of_bbox": len(open_cells) / bbox_area,
        "fill_of_grid": len(open_cells) / (grid * grid),
        "enclosed_rock_share": _enclosed_rock(tiles, is_floor, grid,
                                              (x0, y0, x1, y1)) / bbox_area,
        "connective_ratio": connective / max(1, destination),
        "rooms": len(rooms),
        "room_area_p50": _percentile(areas, 0.5),
        "room_area_p90": _percentile(areas, 0.9),
        "room_area_max": max(areas, default=0),
        "room_aspect_p50": _percentile(aspects, 0.5),
        "room_major_p90": _percentile(majors, 0.9),
        "biggest_room_share": graph["biggest_room_share"],
        "perfect_rectangle_share": graph["perfect_rectangle_share"],
        "graph_cycles": graph["graph_cycles"],
        "dead_end_ratio": graph["dead_end_ratio"],
        "junction_share": (sum(1 for d in degrees if d >= 3) / len(degrees)
                           if degrees else 0.0),
        "loop_detour_p50": _percentile(detours, 0.5),
        "dead_end_payoff": payoff / paid if paid else 0.0,
        "corridor_share": I.corridor_share(tiles, is_floor, grid),
        "longest_run": I.longest_straight_run(tiles, is_floor, grid),
        "route_bend_rate": _route_bend_rate(tiles, is_floor, grid, start, exit_stand),
    }


# --------------------------------------------------------------------------- loading

def load_corpus(pattern: str) -> tuple[list[dict], dict]:
    rows, shapes = [], {}
    for entry in iter_corpus_maps(pattern):
        row = measure(entry.tiles, entry.things, I.corpus_is_floor, entry.width)
        if row:
            rows.append(row)
    return rows, shapes


def load_generated(seeds: int, floors: tuple[int, ...]) -> tuple[list[dict], dict]:
    from infiniwolf import generator as G

    rows: list[dict] = []
    shapes: dict[str, int] = {}
    for index in range(seeds):
        config = CampaignConfig(seed=str(index))
        for floor in floors:
            for attempt in range(50):
                try:
                    level = G.generate_map(config, floor, attempt)
                except ValueError:
                    continue
                row = measure(level.tiles, level.things, G._is_floor, G.GRID,
                              level.start, getattr(level, "exit_stand", None))
                if row:
                    row["_critique"] = list(level.critique or ())
                    rows.append(row)
                for shape in getattr(level, "room_shapes", ()) or ():
                    shapes[shape] = shapes.get(shape, 0) + 1
                break
    return rows, shapes


def load_id_corpus(data_dir: Path, variant: str) -> tuple[list[dict], dict]:
    """id's own 60 maps, decoded from GAMEMAPS. Reported apart from the fan
    corpus rather than blended into it: two mods supply 44% of the fan maps, so
    a tail figure there may be one team's house style rather than a signature."""
    from gamemaps import read_gamemaps

    rows = []
    for record in read_gamemaps(data_dir / f"GAMEMAPS.{variant}",
                                data_dir / f"MAPHEAD.{variant}"):
        row = measure(record.tiles, record.things, I.corpus_is_floor, record.width)
        if row:
            rows.append(row)
    return rows, {}


# --------------------------------------------------------------------------- summary

def summarise(rows: list[dict], shapes: dict) -> dict:
    out: dict[str, object] = {"maps": len(rows)}
    if not rows:
        return out
    for key in rows[0]:
        if key.startswith("_"):
            continue
        values = [row[key] for row in rows if row.get(key) is not None]
        if not values:
            continue
        out[f"{key}_p50"] = _percentile(values, 0.5)
        out[f"{key}_p90"] = _percentile(values, 0.9)
        out[f"{key}_mean"] = statistics.fmean(values)
    total_shapes = sum(shapes.values())
    if total_shapes:
        ranked = sorted(shapes.items(), key=lambda kv: -kv[1])
        shaped = [(name, n) for name, n in ranked if name != "rectangle"]
        shaped_total = sum(n for _, n in shaped)
        out["shape_census"] = ranked
        out["rectangle_share"] = shapes.get("rectangle", 0) / total_shapes
        out["top3_family_share"] = (sum(n for _, n in shaped[:3]) / shaped_total
                                    if shaped_total else 0.0)
    flags: dict[str, int] = {}
    for row in rows:
        for flag in row.get("_critique", ()):
            flags[flag] = flags.get(flag, 0) + 1
    if flags:
        out["critique"] = sorted(flags.items(), key=lambda kv: -kv[1])
        out["no_loop_rate"] = flags.get("no_loop", 0) / len(rows)
    return out


ROW_SPECS = (
    ("walkable floor cells", "floor_cells_p50", "{:.0f}"),
    ("  p90", "floor_cells_p90", "{:.0f}"),
    ("doors", "doors_p50", "{:.0f}"),
    ("  locked", "doors_locked_p50", "{:.0f}"),
    ("played bbox width", "bbox_w_p50", "{:.0f}"),
    ("played bbox height", "bbox_h_p50", "{:.0f}"),
    ("fill of bbox", "fill_of_bbox_p50", "{:.3f}"),
    ("fill of grid", "fill_of_grid_p50", "{:.3f}"),
    ("enclosed rock share", "enclosed_rock_share_p50", "{:.3f}"),
    ("connective/destination", "connective_ratio_p50", "{:.3f}"),
    ("door-bounded rooms", "rooms_p50", "{:.0f}"),
    ("room area p50", "room_area_p50_p50", "{:.0f}"),
    ("room area p90", "room_area_p90_p50", "{:.0f}"),
    ("room area max", "room_area_max_p90", "{:.0f}"),
    ("room aspect p50", "room_aspect_p50_p50", "{:.2f}"),
    ("room major span p90", "room_major_p90_p50", "{:.0f}"),
    ("biggest-room share", "biggest_room_share_p50", "{:.3f}"),
    ("  p90", "biggest_room_share_p90", "{:.3f}"),
    ("perfect-rectangle share", "perfect_rectangle_share_p50", "{:.3f}"),
    ("door-graph cycles (mean)", "graph_cycles_mean", "{:.2f}"),
    ("dead-end ratio", "dead_end_ratio_p50", "{:.3f}"),
    ("junction share (deg>=3)", "junction_share_p50", "{:.3f}"),
    ("loop detour p50", "loop_detour_p50_p50", "{:.2f}"),
    ("dead-end payoff", "dead_end_payoff_p50", "{:.3f}"),
    ("corridor share", "corridor_share_p50", "{:.3f}"),
    ("longest straight run", "longest_run_p50", "{:.0f}"),
    ("  p90", "longest_run_p90", "{:.0f}"),
    ("route bends / 10 tiles", "route_bend_rate_p50", "{:.2f}"),
)


def print_table(columns: list[tuple[str, dict]]) -> None:
    label_w = max(len(label) for label, _, _ in ROW_SPECS) + 2
    width = 16
    head = "".join(f"{name:>{width}}" for name, _ in columns)
    delta = len(columns) == 2
    if delta:
        head += f"{'delta':>{width}}"
    print(f"{'metric':<{label_w}}{head}")
    print("-" * (label_w + width * (len(columns) + delta)))
    print(f"{'maps measured':<{label_w}}"
          + "".join(f"{s['maps']:>{width}}" for _, s in columns))
    for label, key, fmt in ROW_SPECS:
        cells = ""
        for _, summary in columns:
            value = summary.get(key)
            cells += f"{fmt.format(value) if value is not None else '-':>{width}}"
        if delta:
            base, other = (columns[0][1].get(key), columns[1][1].get(key))
            if base and other is not None:
                cells += f"{(other - base) / abs(base):>{width - 1}.0%} "
            else:
                cells += f"{'-':>{width}}"
        print(f"{label:<{label_w}}{cells}")


def print_shapes(summary: dict) -> None:
    census = summary.get("shape_census")
    if not census:
        return
    print("\nroom-shape census (generated)")
    total = sum(n for _, n in census)
    for name, count in census[:14]:
        print(f"  {name:32s} {count:5d}  {count / total:6.1%}")
    print(f"  {'-- plain rectangle share':32s} {summary['rectangle_share']:12.1%}")
    print(f"  {'-- top-3 shaped families':32s} {summary['top3_family_share']:12.1%}")
    if summary.get("critique"):
        print("\ncritique flags (generated)")
        for flag, count in summary["critique"]:
            print(f"  {flag:32s} {count:5d}")


# --------------------------------------------------------------------------- gate

# Bands come from measured corpora, never from invented ideals, and every one
# below is a figure id's own 60 maps and the 227 fan maps agree on.
#
# Total walkable area is deliberately a BAND rather than a floor. Against the fan
# corpus the generator looked a third too small, and that reading drove a plan to
# inflate every room; against id's own maps -- 1098 cells to the generator's 1120
# -- it is already right, and the fan figure turned out to be two mods' house
# style (totengraeber and wolfoverdrive are 44% of that corpus). What both
# corpora do agree on is how the area is DIVIDED: id spends 1100 cells on 15
# rooms with a 514-cell largest, the generator on 21 rooms with a 222-cell
# largest. So the bands below chase distribution, and the area band exists only
# to catch drift in either direction.
GATE_BANDS = (
    ("floor_cells_p50", 950, 1600, "area band -- drift guard, not a growth goal"),
    ("doors_p50", 19, None, "stage 2 loops"),
    ("graph_cycles_mean", 0.9, None, "stage 2 loops"),
    ("top3_family_share", None, 0.60, "stage 3 shape gates"),
    ("room_aspect_p50_p50", 1.35, None, "stage 4 room redistribution"),
    ("room_major_p90_p50", 17, None, "stage 4 room redistribution"),
    ("room_area_max_p90", 400, None, "stage 4 spatial masses"),
    ("biggest_room_share_p90", 0.35, None, "stage 4 spatial masses"),
    # Both corpora put this at 48-49, and the band does NOT chase them. The
    # sightline breaker caps runs at 30 because the same measurement is what
    # catches two spaces fused with no separating architecture, and that guard is
    # worth more than the last 18 tiles of corpus fidelity. This band asserts the
    # cap is being approached, not that the corpus is matched -- a deliberate
    # divergence, recorded rather than quietly dropped.
    ("longest_run_p90", 25, None, "capped at 30 by _break_long_sightlines"),
    ("corridor_share_p50", 0.15, 0.24, "stage 4 corridor sizing"),
    ("dead_end_ratio_p50", 0.26, None, "stage 4 room redistribution"),
    ("route_bend_rate_p50", 1.55, None, "stage 4 room redistribution"),
)


def run_gate(summary: dict) -> int:
    failures = []
    for key, low, high, owner in GATE_BANDS:
        value = summary.get(key)
        if value is None:
            failures.append(f"  {key:28s} MISSING          ({owner})")
            continue
        if low is not None and value < low:
            failures.append(f"  {key:28s} {value:8.3f} < {low}   ({owner})")
        elif high is not None and value > high:
            failures.append(f"  {key:28s} {value:8.3f} > {high}   ({owner})")
    # Worth stating plainly, because the numbers below are pessimistic by
    # construction: load_generated calls generate_map, which does not run
    # candidate selection. A shipped campaign picks the best of a pool, so its
    # figures are better -- measured over two full campaigns, zero of twenty
    # floors had no loop, against 37% of raw attempts here.
    print("measuring generate_map output; a shipped campaign selects among "
          "candidates and scores better")
    if failures:
        print("structure gate FAILED:")
        print("\n".join(failures))
        return 1
    print(f"structure gate passed ({len(GATE_BANDS)} bands)")
    return 0


# --------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--corpus", action="store_true", help="measure authored .pk3 maps")
    parser.add_argument("--generated", action="store_true", help="measure fresh floors")
    parser.add_argument("--corpus-glob", default=DEFAULT_CORPUS)
    parser.add_argument("--id-corpus", type=Path,
                        help="directory holding GAMEMAPS/MAPHEAD for id's own maps")
    parser.add_argument("--id-variant", default="WL6")
    parser.add_argument("--seeds", type=int, default=12)
    parser.add_argument("--floors", default=",".join(str(n) for n in DEFAULT_FLOORS))
    parser.add_argument("--gate", action="store_true",
                        help="measure generated floors and exit 1 if a band fails")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.gate:
        args.generated = True
    if not args.corpus and not args.generated and not args.id_corpus:
        args.corpus = args.generated = True
    floors = tuple(int(n) for n in args.floors.split(",") if n.strip())

    columns: list[tuple[str, dict]] = []
    generated_summary: dict = {}
    if args.corpus:
        columns.append(("hand-authored", summarise(*load_corpus(args.corpus_glob))))
    if args.id_corpus:
        columns.append(("id original",
                        summarise(*load_id_corpus(args.id_corpus, args.id_variant))))
    if args.generated:
        generated_summary = summarise(*load_generated(args.seeds, floors))
        columns.append(("generated", generated_summary))

    if args.json:
        print(json.dumps({name: summary for name, summary in columns},
                         indent=2, sort_keys=True, default=float))
    else:
        print_table(columns)
        for _, summary in columns:
            print_shapes(summary)

    if args.gate:
        print()
        return run_gate(generated_summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
