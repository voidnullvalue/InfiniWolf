"""Deterministic WL6 campaign generation and ECWolf package writing."""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, replace
import heapq
from itertools import combinations
import json
import math
from pathlib import Path
import random
import struct
import tempfile
from typing import Callable
import zipfile

from . import __version__
from .build_info import COMMIT as BUILD_COMMIT
from .config import CampaignConfig

# The WL6 code vocabulary lives in the tiles leaf. Imported by name (rather
# than star) so it is re-exported from `infiniwolf.generator` exactly as
# before: generator_validation, the test suite, and tools/ all import these
# from here.
from .wl6 import (  # noqa: F401
    GRID, WALL, FLOOR, ZONE_MAX, DOOR_EW, DOOR_NS, DOOR_ELEVATOR, DOOR_ELEVATOR_NS,
    DOOR_GOLD_EW, DOOR_GOLD_NS, DOOR_SILVER_EW, DOOR_SILVER_NS, GOLD_DOORS, SILVER_DOORS,
    LOCKED_DOORS, DOORS, PLAYER_START_CODES, PLAYER_START, PUSHWALL, ELEVATOR_TILE,
    DUMMY_ELEVATOR_TILE, SECRET_EXIT_ZONE, GOLD_KEY, SILVER_KEY, HANS_GROSSE, SCHABBS, GRETEL,
    GIFT, FAT_FACE, MECHA_HITLER, FAKE_HITLER, GHOSTS, BOSSES, KEY_DROP_BOSSES, GUARDS,
    OFFICERS, SS, DOGS, PATROL_GUARDS, PATROL_OFFICERS, PATROL_SS, PATROL_DOGS,
    PATROL_POINT_CODES, PATROL_POINT_DIRECTIONS, DOG_FOOD, AMMO, FOOD, FIRST_AID, MACHINE_GUN,
    CHAINGUN, ONE_UP, TREASURE, PICKUP_CODES, ENEMY_CODES, ENEMY_FAMILIES,
    NOVELTY_SPAWN_CHANCE, PATROLS_BY_FAMILY, FAMILY_BY_CODE, AMMO_COST, AMMO_SUPPLY_SCALE,
    AMMO_SUPPLY_EXEMPT_FLOORS, ACTOR_BUDGET_SCALE, ACTOR_SPACING, WallMaterialFamily,
    WALL_MATERIALS, MATERIAL_BY_BASE, WALL_THEMES, JAIL_CANDIDATE_PROBABILITY,
    _codes_for_colors,
    FLOOR_TEN_STONE_THEME, PURPLE_MIN_FLOOR, DECOR_WALLS, SPECIAL_WALL_TILES,
    SECRET_HINT_BY_BASE, SECRET_HINT_WALLS, WALL_LANDMARK_CONCEPTS, DAMAGED_WALL_CONCEPTS,
    STATIC_BLOCKING, STATIC_OPEN, LIGHTING_ITEMS, LIGHTING_FAMILY_ITEMS, SPEAR_CONCEPTS,
    VINE_SCREEN_CONCEPTS,
)
from .model import (  # noqa: F401
    ArrivalDetail, BossArenaDetail, EncounterPlacement, FloorPlan, FloorVariant,
    GatePlan, GeneratedMap, GuardGallery, GuardRecess, KeyObjective, PatrolRoute,
    PlacedPlan, RareMotifDetail, Room, RoomIdentity, RoomSpec, SecretDetail,
    SpritePlacement, VineScreen,
)
from .grid import (  # noqa: F401
    _at, _set, _is_floor, _inside_room, _door_zone, _reachable,
    _floor_components, _floor_distances, _shortest_floor_path, _path_bends,
    _overlaps, _rooms_by_distance, _room_graph_path, _room_predecessor,
    _FLOOR_OR_DOOR,
)
from .placement import (  # noqa: F401
    AUTHORED_PICKUP_TEMPLATES, RoomAnchors, TraversalFrame, _PlacementGrammar,
    _room_anchors, _room_traversal_frame, _traversal_pair_candidates,
)
from .generator_validation import (  # noqa: F401
    validate_map, validate_objects, validate_patrols, validate_door_axes,
    _patrol_actor_direction,
)
from .progression import _minimum_critical_route_rooms  # noqa: F401
from .planning import _plan_floor  # noqa: F401
from .geometry import (  # noqa: F401
    _add_pillars, _carve_notches, _carve_swastika_profile,
    _carve_symmetric_profiles, _place_planned_rooms, _room_size, _snap_offsets,
)
from .campaign import (  # noqa: F401
    CIRCULATION_MODES, CIRCULATION_SKELETONS, FLOOR_VARIANT_ROTATION,
    HALLWAY_FIRST_SKELETONS, PROGRESSION_GRAMMARS, RARE_MOTIF_CHANCE,
    VARIANT_STRONGHOLD, VARIANT_VAULT, _aardwolf_variant, _candidate_score,
    _circulation_sequence, _lock_schedule, _progression_sequence,
    _rare_motif_schedule, _set_distance, _variant_sequence,
)
from .generator_artifacts import (  # noqa: F401
    _wad_bytes, _mapinfo, _display_name, _reproducibility_text,
    read_manifest, validate_package,
)
from .decorations import (  # noqa: F401
    SKY_VISTA_COURTYARD_CHANCE, SKY_VISTA_INTERIOR_CHANCE, _DECOR_BLOCKING, _DECOR_OPEN,
    _DECOR_ZONES, _FRAMEABLE, _LIGHTING_OPTIONS, _decor_theme, _lighting_family,
    _place_decorations, _place_zoned,
)


DECORATION_MULTIPLIERS = (0.0, 0.70, 0.85, 1.00, 1.15, 1.30)
SHAPE_MULTIPLIERS = (0.0, 0.65, 0.82, 1.00, 1.10, 1.20)
# Target share of ordinary actors that should visibly patrol. The old values
# were per-room attempt chances and produced only ~3% moving actors at the
# normal setting because most full-room loops failed geometry reservations.
PATROL_TARGETS = (0.0, 0.04, 0.09, 0.16, 0.23, 0.30)

SHAPE_TARGETS = (0.0, 0.15, 0.25, 0.40, 0.48, 0.55)


class GenerationCancelled(RuntimeError):
    """Raised when a caller cancels before atomic package installation."""


def _room_identities(rooms: list[Room], specs: list[RoomSpec], districts: list[int],
                     edges: list[tuple[int, int]], variant: FloorVariant,
                     jail_rooms: frozenset[int],
                     component_of: dict[tuple[int, int], int],
                     group_theme: dict[int, tuple[int, tuple[int, ...]]],
                     exit_room: Room, boss_room: Room | None = None,
                     special_family: str = "standard",
                     key_objectives: tuple[KeyObjective, ...] = ()
                     ) -> list[RoomIdentity]:
    """Resolve grammar forward into compatible room concepts.

    Role/tier/motif and the floor variant are already fixed at this point;
    material and decoration only refine that earlier identity.
    """
    overrides = dict(variant.decor_overrides)
    resolved: list[tuple[str, str, int]] = []
    for index, (room, spec, district) in enumerate(zip(rooms, specs, districts)):
        theme = "jail" if index in jail_rooms else _decor_theme(spec.role, spec.tier)
        if index not in jail_rooms:
            theme = overrides.get(theme, theme)
        special = ("start" if index == 0 else "exit" if room == exit_room else
                   "boss" if boss_room is not None and room == boss_room else
                   spec.role if spec.role in
                   ("arrival", "staging", "victory", "premium-vault", "recovery") else
                   "jail" if index in jail_rooms else "")
        wall_base = group_theme[component_of[room.center]][0]
        resolved.append((theme, special, wall_base))

    # A kitchen is a deliberate floor-level set piece, not the default
    # interpretation of every lounge. Prefer a normal relief room and cap the
    # concept at one for the entire floor.
    kitchen_index: int | None = None
    if variant.name in ("quarters", "grand-halls"):
        candidates = [index for index, ((theme, special, _), spec) in
                      enumerate(zip(resolved, specs))
                      if theme == "lounge" and not special
                      and spec.tier not in ("hall", "closet")]
        if candidates:
            kitchen_index = min(candidates, key=lambda index: (
                specs[index].role != "relief",
                abs(rooms[index].w * rooms[index].h - 64),
                districts[index], index))

    palettes = {
        "guardpost": ("guardpost", "armory", "checkpoint"),
        "grand": ("war-room", "trophy-hall", "courtyard"),
        "barracks": (("crypt", "ossuary", "burial-chamber")
                     if variant.name == "catacombs" else
                     ("barracks", "ready-room", "training-room")),
        "storage": ("storage", "supply-cache", "workshop"),
        "lounge": (("gallery", "dining-hall", "lounge")
                   if variant.name == "grand-halls" else
                   ("officers-quarters", "lounge", "dining-hall")
                   if variant.name == "quarters" else
                   ("lounge", "dining-hall")),
        # Authored hallway-first scaffolds deliberately connect several
        # circulation rooms. Give those spaces compatible identities so the
        # normal neighbor-aware choice can vary an axis and its occupied arm
        # instead of producing an unavoidable run of identical corridors.
        "corridor": ("corridor", "checkpoint", "guardpost"),
        "jail": ("jail", "holding-cell", "interrogation-room"),
    }
    neighbors: dict[int, set[int]] = {index: set() for index in range(len(rooms))}
    for first, second in edges:
        neighbors[first].add(second)
        neighbors[second].add(first)
    concepts: list[str] = []
    counts: Counter[str] = Counter()
    boss_arena_concepts = {
        "throne-stronghold": "trophy-hall",
        "command-bunker": "war-room",
        "laboratory-gauntlet": "workshop",
        "columned-fortress": "courtyard",
        "central-duel": "war-room",
    }
    vault_palettes = {
        "central-vault": ("supply-cache", "gallery", "armory"),
        "museum-circuit": ("gallery", "trophy-hall", "war-room"),
        "nested-reliquary": ("burial-chamber", "ossuary", "gallery"),
        "abandoned-armory": ("armory", "supply-cache", "training-room"),
        "treasure-palace": ("trophy-hall", "dining-hall", "gallery"),
    }
    physical_key_hosts = {objective.host_room for objective in key_objectives
                          if objective.treatment != "boss-drop"}
    for index, ((theme, special, _), spec, district) in enumerate(
            zip(resolved, specs, districts)):
        if index == kitchen_index:
            concept = "mess-kitchen"
        elif index in physical_key_hosts:
            concept = ({"storage": "supply-cache", "grand": "war-room",
                        "lounge": "officers-quarters",
                        "barracks": "armory"}.get(theme, "checkpoint"))
        elif spec.role == "boss-arena":
            concept = boss_arena_concepts.get(special_family, "war-room")
        elif spec.role == "staging":
            concept = "ready-room"
        elif spec.role == "victory":
            concept = "trophy-hall"
        elif spec.role == "arrival":
            concept = "gallery"
        elif spec.role == "premium-vault":
            concept = vault_palettes.get(special_family,
                                         ("gallery",))[0]
        elif spec.role == "recovery":
            concept = "lounge"
        elif (special_family in vault_palettes and spec.role not in
              ("start", "exit", "circulation") and spec.tier != "corridor"):
            palette = vault_palettes[special_family]
            ordered = palette[(index + district) % len(palette):] + palette[:
                (index + district) % len(palette)]
            concept = min(ordered, key=lambda candidate: (
                sum(concepts[neighbor] == candidate
                    for neighbor in neighbors[index] if neighbor < len(concepts)),
                counts[candidate], ordered.index(candidate)))
        elif theme == "grand" and spec.role == "hub":
            concept = "courtyard"
        else:
            palette = palettes.get(theme, (theme,))
            offset = (index + district) % len(palette)
            ordered = palette[offset:] + palette[:offset]
            concept = min(ordered, key=lambda candidate: (
                sum(concepts[neighbor] == candidate
                    for neighbor in neighbors[index] if neighbor < len(concepts)),
                counts[candidate], ordered.index(candidate)))
        concepts.append(concept)
        counts[concept] += 1

    result = []
    for room, spec, district, (theme, special, wall_base), concept in zip(
            rooms, specs, districts, resolved, concepts):
        result.append(RoomIdentity(spec.role, spec.tier, spec.motif, district,
                                   variant.name, concept, theme, wall_base, special))
    return result


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


def _lock_code(normal_code: int, color: str) -> int:
    if color == "gold":
        return DOOR_GOLD_EW if normal_code == DOOR_EW else DOOR_GOLD_NS
    if color == "silver":
        return DOOR_SILVER_EW if normal_code == DOOR_EW else DOOR_SILVER_NS
    raise ValueError(f"unknown key color: {color}")


def _key_spot_in_region(tiles: list[int], things: list[int], rooms: list[Room],
                        roles: list[str], allowed: set[tuple[int, int]],
                        excluded: set[tuple[int, int]], start: tuple[int, int],
                        lock_cells: set[tuple[int, int]],
                        occupied: set[tuple[int, int]] = frozenset()
                        ) -> tuple[tuple[int, int], int, int, str] | None:
    """Choose an off-route key objective inside one progression stage.

    The returned detour is the extra walk over the shortest start-to-lock
    approach. A zero-detour cell lies directly on progression and is never an
    acceptable key objective.
    """
    lock_sides = {(x + dx, y + dy) for x, y in lock_cells
                  for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))}

    def distances(sources: set[tuple[int, int]]) -> dict[tuple[int, int], int]:
        result = {source: 0 for source in sources if source in allowed}
        queue = deque(result)
        while queue:
            x, y = queue.popleft()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                cell = x + dx, y + dy
                if cell in allowed and cell not in result:
                    result[cell] = result[(x, y)] + 1
                    queue.append(cell)
        return result

    targets = lock_sides & allowed
    from_start = distances({start})
    to_lock = distances(targets)
    direct = min((from_start[cell] for cell in targets if cell in from_start),
                 default=0)
    minimum_detour = max(2, min(8, direct // 6))
    ranked: list[tuple[tuple[int, int, int, int, int, int],
                       tuple[int, int], int, int, str]] = []
    exploratory_roles = {"branch", "ring", "relief", "closet", "staging",
                         "recovery"}

    for room_index, (room, role) in enumerate(zip(rooms, roles)):
        # Evaluate the experienced door-bounded room once, not for every cell.
        probe = next((cell for y in range(room.y, room.y + room.h)
                      for x in range(room.x, room.x + room.w)
                      for cell in ((x, y),) if cell in allowed), None)
        if probe is None or lock_sides & _door_zone(tiles, probe):
            continue
        anchors = _room_anchors(room, tiles)
        entries = [cell for cell, _ in anchors.door_entries]
        cells = [(x, y) for y in range(room.y, room.y + room.h)
                 for x in range(room.x, room.x + room.w)
                 if (x, y) in allowed and (x, y) not in excluded
                 and (x, y) not in occupied and (x, y) != start
                 and _at(things, x, y) == 0]
        for cell in cells:
            if cell not in from_start or cell not in to_lock:
                continue
            detour = from_start[cell] + to_lock[cell] - direct
            if detour < minimum_detour:
                continue
            x, y = cell
            perimeter = (x in (room.x, room.x + room.w - 1)
                         or y in (room.y, room.y + room.h - 1))
            doorway_depth = min((abs(x - ex) + abs(y - ey)
                                 for ex, ey in entries), default=4)
            # A straight unobstructed row/column from a doorway makes the key
            # immediately visible. This remains a preference, not concealment
            # behind solid clutter.
            visible = any((ex == x and all(_is_floor(_at(tiles, x, scan))
                                           for scan in range(min(ey, y), max(ey, y) + 1)))
                          or (ey == y and all(_is_floor(_at(tiles, scan, y))
                                             for scan in range(min(ex, x), max(ex, x) + 1)))
                          for ex, ey in entries)
            treatment = ("back-wall-display" if perimeter and doorway_depth >= 3
                         else "side-display" if perimeter else "room-cache")
            score = (role in exploratory_roles, detour, doorway_depth,
                     perimeter, not visible,
                     abs(x - room.center[0]) + abs(y - room.center[1]))
            ranked.append((score, cell, room_index, detour, treatment))
    if not ranked:
        return None
    _, cell, room_index, detour, treatment = max(ranked, key=lambda item: item[0])
    return cell, room_index, detour, treatment


def _place_doors(tiles: list[int], things: list[int], rooms: list[Room],
                 edges: list[tuple[int, int]], paths: list[list[tuple[int, int]]],
                 rng: random.Random, start: tuple[int, int],
                 gate_target: tuple[int, int], roles: list[str],
                 reserved: set[tuple[int, int]], gate_plan: GatePlan,
                 critical_route: list[int]
                 ) -> tuple[int, tuple[str, ...], tuple[KeyObjective, ...]]:
    records = [(edge, candidate) for edge, path in zip(edges, paths)
               if (candidate := _door_candidate(tiles, rooms, path))
               and candidate[:2] not in reserved]
    candidates = [candidate for _, candidate in records]
    # Every viable room-to-room junction gets a door: sound zones (see
    # _assign_sound_zones) only split at door tiles, so leaving most
    # candidates doorless silently merges most of the floor into one giant
    # zone and one gunshot wakes almost the whole map. This still misses
    # incidental adjacency where two unrelated corridors happen to run flush
    # against each other away from their own intended junction -- see
    # _split_oversized_zones, which catches what's left. Locked-door
    # schedule independently controls whether zero, one, or two of these
    # thresholds become mandatory progression gates.
    placed = candidates
    for x, y, code in placed:
        _set(tiles, x, y, code)
    if not gate_plan.colors:
        return 0, (), ()

    # Secrets are carved before doors, so gating must also hold with every
    # pushwall already pushed: otherwise a secret pocket can quietly open a
    # route around the lock and the key becomes optional.
    pushwalls = {(i % GRID, i // GRID) for i, thing in enumerate(things) if thing == PUSHWALL}
    rests = {(x + 2, y) for x, y in pushwalls}
    route_edges = [{critical_route[index], critical_route[index + 1]}
                   for index in range(len(critical_route) - 1)]
    route_records: list[tuple[int, tuple[int, int, int]]] = []
    for edge, candidate in records:
        endpoints = set(edge)
        if endpoints in route_edges:
            route_records.append((route_edges.index(endpoints) + 1, candidate))
    if not route_records:
        return 0, (), ()

    def reachable(open_colors: set[str]) -> set[tuple[int, int]]:
        return _reachable(tiles, start, locked_open=False,
                          extra_passable=pushwalls, blocked=rests,
                          open_lock_codes=_codes_for_colors(open_colors))

    def restore(trial: list[tuple[int, tuple[int, int, int], str]]) -> None:
        for _, (x, y, normal), _ in trial:
            _set(tiles, x, y, normal)

    def commit(trial: list[tuple[int, tuple[int, int, int], str]],
               key_spots: list[tuple[tuple[int, int], int, int, str]]
               ) -> tuple[int, tuple[str, ...], tuple[KeyObjective, ...]]:
        colors = tuple(color for _, _, color in trial)
        objectives = []
        for stage, (color, key) in enumerate(zip(colors, key_spots), 1):
            spot, host_room, detour, treatment = key
            _set(things, *spot, GOLD_KEY if color == "gold" else SILVER_KEY)
            reserved.add(spot)
            objectives.append(KeyObjective(color, spot, host_room, stage,
                                           detour, treatment))
        return len(trial), colors, tuple(objectives)

    if len(gate_plan.colors) >= 2:
        first_color, second_color = gate_plan.colors[:2]
        trials = [(first, second) for first, second in combinations(route_records, 2)
                  if second[0] - first[0] >= 2]
        trials.sort(key=lambda pair: (
            abs(pair[0][0] / len(critical_route) - 0.38)
            + abs(pair[1][0] / len(critical_route) - 0.72)))
        for first, second in trials:
            trial = [(first[0], first[1], first_color),
                     (second[0], second[1], second_color)]
            for _, (x, y, normal), color in trial:
                _set(tiles, x, y, _lock_code(normal, color))
            closed = reachable(set())
            only_first = reachable({first_color})
            only_second = reachable({second_color})
            both = reachable({first_color, second_color})
            first_key = _key_spot_in_region(
                tiles, things, rooms, roles, closed, set(), start,
                {(first[1][0], first[1][1])}, set(reserved))
            second_key = (_key_spot_in_region(
                tiles, things, rooms, roles, only_first, closed, start,
                {(second[1][0], second[1][1])},
                set(reserved) | ({first_key[0]} if first_key else set()))
                          if first_key else None)
            if (first_key and second_key and gate_target not in closed
                    and gate_target not in only_first and gate_target not in only_second
                    and gate_target in both):
                return commit(trial, [first_key, second_key])
            restore(trial)

    # Gracefully downgrade a geometrically impossible dual gate to one real
    # mandatory lock; never preserve a decorative or bypassable second lock.
    for color in gate_plan.colors:
        ordered = sorted(route_records, key=lambda item:
                         abs(item[0] / len(critical_route) - 0.62))
        for progress, candidate in ordered:
            x, y, normal = candidate
            _set(tiles, x, y, _lock_code(normal, color))
            closed = reachable(set())
            opened = reachable({color})
            key = _key_spot_in_region(
                tiles, things, rooms, roles, closed, set(), start, {(x, y)},
                set(reserved))
            if key and gate_target not in closed and gate_target in opened:
                return commit([(progress, candidate, color)], [key])
            _set(tiles, x, y, normal)
    return 0, (), ()


def _key_spot(tiles: list[int], things: list[int], rooms: list[Room], roles: list[str],
              locked: tuple[tuple[int, int, int], ...],
              start: tuple[int, int]) -> tuple[int, int] | None:
    """Farthest reachable room center whose door-bounded region touches no
    locked door: finding the key beside the very door it opens is a
    non-puzzle, so such rooms never host it. A room center is otherwise
    always plain floor, but an earlier pass (bonus rewards, a secret, decor)
    can already have claimed it by the time this runs, so skip any
    candidate whose center isn't free rather than assuming the farthest
    eligible room is always available."""
    pre_lock = _reachable(tiles, start, locked_open=False)
    lock_sides = {(x + dx, y + dy) for x, y, _ in locked
                  for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))}
    candidates = [(room.center, role) for room, role in zip(rooms, roles)
                  if room.center in pre_lock and room.center != start]
    candidates.sort(key=lambda item: (item[1] == "branch",
                                      abs(item[0][0] - start[0]) + abs(item[0][1] - start[1])),
                    reverse=True)
    for center, _ in candidates:
        if _at(things, *center) != 0:
            continue
        if not lock_sides & _door_zone(tiles, center):
            return center
    return None


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
        reserved.add(cell)
        door_zones.add(cell)
        placed += 1
    return placed


def _critique(level: GeneratedMap) -> tuple[str, ...]:
    components = _floor_components(level.tiles)
    owner = {cell: index for index, component in enumerate(components) for cell in component}
    graph_edges: set[tuple[int, int]] = set()
    for index, tile in enumerate(level.tiles):
        if tile not in DOORS:
            continue
        x, y = index % GRID, index // GRID
        neighbors = {owner[cell] for cell in ((x + 1, y), (x - 1, y),
                                              (x, y + 1), (x, y - 1))
                     if cell in owner}
        graph_edges.update(tuple(sorted(edge)) for edge in combinations(neighbors, 2))
    links = {index: set() for index in range(len(components))}
    for a, b in graph_edges:
        links[a].add(b); links[b].add(a)
    graph_components = 0
    unseen = set(links)
    while unseen:
        graph_components += 1
        queue = [unseen.pop()]
        while queue:
            for nxt in links[queue.pop()] & unseen:
                unseen.remove(nxt); queue.append(nxt)
    cycles = len(graph_edges) - len(components) + graph_components
    sizes = sorted((len(component) for component in components), reverse=True)
    total = sum(sizes) or 1
    room_floor = {cell for room in level.rooms
                  for y in range(room.y, room.y + room.h)
                  for x in range(room.x, room.x + room.w)
                  for cell in ((x, y),) if _is_floor(_at(level.tiles, x, y))}
    all_floor = {(x, y) for y in range(GRID) for x in range(GRID)
                 if _is_floor(_at(level.tiles, x, y))}
    flags = []
    if cycles == 0:
        flags.append("no_loop")
    if sizes and sizes[0] / total < 0.10:
        flags.append("no_anchor")
    if sum(sizes[:3]) / total < 0.25:
        flags.append("flat_hierarchy")
    if all_floor and len(all_floor - room_floor) / len(all_floor) > 0.45:
        flags.append("corridor_heavy")
    longest = 0
    for horizontal in (True, False):
        for fixed in range(GRID):
            run = 0
            for moving in range(GRID):
                x, y = (moving, fixed) if horizontal else (fixed, moving)
                run = run + 1 if _is_floor(_at(level.tiles, x, y)) else 0
                longest = max(longest, run)
    if longest > 21:
        flags.append("long_sightline")
    motif_counts = {motif: level.motif_rooms.count(motif) for motif in level.motifs}
    if level.rooms and any(count / len(level.rooms) > 0.40
                           for count in motif_counts.values()):
        flags.append("motif_imbalance")
    if (len(level.secret_variants) >= 3
            and set(level.secret_variants) == {"square"}):
        flags.append("secret_monotony")
    encounter_templates = [encounter.template for encounter in level.encounters
                           if encounter.template not in
                           ("novelty", "boss-support", "patrol")]
    if (len(encounter_templates) >= 5
            and max(Counter(encounter_templates).values())
            / len(encounter_templates) > 0.55):
        flags.append("encounter_repetition")
    ordinary_actors = [thing for thing in level.things
                       if thing in ENEMY_CODES and thing not in BOSSES]
    moving = sum(_patrol_actor_direction(actor) is not None
                 for actor in ordinary_actors)
    if (level.patrol_target and len(ordinary_actors) >= 8
            and moving / len(ordinary_actors) < level.patrol_target * 0.75):
        flags.append("patrol_sparse")
    return tuple(flags)


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


def _assign_area_themes(tiles: list[int], rooms: list[Room], districts: list[int],
                        rng: random.Random, number: int,
                        theme_pool: tuple[int, ...] = ()
                        ) -> tuple[dict[tuple[int, int], int],
                                   dict[int, tuple[int, tuple[int, ...]]]]:
    """Choose one wall family per door-bounded area without exposing seams.

    A bare wall shared by two floor components joins their theme groups before
    painting.  Different groups can therefore meet only at an actual door.
    """
    components = _floor_components(tiles)
    owner = {cell: index for index, component in enumerate(components)
             for cell in component}
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

    for index, tile in enumerate(tiles):
        if tile != WALL:
            continue
        x, y = index % GRID, index // GRID
        neighbors = {owner[cell] for cell in ((x + 1, y), (x - 1, y),
                                              (x, y + 1), (x, y - 1))
                     if cell in owner}
        for first, second in combinations(sorted(neighbors), 2):
            union(first, second)

    component_of = {cell: find(component) for cell, component in owner.items()}
    groups = sorted(set(component_of.values()))
    votes: dict[int, dict[int, int]] = {group: {} for group in groups}
    for room, district in zip(rooms, districts):
        group = component_of[room.center]
        votes[group][district] = votes[group].get(district, 0) + 1
    assigned = {group: min(district for district, count in tally.items()
                           if count == max(tally.values()))
                for group, tally in votes.items() if tally}

    links = {group: set() for group in groups}
    for index, tile in enumerate(tiles):
        if tile not in DOORS:
            continue
        x, y = index % GRID, index // GRID
        neighbors = {component_of[cell] for cell in ((x + 1, y), (x - 1, y),
                                                       (x, y + 1), (x, y - 1))
                     if cell in component_of}
        for first, second in combinations(sorted(neighbors), 2):
            links[first].add(second); links[second].add(first)
    queue = deque(sorted(assigned))
    while queue:
        group = queue.popleft()
        for neighbor in sorted(links[group]):
            if neighbor not in assigned:
                assigned[neighbor] = assigned[group]
                queue.append(neighbor)
    for group in groups:
        assigned.setdefault(group, 0)

    distinct_districts = sorted(set(districts))
    deduped = list({theme[0]: theme for theme in WALL_THEMES}.values())
    # Floors 1--6 can own the campaign's hidden elevator. Plaster has no
    # symmetric in-family landmark suitable for that mandatory hint, so save
    # it for later administrative/reward districts rather than forcing a
    # conspicuous cross-family triptych around a secret exit.
    if number <= 6:
        deduped = [theme for theme in deduped if theme[0] != 48]
    # Purple reads as an unusually rich, ominous finish.  Reserve it for the
    # campaign's later half instead of letting an early grand-halls roll spend
    # that visual escalation on floor one or two.
    if number < PURPLE_MIN_FLOOR:
        deduped = [theme for theme in deduped if theme[0] != 19]
    if theme_pool:
        pooled = [theme for theme in deduped if theme[0] in theme_pool]
        # A pool too small to give every district its own material would
        # crash rng.sample; fall back to the full roster instead.
        if len(pooled) >= len(distinct_districts):
            deduped = pooled
    if number == 10 and rng.random() < 0.25:
        chosen = [FLOOR_TEN_STONE_THEME] + rng.sample(
            deduped, k=len(distinct_districts) - 1)
    else:
        chosen = rng.sample(deduped, k=len(distinct_districts))
    rng.shuffle(chosen)
    theme_by_district = dict(zip(distinct_districts, chosen))
    group_theme = {group: theme_by_district[assigned[group]] for group in groups}
    return component_of, group_theme


def _select_jail_rooms(rooms: list[Room], districts: list[int],
                       component_of: dict[tuple[int, int], int],
                       group_theme: dict[int, tuple[int, tuple[int, ...]]],
                       tiles: list[int], rng: random.Random,
                       jail_probability: float = JAIL_CANDIDATE_PROBABILITY
                       ) -> frozenset[int]:
    """Pick blue-stone rooms with a long enough unpainted wall for cells."""
    blue_rooms = [ridx for ridx, room in enumerate(rooms)
                  if group_theme[component_of[room.center]][0] == 8]
    selected = []
    for ridx, room in enumerate(rooms):
        base = group_theme[component_of[room.center]][0]
        if base != 8:
            continue
        sides = (
            [(x, room.y - 1) for x in range(room.x - 1, room.x + room.w + 1)],
            [(x, room.y + room.h) for x in range(room.x - 1, room.x + room.w + 1)],
            [(room.x - 1, y) for y in range(room.y, room.y + room.h)],
            [(room.x + room.w, y) for y in range(room.y, room.y + room.h)],
        )
        longest = 0
        for side in sides:
            run = 0
            for cell in side:
                run = run + 1 if _at(tiles, *cell) == WALL else 0
                longest = max(longest, run)
        if longest >= 5 and rng.random() < jail_probability:
            selected.append(ridx)
    # A blue-stone district still needs ordinary rooms so the cell treatment
    # reads as a deliberate sub-area instead of consuming the whole material
    # family. Keep jails a strict minority of all blue-stone rooms, including
    # high-probability catacomb variants.
    limit = max(0, (len(blue_rooms) - 1) // 2)
    if len(selected) > limit:
        rng.shuffle(selected)
        selected = selected[:limit]
    return frozenset(selected)


def _apply_wall_theme(tiles: list[int], things: list[int], rooms: list[Room],
                      districts: list[int], component_of: dict[tuple[int, int], int],
                      group_theme: dict[int, tuple[int, tuple[int, ...]]],
                      rng: random.Random,
                      jail_rooms: frozenset[int] = frozenset(),
                      identities: list[RoomIdentity] | None = None,
                      atmosphere: int = 3,
                      ) -> dict[int, list[tuple[int, int]]]:
    """Apply native WL6 materials without changing traversable geometry.

    Returns each room's landmark decor-wall cells (portraits, banners,
    insignia) so the decoration pass can frame them with furniture instead
    of placing pieces mid-room."""
    landmark_cells: dict[int, list[tuple[int, int]]] = {}
    for index, tile in enumerate(tiles):
        if tile != WALL:
            continue
        x, y = index % GRID, index // GRID
        group = next((component_of[cell] for cell in ((x + 1, y), (x - 1, y),
                                                       (x, y + 1), (x, y - 1))
                      if cell in component_of), None)
        if group is not None:
            tiles[index] = group_theme[group][0]
    for ridx, (room, district) in enumerate(zip(rooms, districts)):
        base, accents = group_theme[component_of[room.center]]
        sides = (
            [(x, room.y - 1) for x in range(room.x - 1, room.x + room.w + 1)],
            [(x, room.y + room.h) for x in range(room.x - 1, room.x + room.w + 1)],
            [(room.x - 1, y) for y in range(room.y, room.y + room.h)],
            [(room.x + room.w, y) for y in range(room.y, room.y + room.h)],
        )
        if ridx in jail_rooms and base == 8:
            other_accents = {41}
            for side in sides:
                run = [cell for cell in side if _at(tiles, *cell) == base]
                for i, (x, y) in enumerate(run):
                    if i % 3 == 0:
                        continue
                    # Keep a neutral stone buffer at nearby room seams, as
                    # the ordinary accent pass does below.
                    if any(_at(tiles, x + dx, y + dy) in other_accents
                           for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                        continue
                    tile = rng.choices((5, 7), weights=(9, 1))[0]
                    _set(tiles, x, y, tile)
                    # Plain bars sometimes get the loose remains that distinguish a
                    # neglected cellblock without turning the wall texture itself
                    # into a room-wide skeleton set piece.
                    if tile == 5 and rng.random() < 0.3:
                        interior = [(nx, ny) for nx, ny in ((x - 1, y), (x + 1, y),
                                                             (x, y - 1), (x, y + 1))
                                    if room.x <= nx < room.x + room.w
                                    and room.y <= ny < room.y + room.h
                                    and _is_floor(_at(tiles, nx, ny))
                                    and _at(things, nx, ny) == 0]
                        if interior:
                            _set(things, *rng.choice(interior), rng.choice((42, 64, 65, 66)))
            continue
        material = MATERIAL_BY_BASE.get(base)
        identity = (identities[ridx]
                    if identities is not None and ridx < len(identities) else None)
        concept = identity.concept if identity is not None else ""
        plain_variants = tuple(tile for tile in (material.plain_variants
                                                 if material else ())
                               if tile in accents)
        damage_variants = tuple(tile for tile in (material.damage_variants
                                                  if material else ())
                                if tile in accents)

        # A room may use one coherent surface variant, never a scatter of
        # unrelated tiles. Damage is additionally gated by both atmosphere
        # and semantic room identity.
        surface = base
        if (damage_variants and atmosphere >= 3
                and (identity is None or concept in DAMAGED_WALL_CONCEPTS)
                and rng.random() < min(0.65, 0.20 + atmosphere * 0.09)):
            surface = rng.choice(damage_variants)
        elif plain_variants and (identity is None or rng.random() < 0.58):
            surface = plain_variants[(ridx + district) % len(plain_variants)]
        if surface != base:
            for side in sides:
                for x, y in side:
                    if _at(tiles, x, y) == base:
                        _set(tiles, x, y, surface)

        material_landmarks = tuple(tile for tile in (material.landmarks
                                                      if material else accents)
                                   if tile in accents and tile in DECOR_WALLS)
        eligible_landmarks = tuple(
            tile for tile in material_landmarks
            if identity is None or concept in WALL_LANDMARK_CONCEPTS.get(tile, ()))
        formal = concept in {
            "guardpost", "checkpoint", "war-room", "trophy-hall", "gallery",
            "officers-quarters", "armory", "jail", "holding-cell",
            "interrogation-room",
        }
        place_landmark = bool(eligible_landmarks) and (
            identity is None or rng.random() < (0.30 if formal else 0.12))

        # Stained glass is a complete paired composition in prestigious
        # marble rooms, not a general-purpose material or isolated window.
        special_glass = (identity is not None and base == 42
                         and concept in {"gallery", "trophy-hall", "war-room"}
                         and rng.random() < 0.12)

        if special_glass or place_landmark:
            # Landmark tiles hang like pictures on the longest clean
            # (contiguous, same-base) wall run -- never the material for the
            # whole room. Short runs get one centered tile; longer runs get a
            # mirrored pair, and the longest a center-plus-pair triplet, so a
            # dressed wall reads as deliberately symmetric composition.
            runs: list[tuple[int, list[tuple[int, int]]]] = []
            for side_index, side in enumerate(sides):
                current: list[tuple[int, int]] = []
                for cell in side:
                    if _at(tiles, *cell) in ({base, surface} | set(plain_variants)
                                             | set(damage_variants)):
                        current.append(cell)
                    elif current:
                        runs.append((side_index, current))
                        current = []
                if current:
                    runs.append((side_index, current))
            side_index, run = max(runs, key=lambda item: len(item[1]), default=(-1, []))
            if run:
                selected_runs = [run]
                opposite = {0: 1, 1: 0, 2: 3, 3: 2}[side_index]
                compatible = [candidate for candidate_side, candidate in runs
                              if candidate_side == opposite
                              and abs(len(candidate) - len(run)) <= 2]
                # Opposing dressed faces are an occasional whole-room
                # composition, never an independently rolled second wall.
                if compatible and len(run) >= 9 and rng.random() < 0.25:
                    selected_runs.append(max(compatible, key=len))
                accent = (33 if special_glass else
                          eligible_landmarks[district % len(eligible_landmarks)])
                for selected in selected_runs:
                    mid = len(selected) // 2
                    if special_glass:
                        if len(selected) < 7:
                            continue
                        offset = max(2, len(selected) // 4)
                        spots = [selected[mid - offset], selected[mid + offset]]
                    elif accent == 7 or len(selected) < 9:
                        spots = [selected[mid]]
                    elif len(selected) < 13:
                        offset = max(2, len(selected) // 4)
                        spots = [selected[mid - offset], selected[mid + offset]]
                    else:
                        offset = max(3, len(selected) // 4)
                        spots = [selected[mid - offset], selected[mid], selected[mid + offset]]
                    for x, y in spots:
                        landmark = (rng.choices((5, 7), weights=(9, 1))[0]
                                    if accent == 7 else accent)
                        _set(tiles, x, y, landmark)
                        landmark_cells.setdefault(ridx, []).append((x, y))
                        if landmark == 5 and rng.random() < 0.3:
                            interior = [(nx, ny) for nx, ny in
                                        ((x - 1, y), (x + 1, y),
                                         (x, y - 1), (x, y + 1))
                                        if room.x <= nx < room.x + room.w
                                        and room.y <= ny < room.y + room.h
                                        and _is_floor(_at(tiles, nx, ny))
                                        and _at(things, nx, ny) == 0]
                            if interior:
                                _set(things, *rng.choice(interior),
                                     rng.choice((42, 64, 65, 66)))
    return landmark_cells


def _hint_secrets(tiles: list[int], things: list[int],
                  component_of: dict[tuple[int, int], int],
                  group_theme: dict[int, tuple[int, tuple[int, ...]]],
                  rng: random.Random,
                  special_pushwall: tuple[int, int] | None = None,
                  plain_walls: frozenset[tuple[int, int]] = frozenset(),
                  ) -> dict[tuple[int, int], str]:
    """Hang a landmark decor tile (banner, portrait, insignia) on most
    pushwalls, the way the original episodes telegraph theirs. Runs after
    _apply_wall_theme so the theme can't repaint the hint, and prefers the
    floor theme's own decor accents so the hint matches the material. Falls
    back to a same-base sibling theme's accents rather than a hardcoded
    cross-family constant, so a hint tile can never mix material families.

    Walls in ``plain_walls`` are deliberately left as ordinary wall material
    with no landmark -- the inner wall of a nested double secret, so the second
    chamber is not given away. The mandatory secret-exit ``special_pushwall``
    is always hinted."""
    treatments: dict[tuple[int, int], str] = {}
    for index, thing in enumerate(things):
        if thing != PUSHWALL:
            continue
        x, y = index % GRID, index // GRID
        group = next((component_of[cell] for cell in ((x + 1, y), (x - 1, y),
                                                       (x, y + 1), (x, y - 1))
                      if cell in component_of), None)
        if group is None:
            continue
        base, accents = group_theme[group]
        hints = tuple(accent for accent in accents if accent in DECOR_WALLS)
        if not hints:
            hints = tuple(accent for other_base, other_accents in WALL_THEMES
                          if other_base == base
                          for accent in other_accents if accent in DECOR_WALLS)
        if not hints:
            hints = SECRET_HINT_BY_BASE.get(base, ())
        force_plain = (x, y) in plain_walls and (x, y) != special_pushwall
        if hints:
            # Always draw the hint even when the wall is deliberately left
            # plain, so the marking choice never shifts the main rng stream
            # that downstream population and item economy depend on.
            hint = rng.choice(hints)
            if force_plain:
                # An unmarked secret reads as ordinary wall, so it is found by
                # pushing rather than by spotting a landmark.
                tiles[index] = base
                treatments[(x, y)] = "plain-wall"
                continue
            tiles[index] = hint
            treatments[(x, y)] = ("plain-wall" if hint == base
                                  else "single-landmark")
            if (x, y) == special_pushwall and hint != base:
                # A matching pair around the center hint gives the route to
                # floor 10 a coherent landmark without borrowing another
                # material family or spelling out "secret elevator".
                for offset in (1, 2):
                    pair = ((x, y - offset), (x, y + offset))
                    family_surfaces = ({base}
                                       | {tile for tile in accents
                                          if tile not in DECOR_WALLS})
                    if all(_at(tiles, *cell) in family_surfaces for cell in pair):
                        for cell in pair:
                            _set(tiles, *cell, hint)
                        treatments[(x, y)] = "symmetric-landmark"
                        break
        elif force_plain:
            # No landmark accent exists for this material, so the wall is
            # already ordinary; just record it as the deliberate plain choice.
            tiles[index] = base
            treatments[(x, y)] = "plain-wall"
    return treatments


def _place_elevator(tiles: list[int], room: Room, locked: bool = False) -> tuple[int, int]:
    """Carve a native one-tile elevator shaft into an east or west wall.

    Bays never face north/south: the tile-21 exit switch only activates on
    its east/west faces, so a shaft entered heading north or south could
    never be exited. The shaft is framed entirely in tile 21, whose faces
    visible from inside the car are the plain elevator paneling; the only
    exposed switch face is the centered back wall, reachable exactly like
    the original game's elevators. From the room the player sees only a
    real elevator door (or a gold-locked door on the boss floor) set into
    the room's own wall material -- no decoy switch panels.
    """
    cx, cy = room.center
    # Sweep rows outward from the room's midline on both east/west walls so
    # a corridor crossing one spot doesn't doom the whole placement.
    offsets = sorted(range(room.y + 1, room.y + room.h - 1), key=lambda y: abs(y - cy))
    candidates = [(wx, wy, dx) for wy in offsets
                  for wx, dx in ((room.x + room.w, 1), (room.x - 1, -1))]
    for wx, wy, dx in candidates:
        if not _is_floor(_at(tiles, wx - dx, wy)):
            continue
        # depth range(5): the extra ring ensures the cell immediately beyond
        # the back wall is also solid, so tile-21's east face can never be
        # approached from outside the shaft. side range(-2, 3): tile 21's
        # north/south faces show the same paneling graphic on both sides, so
        # the shaft's rail walls (side +-1) need a second rock's worth of
        # backing at side +-2 -- otherwise a room or corridor that happens to
        # run flush against the shaft would see the elevator dressing bleed
        # through into a space that was never meant to be the elevator room.
        footprint = [(wx + dx * depth, wy + side)
                     for depth in range(5) for side in (-2, -1, 0, 1, 2)]
        if any(not (1 <= x < GRID - 1 and 1 <= y < GRID - 1) or _at(tiles, x, y) != WALL
               for x, y in footprint):
            continue
        for depth in (1, 2):
            _set(tiles, wx + dx * depth, wy, FLOOR)
        # Dress the complete car from the doorway to the switch wall. The
        # five-by-five rock footprint keeps these rails invisible from any
        # neighboring room; delaying them by one tile instead exposes an
        # ordinary wall strip inside a three-deep elevator.
        for depth in (1, 2, 3):
            for side in (-1, 1):
                _set(tiles, wx + dx * depth, wy + side, ELEVATOR_TILE)
        _set(tiles, wx + dx * 3, wy, ELEVATOR_TILE)
        _set(tiles, wx, wy, DOOR_GOLD_EW if locked else DOOR_ELEVATOR)
        return wx + dx * 2, wy
    raise ValueError("terminal room has no clear east/west wall for an elevator")


def _place_arrival_elevator(tiles: list[int], room: Room,
                            toward: tuple[int, int], rng: random.Random,
                            variant: str = "garrison",
                            forced_kind: str | None = None) -> ArrivalDetail:
    """Place one bounded, inactive native-elevator arrival composition."""
    facings = ((0, -1), (1, 0), (0, 1), (-1, 0))
    # A start elevator must always be a complete car behind a working door.
    # The former single-panel "flush-facade" could render as a bare elevator
    # rail with no doorway at all, depending on which face the player saw.
    kinds = ("outside-empty", "outside-supply", "inside-closed")
    if forced_kind is not None and forced_kind not in kinds:
        raise ValueError("unknown arrival elevator kind")
    weights = [0.38, 0.24, 0.38]
    if variant == "storehouse":
        weights = [0.29, 0.36, 0.35]
    elif variant == "quarters":
        weights = [0.43, 0.29, 0.28]
    kind = forced_kind or rng.choices(kinds, weights=weights, k=1)[0]
    tx, ty = toward[0] - room.center[0], toward[1] - room.center[1]
    if abs(tx) >= abs(ty):
        preferred = (-1, 0) if tx >= 0 else (1, 0)
    else:
        preferred = (0, -1) if ty >= 0 else (0, 1)
    # WL6's elevator wall tile is directional: ELEV1_1 is the rail face and
    # ELEV1_2 is the control-panel face. Old-format map tiles cannot rotate
    # that assignment, so only horizontal car axes render rails on both side
    # walls and the panel on the rear wall. Vertical cars necessarily invert
    # that visual language and are therefore invalid arrival candidates.
    horizontal = ((-1, 0), (1, 0))
    sides = [preferred] if preferred in horizontal else []
    sides.extend(side for side in horizontal if side not in sides)
    cx, cy = room.center
    for dx, dy in sides:
        if dx:
            wall = room.x + room.w if dx > 0 else room.x - 1
            offsets = sorted(range(room.y + 1, room.y + room.h - 1),
                             key=lambda value: abs(value - cy))
            panels = [(wall, offset) for offset in offsets]
        else:
            wall = room.y + room.h if dy > 0 else room.y - 1
            offsets = sorted(range(room.x + 1, room.x + room.w - 1),
                             key=lambda value: abs(value - cx))
            panels = [(offset, wall) for offset in offsets]
        px, py = -dy, dx
        for panel in panels:
            footprint = tuple(sorted({
                (panel[0] + depth * dx + side * px,
                 panel[1] + depth * dy + side * py)
                for depth in range(5) for side in (-2, -1, 0, 1, 2)}))
            if (not all(1 <= x < GRID - 1 and 1 <= y < GRID - 1
                        for x, y in footprint)
                    or any(_at(tiles, x, y) != WALL for x, y in footprint)
                    or not all(_is_floor(_at(
                        tiles, panel[0] - depth * dx, panel[1] - depth * dy))
                               for depth in (1, 2, 3))):
                continue
            inward = (-dx, -dy)
            facing = facings.index(inward)
            car_cells = tuple((panel[0] + depth * dx,
                               panel[1] + depth * dy)
                              for depth in (1, 2))
            for cell in car_cells:
                _set(tiles, *cell, FLOOR)
            # The rock-backed footprint contains the car, so its inert panels
            # can begin immediately behind the door without leaking into an
            # adjacent room or leaving a normal wall strip inside the lift.
            for depth in (1, 2, 3):
                for side in (-1, 1):
                    _set(tiles, panel[0] + depth * dx + side * px,
                         panel[1] + depth * dy + side * py,
                         DUMMY_ELEVATOR_TILE)
            _set(tiles, panel[0] + 3 * dx, panel[1] + 3 * dy,
                 DUMMY_ELEVATOR_TILE)
            # Old-format maps cannot encode a door slab permanently parked in
            # its open position.  A plain floor portal has no door or track at
            # all, so every full arrival car uses a genuine elevator door.  It
            # opens normally, while the tile-85 car remains inert and cannot
            # act as another level exit.
            _set(tiles, *panel, DOOR_ELEVATOR if dx else DOOR_ELEVATOR_NS)
            inside = kind.startswith("inside-")
            player = (car_cells[-1] if inside else
                      (panel[0] - 2 * dx, panel[1] - 2 * dy))
            clearance = (((panel[0] + dx, panel[1] + dy),
                          (panel[0] - dx, panel[1] - dy)) if inside else
                         ((panel[0] - dx, panel[1] - dy),
                          (panel[0] - 3 * dx, panel[1] - 3 * dy)))
            item = None
            if kind == "outside-supply":
                supplies = {
                    "garrison": AMMO, "catacombs": FIRST_AID,
                    "grand-halls": TREASURE[0], "storehouse": AMMO,
                    "quarters": FOOD, "stronghold": FIRST_AID,
                    "vault": TREASURE[-1],
                }
                item = (*car_cells[-1], supplies.get(variant, AMMO))
            return ArrivalDetail(kind, panel, player, facing, footprint,
                                 car_cells, clearance, item)
    raise ValueError("start room has no rock-backed wall for a complete arrival car")


def _carve_guard_recesses(tiles: list[int], things: list[int], rooms: list[Room],
                          specs: list[RoomSpec], roles: list[str],
                          reserved: set[tuple[int, int]], rng: random.Random,
                          start: tuple[int, int], exit_room: Room,
                          chance: float = 0.40) -> tuple[GuardRecess, ...]:
    """Rarely carve one mirrored hallway pair owned by an ambush encounter.

    This is deliberately not the removed generic alcove pass. Both recesses
    are reflected across the hall's travel axis, only one hides a sentry, and
    no geometry is committed unless its shoulders remain solid and it stays
    clear of progression doors and the arrival/exit transitions.
    """
    if rng.random() >= chance:
        return ()
    doors = {(x, y) for y in range(GRID) for x in range(GRID)
             if _at(tiles, x, y) in DOORS}
    candidates = [index for index, (room, spec, role) in
                  enumerate(zip(rooms, specs, roles))
                  if index and room != exit_room
                  and spec.tier in ("corridor", "hall")
                  and role not in ("arrival", "victory", "recovery", "boss-arena")
                  and max(room.w, room.h) >= 8]
    rng.shuffle(candidates)
    for room_index in candidates:
        room = rooms[room_index]
        positions = []
        if room.w >= room.h:
            for x in (room.x + room.w // 3, room.x + room.w // 2,
                      room.x + (2 * room.w) // 3):
                positions.append(((x, room.y - 1), (x, room.y + room.h),
                                  ((0, 1), (0, -1))))
        else:
            for y in (room.y + room.h // 3, room.y + room.h // 2,
                      room.y + (2 * room.h) // 3):
                positions.append(((room.x - 1, y), (room.x + room.w, y),
                                  ((1, 0), (-1, 0))))
        rng.shuffle(positions)
        for first, second, inwards in positions:
            cells = (first, second)
            if (any(_at(tiles, *cell) != WALL or _at(things, *cell)
                    or cell in reserved or
                    abs(cell[0] - start[0]) + abs(cell[1] - start[1]) < 8
                    or any(abs(cell[0] - x) + abs(cell[1] - y) <= 3
                           for x, y in doors) for cell in cells)):
                continue
            valid = True
            for cell, inward in zip(cells, inwards):
                if not _is_floor(_at(tiles, cell[0] + inward[0],
                                     cell[1] + inward[1])):
                    valid = False
                    break
                outward = (-inward[0], -inward[1])
                shoulders = ((cell[0] + outward[0], cell[1] + outward[1]),
                             (cell[0] + inward[1], cell[1] + inward[0]),
                             (cell[0] - inward[1], cell[1] - inward[0]))
                if any(_at(tiles, *neighbor) != WALL for neighbor in shoulders):
                    valid = False
                    break
            if not valid:
                continue
            for cell in cells:
                _set(tiles, *cell, FLOOR)
            actor_cell = rng.choice(cells)
            reserved.update(cells)
            return (GuardRecess(room_index, cells, actor_cell),)
    return ()


def _place_guard_gallery(tiles: list[int], things: list[int], rooms: list[Room],
                         identities: list[RoomIdentity], room_shapes: list[str],
                         reserved: set[tuple[int, int]], rng: random.Random,
                         start: tuple[int, int], eligible_rooms: frozenset[int]
                         ) -> tuple[GuardGallery, ...]:
    """Partition one optional symmetric room into a rare firing gallery.

    A complete line of matched pillars is the chamber's only open face. The
    floor remains one sound zone, but collision-aware reachability proves the
    rear cells cannot be entered. Reserving the entire rear chamber before the
    general population/pickup/decor passes gives the gallery exclusive
    ownership of both its actors and its deliberately empty floor.
    """
    suitable_concepts = {"war-room", "trophy-hall", "gallery", "courtyard",
                         "guardpost", "checkpoint"}
    candidates = [index for index in eligible_rooms
                  if index and room_shapes[index] == "rectangle"
                  and identities[index].concept in suitable_concepts
                  and min(rooms[index].w, rooms[index].h) >= 7
                  and max(rooms[index].w, rooms[index].h) >= 9]
    rng.shuffle(candidates)
    candidates.sort(key=lambda index: (
        identities[index].concept not in {"war-room", "trophy-hall", "gallery"},
        abs(rooms[index].w * rooms[index].h - 80)))
    for room_index in candidates:
        room = rooms[room_index]
        entries = []
        ring = ({(x, y) for x in range(room.x, room.x + room.w)
                 for y in (room.y, room.y + room.h - 1)}
                | {(x, y) for x in (room.x, room.x + room.w - 1)
                   for y in range(room.y, room.y + room.h)})
        for x, y in ring:
            if any((nx < room.x or nx >= room.x + room.w
                    or ny < room.y or ny >= room.y + room.h)
                   and (_is_floor(_at(tiles, nx, ny))
                        or _at(tiles, nx, ny) in DOORS)
                   for nx, ny in ((x + 1, y), (x - 1, y),
                                  (x, y + 1), (x, y - 1))):
                entries.append((x, y))
        if not entries:
            continue

        arrangements: list[tuple[tuple[tuple[int, int], ...],
                                 tuple[tuple[int, int], ...], int]] = []
        # (screen, rear cells, actor facing toward the accessible half)
        if room.w <= 9 and room.h >= 9:
            divider = room.y + room.h // 2
            if all(y < divider for _, y in entries):
                arrangements.append((
                    tuple((x, divider) for x in range(room.x, room.x + room.w)),
                    tuple((x, y) for y in range(divider + 1, room.y + room.h)
                          for x in range(room.x, room.x + room.w)), 0))
            if all(y > divider for _, y in entries):
                arrangements.append((
                    tuple((x, divider) for x in range(room.x, room.x + room.w)),
                    tuple((x, y) for y in range(room.y, divider)
                          for x in range(room.x, room.x + room.w)), 2))
        if room.h <= 9 and room.w >= 9:
            divider = room.x + room.w // 2
            if all(x < divider for x, _ in entries):
                arrangements.append((
                    tuple((divider, y) for y in range(room.y, room.y + room.h)),
                    tuple((x, y) for x in range(divider + 1, room.x + room.w)
                          for y in range(room.y, room.y + room.h)), 3))
            if all(x > divider for x, _ in entries):
                arrangements.append((
                    tuple((divider, y) for y in range(room.y, room.y + room.h)),
                    tuple((x, y) for x in range(room.x, divider)
                          for y in range(room.y, room.y + room.h)), 1))
        rng.shuffle(arrangements)
        for screen, rear_cells, facing in arrangements:
            occupied = set(screen) | set(rear_cells)
            if (any(not _is_floor(_at(tiles, *cell)) or _at(things, *cell)
                    or cell in reserved for cell in occupied)
                    or len(screen) > 9):
                continue
            reachable = _reachable(tiles, start, locked_open=True,
                                   blocked=set(screen))
            if any(cell in reachable for cell in rear_cells):
                continue
            if facing in (0, 2):
                rear_y = (screen[0][1] + (2 if facing == 0 else -2))
                offset = max(1, room.w // 4)
                actors = ((room.center[0] - offset, rear_y),
                          (room.center[0] + offset, rear_y))
            else:
                rear_x = (screen[0][0] + (2 if facing == 3 else -2))
                offset = max(1, room.h // 4)
                actors = ((rear_x, room.center[1] - offset),
                          (rear_x, room.center[1] + offset))
            if (actors[0] == actors[1] or any(cell not in rear_cells for cell in actors)):
                continue
            for cell in screen:
                _set(things, *cell, 30)  # one matched white-pillar screen
            reserved.update(screen)
            reserved.update(rear_cells)
            return (GuardGallery(room_index, screen, actors, rear_cells, facing),)
    return ()


def _populate_guard_galleries(galleries: tuple[GuardGallery, ...], things: list[int],
                              number: int, rng: random.Random,
                              encounters: list[EncounterPlacement]
                              ) -> tuple[int, int, int]:
    """Give each gallery exactly one mirrored pair of stationary guards."""
    tiers = [0, 0, 0]
    for gallery in galleries:
        tier = 1 if number >= 7 and rng.random() < 0.45 else 0
        code = GUARDS[gallery.facing] + 36 * tier
        placed = []
        for x, y in gallery.actor_cells:
            if _at(things, x, y):
                raise ValueError("guard gallery actor cell was preempted")
            _set(things, x, y, code)
            placed.append((x, y, code))
            tiers[tier] += 1
        encounters.append(EncounterPlacement(
            "guard-gallery", gallery.room_index, tuple(placed),
            hidden_cells=gallery.actor_cells, family="guard"))
    return tuple(tiers)


def _pick_secret_variant(rng: random.Random, used: list[str]) -> str:
    variants = [("square", 0.25), ("vault", 0.25), ("reliquary", 0.20),
                ("gallery", 0.18), ("nested", 0.12)]
    available = [(name, weight) for name, weight in variants if used.count(name) < 2]
    if len(used) >= 2 and len(set(used)) == 1:
        available = [(name, weight) for name, weight in available if name != used[0]]
    return rng.choices([name for name, _ in available],
                       weights=[weight for _, weight in available], k=1)[0]


def _secret_reward(rng: random.Random, depth: float,
                   premium: bool = False, lesser: bool = False,
                   quality: int = 3, allow_one_up: bool = True) -> int:
    if lesser:
        return rng.choices((AMMO, TREASURE[0], TREASURE[1]),
                           weights=(4.0 - 2.5 * depth, 2.0, 1.5 + depth), k=1)[0]
    quality_scale = 0.55 + 0.225 * quality
    if premium:
        if allow_one_up and rng.random() < 0.05:
            return ONE_UP
        choices = (TREASURE[2], TREASURE[3], MACHINE_GUN, CHAINGUN)
        weights = (2.2, 0.8 + depth * quality_scale, 0.5 + depth * quality_scale,
                   0.1 + depth * max(0.2, quality - 2) * 0.7)
        return rng.choices(choices, weights=weights, k=1)[0]
    choices = (AMMO, TREASURE[0], TREASURE[1], TREASURE[2], TREASURE[3],
               MACHINE_GUN, CHAINGUN, ONE_UP)
    weights = (4.5 * (1.0 - depth) + 0.2,
               3.0 * (1.0 - depth) + 0.8,
               2.4 * (1.0 - depth) + 0.8,
               0.5 + 2.0 * depth, 0.4 + 2.5 * depth,
               0.4 + 1.8 * depth, 0.1 + 2.2 * depth,
               0.03 + 0.65 * depth * depth)
    return rng.choices(choices, weights=weights, k=1)[0]


def _place_secret(tiles: list[int], things: list[int], room: Room,
                  rng: random.Random, variant: str, depth: float,
                  secret_exit: bool = False, *, reward_quality: int = 3,
                  number: int = 0,
                  protected: set[tuple[int, int]] | None = None,
                  direction: int = 1,
                  ) -> tuple[tuple[int, int], str, tuple[int, int]] | None:
    if direction not in (-1, 1):
        raise ValueError("secret push direction must be horizontal")
    px = room.x + room.w if direction == 1 else room.x - 1
    if not 1 <= px < GRID - 1:
        return None
    # Sweep rows outward from the wall's midline so one crossing corridor
    # doesn't doom the whole room's secret.
    mid = room.y + room.h // 2
    rows = sorted(range(room.y + 1, room.y + room.h - 1), key=lambda y: abs(y - mid))
    for py in rows:
        reward = _carve_secret_pocket(tiles, things, px, py, rng, secret_exit,
                                      variant, depth, reward_quality=reward_quality,
                                      number=number,
                                      protected=protected,
                                      direction=direction)
        if reward:
            return reward, variant, (px, py)
    return None


def _carve_secret_pocket(tiles: list[int], things: list[int], px: int, py: int,
                         rng: random.Random, secret_exit: bool,
                         variant: str = "square", depth: float = 0.5,
                         *, reward_quality: int = 3,
                         number: int = 0,
                         protected: set[tuple[int, int]] | None = None,
                         direction: int = 1,
                         ) -> tuple[int, int] | None:
    """Carve one purpose-built, rock-shelled horizontal secret pocket."""
    if direction not in (-1, 1):
        raise ValueError("secret push direction must be horizontal")
    point = lambda dx, dy=0: (px + direction * dx, py + dy)
    if (_at(tiles, px, py) != WALL
            or not _is_floor(_at(tiles, px - direction, py))):
        return None

    if variant == "square":
        cells = {point(dx, dy) for dx in range(1, 4) for dy in range(-1, 2)}
    elif variant == "vault":
        cells = {point(dx, dy) for dx in range(1, 7) for dy in range(-1, 2)}
    elif variant == "reliquary":
        side = rng.choice((-1, 1))
        cells = ({point(dx, dy) for dx in range(1, 4) for dy in range(-1, 2)}
                 | {point(dx, side * dy) for dx in range(3, 6)
                    for dy in range(1, 4)})
    elif variant == "gallery":
        cells = ({point(dx, dy) for dx in range(1, 4) for dy in range(-1, 2)}
                 | {point(dx, dy) for dx in range(3, 6) for dy in range(-2, 3)})
    elif variant == "nested":
        cells = {point(dx, dy) for dx in range(1, 8) for dy in range(-1, 2)}
        cells -= {point(4, dy) for dy in (-1, 0, 1)}
    else:
        return None

    inner_wall = ({point(4, dy) for dy in (-1, 0, 1)}
                  if variant == "nested" else set())
    entry = (px, py)
    back = (max(x for x, _ in cells) if direction == 1
            else min(x for x, _ in cells))
    elevator_rows = sorted(
        (y for x, y in cells if x == back
         and (back - direction, y) in cells
         and (back - 2 * direction, y) in cells),
        key=lambda y: (abs(y - py), y))
    if secret_exit and not elevator_rows:
        return None
    elevator_y = elevator_rows[0] if elevator_rows else py
    shell = {neighbor for x, y in cells
             for neighbor in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))
             if neighbor not in cells}
    shell.discard(entry)
    shell |= inner_wall
    # A secret elevator is a real enclosed car, not a switch texture pasted
    # onto the pocket's back wall. Reserve the same five-deep, five-wide rock
    # envelope as a normal elevator before carving anything: door at depth 0,
    # two floor cells, tile-21 side rails/back wall, and untouched outer rock.
    elevator_door = (back + direction, elevator_y)
    elevator_footprint = (
        {(elevator_door[0] + direction * depth, elevator_y + side)
         for depth in range(5) for side in (-2, -1, 0, 1, 2)}
        if secret_exit else set())
    footprint = cells | shell | {entry} | elevator_footprint
    if any(not (1 <= x < GRID - 1 and 1 <= y < GRID - 1)
           or _at(tiles, x, y) != WALL or _at(things, x, y) != 0
           for x, y in footprint):
        return None

    for cell in cells:
        _set(tiles, *cell, FLOOR)
    if variant == "nested":
        _set(things, px + 4 * direction, py, PUSHWALL)

    rests = {point(2)}
    if variant == "nested":
        rests.add(point(6))
    candidates = sorted((cell for cell in cells if cell not in rests
                         and (variant != "nested"
                              or cell[0] != px + 4 * direction)),
                        key=lambda point: (-direction * point[0],
                                           abs(point[1] - py), point[1]))
    if secret_exit:
        # Preserve a clear, legible approach from the reward chamber to the
        # elevator door instead of piling loot in front of it.
        candidates = [point for point in candidates
                      if point not in {(back, elevator_y),
                                       (back - direction, elevator_y)}]
    reward_count = 7 if number == 9 else 3
    if len(candidates) < reward_count:
        return None

    if number == 9:
        # Boss-floor secrets are preparation caches, not ordinary treasure
        # cupboards. Four clips make the discovery materially change the
        # coming fight, while a weapon, first-aid, and premium slot provide
        # an exciting upgrade without making the secret mandatory to win.
        chaingun_chance = min(0.85, 0.20 + 0.10 * reward_quality + 0.20 * depth)
        weapon = CHAINGUN if rng.random() < chaingun_chance else MACHINE_GUN
        one_up_chance = min(0.40, 0.10 + 0.05 * reward_quality)
        premium = (ONE_UP if ONE_UP not in things and rng.random() < one_up_chance
                   else _secret_reward(rng, depth, premium=True,
                                       quality=reward_quality, allow_one_up=False))
        rewards = [AMMO, AMMO, AMMO, AMMO, weapon, FIRST_AID, premium]
    elif secret_exit:
        # The special elevator pocket is a discovery sequence: a premium
        # focal reward at the deepest readable point, useful recovery near
        # it, and one high-value treasure accent. It is not an ordinary
        # cupboard whose switch happened to fit.
        rewards = [
            _secret_reward(rng, max(0.7, depth), premium=True,
                           quality=reward_quality,
                           allow_one_up=ONE_UP not in things),
            FIRST_AID if reward_quality >= 3 else AMMO,
            rng.choice(TREASURE[2:]),
        ]
    else:
        treasure_weights = (max(0.2, 2.5 - depth * reward_quality),
                            1.5, 0.5 + depth * reward_quality,
                            0.2 + depth * reward_quality)
        rewards = [rng.choices(TREASURE, weights=treasure_weights, k=1)[0]]
        useful_choices = (AMMO, FOOD, FIRST_AID)
        useful_weights = (3.0, max(0.2, 3.0 - reward_quality * 0.4),
                          0.5 + reward_quality * 0.7)
        rewards.append(rng.choices(useful_choices, weights=useful_weights, k=1)[0])
        rewards.append(_secret_reward(rng, depth, premium=True, quality=reward_quality,
                                      allow_one_up=ONE_UP not in things))
    if variant == "nested" and not secret_exit:
        # A double secret must not read as an obvious empty antechamber: the
        # first chamber holds the bulk of the reward so opening the outer wall
        # already feels complete, and the second (behind an unmarked wall) is
        # a genuine bonus for the thorough player rather than the only payoff.
        depth_of = lambda cell: direction * (cell[0] - px)
        first_cells = [cell for cell in candidates if depth_of(cell) < 4]
        second_cells = [cell for cell in candidates if depth_of(cell) > 4]
        n_second = max(1, reward_count // 3)
        n_first = reward_count - n_second
        reward_cells = first_cells[:n_first] + second_cells[:n_second]
        if len(reward_cells) < reward_count:
            spare = [cell for cell in candidates if cell not in reward_cells]
            reward_cells += spare[:reward_count - len(reward_cells)]
    else:
        reward_cells = candidates[:reward_count]
    for cell, item in zip(reward_cells, rewards):
        _set(things, *cell, item)

    if secret_exit:
        wx, wy = elevator_door
        for depth in (1, 2):
            _set(tiles, wx + direction * depth, wy, FLOOR)
        for depth in (1, 2, 3):
            for side in (-1, 1):
                _set(tiles, wx + direction * depth, wy + side, ELEVATOR_TILE)
        _set(tiles, wx + direction * 3, wy, ELEVATOR_TILE)
        _set(tiles, wx, wy, DOOR_ELEVATOR)
        _set(tiles, wx + direction * 2, wy, SECRET_EXIT_ZONE)
    _set(things, *entry, PUSHWALL)
    if protected is not None:
        protected.update(footprint)
        protected.update(reward_cells)
    return reward_cells[0]


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


def _spread_actor_cells(candidates: list[tuple[int, int]], count: int,
                        occupied: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Take ``count`` ranked slots while keeping actors out of each other's laps.

    The encounter templates rank every interior cell, and taking the ranked
    prefix wholesale hands back a contiguous blob: `visible-sentry` files its
    guards along one line at the same distance from the entry, `strongpoint`
    packs them into the far corner. A room composition should read as posted
    positions covering the space, so each slot is skipped while it sits inside
    an already-chosen actor's personal space. Rank order still decides who is
    offered a slot first, so every template keeps its shape.

    The spacing relaxes to nothing over successive sweeps: a cramped room must
    still fill its budget rather than silently under-populate.
    """
    if count <= 0:
        return []
    picked: list[tuple[int, int]] = []
    used: set[tuple[int, int]] = set()
    for spacing in range(ACTOR_SPACING, 0, -1):
        for cell in candidates:
            if len(picked) >= count:
                break
            if cell in used:
                continue
            if any(max(abs(cell[0] - x), abs(cell[1] - y)) < spacing
                   for x, y in occupied + picked):
                continue
            picked.append(cell)
            used.add(cell)
        if len(picked) >= count:
            break
    return picked


def _place_population(config: CampaignConfig, number: int, rooms: list[Room],
                      tiles: list[int], things: list[int], reserved: set[tuple[int, int]],
                      rng: random.Random, start: tuple[int, int],
                      exit_room: Room, *, patrol_chance: float = 0.15,
                      placements: list[SpritePlacement] | None = None,
                      actor_clearance: set[tuple[int, int]] | None = None,
                      progression_number: int | None = None,
                      calm_rooms: frozenset[int] = frozenset(),
                      boss_room: Room | None = None,
                      optional_rooms: frozenset[int] = frozenset(),
                      identities: list[RoomIdentity] | None = None,
                      critical_route: tuple[int, ...] = (),
                      guard_recesses: tuple[GuardRecess, ...] = (),
                      key_objectives: tuple[KeyObjective, ...] = (),
                      encounter_out: list[EncounterPlacement] | None = None
                      ) -> tuple[int, int, int]:
    """Plan coherent room encounters, then realize their actor slots.

    ``patrol_chance`` is retained as an API name for compatibility, but now
    means the desired moving-actor share rather than a per-room coin flip.
    Every actor placed here belongs to one recorded room composition.
    """
    progression = min(1.0, ((progression_number or number) - 1) / 8)
    per_room = max(1, round(config.guard_density * .7 + progression * 2))
    toughness = int(config.enemy_toughness)
    # Normal and above use the complete classic roster. Lower settings retain
    # the gentler progressive unlock without making SS absent from defaults.
    unlocked_count = len(ENEMY_FAMILIES) if toughness >= 3 else max(1, toughness)
    unlocked = ENEMY_FAMILIES[:unlocked_count]
    names = [name for name, *_ in unlocked]
    families = [family for _, family, *_ in unlocked]
    base_weights = [weight for _, _, weight, _ in unlocked]
    # Keep officers away from point-blank door breaches. SS remain eligible
    # here: suppressing them around every door made the family much rarer than
    # its roster weight implied on ordinary door-heavy layouts.
    doors = {(x, y) for y in range(GRID) for x in range(GRID) if _at(tiles, x, y) in DOORS}

    def near_door(x: int, y: int, radius: int = 3) -> bool:
        return any(abs(x - dx) + abs(y - dy) <= radius for dx, dy in doors)

    def pick_family(depth: float, concept: str, template: str
                    ) -> tuple[str, tuple[int, ...]]:
        """Choose one primary family for the whole room composition."""
        elite_scale = 0.45 if depth < 0.2 else (1.35 if 0.6 <= depth <= 0.85 else 1.0)
        weights = []
        for name, weight in zip(names, base_weights):
            if name in ("officer", "ss"):
                weight *= (1 + progression) * elite_scale
            if template in ("objective-guard", "strongpoint"):
                weight *= 1.5 if name in ("guard", "officer", "ss") else 0.25
            if concept in ("barracks", "ready-room", "guardpost") and name == "dog":
                weight *= 1.35
            if concept in ("gallery", "lounge", "dining-hall") and name == "dog":
                weight *= 0.35
            weights.append(weight)
        index = rng.choices(range(len(families)), weights=weights, k=1)[0]
        return names[index], families[index]

    facings = ((0, -1), (1, 0), (0, 1), (-1, 0))
    dog_cells: dict[Room, list[tuple[int, int]]] = {}

    def place_enemy(x: int, y: int, tier: int, name: str,
                    family: tuple[int, ...], room: Room | None = None,
                    forced_facing: int | None = None, patrol: bool = False
                    ) -> tuple[int, int, int]:
        if name == "officer" and near_door(x, y):
            name, family = "guard", GUARDS
        if forced_facing is not None:
            facing = forced_facing
        elif room is not None:
            facing = _pick_stationary_facing(x, y, room)
        elif open_facings := [i for i in range(4)
                              if _is_floor(_at(tiles, x + facings[i][0],
                                               y + facings[i][1]))]:
            facing = rng.choice(open_facings)
        else:
            facing = rng.randrange(4)
        if patrol:
            family = PATROLS_BY_FAMILY[family]
        code = family[facing] + 36 * tier
        _set(things, x, y, code)
        if name == "dog" and room is not None:
            dog_cells.setdefault(room, []).append((x, y))
        # Decoration placement runs after population and only checks that a
        # cell is empty, not who's facing it; reserve the tile directly ahead
        # so a later pillar/barrel/table can't get dropped in a stationary
        # actor's face.
        dx, dy = facings[facing]
        facing_cell = (x + dx, y + dy)
        reserved.add(facing_cell)
        if actor_clearance is not None:
            actor_clearance.add(facing_cell)
        return x, y, code

    distances = _floor_distances(tiles, start)
    room_distances = {room: distances.get(room.center, 0) for room in rooms}
    max_distance = max(room_distances.values(), default=1) or 1

    # Collect corridor/door cells adjacent to each room's boundary, then
    # restrict to approach-side entries (BFS distance from start < room's own
    # depth) so actors face the door the player arrived through, not a back
    # door leading deeper into the level.  Falls back to all entries for the
    # start room or any room with no closer-than-self adjacent cells.
    room_entries: dict[Room, list[tuple[int, int]]] = {}
    for _room in rooms:
        _entries: list[tuple[int, int]] = []
        for _ry in range(_room.y, _room.y + _room.h):
            for _nx in (_room.x - 1, _room.x + _room.w):
                _t = _at(tiles, _nx, _ry)
                if _is_floor(_t) or _t in DOORS:
                    _entries.append((_nx, _ry))
        for _rx in range(_room.x, _room.x + _room.w):
            for _ny in (_room.y - 1, _room.y + _room.h):
                _t = _at(tiles, _rx, _ny)
                if _is_floor(_t) or _t in DOORS:
                    _entries.append((_rx, _ny))
        _room_d = room_distances[_room]
        _approach = [e for e in _entries if distances.get(e, float('inf')) < _room_d]
        room_entries[_room] = _approach or _entries or [_room.center]

    def _entry_pull(x: int, y: int, idx: int,
                    entries: list[tuple[int, int]]) -> float:
        dx, dy = facings[idx]
        best = -1e9
        for ex, ey in entries:
            vx, vy = ex - x, ey - y
            para = dx * vx + dy * vy
            if para <= 0:
                continue
            perp = abs(dy * vx - dx * vy)
            score = para - 2 * perp
            if score > best:
                best = score
        return best

    def _clear_ahead(x: int, y: int, idx: int, cap: int = 8) -> int:
        dx, dy = facings[idx]
        n = 0
        while n < cap and _is_floor(_at(tiles, x + dx * (n + 1),
                                        y + dy * (n + 1))):
            n += 1
        return n

    def _pick_stationary_facing(x: int, y: int, room: Room) -> int:
        entries = room_entries.get(room) or [room.center]
        pulls = [_entry_pull(x, y, i, entries) for i in range(4)]
        clears = [_clear_ahead(x, y, i) for i in range(4)]
        # Require at least 1 open tile ahead so the actor doesn't nose into a
        # wall.  Secondary sort on clear count breaks pull ties and prevents
        # actors from facing into corners when all pulls are equal or degenerate.
        open_idxs = [i for i in range(4) if clears[i] >= 1]
        pool = open_idxs or list(range(4))
        return max(pool, key=lambda i: (pulls[i], clears[i]))

    def _inside_entries(room: Room) -> list[tuple[int, int]]:
        inside = []
        for ex, ey in room_entries.get(room, (room.center,)):
            candidates = [(ex + dx, ey + dy) for dx, dy in facings
                          if room.x <= ex + dx < room.x + room.w
                          and room.y <= ey + dy < room.y + room.h
                          and _is_floor(_at(tiles, ex + dx, ey + dy))]
            inside.extend(candidates)
        return list(dict.fromkeys(inside)) or [room.center]

    def _line_visible(origin: tuple[int, int], target: tuple[int, int]) -> bool:
        """Grid ray used only to classify deliberate doorway reveals."""
        x0, y0 = origin
        x1, y1 = target
        dx, dy = abs(x1 - x0), abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        error = dx - dy
        x, y = x0, y0
        while (x, y) != (x1, y1):
            twice = 2 * error
            if twice > -dy:
                error -= dy; x += sx
            if twice < dx:
                error += dx; y += sy
            if (x, y) != (x1, y1):
                tile = _at(tiles, x, y)
                if not _is_floor(tile) and tile not in DOORS:
                    return False
        return True

    def depth_of(room: Room) -> float:
        return room_distances[room] / max_distance

    def pacing(depth: float) -> float:
        if depth < 0.2:
            return 0.4
        if depth < 0.6:
            return 0.4 + (depth - 0.2) * 2.75
        if depth <= 0.85:
            return 1.5
        if depth < 0.9:
            return 1.5 - (depth - 0.85) * 14
        return 0.8

    def _loop_route(left: int, right: int, top: int, bottom: int,
                    kind: str) -> PatrolRoute | None:
        if right - left < 2 or bottom - top < 2:
            return None
        path = ([(x, top) for x in range(left, right + 1)]
                + [(right, y) for y in range(top + 1, bottom + 1)]
                + [(x, bottom) for x in range(right - 1, left - 1, -1)]
                + [(left, y) for y in range(bottom - 1, top, -1)])
        corners = {(left, top), (right, top), (right, bottom), (left, bottom)}
        turns = tuple((cell, next(i for i, delta in enumerate(facings)
                                  if (cell[0] + delta[0], cell[1] + delta[1])
                                  == path[(index + 1) % len(path)]))
                      for index, cell in enumerate(path) if cell in corners)
        return PatrolRoute(kind, tuple(path), turns)

    def _straight_run(cell: tuple[int, int], horizontal: bool) -> list[tuple[int, int]]:
        axis = (1, 0) if horizontal else (0, 1)
        allowed_doors = {DOOR_EW if horizontal else DOOR_NS}
        before = []
        cursor = cell
        while True:
            cursor = cursor[0] - axis[0], cursor[1] - axis[1]
            tile = _at(tiles, *cursor)
            if not (_is_floor(tile) or tile in allowed_doors):
                break
            before.append(cursor)
        after = []
        cursor = cell
        while True:
            cursor = cursor[0] + axis[0], cursor[1] + axis[1]
            tile = _at(tiles, *cursor)
            if not (_is_floor(tile) or tile in allowed_doors):
                break
            after.append(cursor)
        return list(reversed(before)) + [cell] + after

    def _patrol_routes(room: Room) -> list[PatrolRoute]:
        routes: list[PatrolRoute] = []
        if room.w >= 7 and room.h >= 7:
            for inset, kind in ((2, "room-loop"), (1, "compact-loop")):
                route = _loop_route(room.x + inset, room.x + room.w - 1 - inset,
                                    room.y + inset, room.y + room.h - 1 - inset,
                                    kind)
                if route is not None:
                    routes.append(route)
        horizontal = room.w >= room.h
        cross_offsets = (0, -1, 1, -2, 2)
        seen_runs: set[tuple[tuple[int, int], ...]] = set()
        for offset in cross_offsets:
            seed = ((room.center[0], room.center[1] + offset) if horizontal else
                    (room.center[0] + offset, room.center[1]))
            if not _is_floor(_at(tiles, *seed)):
                continue
            run = _straight_run(seed, horizontal)
            run_key = tuple(run)
            if len(run) < 6 or run_key in seen_runs:
                continue
            seen_runs.add(run_key)
            axis_facing = 1 if horizontal else 2
            reverse_facing = 3 if horizontal else 0
            kind = ("doorway-shuttle" if any(_at(tiles, *cell) in DOORS for cell in run)
                    else "hall-shuttle")
            routes.append(PatrolRoute(
                kind, run_key,
                ((run[0], axis_facing), (run[-1], reverse_facing))))
        rng.shuffle(routes)
        return routes

    def _route_available(route: PatrolRoute) -> bool:
        turn_cells = {cell for cell, _ in route.turns}
        return (len(set(route.cells)) > len(turn_cells)
                and all((_is_floor(_at(tiles, *cell)) or _at(tiles, *cell) in
                         (DOOR_EW, DOOR_NS))
                        and _at(things, *cell) == 0 and cell not in reserved
                        and abs(cell[0] - start[0]) + abs(cell[1] - start[1]) >= 6
                        for cell in route.cells)
                and all(_is_floor(_at(tiles, *cell)) for cell in turn_cells))

    identities = identities or [RoomIdentity("beat", "standard", "spine", 0,
                                              "", "guardpost", "barracks")
                                  for _ in rooms]
    critical_positions = {room_index: position
                          for position, room_index in enumerate(critical_route)}
    key_hosts = {objective.host_room for objective in key_objectives
                 if objective.treatment != "boss-drop"}
    recess_by_room = {recess.room_index: recess for recess in guard_recesses}

    budgets: dict[int, int] = {}
    for ridx, room in enumerate(rooms[1:], 1):
        depth = depth_of(room)
        budget = max(0, round(per_room * ACTOR_BUDGET_SCALE
                              * (0.4 if room == exit_room else pacing(depth))))
        if ridx in calm_rooms:
            budget = 0
        elif room == boss_room:
            budget = 0 if rng.random() < 0.55 else min(2, budget)
        budgets[ridx] = budget

    # Plan routes globally until the requested moving share is met. A route
    # owns its cells before any stationary actor or later decoration exists.
    estimated_actors = sum(
        budget + 2 * max(0, round(budget * (0.20 + progression * 0.12)))
        for budget in budgets.values())
    max_routes_per_room = 2 if patrol_chance >= 0.23 else 1
    patrol_capacity = sum(min(budget, max_routes_per_room)
                          for budget in budgets.values())
    patrol_target = min(patrol_capacity, round(estimated_actors * patrol_chance))
    patrol_rooms = [index for index, budget in budgets.items()
                    if budget and index not in calm_rooms and rooms[index] != boss_room
                    and index not in recess_by_room and index not in key_hosts]
    rng.shuffle(patrol_rooms)
    patrol_rooms.sort(key=lambda index: (
        identities[index].tier not in ("corridor", "hall"),
        -depth_of(rooms[index])))
    planned_patrols: dict[int, list[PatrolRoute]] = {}
    for ridx in patrol_rooms:
        if sum(len(routes) for routes in planned_patrols.values()) >= patrol_target:
            break
        routes = _patrol_routes(rooms[ridx])
        if max_routes_per_room > 1:
            routes.sort(key=lambda route: route.kind not in
                        ("hall-shuttle", "doorway-shuttle"))
        for route in routes:
            routes_here = len(planned_patrols.get(ridx, ()))
            if (routes_here >= max_routes_per_room
                    or routes_here >= budgets[ridx]):
                break
            if not _route_available(route):
                continue
            planned_patrols.setdefault(ridx, []).append(route)
            for cell, direction in route.turns:
                _set(things, *cell, PATROL_POINT_CODES[direction])
            reserved.update(route.cells)
            if sum(len(routes) for routes in planned_patrols.values()) >= patrol_target:
                break

    tier_counts = [0, 0, 0]
    encounter_counts: Counter[str] = Counter()
    previous_template = ""
    ambush_positions: set[int] = set()
    ambush_budget = max(1, round(sum(bool(value) for value in budgets.values()) * 0.18))
    for ridx, room in enumerate(rooms[1:], 1):
        depth = depth_of(room)
        base_budget = budgets[ridx]
        budget = base_budget
        identity = identities[ridx]
        entries = _inside_entries(room)
        primary_entry = min(entries, key=lambda cell: distances.get(cell, 10 ** 9))
        candidates = [(x, y) for y in range(room.y + 1, room.y + room.h - 1)
                      for x in range(room.x + 1, room.x + room.w - 1)
                      if (x, y) not in reserved and _at(things, x, y) == 0
                      and _is_floor(_at(tiles, x, y))
                      and abs(x - start[0]) + abs(y - start[1]) >= 6]
        hidden_candidates = [cell for cell in candidates
                             if not any(_line_visible(entry, cell) for entry in entries)]
        critical_position = critical_positions.get(ridx)
        can_ambush = (hidden_candidates and len(ambush_positions) < ambush_budget
                      and depth >= 0.25 and room != exit_room and ridx not in calm_rooms
                      and (critical_position is None
                           or all(abs(critical_position - other) > 1
                                  for other in ambush_positions)))
        if ridx in recess_by_room:
            template = "blind-corner-ambush"
        elif ridx in planned_patrols:
            template = "patrol"
        elif ridx in key_hosts:
            template = "objective-guard"
        elif room == boss_room:
            template = "boss-support"
        elif can_ambush and rng.random() < 0.55:
            template = "blind-corner-ambush"
        elif identity.concept in ("checkpoint", "guardpost"):
            template = "visible-sentry"
        elif identity.concept in ("armory", "war-room", "training-room",
                                  "interrogation-room"):
            template = "strongpoint"
        else:
            choices = ["visible-sentry", "staggered-flank", "strongpoint"]
            choices.sort(key=lambda name: (encounter_counts[name], name == previous_template))
            template = choices[0]
        if template == "blind-corner-ambush" and critical_position is not None:
            ambush_positions.add(critical_position)

        name, family = pick_family(depth, identity.concept, template)
        if ridx in recess_by_room:
            name, family = "guard", GUARDS
        placed_cells: list[tuple[int, int, int]] = []
        hidden_cells: list[tuple[int, int]] = []

        # A mirrored guard recess owns one deliberately hidden guard; the
        # matching recess stays clear to preserve the architectural pair.
        if budget and ridx in recess_by_room:
            recess = recess_by_room[ridx]
            actor = recess.actor_cell
            facing = (2 if actor[1] < room.y else 0
                      if actor[1] >= room.y + room.h else 1
                      if actor[0] < room.x else 3)
            record = place_enemy(*actor, 0, "guard", GUARDS, room,
                                 forced_facing=facing)
            placed_cells.append(record); hidden_cells.append(actor)
            tier_counts[0] += 1
            budget -= 1

        for route in planned_patrols.get(ridx, ()):
            if not budget:
                break
            turn_cells = {cell for cell, _ in route.turns}
            spawn_options = [cell for cell in route.cells
                             if cell not in turn_cells and _is_floor(_at(tiles, *cell))]
            spawn = rng.choice(spawn_options)
            index = route.cells.index(spawn)
            successor = route.cells[(index + 1) % len(route.cells)]
            if abs(successor[0] - spawn[0]) + abs(successor[1] - spawn[1]) != 1:
                successor = route.cells[index - 1]
            facing = next(i for i, delta in enumerate(facings)
                          if (spawn[0] + delta[0], spawn[1] + delta[1]) == successor)
            record = place_enemy(*spawn, 0, name, family, room,
                                 forced_facing=facing, patrol=True)
            tier_counts[0] += 1
            budget -= 1
            if encounter_out is not None:
                encounter_out.append(EncounterPlacement(
                    "patrol", ridx, (record,), (), route.kind, route.cells, name))
            encounter_counts["patrol"] += 1

        def rank(cell: tuple[int, int]) -> tuple[float, ...]:
            x, y = cell
            distance = abs(x - primary_entry[0]) + abs(y - primary_entry[1])
            route_dx, route_dy = room.center[0] - primary_entry[0], room.center[1] - primary_entry[1]
            side = abs(route_dy * (x - primary_entry[0])
                       - route_dx * (y - primary_entry[1]))
            visible = any(_line_visible(entry, cell) for entry in entries)
            if template == "blind-corner-ambush":
                return (visible, -distance, -side, y, x)
            if template == "visible-sentry":
                return (not visible, abs(distance - 5), side, y, x)
            if template == "staggered-flank":
                return (-side, abs(distance - 5), not visible, y, x)
            if template == "objective-guard":
                objectives = [objective.cell for objective in key_objectives
                              if objective.host_room == ridx]
                objective_distance = min((abs(x - ox) + abs(y - oy)
                                          for ox, oy in objectives), default=distance)
                return (abs(objective_distance - 3), not visible, -distance, y, x)
            return (-distance, not visible, -side, y, x)

        candidates.sort(key=rank)
        # ECWolf's base translator treats +36 as the next cumulative skill
        # tier: skill 2 actors join the easy population on medium, and skill 3
        # actors join both on hard. They require their own cells in plane 2.
        extra = max(0, round(base_budget * (0.20 + progression * 0.12)))
        # All three tiers are live together on hard, so they share one spacing
        # sweep; recess guards and patrol spawns already hold their own cells.
        slots = _spread_actor_cells(candidates, budget + 2 * extra,
                                    [(x, y) for x, y, _ in placed_cells])
        cursor = 0
        for x, y in slots[cursor:cursor + budget]:
            record = place_enemy(x, y, 0, name, family, room)
            placed_cells.append(record)
            if not any(_line_visible(entry, (x, y)) for entry in entries):
                hidden_cells.append((x, y))
            tier_counts[0] += 1
        cursor += budget
        for tier in (1, 2):
            for x, y in slots[cursor:cursor + extra]:
                record = place_enemy(x, y, tier, name, family, room)
                placed_cells.append(record)
                if not any(_line_visible(entry, (x, y)) for entry in entries):
                    hidden_cells.append((x, y))
                tier_counts[tier] += 1
            cursor += extra
        if placed_cells:
            if template == "patrol":
                template = ("strongpoint" if identity.concept in
                            ("armory", "war-room", "checkpoint") else
                            "staggered-flank")
            if encounter_out is not None:
                encounter_out.append(EncounterPlacement(
                    template, ridx, tuple(placed_cells), tuple(hidden_cells),
                    family=name))
            encounter_counts[template] += 1
            previous_template = template
    novelty = FAKE_HITLER if number == 9 else rng.choice(GHOSTS) if number == 10 else None
    if novelty is not None and rng.random() < NOVELTY_SPAWN_CHANCE:
        novelty_rooms = ([rooms[index] for index in sorted(optional_rooms)]
                         if optional_rooms else rooms)
        candidates = [(x, y) for room in novelty_rooms
                      for y in range(room.y + 1, room.y + room.h - 1)
                      for x in range(room.x + 1, room.x + room.w - 1)
                      if (x, y) not in reserved and _at(things, x, y) == 0
                      and _is_floor(_at(tiles, x, y))
                      and abs(x - start[0]) + abs(y - start[1]) >= 6]
        if candidates:
            cell = rng.choice(candidates)
            _set(things, *cell, novelty)
            reserved.add(cell)
            if encounter_out is not None:
                owner = next((index for index, room in enumerate(rooms)
                              if room.x <= cell[0] < room.x + room.w
                              and room.y <= cell[1] < room.y + room.h), -1)
                encounter_out.append(EncounterPlacement(
                    "novelty", owner, ((cell[0], cell[1], novelty),),
                    family="novelty"))
    # A human kennel has food near its dogs, not randomly elsewhere. Rank
    # dog rooms by pack size and depth and furnish at most three of them.
    ranked_dog_rooms = sorted(dog_cells, key=lambda room: (
        -len(dog_cells[room]), -room_distances.get(room, 0), room.y, room.x))
    for room in ranked_dog_rooms[:3]:
        pack = dog_cells[room]
        candidates = [(x, y) for y in range(room.y + 1, room.y + room.h - 1)
                      for x in range(room.x + 1, room.x + room.w - 1)
                      if (x, y) not in reserved and _at(things, x, y) == 0
                      and _is_floor(_at(tiles, x, y)) and (x, y) != room.center
                      and min(abs(x - dx) + abs(y - dy) for dx, dy in pack) <= 4
                      and min(x - room.x, room.x + room.w - 1 - x,
                              y - room.y, room.y + room.h - 1 - y) <= 2]
        rng.shuffle(candidates)
        candidates.sort(key=lambda cell: min(
            abs(cell[0] - dx) + abs(cell[1] - dy) for dx, dy in pack))
        if candidates:
            _set(things, *candidates[0], DOG_FOOD)
            reserved.add(candidates[0])
            if placements is not None:
                room_index = rooms.index(room)
                placements.append(SpritePlacement(
                    "kennel-support", "kennel-wall", room_index,
                    ((candidates[0][0], candidates[0][1], DOG_FOOD),)))
    return tuple(tier_counts)


def _place_authored_pickups(config: CampaignConfig, number: int, rooms: list[Room],
                            tiles: list[int], things: list[int],
                            reserved: set[tuple[int, int]], rng: random.Random,
                            start: tuple[int, int], identities: list[RoomIdentity],
                            critical_route: list[int], edges: list[tuple[int, int]],
                            placements: list[SpritePlacement],
                            preboss_index: int | None = None,
                            premium_index: int | None = None,
                            expedition_candidates: tuple[int, ...] = (),
                            expedition_rooms_out: list[int] | None = None) -> None:
    """Allocate gameplay needs, then realize each as an authored vignette."""
    grammar = _PlacementGrammar(rooms, tiles, things, reserved, identities, rng,
                                placements)
    distances = _floor_distances(tiles, start)
    max_distance = max((distances.get(room.center, 0) for room in rooms),
                       default=1) or 1
    depths = [distances.get(room.center, 0) / max_distance for room in rooms]
    degrees = [sum(index in edge for edge in edges) for index in range(len(rooms))]
    route_position = {room_index: index
                      for index, room_index in enumerate(critical_route)}
    vignette_counts: Counter[int] = Counter(
        placement.room_index for placement in placements if placement.room_index >= 0)

    def room_threat(room_index: int) -> float:
        room = rooms[room_index]
        return sum(AMMO_COST.get(FAMILY_BY_CODE.get(
            _at(things, x, y)), 0.0)
            for y in range(room.y, room.y + room.h)
            for x in range(room.x, room.x + room.w))

    threats = [room_threat(index) for index in range(len(rooms))]

    def place_group(items: tuple[int, ...], reason: str,
                    candidates: list[int], templates: tuple[str, ...]) -> bool:
        unique = list(dict.fromkeys(candidates))
        preference = {index: position for position, index in enumerate(unique)}
        ranked = sorted(unique, key=lambda index: (
            vignette_counts[index], identities[index].special in ("start", "exit", "boss"),
            identities[index].tier == "corridor", preference[index]))
        for room_index in ranked:
            room_templates = templates
            if (reason == "exploration-treasure"
                    and identities[room_index].concept in
                    ("gallery", "trophy-hall", "courtyard", "war-room")):
                room_templates += ("center-dais",)
            placement = grammar.place(room_index, items, reason, room_templates)
            if placement is not None:
                vignette_counts[room_index] += 1
                return True
        return False

    # The pre-boss room is a visible staging area, not loose supplies left on
    # arbitrary remaining population cells.
    if preboss_index is not None:
        loot = [FIRST_AID, AMMO]
        if rng.random() < 0.35:
            loot.append(rng.choice((MACHINE_GUN, CHAINGUN)))
        if rng.random() < 0.2:
            loot.append(ONE_UP)
        if not place_group(tuple(loot), "preboss-stockup", [preboss_index],
                           ("wall-cache", "corner-cache", "center-dais")):
            raise ValueError("pre-boss room cannot fit an authored stock-up cache")

    if number == 10 and premium_index is not None:
        premium_pool = [CHAINGUN, TREASURE[3]]
        if ONE_UP not in things:
            premium_pool.append(ONE_UP)
        premium = rng.choice(premium_pool)
        if not place_group((premium,), "floor-ten-premium", [premium_index],
                           ("center-dais", "treasure-display")):
            raise ValueError("floor 10 premium chamber cannot stage its focal reward")
        if expedition_rooms_out is not None:
            expedition_rooms_out.append(placements[-1].room_index)

        # Two to four open expeditions each tell a different supply story.
        # The family and identities select the rooms; the pickup grammar owns
        # exact geometry, preserving variation without free scatter.
        ordered_candidates = list(dict.fromkeys(expedition_candidates))
        rng.shuffle(ordered_candidates)
        ordered_candidates.sort(key=lambda index: (
            vignette_counts[index], identities[index].concept,
            -depths[index], index))
        selected: list[int] = []
        seen_concepts: set[str] = set()
        for index in ordered_candidates:
            concept = identities[index].concept
            if concept in seen_concepts and len(ordered_candidates) > 2:
                continue
            selected.append(index)
            seen_concepts.add(concept)
            if len(selected) == min(4, max(2, len(ordered_candidates) // 2)):
                break
        realized = 0
        for index in selected:
            concept = identities[index].concept
            if concept in ("armory", "training-room", "workshop"):
                items = (MACHINE_GUN, AMMO)
                templates = ("wall-cache", "corner-cache")
            elif concept in ("lounge", "dining-hall", "officers-quarters"):
                items = (FIRST_AID, FOOD)
                templates = ("recovery-station", "wall-cache")
            elif concept in ("supply-cache", "storage"):
                items = (AMMO, AMMO)
                templates = ("corner-cache", "wall-cache")
            else:
                items = (rng.choice(TREASURE[1:]), rng.choice(TREASURE[2:]))
                templates = ("treasure-display", "center-dais")
            if place_group(items, "floor-ten-expedition", [index], templates):
                realized += 1
                if expedition_rooms_out is not None:
                    expedition_rooms_out.append(placements[-1].room_index)
        if realized < 2:
            raise ValueError("floor 10 lacks two realized reward expeditions")

    # Guarantee one early recovery beat through the same grammar. Existing
    # secret health does not count because closed pushwalls are not in this
    # distance field.
    within = {cell for cell, distance in distances.items() if distance <= 20}
    if not any(_at(things, *cell) in (DOG_FOOD, FOOD, FIRST_AID)
               for cell in within):
        early = [index for index in critical_route[:max(2, len(critical_route) // 4)]
                 if identities[index].special not in ("exit", "boss")]
        early.sort(key=lambda index: (
            identities[index].concept not in
            ("mess-kitchen", "officers-quarters", "lounge", "barracks"),
            route_position[index]))
        if not place_group((FOOD,), "early-recovery", early,
                           ("recovery-station", "wall-cache")):
            raise ValueError("early route cannot fit an authored recovery item")

    # Preserve the expected-bullet-sink economy, but count and distribute
    # clips only after encounters exist. Necessary ammo stays on the mandatory
    # route, staged before its most expensive forthcoming rooms.
    expected_need = sum(AMMO_COST.get(FAMILY_BY_CODE.get(code), 0.0)
                        for code in things if code)
    supply_scale = (1.0 if number in AMMO_SUPPLY_EXEMPT_FLOORS
                    else AMMO_SUPPLY_SCALE)
    target_ratio = (1.15 + 0.05 * int(config.supplies)) * supply_scale
    styled_items = [item for placement in placements
                    for _, _, item in placement.cells]
    ammo_target = max(0, math.ceil((expected_need * target_ratio
                                   - (8 + 8 * styled_items.count(AMMO))) / 8))
    ammo_rooms = list(critical_route[:-1])
    ammo_rooms.sort(key=lambda index: (
        identities[index].concept not in
        ("supply-cache", "armory", "storage", "checkpoint", "guardpost",
         "workshop", "war-room", "corridor"),
        -threats[critical_route[min(len(critical_route) - 1,
                                   route_position[index] + 1)]],
        route_position[index]))
    while ammo_target:
        count = min(2, ammo_target)
        if not place_group((AMMO,) * count, "route-ammo", ammo_rooms,
                           ("entry-staging", "wall-cache", "corner-cache")):
            raise ValueError("mandatory route cannot fit required authored ammo")
        ammo_target -= count

    total_enemies = sum(1 for code in things if code in FAMILY_BY_CODE)
    health_target = max(1, total_enemies // max(6, 14 - int(config.supplies)))
    health_now = sum(item in (DOG_FOOD, FOOD, FIRST_AID) for item in styled_items)
    health_needed = max(0, health_target - health_now)
    health_rooms = list(critical_route[1:-1])
    health_rooms.sort(key=lambda index: (
        identities[index].concept not in
        ("mess-kitchen", "officers-quarters", "lounge", "barracks",
         "ready-room", "dining-hall"),
        -threats[critical_route[max(0, route_position[index] - 1)]],
        route_position[index]))
    while health_needed:
        count = min(2, health_needed)
        if not place_group((FIRST_AID,) * count, "post-combat-recovery",
                           health_rooms,
                           ("recovery-station", "wall-cache", "corner-cache")):
            raise ValueError("mandatory route cannot fit required authored health")
        health_needed -= count

    # Treasure rewards exploration rather than an arbitrary room-index cadence.
    # Dead ends, branches, relief spaces, and display-oriented concepts rank
    # ahead of mandatory circulation rooms.
    cadence = max(2, 7 - int(config.treasure) - (2 if number == 10 else 0))
    treasure_target = max(1, math.ceil((len(rooms) - 1) / cadence))
    if number == 10:
        treasure_target *= 2
    optional = [index for index in range(1, len(rooms))
                if index not in route_position and not identities[index].special]
    fallback = [index for index in range(1, len(rooms))
                if identities[index].special not in ("exit", "boss")
                and identities[index].tier != "corridor"]
    treasure_rooms = optional + fallback
    treasure_rooms.sort(key=lambda index: (
        vignette_counts[index],
        identities[index].concept not in
        ("gallery", "trophy-hall", "courtyard", "supply-cache", "storage",
         "burial-chamber", "officers-quarters"),
        identities[index].role not in ("branch", "ring", "relief", "closet"),
        degrees[index] != 1, -depths[index], index))
    if not treasure_rooms:
        raise ValueError("floor has no room eligible for authored treasure")
    treasure_preference = {index: position
                           for position, index in enumerate(treasure_rooms)}
    group_size = 2 if number == 10 else 1
    while treasure_target:
        count = min(group_size, treasure_target)
        target_room = min(treasure_rooms, key=lambda index: (
            vignette_counts[index], treasure_preference[index]))
        depth = depths[target_room]
        if depth < 0.35:
            pool = TREASURE[:2]
        elif depth < 0.70:
            pool = TREASURE[:3]
        else:
            pool = TREASURE[1:]
        items = tuple(rng.choice(pool) for _ in range(count))
        if not place_group(items, "exploration-treasure", treasure_rooms,
                           ("treasure-display", "corner-cache")):
            raise ValueError("floor cannot fit its authored treasure budget")
        treasure_target -= count


def _prepare_boss_arena(tiles: list[int], things: list[int], room: Room,
                        reserved: set[tuple[int, int]], rng: random.Random,
                        family: str) -> BossArenaDetail:
    """Build family-owned cover and decoration around a broad combat loop."""
    cx, cy = room.center
    dx = max(3, min(5, room.w // 3))
    dy = max(3, min(5, room.h // 3))
    patterns = {
        "throne-stronghold": [((cx - dx, cy - dy), (cx + dx, cy - dy)),
                               ((cx - dx, cy + dy), (cx + dx, cy + dy))],
        "command-bunker": [((cx - dx, cy), (cx + dx, cy))],
        "laboratory-gauntlet": [((cx - dx, cy - 2), (cx + dx, cy + 2)),
                                 ((cx - dx, cy + 2), (cx + dx, cy - 2))],
        "columned-fortress": [((cx - dx, cy - dy), (cx + dx, cy - dy)),
                               ((cx - dx, cy + dy), (cx + dx, cy + dy))],
        "central-duel": [((cx, cy - dy), (cx, cy + dy))],
    }.get(family, [((cx - dx, cy), (cx + dx, cy))])
    profiles = {
        "throne-stronghold": "stepped-apse",
        "command-bunker": "offset-command-bunker",
        "laboratory-gauntlet": "paired-side-laboratories",
        "columned-fortress": "cruciform-colonnade",
        "central-duel": "chamfered-duel-ring",
    }
    rng.shuffle(patterns)
    geometry: list[tuple[int, int]] = []
    for pair in patterns:
        if not all(_is_floor(_at(tiles, *cell)) and _at(things, *cell) == 0
                   and cell not in reserved
                   and all(_is_floor(_at(tiles, cell[0] + sx, cell[1] + sy))
                           for sx, sy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
                   for cell in pair):
            continue
        for cell in pair:
            _set(tiles, *cell, WALL)
            reserved.add(cell)
            geometry.append(cell)

    decor_specs = {
        "throne-stronghold": (
            (cx - 5, cy - 5, 62), (cx + 5, cy - 5, 62),
            (cx - 5, cy + 4, 39), (cx + 5, cy + 4, 39),
            (cx, cy - 5, 27)),
        "command-bunker": (
            (cx - 4, cy - 4, 36), (cx + 4, cy + 4, 36),
            (cx - 5, cy + 4, 62), (cx + 5, cy - 4, 62),
            (cx, cy - 5, 37), (cx, cy + 5, 37)),
        "laboratory-gauntlet": (
            (cx - 5, cy - 4, 36), (cx + 5, cy - 4, 33),
            (cx - 5, cy + 4, 24), (cx + 5, cy + 4, 36),
            (cx - 2, cy, 37), (cx + 2, cy, 37)),
        "columned-fortress": (
            (cx - 5, cy, 39), (cx + 5, cy, 39),
            (cx, cy - 5, 62), (cx, cy + 5, 62),
            (cx - 3, cy - 3, 27), (cx + 3, cy + 3, 27)),
        "central-duel": (
            (cx - 5, cy - 5, 26), (cx + 5, cy - 5, 26),
            (cx - 5, cy + 5, 26), (cx + 5, cy + 5, 26)),
    }.get(family, ())
    decorations: list[tuple[int, int, int]] = []
    for x, y, item in decor_specs:
        if (_is_floor(_at(tiles, x, y)) and _at(things, x, y) == 0
                and (x, y) not in reserved):
            _set(things, x, y, item)
            reserved.add((x, y))
            decorations.append((x, y, item))
    return BossArenaDetail(family, profiles.get(family, "symmetric-arena"),
                           tuple(geometry), tuple(decorations))


def _place_boss(tiles: list[int], things: list[int], room: Room,
                reserved: set[tuple[int, int]], rng: random.Random,
                *, room_index: int = -1,
                placements: list[SpritePlacement] | None = None,
                boss: int | None = None,
                family: str = "central-duel") -> int:
    cx, cy = room.center
    positions = [(cx, cy), (cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)]
    bx, by = next(((x, y) for x, y in positions
                   if (x, y) not in reserved and _at(things, x, y) == 0
                   and _is_floor(_at(tiles, x, y))), (cx, cy))
    # A boss-gated elevator is only genuine when the kill itself provides the
    # gold key. WL6 exposes reliable native drops for Hans and Gretel; using a
    # loose physical key for other bosses lets the player leave them alive.
    boss = boss or rng.choice(tuple(sorted(KEY_DROP_BOSSES)))
    _set(things, bx, by, boss)
    reserved.add((bx, by))
    supply_patterns = {
        "throne-stronghold": ((cx - 3, cy + 5, FIRST_AID),
                               (cx + 3, cy + 5, AMMO)),
        "command-bunker": ((cx - 5, cy, AMMO), (cx + 5, cy, AMMO),
                            (cx, cy + 5, FIRST_AID)),
        "laboratory-gauntlet": ((cx - 4, cy, FIRST_AID),
                                 (cx + 4, cy, FIRST_AID),
                                 (cx, cy + 5, AMMO)),
        "columned-fortress": ((cx - 4, cy + 4, AMMO),
                               (cx + 4, cy - 4, FIRST_AID)),
        "central-duel": ((cx, cy - 5, FIRST_AID), (cx, cy + 5, AMMO)),
    }
    supplies = supply_patterns.get(family, ((cx - 2, cy - 2, FIRST_AID),
                                            (cx + 2, cy + 2, AMMO)))
    placed_supplies = []
    for x, y, thing in supplies:
        if _at(things, x, y) == 0 and _is_floor(_at(tiles, x, y)):
            _set(things, x, y, thing)
            reserved.add((x, y))
            placed_supplies.append((x, y, thing))
    if placements is not None and placed_supplies:
        placements.append(SpritePlacement(
            "boss-arena-support", "boss-arena-cross", room_index,
            tuple(placed_supplies)))
    return boss


def generate_map(config: CampaignConfig, number: int, attempt: int = 0,
                 secret_exit: bool = False, secret_source: int | None = None,
                 hallway_vine_budget: int = 0,
                 guard_gallery_enabled: bool = False,
                 rare_motif_enabled: bool = False,
                 sky_vista_enabled: bool = True,
                 ) -> GeneratedMap:
    seed = config.floor_seed(number, attempt)
    if config.say_aardwolf:
        seed ^= config.aardwolf_seed(number)
    rng = random.Random(seed)
    tiles = [WALL] * (GRID * GRID)
    things = [0] * (GRID * GRID)
    complexity = int(config.layout_complexity)
    floor_variant = _aardwolf_variant(
        config, number, _variant_sequence(config)[number - 1])
    circulation_skeleton = _circulation_sequence(config)[number - 1]
    progression_grammar = _progression_sequence(config)[number - 1]
    scheduled_gate = _lock_schedule(config)[number - 1]
    plan = _plan_floor(rng, complexity, number, variant=floor_variant,
                       skeleton=circulation_skeleton,
                       progression_grammar=progression_grammar,
                       rare_motif=rare_motif_enabled)
    placed = _place_planned_rooms(rng, plan, number)
    rooms = placed.rooms
    edges = placed.edges
    specs = [plan.specs[index] for index in placed.spec_indices]
    roles = [spec.role for spec in specs]
    districts = _spatial_districts(rooms, len({spec.district for spec in specs}))
    for room in rooms:
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                _set(tiles, x, y, FLOOR)
    shape_scale = SHAPE_MULTIPLIERS[int(config.room_shape_variation)]
    shape_target = SHAPE_TARGETS[int(config.room_shape_variation)]
    shape_budget = max(1, round(len(rooms) * shape_target))
    utility_shapes = frozenset(
        index for index, spec in enumerate(specs)
        if spec.role in {"start", "arrival", "exit", "victory", "recovery"}
        or spec.tier in {"closet", "corridor", "motif"}
        or spec.role == "boss-arena")
    rare_profile: tuple[int, str, tuple[tuple[int, int], ...]] | None = None
    for room_index, (room, spec) in enumerate(zip(rooms, specs)):
        if spec.motif != "swastika":
            continue
        carved = _carve_swastika_profile(tiles, room, rng)
        if carved is not None:
            rare_profile = (room_index, carved[0], carved[1])
        break
    if rare_motif_enabled and rare_profile is None:
        raise ValueError("scheduled rare motif could not be realized")
    notch_budget = max(1, round(shape_budget * 0.30))
    notch_anchors = _carve_notches(
        tiles, rooms, rng,
        chance=min(1.0, floor_variant.notch_chance * shape_scale),
        max_rooms=notch_budget, excluded=utility_shapes)
    authored_shape_count = (1 if rare_profile is not None else 0) + (1 if number == 9 else 0)
    profile_anchors, profile_shapes = _carve_symmetric_profiles(
        tiles, rooms, rng,
        chance=min(1.0, shape_scale),
        max_rooms=max(0, shape_budget - len(notch_anchors) - authored_shape_count),
        excluded=frozenset(notch_anchors) | utility_shapes)
    notch_anchors.update(profile_anchors)
    realized_shapes = ["rectangle"] * len(rooms)
    for room_index in notch_anchors:
        realized_shapes[room_index] = profile_shapes.get(room_index, "mirrored-notch")
    if rare_profile is not None:
        realized_shapes[rare_profile[0]] = "swastika-profile"
    overrides = dict(floor_variant.decor_overrides)
    for room, spec in zip(rooms, specs):
        predicted = overrides.get(_decor_theme(spec.role, spec.tier),
                                  _decor_theme(spec.role, spec.tier))
        structural = (predicted == "grand"
                      or (floor_variant.name == "catacombs" and spec.tier == "anchor"))
        if structural:
            _add_pillars(tiles, room, rng, chance=floor_variant.pillar_chance)
    is_boss = number == 9
    planned_exit_index = roles.index("exit")
    anchor_index = next(index for index, spec in enumerate(specs)
                        if spec.tier == "anchor")
    minimum_route_rooms = _minimum_critical_route_rooms(roles)
    required_post_anchor = next((index for index, role in enumerate(roles)
                                 if role in ("victory", "recovery")), None)

    # Floor one begins inside the castle rather than beside an inert lift.
    # Later floors retain their bounded arrival cars and reserve that geometry
    # before routing, so their rock backing cannot be consumed by corridors.
    first_neighbor = next((second if first == 0 else first
                           for first, second in edges if 0 in (first, second)), 1)
    arrival = None
    if number != 1:
        arrival = _place_arrival_elevator(
            tiles, rooms[0], rooms[first_neighbor].center, rng,
            floor_variant.name)
    # Reserve the terminal car at the same architectural stage. Prefer the
    # planned final spine room, then another sufficiently deep post-anchor
    # route when its horizontal exterior wall is the one that remains clean.
    exit_geometry_candidates: list[tuple[int, list[int]]] = []
    for room_index in range(1, len(rooms)):
        route = _room_graph_path(len(rooms), edges, room_index)
        if (anchor_index not in route[:-1] or room_index == anchor_index
                or len(route) < minimum_route_rooms
                or (required_post_anchor is not None
                    and required_post_anchor not in route[:-1])):
            continue
        exit_geometry_candidates.append((room_index, route))
    exit_geometry_candidates.sort(
        key=lambda item: (item[0] == planned_exit_index, len(item[1])),
        reverse=True)
    preplaced_exit_index = -1
    preplaced_exit_route: list[int] = []
    exit_stand = None
    for room_index, route in exit_geometry_candidates:
        trial_tiles = tiles.copy()
        try:
            trial_stand = _place_elevator(
                trial_tiles, rooms[room_index], locked=is_boss)
        except ValueError:
            continue
        tiles[:] = trial_tiles
        preplaced_exit_index = room_index
        preplaced_exit_route = route
        exit_stand = trial_stand
        break
    if exit_stand is None:
        raise ValueError("no post-climax room has a rock-backed horizontal elevator wall")

    switch_dx = next(dx for dx in (-1, 1)
                     if _at(tiles, exit_stand[0] + dx, exit_stand[1])
                     == ELEVATOR_TILE)
    exit_portal = (exit_stand[0] - 2 * switch_dx, exit_stand[1])
    terminal_footprint = {
        (exit_portal[0] + switch_dx * depth, exit_portal[1] + side)
        for depth in range(5) for side in (-2, -1, 0, 1, 2)}
    protected_elevators = ((set(arrival.footprint) if arrival else set())
                            | terminal_footprint)
    door_zones: set[tuple[int, int]] = ({arrival.portal} if arrival else set())
    paths = [_carve_connection(tiles, rooms[a], rooms[b], rng, complexity,
                               door_zones, protected_elevators)
             for a, b in edges]
    _widen_corridors(tiles, rooms, paths, rng,
                     widen_chance=floor_variant.widen_chance,
                     protected=protected_elevators)
    if arrival is not None:
        start = arrival.player
        facing = arrival.facing
    else:
        start = rooms[0].center
        tx, ty = rooms[first_neighbor].center
        dx, dy = tx - start[0], ty - start[1]
        facing = (1 if abs(dx) >= abs(dy) and dx >= 0 else
                  3 if abs(dx) >= abs(dy) else
                  2 if dy >= 0 else 0)
    _set(things, *start, PLAYER_START_CODES[facing])
    if arrival is not None and arrival.item is not None:
        _set(things, *arrival.item)
    exit_room = rooms[preplaced_exit_index]
    # The elevator belongs near the deepest authored frontier, after the
    # anchor/climax room and at the end of a route containing most of the
    # mandatory spine. The old behavior always tried the nominal exit first,
    # even when a much deeper wing made it a trivial early solution.
    preliminary_distances = _floor_distances(tiles, start)
    center_distances = {index: preliminary_distances.get(room.center, 0)
                        for index, room in enumerate(rooms)}
    room_routes = {index: _room_graph_path(len(rooms), edges, index)
                   for index in range(1, len(rooms))}
    post_anchor_frontier = [
        index for index, route in room_routes.items()
        if anchor_index in route[:-1] and len(route) >= minimum_route_rooms
        and (required_post_anchor is None
             or required_post_anchor in route[:-1])]
    # Side destinations on a strong central hall may be physically farther
    # from the start while branching before the climax. They should enrich
    # exploration, not make every legitimate post-climax elevator look
    # artificially shallow. Compare exit depth only with the eligible
    # post-anchor frontier that an exit is actually allowed to occupy.
    deepest_center = max((center_distances[index]
                          for index in post_anchor_frontier), default=1) or 1
    exit_index = preplaced_exit_index
    critical_route = preplaced_exit_route
    if preliminary_distances.get(exit_stand, 0) / deepest_center < 0.75:
        raise ValueError("no post-climax room satisfies the deep-exit route")
    if exit_index != planned_exit_index:
        roles[planned_exit_index] = "relief"
        roles[exit_index] = "exit"
    notch_cells = {cell for cells in notch_anchors.values() for cell in cells}
    reserved = ({start, exit_stand, *notch_cells}
                | (set(arrival.clearance) | set(arrival.car_cells)
                   if arrival else set()))
    rewards: list[tuple[int, int]] = []
    secret_variants: list[str] = []
    secret_details: list[SecretDetail] = []
    shortcut_pushwalls: list[tuple[int, int]] = []
    secret_protected: set[tuple[int, int]] = (set(arrival.footprint)
                                              if arrival else set())
    floor_distances = _floor_distances(tiles, start)
    room_distances = {room: floor_distances.get(room.center, 0) for room in rooms}
    max_room_distance = max(room_distances.values(), default=1) or 1
    # The secret elevator is planned in addition to the ordinary secret
    # budget. Discovering the route to floor 10 must not silently consume one
    # of the floor's normal reward pockets.
    ordinary_secret_target = max(2, int(config.secrets)
                                 + (1 if number == 10 else 0))
    target_secrets = ordinary_secret_target + (1 if secret_exit else 0)
    # A secret pocket must never reuse or seal the terminal room's elevator
    # wall after the elevator has been carved.
    rare_room_index = rare_profile[0] if rare_profile is not None else -1
    candidates = [room for index, room in enumerate(rooms[1:], 1)
                  if room != exit_room and index != rare_room_index]
    room_index_by_room = {room: index for index, room in enumerate(rooms)}
    if number == 9:
        arena_depth = room_distances[rooms[anchor_index]]
        candidates = [room for room in candidates
                      if roles[room_index_by_room[room]] not in
                      ("boss-arena", "victory", "exit")
                      and room_distances[room] <= arena_depth]

    if secret_exit:
        # Build and rank the entire host roster before carving. Deep optional
        # rooms, distance from the normal lift, and generous room proportions
        # win; a small square is intentionally not a fallback for this route.
        # Measure that depth within the eligible host roster. The terminal
        # elevator room cannot host this pocket, and using its often-extreme
        # distance as the denominator can incorrectly disqualify every
        # optional room even when one is deep within the explorable floor.
        host_depth_scale = (max((room_distances[room] for room in candidates),
                                default=max_room_distance) or 1)
        ranked_hosts = sorted(candidates, key=lambda room: (
            room_distances[room] / host_depth_scale >= 0.45,
            room_index_by_room[room] not in critical_route,
            roles[room_index_by_room[room]] in ("branch", "ring", "relief", "closet"),
            room_distances[room] / host_depth_scale,
            abs(room.center[0] - exit_room.center[0])
            + abs(room.center[1] - exit_room.center[1]),
            room.w * room.h), reverse=True)
        ranked_hosts = [room for room in ranked_hosts
                        if room_distances[room] / host_depth_scale >= 0.45]
        variant_order = list(("vault", "reliquary", "gallery", "nested"))
        rng.shuffle(variant_order)
        placed_exit = None
        exit_host = None
        exit_direction = 1
        for variant in variant_order:
            for room in ranked_hosts:
                for direction in (1, -1):
                    placed_exit = _place_secret(
                        tiles, things, room, rng, variant,
                        room_distances[room] / host_depth_scale, True,
                        reward_quality=int(config.secret_reward_quality),
                        number=number, protected=secret_protected,
                        direction=direction)
                    if placed_exit:
                        exit_host = room
                        exit_direction = direction
                        break
                if placed_exit:
                    break
            if placed_exit:
                break
        if placed_exit is None or exit_host is None:
            raise ValueError("no substantial deep host fits the secret elevator")
        reward, realized_variant, push_cell = placed_exit
        depth_ratio = room_distances[exit_host] / host_depth_scale
        rewards.append(reward)
        secret_variants.append(realized_variant)
        secret_details.append(SecretDetail(
            realized_variant, 3, room_index_by_room[exit_host], depth_ratio,
            push_cell, True, "symmetric-landmark", number + 1,
            exit_direction))
        reserved.add(reward)
        candidates.remove(exit_host)

    rng.shuffle(candidates)
    while len(rewards) < target_secrets and candidates:
        variant = _pick_secret_variant(rng, secret_variants)
        placed_secret = None
        host = None
        for room in candidates:
            placed_secret = _place_secret(tiles, things, room, rng, variant,
                                          room_distances[room] / max_room_distance,
                                          False,
                                          reward_quality=int(config.secret_reward_quality),
                                          number=number,
                                          protected=secret_protected)
            if placed_secret:
                host = room
                break
        # A slot whose larger footprint fits nowhere still gets the proven
        # baseline experience rather than silently shrinking the budget.
        if placed_secret is None and variant != "square":
            for room in candidates:
                placed_secret = _place_secret(tiles, things, room, rng, "square",
                                              room_distances[room] / max_room_distance,
                                              False,
                                              reward_quality=int(config.secret_reward_quality),
                                              number=number,
                                              protected=secret_protected)
                if placed_secret:
                    host = room
                    break
        if placed_secret:
            reward, realized_variant, push_cell = placed_secret
            rewards.append(reward); secret_variants.append(realized_variant)
            secret_details.append(SecretDetail(
                realized_variant, 7 if number == 9 else 3,
                room_index_by_room[host],
                room_distances[host] / max_room_distance, push_cell, False))
            reserved.add(reward)
            candidates.remove(host)
        else:
            break
    # Dense motifs can consume every nominal east wall; a rock-backed hall
    # threshold is a safe last host with the same push direction and margin.
    reachable_walls = _reachable(tiles, start, locked_open=True)
    fallback_walls = [(x, y) for y in range(3, GRID - 3) for x in range(3, GRID - 4)
                      if _at(tiles, x, y) == WALL and (x - 1, y) in reachable_walls
                      and (number != 9
                           or floor_distances.get((x - 1, y), max_room_distance + 1)
                           <= room_distances[rooms[anchor_index]])]
    rng.shuffle(fallback_walls)
    while len(rewards) < target_secrets:
        variant = _pick_secret_variant(rng, secret_variants)
        reward = None
        fallback_push: tuple[int, int] | None = None
        fallback_depth = 0.0
        for px, py in fallback_walls:
            approach_distance = floor_distances.get((px - 1, py), 0)
            reward = _carve_secret_pocket(
                tiles, things, px, py, rng, False, variant,
                min(1.0, approach_distance / max_room_distance),
                reward_quality=int(config.secret_reward_quality),
                number=number,
                protected=secret_protected)
            if reward:
                fallback_push = (px, py)
                fallback_depth = min(1.0, approach_distance / max_room_distance)
                break
        if reward is None and variant != "square":
            variant = "square"
            for px, py in fallback_walls:
                approach_distance = floor_distances.get((px - 1, py), 0)
                reward = _carve_secret_pocket(
                    tiles, things, px, py, rng, False, variant,
                    min(1.0, approach_distance / max_room_distance),
                    reward_quality=int(config.secret_reward_quality),
                    number=number,
                    protected=secret_protected)
                if reward:
                    fallback_push = (px, py)
                    fallback_depth = min(1.0, approach_distance / max_room_distance)
                    break
        if reward:
            rewards.append(reward); secret_variants.append(variant); reserved.add(reward)
            if fallback_push is None:
                raise ValueError("fallback secret lost its pushwall metadata")
            secret_details.append(SecretDetail(
                variant, 7 if number == 9 else 3, -1, fallback_depth,
                fallback_push, False))
        else:
            break
    reserved.update(secret_protected)
    known_push_directions = {detail.pushwall: detail.push_direction
                             for detail in secret_details}
    reserved.update((index % GRID
                     - known_push_directions.get((index % GRID, index // GRID), 1),
                     index // GRID)
                    for index, thing in enumerate(things) if thing == PUSHWALL)
    if is_boss and scheduled_gate.colors[:1] == ("silver",):
        anchor_route_end = critical_route.index(anchor_index) + 1
        door_gate_plan = GatePlan(("silver",))
        door_route = critical_route[:anchor_route_end]
        door_target = rooms[anchor_index].center
    elif is_boss:
        door_gate_plan = GatePlan()
        door_route = critical_route
        door_target = rooms[anchor_index].center
    else:
        door_gate_plan = scheduled_gate
        door_route = critical_route
        door_target = exit_stand
    rare_key_reservations: set[tuple[int, int]] = set()
    if rare_room_index >= 0:
        rare_room = rooms[rare_room_index]
        rare_key_reservations = {
            (x, y) for y in range(rare_room.y, rare_room.y + rare_room.h)
            for x in range(rare_room.x, rare_room.x + rare_room.w)
            if _is_floor(_at(tiles, x, y))}
        reserved.update(rare_key_reservations)
    locks, key_order, key_objectives = _place_doors(
        tiles, things, rooms, edges, paths, rng, start, door_target, roles,
        reserved, door_gate_plan, door_route)
    reserved.difference_update(rare_key_reservations)
    for objective in key_objectives:
        room = rooms[objective.host_room]
        inward = sorted(
            ((objective.cell[0] + dx, objective.cell[1] + dy)
             for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
             if room.x <= objective.cell[0] + dx < room.x + room.w
             and room.y <= objective.cell[1] + dy < room.y + room.h
             and _is_floor(_at(tiles, objective.cell[0] + dx,
                               objective.cell[1] + dy))),
            key=lambda cell: (abs(cell[0] - room.center[0])
                              + abs(cell[1] - room.center[1]), cell))
        if inward:
            reserved.add(inward[0])
    _break_long_sightlines(tiles, things, rooms, reserved, rng, start)
    _split_oversized_zones(tiles, rooms, rng, reserved)
    if _remove_redundant_plain_doors(tiles):
        # A removed door can extend a floor-only sightline which the earlier
        # pass correctly treated as interrupted; repair only that new case.
        _break_long_sightlines(tiles, things, rooms, reserved, rng, start,
                               allow_doors=False, walls_for_redundant_doors=True)
    # Collapse tight double-doorways where a corridor clips a pinched room
    # corner into a single clean threshold (rare; leaves wide double entrances).
    door_pushwalls = {(index % GRID, index // GRID)
                      for index, thing in enumerate(things) if thing == PUSHWALL}
    if _heal_pinched_room_door_pairs(tiles, rooms, start, door_pushwalls):
        _break_long_sightlines(tiles, things, rooms, reserved, rng, start,
                               allow_doors=False, walls_for_redundant_doors=True)
    # With the gate probe below, seeds 0--19 on floors 2/5/8 placed at most
    # two doors (40 total doors at most), retried no maps, and left 41/60
    # samples with a second visible base material share of at least 10%.
    _limit_theme_merge_size(tiles, rooms, rng, reserved)
    if sum(tile in DOORS for tile in tiles) > 56:
        raise ValueError("door budget exceeded")
    guard_recesses = _carve_guard_recesses(
        tiles, things, rooms, specs, roles, reserved, rng, start, exit_room)
    boss_room = None
    boss_arena_detail = None
    preboss_index = None
    # Secret pockets are authored pickup compositions too. Record every
    # reward sprite, not only the focal cell exposed in secret metadata, so a
    # pocket that happens to overlap another room's rectangular bookkeeping
    # boundary cannot be mistaken for loose, untracked room loot.
    secret_pickups = tuple(
        (x, y, _at(things, x, y))
        for x, y in sorted(secret_protected)
        if _at(things, x, y) in PICKUP_CODES)
    pickup_placements: list[SpritePlacement] = (
        [SpritePlacement("secret-reward", "secret-cache", -1,
                         secret_pickups)] if secret_pickups else [])
    if is_boss:
        boss_index = anchor_index
        boss_room = rooms[boss_index]
        boss_choice = rng.choice(tuple(sorted(KEY_DROP_BOSSES)))
        boss_arena_detail = _prepare_boss_arena(
            tiles, things, boss_room, reserved, rng, plan.special_family)
        realized_shapes[boss_index] = f"boss-{boss_arena_detail.profile}"
        boss = _place_boss(tiles, things, boss_room, reserved, rng,
                           room_index=boss_index, placements=pickup_placements,
                           boss=boss_choice, family=plan.special_family)
        preboss_index = next((index for index, role in enumerate(roles)
                              if role == "staging"),
                             _room_predecessor(len(rooms), edges, boss_index))
        if preboss_index is not None and rooms[preboss_index] == exit_room:
            preboss_index = None
        boss_cell = next((index % GRID, index // GRID)
                         for index, thing in enumerate(things) if thing == boss)
        key_objectives = key_objectives + (
            KeyObjective("gold", boss_cell, boss_index, len(key_order) + 1,
                         0, "boss-drop"),)
        locks += 1
        key_order = key_order + ("gold",)
    # Resolve architecture and room identity before population. Encounters
    # consume the same role/theme/concept decision as decoration rather than
    # independently guessing what kind of room they occupy.
    _assign_sound_zones(tiles)
    component_of, group_theme = _assign_area_themes(tiles, rooms, districts, rng, number,
                                                    theme_pool=floor_variant.theme_pool)
    jail_rooms = _select_jail_rooms(rooms, districts, component_of, group_theme, tiles, rng,
                                    jail_probability=floor_variant.jail_probability)
    identities = _room_identities(rooms, specs, districts, edges, floor_variant, jail_rooms,
                                  component_of, group_theme, exit_room, boss_room,
                                  plan.special_family, key_objectives)
    landmarks = _apply_wall_theme(tiles, things, rooms, districts, component_of, group_theme,
                                  rng, jail_rooms, identities=identities,
                                  atmosphere=int(config.atmosphere))
    exit_pushwall = next((detail.pushwall for detail in secret_details
                          if detail.secret_exit), None)
    # Only the inner wall of a nested double secret is deliberately forced
    # plain, so the second chamber is not given away. Ordinary secrets are
    # already not uniformly telegraphed: plaster pushwalls stay plain by design
    # and a few materials have no landmark accent, so hint_secrets never marks
    # every secret to begin with. The nested outer wall and the mandatory
    # secret-exit wall are always marked.
    nested_inner = {(detail.pushwall[0] + 4 * detail.push_direction,
                     detail.pushwall[1])
                    for detail in secret_details if detail.shape == "nested"}
    plain_walls = frozenset(nested_inner)
    secret_hints = _hint_secrets(tiles, things, component_of, group_theme, rng,
                                 special_pushwall=exit_pushwall,
                                 plain_walls=plain_walls)
    if exit_pushwall is not None and secret_hints.get(exit_pushwall) != "symmetric-landmark":
        raise ValueError("secret elevator host cannot support its landmark hint")
    # Keep each recorded treatment truthful to what was actually painted.
    secret_details = [
        replace(detail, hint_treatment=secret_hints.get(detail.pushwall,
                                                        detail.hint_treatment))
        for detail in secret_details]
    rare_motif_detail = None
    if rare_profile is not None:
        room_index, realization, endpoints = rare_profile
        for cell in endpoints:
            item = 62
            if (_is_floor(_at(tiles, *cell)) and _at(things, *cell) == 0
                    and cell not in reserved):
                _set(things, *cell, item)
                reserved.add(cell)
        rare_motif_detail = RareMotifDetail(
            "swastika", room_index, realization, endpoints)
    gallery_eligible = frozenset(
        index for index in range(1, len(rooms))
        if index not in critical_route and rooms[index] != exit_room
        and index not in {objective.host_room for objective in key_objectives}
        and roles[index] not in {"arrival", "victory", "recovery", "boss-arena",
                                 "staging", "premium-vault"})
    guard_galleries = (_place_guard_gallery(
        tiles, things, rooms, identities, realized_shapes, reserved, rng, start,
        gallery_eligible) if guard_gallery_enabled else ())
    actor_clearance: set[tuple[int, int]] = set()
    calm_rooms = frozenset(index for index, role in enumerate(roles)
                           if role in ("arrival", "victory"))
    optional_rooms = frozenset(index for index in range(len(rooms))
                               if index not in critical_route
                               and rooms[index] != exit_room)
    encounters: list[EncounterPlacement] = []
    enemy_tiers = _place_population(
        config, number, rooms, tiles, things, reserved, rng, start, exit_room,
        patrol_chance=PATROL_TARGETS[int(config.patrol_activity)],
        placements=pickup_placements, actor_clearance=actor_clearance,
        progression_number=(secret_source if number == 10 and secret_source else number),
        calm_rooms=calm_rooms, boss_room=boss_room,
        optional_rooms=optional_rooms, identities=identities,
        critical_route=tuple(critical_route), guard_recesses=guard_recesses,
        key_objectives=key_objectives, encounter_out=encounters)
    gallery_tiers = _populate_guard_galleries(
        guard_galleries, things, number, rng, encounters)
    enemy_tiers = tuple(ordinary + gallery
                        for ordinary, gallery in zip(enemy_tiers, gallery_tiers))
    premium_index = (next((index for index, role in enumerate(roles)
                           if role == "premium-vault"), None)
                     if number == 10 else None)
    expedition_rooms: list[int] = []
    _place_authored_pickups(
        config, number, rooms, tiles, things, reserved, rng, start, identities,
        critical_route, edges, pickup_placements, preboss_index=preboss_index,
        premium_index=premium_index,
        expedition_candidates=tuple(optional_rooms),
        expedition_rooms_out=expedition_rooms)
    reserved.difference_update(notch_cells)
    # A notch anchor can also be the open tile directly in front of an actor.
    # Releasing the architectural reservation must not erase that later,
    # independent reason to keep the cell clear.
    reserved.update(actor_clearance)
    lighting_families, vine_screens = _place_decorations(
        rooms, tiles, things, reserved, start, rng, roles=roles, specs=specs,
        jail_rooms=jail_rooms,
        density=(floor_variant.decor_density
                 * DECORATION_MULTIPLIERS[int(config.decoration_amount)]),
        theme_overrides=floor_variant.decor_overrides, landmarks=landmarks,
        paths=paths, identities=identities, atmosphere=int(config.atmosphere),
        notch_anchors=notch_anchors, hallway_vine_budget=hallway_vine_budget,
        allow_sky_vista=sky_vista_enabled)
    primary_hall_geometry = tuple(
        (index, room.x, room.y, room.w, room.h)
        for index, (room, spec) in enumerate(zip(rooms, specs))
        if plan.skeleton in HALLWAY_FIRST_SKELETONS and spec.tier == "corridor")
    barrel_families = tuple(
        ("green" if 24 in present else "blue" if 58 in present else "none")
        for room in rooms
        for present in ({_at(things, x, y)
                         for y in range(room.y, room.y + room.h)
                         for x in range(room.x, room.x + room.w)},))
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
    final_distances = _floor_distances(tiles, start)
    deepest_room_distance = max((final_distances.get(room.center, 0) for room in rooms),
                                default=1) or 1
    exit_depth_ratio = final_distances.get(exit_stand, 0) / deepest_room_distance
    corridor_edges = sum(specs[first].tier == "corridor" or specs[second].tier == "corridor"
                         for first, second in edges)
    mediated_ratio = corridor_edges / max(1, len(edges))
    layout_signature = (
        plan.special_family, plan.progression_grammar, plan.skeleton,
        *plan.motif_realizations, *plan.district_circulation,
        f"corridors-{sum(spec.tier == 'corridor' for spec in specs)}",
        f"mediated-{round(mediated_ratio, 1):.1f}",
        f"shapes-{','.join(sorted(Counter(realized_shapes).elements()))}",
        f"recesses-{len(guard_recesses)}",
        f"patrols-{sum(bool(encounter.patrol_kind) for encounter in encounters)}",
    )
    result = GeneratedMap(number=number, tiles=tiles, things=things, start=start,
                          exit_stand=exit_stand, secret_rewards=rewards, seed=seed,
                          has_secret_exit=secret_exit, locked_doors=locks, boss=is_boss,
                          enemy_tiers=enemy_tiers, motifs=plan.motifs,
                          motif_rooms=tuple(spec.motif for spec in specs),
                          secret_variants=tuple(secret_variants),
                          shortcut_pushwalls=tuple(shortcut_pushwalls), rooms=tuple(rooms),
                          edges=tuple(edges), jail_rooms=jail_rooms,
                          variant=floor_variant.name,
                          room_concepts=tuple(identity.concept for identity in identities),
                          key_order=key_order,
                          critical_route=tuple(critical_route),
                          room_districts=tuple(districts),
                          exit_depth_ratio=exit_depth_ratio,
                          room_roles=tuple(roles),
                          room_tiers=tuple(spec.tier for spec in specs),
                          circulation_skeleton=plan.skeleton,
                          district_circulation=plan.district_circulation,
                          layout_signature=layout_signature,
                          pickup_placements=tuple(pickup_placements),
                          room_shapes=tuple(realized_shapes),
                          lighting_families=lighting_families,
                          vine_screens=vine_screens,
                          key_objectives=key_objectives,
                          secret_details=tuple(secret_details),
                          special_family=plan.special_family,
                          boss_arena_room=anchor_index if is_boss else -1,
                          preboss_room=preboss_index if preboss_index is not None else -1,
                          premium_room=premium_index if premium_index is not None else -1,
                          expedition_rooms=tuple(dict.fromkeys(expedition_rooms)),
                          secret_source=secret_source or 0,
                          arrival=arrival, guard_recesses=guard_recesses,
                          guard_galleries=guard_galleries,
                          encounters=tuple(encounters),
                          patrol_target=PATROL_TARGETS[int(config.patrol_activity)],
                          progression_grammar=plan.progression_grammar,
                          motif_realizations=plan.motif_realizations,
                          rare_motif=rare_motif_detail,
                          boss_arena=boss_arena_detail,
                          shape_target=shape_target,
                          primary_hall_geometry=primary_hall_geometry,
                          barrel_families=barrel_families,
                          sky_vistas=tuple(sky_vistas),
                          sky_vista_recesses=tuple(sky_vista_recesses),
                          sky_vista_supports=tuple(sky_vista_supports))
    validate_map(result)
    result.critique = _critique(result)
    return result




def generate_campaign(config: CampaignConfig, output: Path,
                      progress: Callable[[int, int], None] | None = None,
                      cancelled: Callable[[], bool] | None = None) -> Path:
    levels = []
    secret_seed = config.floor_seed(10)
    if config.say_aardwolf:
        secret_seed ^= config.aardwolf_seed(1)
    secret_from = 1 + secret_seed % 6
    variants = _variant_sequence(config)
    vine_seed = config.vine_seed()
    if config.say_aardwolf:
        vine_seed ^= config.aardwolf_seed(8)
    vine_rng = random.Random(vine_seed)
    vine_floors = list(range(2, 9))
    vine_weights = [4 if variants[floor - 1].name == "catacombs" else
                    2 if variants[floor - 1].name in ("storehouse", "grand-halls") else 1
                    for floor in vine_floors]
    vine_floor = vine_rng.choices(vine_floors, weights=vine_weights, k=1)[0]
    vine_budget = 2 if vine_rng.random() < 0.28 else 1
    gallery_seed = config.guard_gallery_seed()
    if config.say_aardwolf:
        gallery_seed ^= config.aardwolf_seed(7)
    gallery_rng = random.Random(gallery_seed)
    gallery_enabled = gallery_rng.random() < 0.22
    gallery_floors = list(range(3, 9))
    gallery_weights = [3 if variants[floor - 1].name in
                       ("garrison", "grand-halls") else 1
                       for floor in gallery_floors]
    gallery_floor = (gallery_rng.choices(gallery_floors, weights=gallery_weights, k=1)[0]
                     if gallery_enabled else 0)
    rare_motif_floor = _rare_motif_schedule(config)
    # Only one parity of floors may request a vista in a campaign. This keeps
    # the rare motif from appearing on consecutive maps without changing
    # standalone-map generation or tying it to a specific theme.
    vista_parity = random.Random(config.circulation_seed(10)
                                 ^ 0x564953544131).randrange(2)
    for number in range(1, 11):
        if cancelled and cancelled():
            raise GenerationCancelled("campaign generation cancelled")
        last_error = None
        candidates: list[GeneratedMap] = []
        clean: list[GeneratedMap] = []
        for attempt in range(50):
            try:
                candidate = generate_map(config, number, attempt, number == secret_from,
                                         secret_source=secret_from if number == 10 else None,
                                         hallway_vine_budget=(vine_budget
                                                              if number == vine_floor else 0),
                                         guard_gallery_enabled=(number == gallery_floor),
                                         rare_motif_enabled=(number == rare_motif_floor),
                                         sky_vista_enabled=(number % 2 == vista_parity))
            except ValueError as error:
                last_error = error
                continue
            candidates.append(candidate)
            if not candidate.critique:
                if not config.say_aardwolf:
                    levels.append(candidate)
                    break
                clean.append(candidate)
                if len(clean) == 2:
                    levels.append(max(
                        clean, key=lambda level: _candidate_score(
                            level, levels, config)))
                    break
            if config.say_aardwolf and len(candidates) == 8:
                pool = clean or candidates
                levels.append(max(
                    pool, key=lambda level: (
                        -len(level.critique),
                        _candidate_score(level, levels, config))))
                break
            if not config.say_aardwolf and len(candidates) == 3:
                levels.append(min(candidates, key=lambda level: len(level.critique)))
                break
        else:
            if candidates:
                if config.say_aardwolf:
                    pool = clean or candidates
                    levels.append(max(
                        pool, key=lambda level: (
                            -len(level.critique),
                            _candidate_score(level, levels, config))))
                else:
                    levels.append(min(candidates,
                                      key=lambda level: len(level.critique)))
            else:
                raise RuntimeError(f"floor {number} failed generation: {last_error}")
        if progress:
            progress(number, 10)
    realized_vine_floors = {
        level.number for level in levels
        if any(screen.kind == "hallway-run" for screen in level.vine_screens)}
    realized_vine_runs = sum(
        screen.kind == "hallway-run" for level in levels for screen in level.vine_screens)
    if (realized_vine_floors - {vine_floor}
            or len(realized_vine_floors) > 1
            or realized_vine_runs > vine_budget):
        raise RuntimeError("campaign hallway-vine budget was violated")
    realized_gallery_floors = {
        level.number for level in levels if level.guard_galleries}
    if realized_gallery_floors - {gallery_floor} or len(realized_gallery_floors) > 1:
        raise RuntimeError("campaign guard-gallery budget was violated")
    if any(first.variant == second.variant
           for first, second in zip(levels, levels[1:])):
        raise RuntimeError("campaign repeated the same floor type consecutively")
    if any(first.circulation_skeleton == second.circulation_skeleton
           for first, second in zip(levels, levels[1:])):
        raise RuntimeError("campaign repeated the same circulation skeleton consecutively")
    if sum(level.circulation_skeleton in HALLWAY_FIRST_SKELETONS
           for level in levels) != 3:
        raise RuntimeError("campaign violated its three-floor hallway-first schedule")
    if any(first.progression_grammar == second.progression_grammar
           for first, second in zip(levels, levels[1:])):
        raise RuntimeError("campaign repeated the same progression grammar consecutively")
    if any(first.sky_vistas and second.sky_vistas
           for first, second in zip(levels, levels[1:])):
        raise RuntimeError("campaign repeated the exterior-vista motif consecutively")
    realized_rare = [level.number for level in levels if level.rare_motif is not None]
    expected_rare = [rare_motif_floor] if rare_motif_floor else []
    if realized_rare != expected_rare:
        raise RuntimeError("campaign rare-motif schedule was violated")
    # Encode metadata-independent provenance only after every gameplay choice
    # is final. Zone-label permutations preserve all acoustic grouping.
    from .watermark import apply_campaign_watermark
    apply_campaign_watermark(levels, config.seed)
    for level in levels:
        validate_map(level)
    manifest = {
        "generator": "infiniwolf", "version": __version__,
        "commit": BUILD_COMMIT or "unknown", "seed": config.seed,
        "seed_source": "LittleEntropyMachine",
        "watermark": {"scheme": "zone-item-geometry-v2",
                      "primary_modulus": 43, "secondary_modulus": 17,
                      "per_map": True, "campaign_residue": 42},
        "settings": json.loads(config.to_json()), "secret_from": secret_from,
        "vine_schedule": {"floor": vine_floor, "requested_runs": vine_budget,
                          "realized_runs": realized_vine_runs},
        "guard_gallery_schedule": {"floor": gallery_floor,
                                   "realized": bool(realized_gallery_floors)},
        "rare_motif_schedule": {"floor": rare_motif_floor,
                                "realized_floor": (realized_rare[0]
                                                   if realized_rare else 0)},
        "sky_vista_schedule": {
            "eligible_parity": vista_parity,
            "realized_floors": [level.number for level in levels
                                if level.sky_vistas]},
        "lock_schedule": [plan.colors for plan in _lock_schedule(config)],
        "floors": [{"number": level.number,
                    "name": _display_name(level.number, level.variant),
                    "seed": level.seed,
                    "secrets": len(level.secret_rewards),
                    "locked_doors": level.locked_doors,
                    "key_order": level.key_order,
                    "critical_route_rooms": len(level.critical_route),
                    "exit_depth_ratio": round(level.exit_depth_ratio, 4),
                    "exit_stand": level.exit_stand,
                    "boss": level.boss,
                    "special_family": level.special_family,
                    "secret_source": level.secret_source,
                    "boss_arena_room": level.boss_arena_room,
                    "preboss_room": level.preboss_room,
                    "premium_room": level.premium_room,
                    "expedition_rooms": level.expedition_rooms,
                    "arrival": ({"kind": level.arrival.kind,
                                  "portal": level.arrival.portal,
                                  "player": level.arrival.player,
                                  "facing": level.arrival.facing,
                                  "car_cells": level.arrival.car_cells,
                                  "item": level.arrival.item}
                                 if level.arrival else None),
                    "guard_recesses": [
                        {"room": recess.room_index, "cells": recess.cells,
                         "actor_cell": recess.actor_cell}
                        for recess in level.guard_recesses],
                    "guard_galleries": [
                        {"room": gallery.room_index, "screen": gallery.screen,
                         "actors": gallery.actor_cells,
                         "rear_cells": gallery.rear_cells,
                         "treatment": gallery.treatment}
                        for gallery in level.guard_galleries],
                    "encounters": [
                        {"template": encounter.template,
                         "room": encounter.room_index,
                         "actors": [item for _, _, item in encounter.cells],
                         "hidden_cells": encounter.hidden_cells,
                         "family": encounter.family,
                         "patrol_kind": encounter.patrol_kind,
                         "patrol_path": encounter.patrol_path}
                        for encounter in level.encounters],
                    "patrol_target": level.patrol_target,
                    "enemy_tiers": level.enemy_tiers,
                    "variant": level.variant,
                    "circulation_skeleton": level.circulation_skeleton,
                    "progression_grammar": level.progression_grammar,
                    "district_circulation": level.district_circulation,
                    "layout_signature": level.layout_signature,
                    "primary_hall_geometry": level.primary_hall_geometry,
                    "barrel_families": level.barrel_families,
                    "sky_vistas": level.sky_vistas,
                    "sky_vista_recesses": level.sky_vista_recesses,
                    "sky_vista_supports": level.sky_vista_supports,
                    "door_axis_parity": [
                        {"room": index, "width": room.w,
                         "height": room.h,
                         "odd_width": bool(room.w % 2),
                         "odd_height": bool(room.h % 2)}
                        for index, room in enumerate(level.rooms)],
                    "motif_realizations": level.motif_realizations,
                    "shape_target": level.shape_target,
                    "rare_motif": ({"kind": level.rare_motif.kind,
                                    "room": level.rare_motif.room_index,
                                    "realization": level.rare_motif.realization,
                                    "endpoints": level.rare_motif.endpoints}
                                   if level.rare_motif else None),
                    "boss_arena": ({"family": level.boss_arena.family,
                                    "profile": level.boss_arena.profile,
                                    "geometry": level.boss_arena.geometry,
                                    "decorations": level.boss_arena.decorations}
                                   if level.boss_arena else None),
                    "room_concepts": level.room_concepts,
                    "room_shapes": level.room_shapes,
                    "lighting_families": level.lighting_families,
                    "vine_screens": [
                        {"kind": screen.kind, "room": screen.room_index,
                         "cells": screen.cells,
                         "ambush_anchor": screen.ambush_anchor}
                        for screen in level.vine_screens],
                    "motifs": level.motifs,
                    "secret_variants": level.secret_variants,
                    "secret_details": [
                        {"shape": detail.shape,
                         "reward_count": detail.reward_count,
                         "host_room": detail.host_room,
                         "depth_ratio": round(detail.depth_ratio, 4),
                         "pushwall": detail.pushwall,
                         "secret_exit": detail.secret_exit,
                         "hint_treatment": detail.hint_treatment,
                         "return_floor": detail.return_floor,
                         "push_direction": detail.push_direction}
                        for detail in level.secret_details],
                    "key_objectives": [
                        {"color": objective.color, "cell": objective.cell,
                         "host_room": objective.host_room,
                         "stage": objective.stage, "detour": objective.detour,
                         "treatment": objective.treatment}
                        for objective in level.key_objectives],
                    "pickup_compositions": [
                        {"reason": placement.reason,
                         "template": placement.template,
                         "room": placement.room_index,
                         "items": [item for _, _, item in placement.cells]}
                        for placement in level.pickup_placements],
                    "critique": level.critique,
                    "validation": {
                        "passed": True,
                        "checks": ["bounds", "connectivity", "door_axes", "elevator",
                                   "exit_depth", "critical_route",
                                   "dual_key_progression", "key_room_separation",
                                   "pushwall_clearance", "rewarded_secrets",
                                   "secret_hints", "secret_route", "boss",
                                   "circulation_hierarchy", "arrival_elevator",
                                   "hallway_first_scaffold", "sky_vista_depth",
                                   "room_barrel_family", "wall_backed_blue_urn",
                                   "encounter_provenance", "patrol_routes",
                                   "wall_backed_flags", "pickup_provenance"],
                    }} for level in levels],
    }
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".tmp", delete=False) as temporary:
        temp_path = Path(temporary.name)
    try:
        if cancelled and cancelled():
            raise GenerationCancelled("campaign generation cancelled")
        with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as package:
            def write(name: str, data: str | bytes) -> None:
                info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                package.writestr(info, data)
            write("mapinfo.txt", _mapinfo(secret_from, tuple(level.variant for level in levels)))
            write("infiniwolf-manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
            write("infiniwolf-settings.txt", _reproducibility_text(config, secret_from))
            for level in levels:
                write(f"maps/iw{level.number:02d}.wad",
                      _wad_bytes(f"IW{level.number:02d}", level.tiles, level.things))
        validate_package(temp_path)
        if cancelled and cancelled():
            raise GenerationCancelled("campaign generation cancelled")
        temp_path.replace(output)
    finally:
        temp_path.unlink(missing_ok=True)
    return output
