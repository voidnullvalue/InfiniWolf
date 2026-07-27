"""Geometric realization: turning an abstract plan into usable tile geometry.

Everything that decides where solid rock becomes walkable floor. Rooms are sized
from their planned tier, placed without overlapping, carved into shapes, then
connected by corridors routed around a one-rock buffer, then repaired: accidental
long sightlines broken, oversized sound zones split, redundant double doorways
removed, pinched room-door pairs healed.

The repair passes run after routing because they fix emergent problems that no
single placement decision can see. They also partition space -- spatial districts,
sound zones, theme-merge limits -- which lives here rather than with semantics
because the partition is a connectivity question; only the labels it emits are
semantic, and splitting the two would fragment one flood fill across two modules.

Corridor routing is the hottest code in the generator by a wide margin -- about
98% of cumulative generation time -- so its inner loop indexes the tile plane
directly and consults the precomputed `_FLOOR_OR_DOOR` table instead of calling
predicates. The clamp to `2 <= x, y < GRID - 2` is what makes the unchecked
indexing safe; loosening it would make neighbour reads wrap the plane.

The division of labour with planning.py is deliberate. Planning says a room is a
`corridor` and connects to two others; geometry decides the actual rectangle, and
may fail. A geometric impossibility rejects the candidate attempt rather than
silently dropping a mandatory room, which is why these functions raise rather
than returning a degraded result.

This module owns `_snap_offsets`, which grid.py deliberately does not: it draws
from the RNG and encodes which room alignments are preferred, making it a
placement decision rather than a spatial query.
"""

from __future__ import annotations

from collections import Counter, deque
import heapq
from itertools import combinations
import math
import random

from .campaign import HALLWAY_FIRST_SKELETONS
from .grid import (_FLOOR_OR_DOOR, _at, _floor_components, _inside_room,
                   _is_floor, _overlaps, _reachable, _set)
from .model import FloorPlan, PlacedPlan, Room
from .wl6 import (DOOR_EW, DOOR_GOLD_EW, DOOR_NS, DOORS, FLOOR, GRID, WALL,
                  ZONE_MAX)
from .ledger import reserve as ledger_reserve


def _snap_offsets(parent: Room, rw: int, rh: int, side: tuple[int, int],
                  rng: random.Random) -> list[int]:
    """Return cross-axis offsets, prioritizing architectural alignment."""
    parent_dim, child_dim = (parent.h, rh) if side[0] else (parent.w, rw)
    delta = parent_dim - child_dim
    flush_low = -(delta // 2)
    flush_high = delta - delta // 2
    flushes = (flush_low, flush_high)
    if rng.randrange(2):
        flushes = flushes[::-1]
    offsets = [0, *flushes, *(rng.randrange(-3, 4) for _ in range(3))]
    return list(dict.fromkeys(offsets))


def _room_size(rng: random.Random, tier: str, number: int = 0) -> tuple[int, int]:
    def preferred(low: int, high: int) -> int:
        """Strongly prefer odd spans without making parity a hard rule."""
        values = list(range(low, high))
        weights = [4 if value % 2 else 1 for value in values]
        return rng.choices(values, weights=weights, k=1)[0]

    bump = 2 if number == 10 else 0
    if tier == "anchor":
        if number == 9:
            return preferred(14, 18), preferred(14, 18)
        return preferred(10 + bump, 14 + bump), preferred(10 + bump, 14 + bump)
    if tier == "motif":
        return 15, 15
    if tier == "closet":
        return rng.randrange(4, 6), rng.randrange(4, 6)
    if tier == "corridor":
        # A circulation node is a traversable hallway, not a long combat
        # room. Its authored major span is three tiles wide; ordinary carved
        # connectors remain one tile wide. Even-width corridors are never the
        # default product of the room-size grammar.
        major, minor = preferred(8 + bump, 14 + bump), 3
        return (major, minor) if rng.random() < 0.5 else (minor, major)
    if tier == "hall":
        major = preferred(9 + bump, 14 + bump)
        minor = preferred(5 + bump, 8 + bump)
        return (major, minor) if rng.random() < 0.5 else (minor, major)
    return preferred(6 + bump, 10 + bump), preferred(6 + bump, 10 + bump)


def _place_planned_rooms(rng: random.Random, plan: FloorPlan, number: int = 0) -> PlacedPlan:
    spine_count = next(index for index, spec in enumerate(plan.specs)
                       if spec.role == "exit") + 1
    sizes = [_room_size(rng, spec.tier, number) for spec in plan.specs]
    for index, spec in enumerate(plan.specs):
        if spec.motif == "hallway-arm":
            sizes[index] = ((7, 3) if rng.random() < 0.5 else (3, 7))
        elif spec.motif == "hallway-destination":
            sizes[index] = (7, 7)
    for group in plan.size_groups:
        shared = sizes[group[0]]
        for index in group[1:]:
            sizes[index] = shared
    parents: dict[int, int] = {}
    for child in range(1, len(plan.specs)):
        parents[child] = next(other for a, b in plan.edges
                              for other in ((b,) if a == child else (a,) if b == child else ())
                              if other < child)
    planned_parents = dict(parents)
    reparented: dict[int, int] = {}
    rooms: list[Room] = []
    kept: list[int] = []
    room_by_spec: dict[int, Room] = {}
    dropped: set[int] = set()
    used_sides: dict[int, dict[tuple[int, int], int]] = {}
    forced_rooms: dict[int, Room] = {}
    hallway_arm_sides: list[tuple[int, int]] = []
    # Native-safe arrival cars have a horizontal axis. Begin from the left or
    # right building edge so the outward horizontal wall retains guaranteed
    # rock depth; the spine may still turn immediately and use every broader
    # circulation form after this first architectural constraint.
    heading = rng.choice(((1, 0), (-1, 0)))
    w, h = sizes[0]
    hallway_first = plan.skeleton in HALLWAY_FIRST_SKELETONS
    edge_low, edge_high = ((8, 15) if hallway_first else (6, 11))
    cross_low_x = max(4, GRID // 2 - w - 8) if hallway_first else 4
    cross_high_x = min(GRID - w - 3, GRID // 2 + 8) if hallway_first else GRID - w - 3
    cross_low_y = max(4, GRID // 2 - h - 8) if hallway_first else 4
    cross_high_y = min(GRID - h - 3, GRID // 2 + 8) if hallway_first else GRID - h - 3
    sx = rng.randrange(edge_low, edge_high) if heading[0] > 0 else (
         GRID - w - rng.randrange(edge_low, edge_high) if heading[0] < 0
         else rng.randrange(cross_low_x, cross_high_x))
    sy = rng.randrange(edge_low, edge_high) if heading[1] > 0 else (
         GRID - h - rng.randrange(edge_low, edge_high) if heading[1] < 0
         else rng.randrange(cross_low_y, cross_high_y))
    start = Room(sx, sy, w, h)
    rooms.append(start); kept.append(0); room_by_spec[0] = start
    protected_elevator_rock: set[tuple[int, int]] = set()

    def elevator_envelopes(room: Room) -> list[set[tuple[int, int]]]:
        envelopes = []
        for dx in (-1, 1):
            wx = room.x - 1 if dx < 0 else room.x + room.w
            for wy in sorted(range(room.y + 1, room.y + room.h - 1),
                             key=lambda value: abs(value - room.center[1])):
                cells = {(wx + dx * depth, wy + side)
                         for depth in range(5) for side in (-2, -1, 0, 1, 2)}
                if all(1 <= x < GRID - 1 and 1 <= y < GRID - 1
                       for x, y in cells):
                    envelopes.append(cells)
                    break
        return envelopes

    # The outward side of the edge-biased start room is owned by its arrival
    # car. Later rooms may not consume that otherwise invisible rock shell.
    start_outward = -heading[0]
    start_envelopes = elevator_envelopes(start)
    if start_envelopes:
        protected_elevator_rock.update(min(
            start_envelopes,
            key=lambda cells: min(x for x, _ in cells) if start_outward < 0
            else -max(x for x, _ in cells)))

    def adjacent(parent: Room, size: tuple[int, int], side: tuple[int, int],
                 gap: int, jitter: int) -> Room:
        rw, rh = size
        dx, dy = side
        if dx:
            x = parent.x + parent.w + gap if dx > 0 else parent.x - rw - gap
            y = parent.y + (parent.h - rh) // 2 + jitter
        else:
            x = parent.x + (parent.w - rw) // 2 + jitter
            y = parent.y + parent.h + gap if dy > 0 else parent.y - rh - gap
        return Room(x, y, rw, rh)

    def legal(room: Room) -> bool:
        return (3 <= room.x and 3 <= room.y and room.x + room.w < 61
                and room.y + room.h < 61
                and not any((x, y) in protected_elevator_rock
                            for y in range(room.y, room.y + room.h)
                            for x in range(room.x, room.x + room.w))
                and not any(_overlaps(room, other) for other in rooms))

    def supports_exit_elevator(room: Room) -> bool:
        return any(
            not (cells & protected_elevator_rock)
            and not any(any(_inside_room([other], *cell) for cell in cells)
                        for other in rooms)
            for cells in elevator_envelopes(room))

    grouped = {index for group in plan.size_groups for index in group}
    order = list(range(1, spine_count))
    pending = set(range(spine_count, len(plan.specs)))
    while pending:
        available = [index for index in pending if parents[index] not in pending]
        index = min(available, key=lambda item: (
            0 if plan.specs[item].motif in {
                "hallway-arm", "hallway-destination"} else
            1 if plan.specs[item].motif == "swastika" else
            2 if plan.specs[parents[item]].role == "hub" else
            3 if item in grouped else 4 if plan.specs[item].role == "ring" else 5,
            item))
        order.append(index); pending.remove(index)
    for index in order:
        parent_index = parents[index]
        while parent_index in dropped:
            parent_index = parents[parent_index]
        parent = room_by_spec[parent_index]
        room = forced_rooms.pop(index, None)
        if room is not None:
            # Hallway destinations are reserved atomically with their arm;
            # no optional room can consume the terminal footprint between
            # the two dependent placements.
            sizes[index] = (room.w, room.h)

        if plan.specs[index].motif == "hallway-arm" and room is None:
            destination = next(
                other for a, b in plan.edges
                for other in ((b,) if a == index else (a,) if b == index else ())
                if other > index
                and plan.specs[other].motif == "hallway-destination")
            scaffold = [
                candidate for candidate in kept
                if candidate < spine_count
                and plan.specs[candidate].tier == "corridor"
            ]
            scaffold.sort(key=lambda candidate: (
                candidate != parents[index], abs(candidate - parents[index])))
            pair_candidates = []
            for candidate_parent in scaffold:
                host = room_by_spec[candidate_parent]
                sides = ([(0, 1), (0, -1)] if host.w >= host.h
                         else [(1, 0), (-1, 0)])
                if hallway_arm_sides:
                    opposite = (-hallway_arm_sides[0][0],
                                -hallway_arm_sides[0][1])
                    sides.sort(key=lambda side: side != opposite)
                for side_rank, side in enumerate(sides):
                    for length_rank, length in enumerate((7, 5)):
                        arm_size = ((length, 3) if side[0]
                                    else (3, length))
                        offsets = _snap_offsets(host, *arm_size, side, rng)
                        offsets.extend(offset for offset in range(-10, 11)
                                       if offset not in offsets)
                        for gap_rank, gap in enumerate((2, 3)):
                            for jitter_rank, jitter in enumerate(offsets):
                                arm = adjacent(host, arm_size, side, gap, jitter)
                                if not legal(arm):
                                    continue
                                terminal_offsets = _snap_offsets(
                                    arm, *sizes[destination], side, rng)
                                terminal_offsets.extend(
                                    offset for offset in range(-3, 4)
                                    if offset not in terminal_offsets)
                                destination_sizes = [sizes[destination]]
                                destination_sizes.extend(
                                    ((5, 7), (5, 5)) if side[0]
                                    else ((7, 5), (5, 5)))
                                for size_rank, terminal_size in enumerate(
                                        destination_sizes):
                                    terminal_offsets = _snap_offsets(
                                        arm, *terminal_size, side, rng)
                                    terminal_offsets.extend(
                                        offset for offset in range(-3, 4)
                                        if offset not in terminal_offsets)
                                    for destination_gap in (2, 3):
                                        for terminal_rank, terminal_jitter in enumerate(
                                                terminal_offsets):
                                            terminal = adjacent(
                                                arm, terminal_size, side,
                                                destination_gap, terminal_jitter)
                                            if (legal(terminal)
                                                    and not _overlaps(terminal, arm)):
                                                pair_candidates.append((
                                                    candidate_parent != parents[index],
                                                    side_rank, length_rank, size_rank,
                                                    gap_rank, jitter_rank,
                                                    destination_gap, terminal_rank,
                                                    candidate_parent, side, arm,
                                                    terminal))
            if pair_candidates:
                (*_, candidate_parent, side, room, terminal) = min(
                    pair_candidates, key=lambda candidate: candidate[:8])
                if candidate_parent != parent_index:
                    parents[index] = candidate_parent
                    reparented[index] = candidate_parent
                    parent_index = candidate_parent
                    parent = room_by_spec[parent_index]
                sizes[index] = (room.w, room.h)
                forced_rooms[destination] = terminal
                hallway_arm_sides.append(side)

        for attempt in range(60 if room is None else 0):
            if index < spine_count:
                dx, dy = heading
                sides = [(dx, dy), (-dy, dx), (dy, -dx)]
                turning_node = plan.specs[index].tier == "corridor"
                grammar_weights = {
                    "axial-journey": (8.0, 1.2, 1.2),
                    "hub-relay": ((2.0, 4.0, 4.0) if turning_node else (5.0, 2.5, 2.5)),
                    "offset-ladder": ((1.5, 4.5, 4.5) if turning_node else (4.0, 3.0, 3.0)),
                    "clustered-chain": ((3.0, 3.5, 3.5) if turning_node else (7.0, 1.5, 1.5)),
                    "nested-circuit": (2.0, 4.0, 4.0),
                    "bounded-perimeter": (1.5, 4.25, 4.25),
                }
                grammar_bias = grammar_weights.get(plan.progression_grammar,
                                                   (7.0, 1.5, 1.5))
                # Skeletons own the large-scale turning rhythm while the
                # progression grammar owns dramatic pacing and loop type.
                # Multiplying their bounded biases composes both choices
                # instead of leaving the recorded skeleton as inert metadata.
                branch_beats = {
                    max(1, spine_count // 3),
                    max(2, (2 * spine_count) // 3),
                }
                skeleton_bias = {
                    "bent-spine": ((2.0, 4.0, 4.0) if turning_node
                                   else (7.0, 1.5, 1.5)),
                    "parallel-cross": ((1.5, 5.0, 5.0) if turning_node
                                       else (5.0, 2.5, 2.5)),
                    "central-wings": ((2.0, 4.5, 4.5)
                                      if abs(index - spine_count // 2) <= 1
                                      else (8.0, 1.0, 1.0)),
                    "forked": ((1.5, 5.0, 5.0) if index in branch_beats
                               else (6.0, 2.0, 2.0)),
                    "perimeter-loop": (1.5, 4.25, 4.25),
                    "staggered-grid": ((2.5, 6.0, 1.0) if index % 2
                                       else (2.5, 1.0, 6.0)),
                    # Hallway-first forms give the central run a readable
                    # authored rhythm. Side-loaded optional rooms attach to
                    # these corridor nodes later, producing the crossbars and
                    # concourse arms without empty terminal hall stubs.
                    "central-axis": (12.0, 0.8, 0.8),
                    "plus-concourse": ((1.0, 7.0, 7.0)
                                        if index == spine_count // 2
                                        else (10.0, 1.0, 1.0)),
                    "t-concourse": ((1.0, 7.0, 2.0)
                                     if index == (2 * spine_count) // 3
                                     else (10.0, 1.0, 1.0)),
                    "offset-boulevard": ((1.0, 8.0, 1.0)
                                           if index in branch_beats
                                           else (9.0, 1.0, 1.0)),
                }[plan.skeleton]
                weights = tuple(grammar * skeleton
                                for grammar, skeleton
                                in zip(grammar_bias, skeleton_bias))
                side = rng.choices(sides, weights=weights, k=1)[0]
                gap = (rng.randrange(1, 3) if plan.progression_grammar == "clustered-chain"
                       else rng.randrange(1, 4))
            else:
                counts = used_sides.setdefault(parent_index, {})
                sides = ((1, 0), (-1, 0), (0, 1), (0, -1))
                mode = (plan.district_circulation[plan.specs[parent_index].district]
                        if plan.district_circulation else "suite")
                if plan.specs[index].motif == "hallway-destination":
                    # Continue to the occupied room at the far end of its
                    # concourse arm. Side-loading here folds the destination
                    # back toward the central hall and makes the arm much
                    # harder to realize cleanly.
                    favored = ({(1, 0), (-1, 0)} if parent.w >= parent.h
                               else {(0, 1), (0, -1)})
                elif plan.specs[index].motif == "hallway-arm":
                    cross = ((0, 1), (0, -1)) if parent.w >= parent.h else ((1, 0), (-1, 0))
                    favored = set(cross)
                elif (plan.specs[parent_index].tier == "corridor"
                      and mode in ("double-loaded", "single-loaded",
                                   "service-bays", "formal-axis")):
                    cross = ((0, 1), (0, -1)) if parent.w >= parent.h else ((1, 0), (-1, 0))
                    favored = ({cross[0]} if mode == "single-loaded" else set(cross))
                else:
                    favored = set(sides)
                side = rng.choices(
                    sides, weights=[(3 if s in favored else 0.5)
                                    / (1 + 5 * counts.get(s, 0)) for s in sides], k=1)[0]
                gap = (2 if plan.specs[index].motif in {
                    "hallway-arm", "hallway-destination"} else rng.randrange(1, 4))
            candidate_size = sizes[index]
            if plan.specs[index].tier == "corridor":
                rw, rh = candidate_size
                if side[0] and rw < rh:
                    candidate_size = rh, rw
                elif side[1] and rh < rw:
                    candidate_size = rh, rw
            # A centered door has a true middle tile only when the span of
            # its wall is odd. Prefer that parity on the child's attaching
            # face; the original even draw remains available on later retries
            # when crowded geometry makes the preference impossible.
            if attempt < 44:
                rw, rh = candidate_size
                if side[0] and rh % 2 == 0:
                    candidate_size = (rw, rh + (1 if rh < 17 else -1))
                elif side[1] and rw % 2 == 0:
                    candidate_size = (rw + (1 if rw < 17 else -1), rh)
            # Human mappers align rooms; jitter is the fallback once these
            # center and edge-flush placements have had a chance.
            jitters = (_snap_offsets(parent, *candidate_size, side, rng)
                       if attempt < 20 else
                       [rng.randrange(-6, 7) if index < spine_count else rng.randrange(-11, 12)])
            for jitter in jitters:
                candidate = adjacent(parent, candidate_size, side, gap, jitter)
                if (legal(candidate)
                        and (plan.specs[index].role != "exit"
                             or supports_exit_elevator(candidate))):
                    room = candidate
                    sizes[index] = candidate_size
                    if index < spine_count:
                        heading = side
                    else:
                        counts[side] = counts.get(side, 0) + 1
                    break
            if room is not None:
                break
        if room is None:
            # A filler room is not semantically tied to its first host. Before
            # dropping it, try other already-realized rooms in the same
            # district. Every retry remains a short (two-to-three tile) local
            # connection, so this fills genuine building space without
            # creating a long hallway to nowhere.
            if (index >= spine_count and plan.specs[index].motif == "filler"):
                alternatives = [
                    candidate for candidate in kept
                    if candidate != parent_index
                    and plan.specs[candidate].district == plan.specs[index].district
                    and plan.specs[candidate].role not in {
                        "start", "arrival", "exit", "victory", "recovery",
                        "boss-arena", "premium-vault",
                    }
                ]
                rng.shuffle(alternatives)
                district_mode = (plan.district_circulation[
                    plan.specs[index].district]
                    if plan.district_circulation else "suite")
                prefer_corridor = district_mode not in ("suite", "tunnel-cluster")
                alternatives.sort(key=lambda candidate: (
                    0 if (prefer_corridor
                          and plan.specs[candidate].tier == "corridor") else 1,
                    sum(candidate in edge for edge in plan.edges)))
                min_x = min(room.x for room in rooms)
                min_y = min(room.y for room in rooms)
                max_x = max(room.x + room.w for room in rooms)
                max_y = max(room.y + room.h for room in rooms)
                current_bbox_area = (max_x - min_x) * (max_y - min_y)
                candidates = []
                for alternative_rank, alternative in enumerate(alternatives):
                    alternative_room = room_by_spec[alternative]
                    counts = used_sides.setdefault(alternative, {})
                    sides = [(1, 0), (-1, 0), (0, 1), (0, -1)]
                    rng.shuffle(sides)
                    sides.sort(key=lambda side: counts.get(side, 0))
                    for side_rank, side in enumerate(sides):
                        for gap in (2, 3):
                            for jitter_rank, jitter in enumerate(_snap_offsets(
                                    alternative_room, *sizes[index], side, rng)):
                                candidate = adjacent(
                                    alternative_room, sizes[index], side, gap, jitter)
                                if legal(candidate):
                                    expanded_area = (
                                        max(max_x, candidate.x + candidate.w)
                                        - min(min_x, candidate.x)) * (
                                        max(max_y, candidate.y + candidate.h)
                                        - min(min_y, candidate.y))
                                    candidates.append((
                                        expanded_area - current_bbox_area,
                                        gap, alternative_rank, side_rank,
                                        jitter_rank, candidate, alternative, side))
                if candidates:
                    _, _, _, _, _, room, parent_index, side = min(
                        candidates, key=lambda candidate: candidate[:5])
                    parents[index] = parent_index
                    reparented[index] = parent_index
                    counts = used_sides.setdefault(parent_index, {})
                    counts[side] = counts.get(side, 0) + 1
        if room is None:
            # Optional filler is valuable only while it remains a local side
            # room. Scattering it across the map creates a long corridor that
            # can become deeper than the authored exit and makes progression
            # feel accidental, so drop it before the long-range fallbacks.
            if index not in plan.critical and index >= spine_count:
                dropped.add(index)
                continue
            # A crowded beat may need a second ring beyond its first wings;
            # keep the graph parent local before conceding to global scatter.
            for _ in range(120):
                side = rng.choice(((1, 0), (-1, 0), (0, 1), (0, -1)))
                candidate = adjacent(parent, sizes[index], side, rng.randrange(3, 8),
                                     rng.randrange(-10, 11))
                if (legal(candidate)
                        and (plan.specs[index].role != "exit"
                             or supports_exit_elevator(candidate))):
                    room = candidate
                    break
        if room is None:
            # Mandatory spine rooms may use the global fallback: a long
            # connection here lengthens the route the player must actually
            # complete. Optional motifs never use it, because their remote
            # corridors could become deeper than the elevator.
            if index < spine_count:
                rw, rh = sizes[index]
                for _ in range(200):
                    candidate = Room(rng.randrange(3, 61 - rw),
                                     rng.randrange(3, 61 - rh), rw, rh)
                    if (legal(candidate)
                            and (plan.specs[index].role != "exit"
                                 or supports_exit_elevator(candidate))):
                        room = candidate
                        break
        if room is None:
            if index in plan.critical or index < spine_count:
                raise ValueError("could not realize critical planned room")
            dropped.add(index)
            continue
        rooms.append(room); kept.append(index); room_by_spec[index] = room
        if plan.specs[index].role == "exit":
            clear_envelopes = [
                cells for cells in elevator_envelopes(room)
                if not (cells & protected_elevator_rock)
                and not any(any(_inside_room([other], *cell) for cell in cells)
                            for other in rooms[:-1])]
            if not clear_envelopes:
                raise ValueError("planned exit room lacks a horizontal elevator envelope")
            # Prefer the side pointing away from its parent; either legal
            # envelope remains an acceptable native shaft fallback.
            parent_room = room_by_spec[parent_index]
            away = 1 if room.center[0] >= parent_room.center[0] else -1
            chosen = max(clear_envelopes, key=lambda cells: (
                max(x for x, _ in cells) if away > 0
                else -min(x for x, _ in cells)))
            protected_elevator_rock.update(chosen)

    remap = {spec_index: room_index for room_index, spec_index in enumerate(kept)}

    def survivor(index: int) -> int:
        while index in dropped:
            index = parents[index]
        return index

    edges = []
    for a, b in plan.edges:
        if b in reparented and a == planned_parents[b]:
            a = reparented[b]
        elif a in reparented and b == planned_parents[a]:
            b = reparented[a]
        if ((a in dropped or b in dropped)
                and (plan.specs[a].motif in {"ring", "courtyard", "service", "ladder"}
                     or plan.specs[b].motif in {"ring", "courtyard", "service", "ladder"})):
            continue
        a, b = survivor(a), survivor(b)
        edge = (remap[a], remap[b])
        if edge[0] != edge[1] and edge not in edges and edge[::-1] not in edges:
            edges.append(edge)
    loop_edges = [(remap[a], remap[b]) for a, b in plan.loop_edges
                  if a not in dropped and b not in dropped]
    return PlacedPlan(rooms, kept, edges, loop_edges)


def _carve_notches(tiles: list[int], rooms: list[Room], rng: random.Random,
                   chance: float = 0.22, max_rooms: int | None = None,
                   excluded: frozenset[int] = frozenset()
                   ) -> dict[int, tuple[tuple[int, int], ...]]:
    """Carve only mirrored corner compositions and return decor anchors."""
    anchors: dict[int, tuple[tuple[int, int], ...]] = {}
    for room_index, room in enumerate(rooms):
        if max_rooms is not None and len(anchors) >= max_rooms:
            break
        if (room_index in excluded or room.w < 6 or room.h < 6
                or rng.random() >= chance):
            continue
        corners = [(False, False), (True, False), (False, True), (True, True)]
        nw = rng.randint(2, min(3, (room.w - 2) // 2))
        nh = rng.randint(2, min(3, (room.h - 2) // 2))
        if rng.random() < 0.20:
            selected = corners
            axis = "four"
        elif rng.randrange(2):
            bottom = rng.randrange(2) == 1
            selected = [(False, bottom), (True, bottom)]
            axis = "horizontal"
        else:
            right = rng.randrange(2) == 1
            selected = [(right, False), (right, True)]
            axis = "vertical"
        room_anchors = []
        for right, bottom in selected:
            nx = room.x + room.w - nw if right else room.x
            ny = room.y + room.h - nh if bottom else room.y
            for y in range(ny, ny + nh):
                for x in range(nx, nx + nw):
                    _set(tiles, x, y, WALL)
            side_x = nx - 1 if right else nx + nw
            side_y = ny - 1 if bottom else ny + nh
            edge_x = nx if right else nx + nw - 1
            edge_y = ny if bottom else ny + nh - 1
            room_anchors.append((side_x, edge_y) if axis == "vertical"
                                else (edge_x, side_y))
        anchors[room_index] = tuple(room_anchors)
    return anchors


def _carve_symmetric_profiles(
        tiles: list[int], rooms: list[Room], rng: random.Random,
        chance: float = 0.24, max_rooms: int = 0,
        excluded: frozenset[int] = frozenset()
        ) -> tuple[dict[int, tuple[tuple[int, int], ...]], dict[int, str]]:
    """Carve a restrained set of non-rectangular, reflection-symmetric rooms.

    These are interior subtractions from an already legal bounding rectangle,
    so they cannot collide with another planned room. Connections are carved
    later and may reopen a shoulder where a doorway genuinely needs it.
    """
    anchors: dict[int, tuple[tuple[int, int], ...]] = {}
    shapes: dict[int, str] = {}
    family_counts: Counter[str] = Counter()
    family_cap = max(1, math.ceil(max_rooms * 0.35)) if max_rooms else 0

    for room_index, room in enumerate(rooms):
        if len(shapes) >= max_rooms:
            break
        if (room_index in excluded or room.w < 6 or room.h < 6
                or rng.random() >= chance):
            continue
        cx, cy = room.center
        candidates: list[tuple[str, set[tuple[int, int]], tuple[tuple[int, int], ...]]] = []

        # Four stepped corners form a broad cruciform/chamfered chamber.
        corner_cells: set[tuple[int, int]] = set()
        corner_anchors: list[tuple[int, int]] = []
        for right, bottom in ((False, False), (True, False),
                              (False, True), (True, True)):
            ox = room.x + room.w - 1 if right else room.x
            oy = room.y + room.h - 1 if bottom else room.y
            sx = -1 if right else 1
            sy = -1 if bottom else 1
            corner_cells.update({(ox, oy), (ox + sx, oy), (ox, oy + sy)})
            corner_anchors.append((ox + 2 * sx, oy + 2 * sy))
        candidates.append(("stepped-cross", corner_cells, tuple(corner_anchors)))

        # Asymmetric corner cuts keep the room legible while breaking the
        # generator's former mirror-everything signature.
        corner_order = [(False, False), (True, False),
                        (False, True), (True, True)]
        rng.shuffle(corner_order)
        right, bottom = corner_order[0]
        ox = room.x + room.w - 1 if right else room.x
        oy = room.y + room.h - 1 if bottom else room.y
        sx = -1 if right else 1
        sy = -1 if bottom else 1
        chamfer = {(ox, oy), (ox + sx, oy), (ox, oy + sy)}
        candidates.append(("single-chamfer", chamfer,
                           ((ox + 2 * sx, oy + 2 * sy),)))

        if room.w >= 9 and room.h >= 9:
            cut_w = min(3, room.w // 3)
            cut_h = min(3, room.h // 3)
            x0 = room.x + room.w - cut_w if right else room.x
            y0 = room.y + room.h - cut_h if bottom else room.y
            l_cut = {(x, y) for y in range(y0, y0 + cut_h)
                     for x in range(x0, x0 + cut_w)}
            candidates.append(("l-shaped", l_cut,
                               ((x0 - 1 if right else x0 + cut_w,
                                 y0 - 1 if bottom else y0 + cut_h),)))

            # Remove both corners from one end, leaving a broad T-shaped stem.
            end_bottom = rng.randrange(2) == 1
            ey = room.y + room.h - 2 if end_bottom else room.y
            t_cells = set()
            for depth in range(2):
                y = ey + (-depth if end_bottom else depth)
                for x in list(range(room.x, room.x + 2)) + list(
                        range(room.x + room.w - 2, room.x + room.w)):
                    t_cells.add((x, y))
            anchor_y = ey + (-2 if end_bottom else 2)
            candidates.append(("shallow-t", t_cells,
                               ((room.x + 2, anchor_y),
                                (room.x + room.w - 3, anchor_y))))

        # Matching mid-wall shoulders create an hourglass/paired-bay plan.
        if room.w >= 10:
            band = (range(cy - 1, cy + 2) if room.h % 2 else
                    range(room.y + room.h // 2 - 1,
                          room.y + room.h // 2 + 1))
            band = tuple(band)
            cells = ({(room.x + depth, y) for depth in (0, 1) for y in band}
                     | {(room.x + room.w - 1 - depth, y)
                        for depth in (0, 1) for y in band})
            candidates.append(("paired-side-bays", cells,
                               tuple((x, y) for y in band
                                     for x in (room.x + 2,
                                               room.x + room.w - 3))))
            side_right = rng.randrange(2) == 1
            bx = room.x + room.w - 2 if side_right else room.x
            offset_cells = {(bx + (-depth if side_right else depth), y)
                            for depth in range(2) for y in band}
            candidates.append(("offset-side-bay", offset_cells,
                               ((room.x + room.w - 3 if side_right else room.x + 2,
                                 band[len(band) // 2]),)))
        if room.h >= 10:
            band = (range(cx - 1, cx + 2) if room.w % 2 else
                    range(room.x + room.w // 2 - 1,
                          room.x + room.w // 2 + 1))
            band = tuple(band)
            cells = ({(x, room.y + depth) for depth in (0, 1) for x in band}
                     | {(x, room.y + room.h - 1 - depth)
                        for depth in (0, 1) for x in band})
            candidates.append(("paired-end-bays", cells,
                               tuple((x, y) for x in band
                                     for y in (room.y + 2,
                                               room.y + room.h - 3))))

        rng.shuffle(candidates)
        for family, walls, room_anchors in candidates:
            if family_counts[family] >= family_cap:
                continue
            if (not all(_is_floor(_at(tiles, *cell)) for cell in walls)
                    or not all(_is_floor(_at(tiles, *cell))
                               and cell not in walls for cell in room_anchors)):
                continue
            # Keep a broad central cross open; profiles are silhouettes, not
            # accidental one-tile choke generators.
            central = ({(x, cy) for x in range(room.x + 2, room.x + room.w - 2)}
                       | {(cx, y) for y in range(room.y + 2,
                                                room.y + room.h - 2)})
            if walls & central:
                continue
            for cell in walls:
                _set(tiles, *cell, WALL)
            anchors[room_index] = room_anchors
            shapes[room_index] = family
            family_counts[family] += 1
            break
    return anchors, shapes


def _carve_swastika_profile(tiles: list[int], room: Room, rng: random.Random
                            ) -> tuple[str, tuple[tuple[int, int], ...]] | None:
    """Carve one bounded, optional three-wide hooked-cross room profile."""
    if room.w < 15 or room.h < 15:
        return None
    cx, cy = room.center
    radius = 7
    handedness = rng.choice(("clockwise", "counterclockwise"))
    cells = ({(x, y) for x in range(cx - 1, cx + 2)
                     for y in range(cy - radius, cy + radius + 1)}
             | {(x, y) for x in range(cx - radius, cx + radius + 1)
                         for y in range(cy - 1, cy + 2)})
    if handedness == "clockwise":
        hooks = (
            {(x, y) for x in range(cx, cx + radius + 1)
             for y in range(cy - radius, cy - radius + 3)},
            {(x, y) for x in range(cx + radius - 2, cx + radius + 1)
             for y in range(cy, cy + radius + 1)},
            {(x, y) for x in range(cx - radius, cx + 1)
             for y in range(cy + radius - 2, cy + radius + 1)},
            {(x, y) for x in range(cx - radius, cx - radius + 3)
             for y in range(cy - radius, cy + 1)},
        )
        endpoints = ((cx + radius, cy - radius + 1),
                     (cx + radius - 1, cy + radius),
                     (cx - radius, cy + radius - 1),
                     (cx - radius + 1, cy - radius))
    else:
        hooks = (
            {(x, y) for x in range(cx - radius, cx + 1)
             for y in range(cy - radius, cy - radius + 3)},
            {(x, y) for x in range(cx + radius - 2, cx + radius + 1)
             for y in range(cy - radius, cy + 1)},
            {(x, y) for x in range(cx, cx + radius + 1)
             for y in range(cy + radius - 2, cy + radius + 1)},
            {(x, y) for x in range(cx - radius, cx - radius + 3)
             for y in range(cy, cy + radius + 1)},
        )
        endpoints = ((cx - radius, cy - radius + 1),
                     (cx + radius - 1, cy - radius),
                     (cx + radius, cy + radius - 1),
                     (cx - radius + 1, cy + radius))
    for hook in hooks:
        cells |= hook
    bounds = {(x, y) for y in range(room.y, room.y + room.h)
             for x in range(room.x, room.x + room.w)}
    if not cells <= bounds:
        return None
    for cell in bounds - cells:
        _set(tiles, *cell, WALL)
    return handedness, endpoints


def _add_pillars(tiles: list[int], room: Room, rng: random.Random,
                 chance: float = 0.4) -> None:
    if room.w < 7 or room.h < 7 or rng.random() >= chance:
        return
    cx, cy = room.center
    # Always use symmetric pairs around the room center — lone single-cell
    # placements read as map glitches rather than intentional architecture.
    patterns = []
    for dx, dy in ((1, 0), (0, 1)):
        for offset in range(1, max(room.w, room.h)):
            cells = ((cx - dx * offset, cy - dy * offset),
                     (cx + dx * offset, cy + dy * offset))
            if all(room.x + 2 <= x < room.x + room.w - 2 and
                   room.y + 2 <= y < room.y + room.h - 2 for x, y in cells):
                patterns.append(cells)
    rng.shuffle(patterns)
    for cells in patterns:
        # Four open flanks make each wall-plane column an island, never a
        # barrier; checking late also rejects spots touched by a notch.
        if all(_is_floor(_at(tiles, x, y)) and
               all(_is_floor(_at(tiles, x + dx, y + dy))
                   for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
               for x, y in cells):
            for x, y in cells:
                _set(tiles, x, y, WALL)
            return


DOOR_SPACING = 3  # minimum Manhattan gap enforced between distinct doorways


def _far_from_doors(cell: tuple[int, int], avoid: set[tuple[int, int]],
                    radius: int = DOOR_SPACING) -> bool:
    """True if cell keeps at least `radius` tiles from every already-placed
    doorway. Two doors crammed a tile or two apart -- a bare rock sliver
    between them -- read as a broken wall rather than a real room, and the
    sliver of hallway between them is a pointless loop back into the same
    room. Filtering candidates here, at threshold-selection time, is cheaper
    and more general than trying to prune finished doors after the fact."""
    return all(abs(cell[0] - ox) + abs(cell[1] - oy) >= radius for ox, oy in avoid)


def _carve_connection(tiles: list[int], a: Room, b: Room,
                      rng: random.Random, complexity: int,
                      avoid: set[tuple[int, int]] | None = None,
                      protected: set[tuple[int, int]] | None = None,
                      *, turn_penalty: int = 4) -> list[tuple[int, int]]:
    """Carve the shortest rock-backed route between two clean thresholds."""
    avoid = set() if avoid is None else avoid
    protected = set() if protected is None else protected

    def portals(room: Room) -> list[tuple[tuple[int, int], tuple[int, int],
                                           tuple[int, int], tuple[int, int]]]:
        result = []
        sides = [((room.x - 1, y), (room.x, y), (-1, 0))
                 for y in range(room.y + 1, room.y + room.h - 1)]
        sides += [((room.x + room.w, y), (room.x + room.w - 1, y), (1, 0))
                  for y in range(room.y + 1, room.y + room.h - 1)]
        sides += [((x, room.y - 1), (x, room.y), (0, -1))
                  for x in range(room.x + 1, room.x + room.w - 1)]
        sides += [((x, room.y + room.h), (x, room.y + room.h - 1), (0, 1))
                  for x in range(room.x + 1, room.x + room.w - 1)]
        for outer, inner, (dx, dy) in sides:
            beyond = outer[0] + dx, outer[1] + dy
            jambs = ((outer[0] - dy, outer[1] - dx),
                     (outer[0] + dy, outer[1] + dx))
            if (_is_floor(_at(tiles, *inner)) and _at(tiles, *outer) == WALL
                    and _at(tiles, *beyond) == WALL
                    and all(_at(tiles, *cell) == WALL for cell in jambs)
                    and _far_from_doors(outer, avoid)):
                result.append((outer, beyond, inner, (dx, dy)))
        return result

    def portal_centering(portal: tuple[tuple[int, int], tuple[int, int],
                                       tuple[int, int], tuple[int, int]],
                         room: Room) -> float:
        outer, _, _, direction = portal
        if direction[0]:
            return abs(outer[1] - (room.y + (room.h - 1) / 2))
        return abs(outer[0] - (room.x + (room.w - 1) / 2))

    def estimated_bends(pa: tuple[tuple[int, int], tuple[int, int],
                                  tuple[int, int], tuple[int, int]],
                        pb: tuple[tuple[int, int], tuple[int, int],
                                  tuple[int, int], tuple[int, int]]) -> int:
        outer_a, _, _, direction_a = pa
        outer_b, _, _, direction_b = pb
        dx, dy = outer_b[0] - outer_a[0], outer_b[1] - outer_a[1]
        if ((dx == 0 or dy == 0) and direction_a == (-direction_b[0], -direction_b[1])
                and (dx * direction_a[0] > 0 or dy * direction_a[1] > 0)):
            return 0
        if direction_a[0] != direction_b[0] and direction_a[1] != direction_b[1]:
            if direction_a[0]:
                forward_a = dx * direction_a[0] >= 0
                forward_b = -dy * direction_b[1] >= 0
            else:
                forward_a = dy * direction_a[1] >= 0
                forward_b = -dx * direction_b[0] >= 0
            if forward_a and forward_b:
                return 1
        return 2

    pairs = [(pa, pb) for pa in portals(a) for pb in portals(b)]
    rng.shuffle(pairs)
    pairs.sort(key=lambda pair: (
        estimated_bends(*pair),
        abs(pair[0][0][0] - pair[1][0][0]) + abs(pair[0][0][1] - pair[1][0][1]),
        portal_centering(pair[0], a) + portal_centering(pair[1], b),
    ))
    directions = [(1, 0), (-1, 0), (0, 1), (0, -1)]
    rng.shuffle(directions)
    def find_route(start: tuple[int, int], goal: tuple[int, int],
                   start_heading: tuple[int, int],
                   goal_heading: tuple[int, int]) -> list[tuple[int, int]] | None:
        start_state = start, start_heading
        previous: dict[tuple[tuple[int, int], tuple[int, int]],
                       tuple[tuple[int, int], tuple[int, int]] | None] = {start_state: None}
        dist = {start_state: 0}
        queue = [(0, 0, start, start_heading)]
        sequence = 1
        best_goal_state = None
        best_goal_cost = math.inf
        while queue:
            cost, _, (x, y), heading = heapq.heappop(queue)
            state = (x, y), heading
            if cost != dist[state]:
                continue
            if cost >= best_goal_cost:
                break
            if (x, y) == goal:
                # A goal state popped later can carry a cheaper raw cost but a
                # worse final-heading total; never let it displace a better one.
                total = cost + (0 if heading == goal_heading else turn_penalty)
                if total < best_goal_cost:
                    best_goal_cost = total
                    best_goal_state = state
                continue
            for dx, dy in directions:
                nx, ny = x + dx, y + dy
                if not (2 <= nx < GRID - 2 and 2 <= ny < GRID - 2):
                    continue
                nxt = nx, ny
                base = ny * GRID + nx
                if nxt in protected or tiles[base] != WALL:
                    continue
                # A one-rock buffer stops unrelated routes and rooms from
                # silently fusing before their planned door can separate them.
                # Indexed directly rather than through _at: nx and ny are
                # already clamped to [2, GRID - 3], so every neighbour below is
                # in bounds and _at's guard could never fire. _FLOOR_OR_DOOR
                # folds the two predicates this used to call twice per direction
                # into one table lookup.
                if nxt != goal and (_FLOOR_OR_DOOR[tiles[base - GRID]]
                                    or _FLOOR_OR_DOOR[tiles[base + GRID]]
                                    or _FLOOR_OR_DOOR[tiles[base - 1]]
                                    or _FLOOR_OR_DOOR[tiles[base + 1]]):
                    continue
                next_state = nxt, (dx, dy)
                next_cost = cost + 1 + (turn_penalty if (dx, dy) != heading else 0)
                if next_cost >= dist.get(next_state, math.inf):
                    continue
                dist[next_state] = next_cost
                previous[next_state] = state
                heapq.heappush(queue, (next_cost, sequence, nxt, (dx, dy)))
                sequence += 1
        if best_goal_state is None:
            return None
        state = best_goal_state
        route = []
        while state is not None:
            route.append(state[0]); state = previous[state]
        route.reverse()
        return route

    # Cheap clean thresholds are common. Try the best centered/bend-minimal
    # authored portals, then use the seam-safe relaxed router below. Exhausting
    # hundreds of nearly equivalent portal pairs makes dense floor-10 plans
    # pathologically slow without discovering a qualitatively different hall.
    for (outer_a, start, _, direction_a), (outer_b, goal, _, direction_b) in pairs[:64]:
        route = find_route(start, goal, direction_a, (-direction_b[0], -direction_b[1]))
        if route is None:
            continue
        direct = abs(start[0] - goal[0]) + abs(start[1] - goal[1])
        if len(route) > math.ceil(direct * 1.6) + 6:
            continue
        path = [outer_a] + route + [outer_b]
        for x, y in path:
            _set(tiles, x, y, FLOOR)
        avoid.update((outer_a, outer_b))
        return path

    source = {a.center}
    queue = deque(source)
    while queue:
        x, y = queue.popleft()
        for dx, dy in directions:
            nxt = x + dx, y + dy
            if nxt not in source and (_is_floor(_at(tiles, *nxt))
                                      or _at(tiles, *nxt) in DOORS):
                source.add(nxt); queue.append(nxt)
    thresholds = []
    for y in range(2, GRID - 2):
        for x in range(2, GRID - 2):
            if (x, y) in protected or _at(tiles, x, y) != WALL:
                continue
            contacts = [(dx, dy) for dx, dy in directions
                        if (x + dx, y + dy) in source]
            if len(contacts) != 1:
                continue
            dx, dy = contacts[0]
            beyond = x - dx, y - dy
            jambs = ((x - dy, y - dx), (x + dy, y + dx))
            if (_at(tiles, *beyond) == WALL
                    and all(_at(tiles, *cell) == WALL for cell in jambs)
                    and _far_from_doors((x, y), avoid)):
                thresholds.append(((x, y), (dx, dy)))
    rng.shuffle(thresholds)
    thresholds.sort(key=lambda item: abs(item[0][0] - b.center[0])
                    + abs(item[0][1] - b.center[1]))

    def threshold_route(start: tuple[int, int],
                        source_side: tuple[int, int]) -> list[tuple[int, int]] | None:
        previous: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
        queue = deque([start])
        while queue:
            x, y = queue.popleft()
            for dx, dy in directions:
                if (x, y) == start and (dx, dy) != (-source_side[0],
                                                        -source_side[1]):
                    continue
                nxt = x + dx, y + dy
                if (nxt in previous or nxt in protected
                        or not (2 <= nxt[0] < GRID - 2
                                             and 2 <= nxt[1] < GRID - 2)
                        or _at(tiles, *nxt) != WALL):
                    continue
                contacts = [(sx, sy) for sx, sy in directions
                            if (_is_floor(_at(tiles, nxt[0] + sx, nxt[1] + sy))
                                or _at(tiles, nxt[0] + sx, nxt[1] + sy) in DOORS)]
                target = [(sx, sy) for sx, sy in contacts
                          if (b.x <= nxt[0] + sx < b.x + b.w
                              and b.y <= nxt[1] + sy < b.y + b.h)]
                if contacts:
                    # The target contact is only usable head-on: the
                    # untouched side rocks become jambs for this exact seam.
                    if (len(contacts) != 1 or len(target) != 1
                            or (x, y) != (nxt[0] - target[0][0],
                                         nxt[1] - target[0][1])):
                        continue
                    jambs = ((nxt[0] - target[0][1], nxt[1] - target[0][0]),
                             (nxt[0] + target[0][1], nxt[1] + target[0][0]))
                    if any(cell in previous or _at(tiles, *cell) != WALL
                           for cell in jambs):
                        continue
                    previous[nxt] = (x, y)
                    route = []
                    cell: tuple[int, int] | None = nxt
                    while cell is not None:
                        route.append(cell); cell = previous[cell]
                    route.reverse()
                    for cell in route[:-1]:
                        _set(tiles, *cell, FLOOR)
                    _set(tiles, *nxt, DOOR_EW if target[0][0] else DOOR_NS)
                    avoid.add(nxt)
                    return route[1:]
                previous[nxt] = (x, y); queue.append(nxt)
        return None

    # A relaxed route joins the intended room from the whole source component;
    # its exact target threshold is doored instead of blended into the room.
    for start, source_side in thresholds:
        path = threshold_route(start, source_side)
        if path is not None:
            return path
    # If the safe loop budget is exhausted, keep the existing reconvergence;
    # forcing a center-line duplicate only opens a redundant sightline.
    if b.center in source:
        return []
    # The true last resort may cross built components, but every transition
    # is head-on through a rock cell that becomes a door, never open floor.
    existing_open = {(x, y) for y in range(GRID) for x in range(GRID)
                     if _is_floor(_at(tiles, x, y)) or _at(tiles, x, y) in DOORS}

    def open_cell(cell: tuple[int, int]) -> bool:
        return cell in existing_open

    start_state = (a.center, (0, 0), False)
    previous = {start_state: None}
    queue = deque([start_state])
    goal_state = None
    while queue and goal_state is None:
        (x, y), heading, forced = queue.popleft()
        current_open = open_cell((x, y))
        for dx, dy in directions:
            if forced and (dx, dy) != heading:
                continue
            nxt = x + dx, y + dy
            if not (2 <= nxt[0] < GRID - 2 and 2 <= nxt[1] < GRID - 2):
                continue
            nxt_open = open_cell(nxt)
            if (not nxt_open and (nxt in protected
                                  or _at(tiles, *nxt) != WALL)):
                continue
            contacts = {(nxt[0] + sx, nxt[1] + sy) for sx, sy in directions
                        if open_cell((nxt[0] + sx, nxt[1] + sy))}
            if current_open and not nxt_open:
                axis = {(x, y), (nxt[0] + dx, nxt[1] + dy)}
                if (x, y) not in contacts or not contacts <= axis:
                    continue
                state = (nxt, (dx, dy), True)
            elif not current_open and nxt_open:
                current_contacts = {(x + sx, y + sy) for sx, sy in directions
                                    if open_cell((x + sx, y + sy))}
                axis = {nxt, (x - dx, y - dy)}
                if (dx, dy) != heading or nxt not in current_contacts or not current_contacts <= axis:
                    continue
                state = (nxt, (dx, dy), False)
            elif not current_open:
                ahead = (nxt[0] + dx, nxt[1] + dy)
                if contacts and contacts != {ahead}:
                    continue
                state = (nxt, (dx, dy), False)
            else:
                state = (nxt, (dx, dy), False)
            if state in previous:
                continue
            previous[state] = ((x, y), heading, forced)
            if nxt == b.center:
                goal_state = state
                break
            queue.append(state)
    if goal_state is None:
        raise ValueError("fallback corridor cannot preserve door seams")
    route = []
    state = goal_state
    while state is not None:
        route.append(state[0]); state = previous[state]
    route.reverse()
    direct = abs(a.center[0] - b.center[0]) + abs(a.center[1] - b.center[1])
    if len(route) > math.ceil(direct * 1.8) + 8:
        raise ValueError("fallback corridor is an excessive perimeter wrap")
    carved = []
    for index, cell in enumerate(route[1:-1], 1):
        if open_cell(cell):
            continue
        contacts = [neighbor for neighbor in ((cell[0] + 1, cell[1]),
                                               (cell[0] - 1, cell[1]),
                                               (cell[0], cell[1] + 1),
                                               (cell[0], cell[1] - 1))
                    if open_cell(neighbor)]
        if contacts:
            before, after = route[index - 1], route[index + 1]
            code = DOOR_NS if before[0] == cell[0] == after[0] else DOOR_EW
            _set(tiles, *cell, code)
            avoid.add(cell)
        else:
            _set(tiles, *cell, FLOOR)
        carved.append(cell)
    return carved


def _adjacent_to_room(rooms: list[Room], x: int, y: int) -> bool:
    return any(_inside_room(rooms, nx, ny)
               for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))


def _widen_corridors(tiles: list[int], rooms: list[Room], paths: list[list[tuple[int, int]]],
                     rng: random.Random, widen_chance: float = 0.8,
                     protected: set[tuple[int, int]] | None = None) -> None:
    """A map built entirely from 1-tile halls reads as door-camping and rush
    traps. Widen eligible straight runs symmetrically from one tile to three,
    but leave doorway thresholds, bends, constrained runs, and short service
    connectors pinched to one tile. A failed symmetric widening leaves both
    sides untouched, so the generator never emits accidental 2-wide halls."""
    protected = set() if protected is None else protected
    for path in paths:
        if len(path) < 6 or rng.random() > widen_chance:
            continue
        for i in range(1, len(path) - 1):
            x, y = path[i]
            if _inside_room(rooms, x, y) or _adjacent_to_room(rooms, x, y):
                continue
            if any(_at(tiles, x + dx, y + dy) in DOORS
                   for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                continue
            px, py = path[i - 1]
            nx, ny = path[i + 1]
            horizontal = (px != x) or (nx != x)
            vertical = (py != y) or (ny != y)
            if horizontal and not vertical:
                wings = ((x, y - 1), (x, y + 1))
            elif vertical and not horizontal:
                wings = ((x - 1, y), (x + 1, y))
            else:
                continue
            if any(_inside_room(rooms, wx, wy)
                   or _adjacent_to_room(rooms, wx, wy) for wx, wy in wings):
                continue
            if any(_at(tiles, wx + dx, wy + dy) in DOORS
                   for wx, wy in wings
                   for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                continue
            if (not any(cell in protected for cell in wings)
                    and all(_at(tiles, wx, wy) == WALL for wx, wy in wings)):
                for wx, wy in wings:
                    _set(tiles, wx, wy, FLOOR)


def _door_axis(tiles: list[int], x: int, y: int) -> int | None:
    """DOOR_EW/DOOR_NS if (x, y) is a one-tile-wide floor chokepoint with an
    unambiguous axis (floor on both sides along one axis, solid and
    door-free on both sides along the other), else None. The jamb sides
    must exclude doors too, not just floor: this also runs after other
    doors already exist on the map (see _split_oversized_zones), and a door
    sitting in another door's jamb is exactly the "bypassed around its
    jamb" case validate_door_axes rejects."""
    def blocked(v: int) -> bool:
        return not _is_floor(v) and v not in DOORS
    horizontal = _is_floor(_at(tiles, x - 1, y)) and _is_floor(_at(tiles, x + 1, y))
    vertical = _is_floor(_at(tiles, x, y - 1)) and _is_floor(_at(tiles, x, y + 1))
    walls_ns = blocked(_at(tiles, x, y - 1)) and blocked(_at(tiles, x, y + 1))
    walls_ew = blocked(_at(tiles, x - 1, y)) and blocked(_at(tiles, x + 1, y))
    if horizontal and walls_ns:
        return DOOR_EW
    if vertical and walls_ew:
        return DOOR_NS
    return None


def _door_candidate(tiles: list[int], rooms: list[Room],
                    path: list[tuple[int, int]]) -> tuple[int, int, int] | None:
    """Find a one-tile-wide corridor cell with an unambiguous door axis.

    A cell touching a room reads as a real threshold; a chokepoint stranded
    mid-corridor just interrupts an otherwise exposed hallway for no visible
    reason, so it's only used when the path has no room-adjacent option.
    """
    fallback = None
    for x, y in path:
        if _inside_room(rooms, x, y) or not _is_floor(_at(tiles, x, y)):
            continue
        axis = _door_axis(tiles, x, y)
        if not axis:
            continue
        if _adjacent_to_room(rooms, x, y):
            return x, y, axis
        fallback = fallback or (x, y, axis)
    return fallback


def _spatial_districts(rooms: list[Room], k: int) -> list[int]:
    """Re-label rooms into count-balanced geometric districts.

    Planning assigns districts along the progression spine before rooms have
    coordinates.  The theme pass benefits instead from nearby rooms sharing
    a district, so split the wider placed axis into contiguous rank groups.
    """
    if not rooms or k <= 1:
        return [0] * len(rooms)
    centers = [room.center for room in rooms]
    x_spread = max(x for x, _ in centers) - min(x for x, _ in centers)
    y_spread = max(y for _, y in centers) - min(y for _, y in centers)
    axis = 0 if x_spread >= y_spread else 1
    ranked = sorted(range(len(rooms)), key=lambda index: (centers[index][axis], index))
    districts = [0] * len(rooms)
    for rank, index in enumerate(ranked):
        districts[index] = rank * k // len(rooms)
    return districts


def _limit_theme_merge_size(tiles: list[int], rooms: list[Room], rng: random.Random,
                            reserved: set[tuple[int, int]],
                            cap_fraction: float = 0.50,
                            max_conversions: int = 2) -> int:
    """Door off a few leak walls that would otherwise join huge theme groups.

    _assign_area_themes must merge every pair of floor components touching a
    bare wall: leaving that rule intact is what prevents materials leaking
    across a thin undoored seam.  This earlier pass only turns a handful of
    useful, valid chokepoint seams into real doors, prioritising bridges that
    divide the largest resulting theme group most evenly.
    """
    if not rooms:
        return 0
    placed = 0
    door_zones = {(x, y) for y in range(GRID) for x in range(GRID)
                  if _at(tiles, x, y) in DOORS}
    while placed < max_conversions:
        components = _floor_components(tiles)
        total = sum(map(len, components))
        if not total:
            break
        owner = {cell: index for index, component in enumerate(components)
                 for cell in component}

        # The full edge map mirrors _assign_area_themes.  A component pair
        # can have several legal door cells; retain all of them so selection
        # can pick a randomized physical seam after choosing the graph edge.
        edge_cells: dict[tuple[int, int], list[tuple[int, int]]] = {}
        for index, tile in enumerate(tiles):
            if tile != WALL:
                continue
            x, y = index % GRID, index // GRID
            neighbors = sorted({owner[cell] for cell in ((x + 1, y), (x - 1, y),
                                                          (x, y + 1), (x, y - 1))
                                if cell in owner})
            candidate_pair = None
            axis = _door_axis(tiles, x, y)
            if axis and (x, y) not in reserved and _far_from_doors((x, y), door_zones):
                dx, dy = (1, 0) if axis == DOOR_EW else (0, 1)
                first = owner.get((x - dx, y - dy))
                second = owner.get((x + dx, y + dy))
                if first is not None and second is not None and first != second:
                    candidate_pair = tuple(sorted((first, second)))
            for first, second in combinations(neighbors, 2):
                edge = first, second
                edge_cells.setdefault(edge, [])
                if edge == candidate_pair:
                    edge_cells[edge].append((x, y))

        # This is deliberately after secrets and locks are complete.  A new
        # door can be far from a pushwall yet open its protected back room,
        # or can reach the far side of an existing lock.  Recognize the
        # finalized pushwall shape from its reserved approach cell and reject
        # only candidates that create one of those new routes.
        start = rooms[0].center
        open_before = _reachable(tiles, start, locked_open=True)
        locked_before = _reachable(tiles, start, locked_open=False)
        pushwalls = {(x + 1, y) for x, y in reserved
                     if (_at(tiles, x + 1, y) == WALL
                         and _is_floor(_at(tiles, x, y))
                         and all(_is_floor(_at(tiles, x + step, y)) for step in (2, 3))
                         and _at(tiles, x + 1, y - 1) == WALL
                         and _at(tiles, x + 1, y + 1) == WALL)}
        lock_sides = {(x + dx, y + dy)
                      for index, tile in enumerate(tiles) if tile in (DOOR_GOLD_EW, 93)
                      for x, y in ((index % GRID, index // GRID),)
                      for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))}

        def preserves_gates(cell: tuple[int, int]) -> bool:
            axis = _door_axis(tiles, *cell)
            assert axis is not None
            _set(tiles, *cell, axis)
            open_after = _reachable(tiles, start, locked_open=True)
            locked_after = _reachable(tiles, start, locked_open=False)
            opens_secret = any((wall[0] + 1, wall[1]) not in open_before
                               and (wall[0] + 1, wall[1]) in open_after
                               for wall in pushwalls)
            crosses_lock = bool(lock_sides & (locked_after - locked_before))
            _set(tiles, *cell, WALL)
            return not opens_secret and not crosses_lock

        parents = list(range(len(components)))

        def find(component: int) -> int:
            while parents[component] != component:
                parents[component] = parents[parents[component]]
                component = parents[component]
            return component

        def union(first: int, second: int) -> None:
            first, second = find(first), find(second)
            if first != second:
                parents[second] = first

        for first, second in edge_cells:
            union(first, second)
        groups: dict[int, list[int]] = {}
        for component in range(len(components)):
            groups.setdefault(find(component), []).append(component)
        largest, nodes = max(groups.items(),
                             key=lambda item: sum(len(components[node]) for node in item[1]))
        largest_size = sum(len(components[node]) for node in nodes)
        if largest_size <= total * cap_fraction:
            break

        node_set = set(nodes)
        best_imbalance = None
        best_edges: list[tuple[int, int]] = []
        for removed, candidates in edge_cells.items():
            first, second = removed
            if not candidates or find(first) != largest or find(second) != largest:
                continue
            links = {node: set() for node in nodes}
            for (left, right) in edge_cells:
                if (left, right) == removed or left not in node_set or right not in node_set:
                    continue
                links[left].add(right); links[right].add(left)
            seen = {first}
            queue = deque([first])
            while queue:
                node = queue.popleft()
                for neighbor in links[node] - seen:
                    seen.add(neighbor); queue.append(neighbor)
            if len(seen) == len(nodes):
                continue
            first_size = sum(len(components[node]) for node in seen)
            second_size = largest_size - first_size
            imbalance = abs(first_size - second_size)
            if best_imbalance is None or imbalance < best_imbalance:
                best_imbalance = imbalance
                best_edges = [removed]
            elif imbalance == best_imbalance:
                best_edges.append(removed)
        if not best_edges:
            break
        cell = None
        unchecked_edges = list(best_edges)
        while unchecked_edges and cell is None:
            edge = rng.choice(unchecked_edges)
            unchecked_edges.remove(edge)
            cells = list(edge_cells[edge])
            while cells and cell is None:
                candidate = rng.choice(cells)
                cells.remove(candidate)
                if preserves_gates(candidate):
                    cell = candidate
        if cell is None:
            break
        _set(tiles, *cell, _door_axis(tiles, *cell))
        ledger_reserve(reserved, [cell], "geometry",
                       "sightline-repair-pillar")
        door_zones.add(cell)
        placed += 1
    return placed


def _split_oversized_zones(tiles: list[int], rooms: list[Room], rng: random.Random,
                           reserved: set[tuple[int, int]],
                           cap: int = 110, min_piece: int = 12) -> int:
    """Corridors carved for unrelated room-to-room connections often end up
    flush against each other -- crossing, running alongside, or just
    touching -- at points no edge's own path ever scanned as a door
    junction (see _place_doors). Left alone, that stray adjacency silently
    fuses several rooms' floor into one blob with no door anywhere inside
    it, so _assign_sound_zones hands the whole blob a single zone id and
    one gunshot alerts every guard in every room it happens to include.

    Hunt down genuine one-tile chokepoints inside any oversized component
    and door off the ones that actually cut it into substantial pieces,
    rather than nibbling off tiny dead-end nooks."""
    placed = 0
    stuck: set[frozenset[tuple[int, int]]] = set()
    # This pass runs after every real door is already on the map, so a fresh
    # doorway placed here is just as prone to landing a tile or two from an
    # existing one as anything _carve_connection carves; keep it under the
    # same minimum spacing.
    door_zones = {(x, y) for y in range(GRID) for x in range(GRID)
                  if _at(tiles, x, y) in DOORS}
    while True:
        components = _floor_components(tiles)
        if len(components) >= ZONE_MAX - FLOOR + 1:
            break
        component = next((c for c in components
                          if len(c) > cap and frozenset(c) not in stuck), None)
        if component is None:
            break
        candidates = [(x, y) for x, y in component
                     if (x, y) not in reserved and not _inside_room(rooms, x, y)
                     and _door_axis(tiles, x, y) and _far_from_doors((x, y), door_zones)]
        rng.shuffle(candidates)
        # Room-adjacent chokepoints read as a real doorway; try those before
        # falling back to a stray mid-corridor pinch (same reasoning as
        # _door_candidate).
        candidates.sort(key=lambda cell: not _adjacent_to_room(rooms, *cell))
        split = False
        for x, y in candidates:
            remaining = component - {(x, y)}
            probe = next(iter(remaining))
            seen = {probe}
            queue = deque([probe])
            while queue:
                cx, cy = queue.popleft()
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nxt = (cx + dx, cy + dy)
                    if nxt in remaining and nxt not in seen:
                        seen.add(nxt); queue.append(nxt)
            other = len(remaining) - len(seen)
            if len(seen) >= min_piece and other >= min_piece:
                _set(tiles, x, y, _door_axis(tiles, x, y))
                door_zones.add((x, y))
                placed += 1
                split = True
                break
        if not split:
            stuck.add(frozenset(component))
    return placed


def _remove_redundant_plain_doors(tiles: list[int]) -> int:
    """Remove plain doors whose two sides already share a floor component.

    Room notches can make a second, tiny walkaround beside the corridor
    chokepoint where _place_doors installed the real doorway.  Those gaps
    are deliberately too small for _split_oversized_zones to door off, so
    leave the open route and remove the now-purely-cosmetic plain door.
    Locked and elevator doors have separate gating invariants and are not
    considered here.
    """
    components = _floor_components(tiles)
    owner = {cell: index for index, component in enumerate(components) for cell in component}
    removed = 0
    for index, tile in enumerate(tiles):
        if tile not in (DOOR_EW, DOOR_NS):
            continue
        x, y = index % GRID, index // GRID
        dx, dy = (1, 0) if tile % 2 == 0 else (0, 1)
        before = owner.get((x - dx, y - dy))
        after = owner.get((x + dx, y + dy))
        if before is not None and before == after:
            _set(tiles, x, y, FLOOR)
            removed += 1
    return removed


def _heal_pinched_room_door_pairs(tiles: list[int], rooms: list[Room],
                                  start: tuple[int, int],
                                  pushwalls: set[tuple[int, int]],
                                  max_blob: int = 8, max_jog: int = 4) -> int:
    """Collapse a tight double-doorway into a single clean threshold.

    A corridor that clips a pinched room corner can leave two plain doors a
    few tiles apart both opening into the same room, each kept load-bearing
    only by the room's own internal notch (the corridor threads in one door,
    across the room's own floor, and back out the other). That reads as a
    redundant pair even though neither door is individually removable.

    Where a single interior wall cell reconnects the room across its notch,
    open it and seal one of the doors so the room presents one threshold and
    the stub becomes a plain alcove. Only short corridor stubs with closely
    spaced doors are touched; a wide blob or a widely separated pair is a
    deliberate double entrance and is left alone. Every edit is guarded by a
    full-reachability check, so nothing is ever stranded. Uses no rng, so the
    shared generation stream is untouched.
    """
    room_of: dict[tuple[int, int], int] = {}
    for index, room in enumerate(rooms):
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                if _is_floor(_at(tiles, x, y)):
                    room_of[(x, y)] = index

    def corridor(x: int, y: int) -> bool:
        value = _at(tiles, x, y)
        return value != -1 and _is_floor(value) and (x, y) not in room_of

    blob_of: dict[tuple[int, int], int] = {}
    blobs: list[list[tuple[int, int]]] = []
    for y in range(GRID):
        for x in range(GRID):
            if corridor(x, y) and (x, y) not in blob_of:
                component = []
                queue = deque([(x, y)])
                blob_of[(x, y)] = len(blobs)
                while queue:
                    cx, cy = queue.popleft()
                    component.append((cx, cy))
                    for nx, ny in ((cx + 1, cy), (cx - 1, cy),
                                   (cx, cy + 1), (cx, cy - 1)):
                        if corridor(nx, ny) and (nx, ny) not in blob_of:
                            blob_of[(nx, ny)] = len(blobs)
                            queue.append((nx, ny))
                blobs.append(component)

    pair_doors: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for index, tile in enumerate(tiles):
        if tile not in (DOOR_EW, DOOR_NS):
            continue
        x, y = index % GRID, index // GRID
        dx, dy = (1, 0) if tile == DOOR_EW else (0, 1)
        for room_side, corr_side in (((x - dx, y - dy), (x + dx, y + dy)),
                                     ((x + dx, y + dy), (x - dx, y - dy))):
            if room_side in room_of and corr_side in blob_of:
                key = (blob_of[corr_side], room_of[room_side])
                pair_doors.setdefault(key, []).append((x, y))

    baseline = _reachable(tiles, start, locked_open=True,
                          extra_passable=pushwalls)
    healed = 0
    for (blob_index, room_index), doors in pair_doors.items():
        if len(doors) < 2 or len(blobs[blob_index]) > max_blob:
            continue
        jog = max(abs(a[0] - b[0]) + abs(a[1] - b[1])
                  for i, a in enumerate(doors) for b in doors[i + 1:])
        if jog > max_jog:
            continue
        room = rooms[room_index]
        # Interior wall cells whose only floor neighbours all belong to this
        # room: opening one heals the room's own notch without bridging into a
        # corridor or a neighbouring room.
        pinches: list[tuple[int, int] | None] = [None]
        for py in range(room.y, room.y + room.h):
            for px in range(room.x, room.x + room.w):
                if _at(tiles, px, py) != WALL:
                    continue
                floor_neighbours = [(px + ddx, py + ddy)
                                    for ddx, ddy in ((1, 0), (-1, 0),
                                                     (0, 1), (0, -1))
                                    if _is_floor(_at(tiles, px + ddx, py + ddy))]
                if (len(floor_neighbours) >= 2
                        and all(room_of.get(cell) == room_index
                                for cell in floor_neighbours)):
                    pinches.append((px, py))
        resolved = False
        for seal in doors:
            if resolved:
                break
            for pinch in pinches:
                trial = list(tiles)
                if pinch is not None:
                    trial[pinch[1] * GRID + pinch[0]] = FLOOR
                trial[seal[1] * GRID + seal[0]] = WALL
                reach = _reachable(trial, start, locked_open=True,
                                   extra_passable=pushwalls)
                if baseline - {seal} <= reach:
                    if pinch is not None:
                        _set(tiles, pinch[0], pinch[1], FLOOR)
                    _set(tiles, seal[0], seal[1], WALL)
                    healed += 1
                    resolved = True
                    break
    return healed


def _assign_sound_zones(tiles: list[int]) -> int:
    """Give each door-separated floor component its own ECWolf MapZone.

    Floor code 107 is skipped: it is the secret-exit modzone and must keep
    its exact value for the translator to rewrite the adjacent switch."""
    components = _floor_components(tiles)
    if len(components) > ZONE_MAX - FLOOR + 1:
        raise ValueError("sound-zone budget exceeded")
    for zone_count, component in enumerate(components):
        zone = FLOOR + zone_count
        for x, y in component:
            _set(tiles, x, y, zone)
    return len(components)


def _break_long_sightlines(tiles: list[int], things: list[int], rooms: list[Room],
                           reserved: set[tuple[int, int]], rng: random.Random,
                           start: tuple[int, int],
                           max_run: int = 21,
                           allow_doors: bool = True,
                           walls_for_redundant_doors: bool = False) -> int:
    centers = {room.center for room in rooms}
    doors = {(x, y) for y in range(GRID) for x in range(GRID)
             if _at(tiles, x, y) in DOORS}

    def runs() -> list[list[tuple[int, int]]]:
        found = []
        for horizontal in (True, False):
            for fixed in range(GRID):
                run: list[tuple[int, int]] = []
                for moving in range(GRID + 1):
                    x, y = ((moving, fixed) if horizontal else (fixed, moving))
                    if moving < GRID and _is_floor(_at(tiles, x, y)):
                        run.append((x, y))
                    else:
                        if len(run) > max_run:
                            found.append(run)
                        run = []
        return found

    placed = 0
    while True:
        baseline = _reachable(tiles, start, locked_open=True)
        changed = False
        for run in runs():
            midpoint = (len(run) - 1) / 2
            candidates = list(enumerate(run))
            rng.shuffle(candidates)
            candidates.sort(key=lambda item: abs(item[0] - midpoint))
            for _, (x, y) in candidates:
                if (x, y) in centers or (x, y) in reserved or _at(things, x, y):
                    continue
                if (x, y) not in baseline:
                    continue
                if any(abs(x - dx) <= 1 and abs(y - dy) <= 1 for dx, dy in doors):
                    continue
                # Open flanks keep cover as an island while the middle bias
                # breaks the most exposed portion of the lane first.
                if not all(_is_floor(_at(tiles, x + dx, y + dy))
                           for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1))):
                    continue
                original = _at(tiles, x, y)
                _set(tiles, x, y, WALL)
                if _reachable(tiles, start, locked_open=True) != baseline - {(x, y)}:
                    _set(tiles, x, y, original)
                    continue
                # Try to add a perpendicular companion so the break reads as
                # an intentional 1×2 pillar rather than a lone floating wall.
                run_horiz = (run[0][1] == run[-1][1])
                companion_dirs = ((0, 1), (0, -1)) if run_horiz else ((1, 0), (-1, 0))
                for cdx, cdy in companion_dirs:
                    cx2, cy2 = x + cdx, y + cdy
                    orig2 = _at(tiles, cx2, cy2)
                    if ((cx2, cy2) not in centers and (cx2, cy2) not in reserved
                            and not _at(things, cx2, cy2)
                            and _is_floor(orig2)
                            and all(_is_floor(_at(tiles, cx2 + ddx, cy2 + ddy))
                                    for ddx, ddy in ((1, 0), (-1, 0), (0, 1), (0, -1)))):
                        _set(tiles, cx2, cy2, WALL)
                        if _reachable(tiles, start, locked_open=True) == (baseline - {(x, y)}) - {(cx2, cy2)}:
                            placed += 1  # companion succeeded
                        else:
                            _set(tiles, cx2, cy2, orig2)  # companion blocked reachability
                        break
                placed += 1
                changed = True
                break
            if changed:
                break
            for _, (x, y) in candidates:
                if not allow_doors and not walls_for_redundant_doors:
                    continue
                axis = _door_axis(tiles, x, y)
                if (not axis or (x, y) in centers or (x, y) in reserved
                        or _at(things, x, y) or _inside_room(rooms, x, y)
                        or any(abs(x - dx) <= 1 and abs(y - dy) <= 1
                               for dx, dy in doors)):
                    continue
                if walls_for_redundant_doors:
                    dx, dy = (1, 0) if axis % 2 == 0 else (0, 1)
                    components = _floor_components(tiles)
                    owner = {cell: index for index, component in enumerate(components)
                             for cell in component}
                    before = owner.get((x - dx, y - dy))
                    after = owner.get((x + dx, y + dy))
                    if before is not None and before == after:
                        original = _at(tiles, x, y)
                        _set(tiles, x, y, WALL)
                        if _reachable(tiles, start, locked_open=True) == baseline - {(x, y)}:
                            placed += 1; changed = True
                            break
                        _set(tiles, x, y, original)
                if not allow_doors:
                    continue
                _set(tiles, x, y, axis)
                doors.add((x, y)); placed += 1; changed = True
                break
            if changed:
                break
            if not allow_doors:
                continue
            vertical = run[0][0] == run[-1][0]
            for _, (x, y) in candidates:
                sides = ((1, 0), (-1, 0)) if vertical else ((0, 1), (0, -1))
                for sx, sy in sides:
                    wall_cell = x + sx, y + sy
                    outer = x - sx, y - sy
                    far = x + 2 * sx, y + 2 * sy
                    along = ((0, 1), (0, -1)) if vertical else ((1, 0), (-1, 0))
                    if ({(x, y), wall_cell} & (centers | reserved)
                            or _at(things, x, y) or _at(things, *wall_cell)
                            or _inside_room(rooms, x, y) or _inside_room(rooms, *wall_cell)
                            or any(abs(x - dx) <= 1 and abs(y - dy) <= 1
                                   for dx, dy in doors)
                            or _at(tiles, *outer) != WALL or _at(tiles, *far) != WALL
                            or not all(_is_floor(_at(tiles, x + dx, y + dy))
                                       and _is_floor(_at(tiles, wall_cell[0] + dx,
                                                        wall_cell[1] + dy))
                                       for dx, dy in along)):
                        continue
                    wall_original = _at(tiles, *wall_cell)
                    door_original = _at(tiles, x, y)
                    _set(tiles, *wall_cell, WALL)
                    _set(tiles, x, y, DOOR_NS if vertical else DOOR_EW)
                    if _reachable(tiles, start, locked_open=True) != baseline - {wall_cell}:
                        _set(tiles, *wall_cell, wall_original)
                        _set(tiles, x, y, door_original)
                        continue
                    # A wall-and-door crossbar is the safe repair for a
                    # two-wide hall where an island pillar cannot fit.
                    doors.add((x, y)); placed += 1; changed = True
                    break
                if changed:
                    break
            if changed:
                break
        if not changed:
            return placed


def _harvest_sky_vistas(tiles: list[int], things: list[int]) -> tuple[
        tuple[tuple[tuple[int, int], ...], ...],
        tuple[tuple[tuple[int, int], ...], ...],
        tuple[tuple[tuple[int, int], ...], ...]]:
    """Read back the exterior vistas the wall pass composed.

    Pure analysis of a finished plane, not a placement decision: it groups sky
    tiles into connected spans and, for each, records the floor cells that look
    into it and which of those carry a pillar support. The wall composer already
    decided where sky belongs; this is how the manifest and validation learn what
    it built, which is why it lives beside the other plane analyses rather than in
    the orchestrator.
    """
    sky_cells = {(index % GRID, index // GRID)
                 for index, tile in enumerate(tiles) if tile == 16}
    sky_vistas: list[tuple[tuple[int, int], ...]] = []
    sky_vista_recesses: list[tuple[tuple[int, int], ...]] = []
    sky_vista_supports: list[tuple[tuple[int, int], ...]] = []
    while sky_cells:
        component = {sky_cells.pop()}
        queue = deque(component)
        while queue:
            x, y = queue.popleft()
            for neighbor in ((x + 1, y), (x - 1, y),
                             (x, y + 1), (x, y - 1)):
                if neighbor in sky_cells:
                    sky_cells.remove(neighbor)
                    component.add(neighbor)
                    queue.append(neighbor)
        ordered_component = tuple(sorted(component))
        recess = tuple(next(
            (neighbor for neighbor in ((x + 1, y), (x - 1, y),
                                       (x, y + 1), (x, y - 1))
             if _is_floor(_at(tiles, *neighbor))))
            for x, y in ordered_component)
        sky_vistas.append(ordered_component)
        sky_vista_recesses.append(recess)
        sky_vista_supports.append(tuple(
            cell for cell in recess if _at(things, *cell) == 30))
    return (tuple(sky_vistas), tuple(sky_vista_recesses),
            tuple(sky_vista_supports))


def _primary_hall_geometry(plan, rooms, specs) -> tuple[
        tuple[int, int, int, int, int], ...]:
    """The realized rectangles of a hallway-first floor's scaffold arms.

    validate_map requires that a hallway-first skeleton records its concourse
    exactly, so this is the readback proving geometry built the footprint the
    schedule asked for. Empty for graph-first floors.
    """
    primary_hall_geometry = tuple(
        (index, room.x, room.y, room.w, room.h)
        for index, (room, spec) in enumerate(zip(rooms, specs))
        if plan.skeleton in HALLWAY_FIRST_SKELETONS and spec.tier == "corridor")
    return primary_hall_geometry
