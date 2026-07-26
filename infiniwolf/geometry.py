"""Geometric realization: turning an abstract plan into usable tile geometry.

Everything that decides where solid rock becomes walkable floor. Rooms are sized
from their planned tier, placed without overlapping, carved into shapes, then
connected; later commits in this stage add corridor routing and the repair passes
that fix accidental sightlines, oversized sound zones and pinched doorways.

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

from collections import Counter
import math
import random

from .campaign import HALLWAY_FIRST_SKELETONS
from .grid import _at, _inside_room, _is_floor, _overlaps, _set
from .model import FloorPlan, PlacedPlan, Room
from .wl6 import GRID, WALL


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
