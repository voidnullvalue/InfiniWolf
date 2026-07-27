"""Progression and solvency: can the floor be finished, and is optional optional.

Owns everything that decides whether a floor can be finished, and whether the
optional parts stay optional: arrival and exit elevators, ordinary doors, locked
gates, physical key objectives and their measured detours, secret pockets, the
secret elevator, and the minimum-route contract the validator re-checks.

The division with geometry is deliberate. Geometry offers candidate threshold
cells; progression decides what each becomes -- a plain doorway, a locked gate, a
secret entrance, an exit structure, or unusable. Geometry never decides that a
wall is a lock.

Kept a leaf with respect to the generator: progression consumes finished geometry
and must never import the orchestrator, or the bottom-of-file import this module
was created to remove would reappear one layer down.
"""

from __future__ import annotations

from collections import deque
from itertools import combinations
import math
import random

from .grid import (_at, _door_zone, _floor_distances, _is_floor, _reachable,
                   _room_graph_path, _set)
from .geometry import _door_candidate
from .placement import _room_anchors
from .config import CampaignConfig
from .model import (ArrivalDetail, FloorCanvas, GatePlan, KeyObjective,
                    Room, SecretDetail, SecretInstallation)
from .wl6 import (AMMO, CHAINGUN, DECOR_WALLS, DOOR_ELEVATOR, DOOR_ELEVATOR_NS,
                  DOOR_EW, DOOR_GOLD_EW, DOOR_GOLD_NS, DOOR_SILVER_EW,
                  DOOR_SILVER_NS, DUMMY_ELEVATOR_TILE, ELEVATOR_TILE,
                  FIRST_AID, FLOOR, FOOD, GOLD_KEY, GRID, MACHINE_GUN, ONE_UP,
                  PUSHWALL, SECRET_EXIT_ZONE, SECRET_HINT_BY_BASE, SILVER_KEY,
                  TREASURE, WALL, WALL_THEMES, _codes_for_colors)
from .ledger import reserve as ledger_reserve


def _minimum_critical_route_rooms(roles: list[str] | tuple[str, ...]) -> int:
    """Require most of the progression spine, independent of side-room count.

    Optional density must not make a valid exit mathematically impossible.
    Roles used exclusively by optional graph nodes are excluded; a reassigned
    optional exit still adds itself to the requirement and its realized route.
    """
    optional_roles = {"ring", "branch", "closet"}
    spine_rooms = sum(role not in optional_roles for role in roles)
    return max(6, math.ceil(spine_rooms * 0.90))


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
            ledger_reserve(reserved, [spot], "progression",
                           "key-objective")
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
        # The whole pocket, not just its reward. Downstream records secret loot as
        # an authored composition, and a pocket can straddle another room's
        # rectangular bookkeeping.
        ledger_reserve(protected, footprint, "progression", "secret-pocket-shell")
        ledger_reserve(protected, reward_cells, "progression", "secret-reward")
    return reward_cells[0]


def verify_exit_depth(tiles: list[int], rooms: list[Room], edges,
                      roles: list[str], start: tuple[int, int],
                      exit_stand: tuple[int, int], anchor_index: int,
                      minimum_route_rooms: int,
                      required_post_anchor: int | None,
                      preplaced_exit_index: int, preplaced_exit_route,
                      planned_exit_index: int) -> tuple[int, list[int]]:
    """Confirm the exit sits deep enough, and reassign the exit role if it moved.

    Depth is compared only against the post-anchor frontier an exit is actually
    allowed to occupy, not against the farthest room on the floor. A side
    destination hanging off a strong central hall can be physically farther from
    the start while branching before the climax; measuring against it would make
    every legitimate post-climax elevator look artificially shallow and reject
    good floors.

    Raises rather than degrading: a floor whose exit is reachable too early is a
    different, worse floor, so the candidate attempt is rejected.
    """
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
    return exit_index, critical_route


def install_secrets(canvas: FloorCanvas, config: CampaignConfig, number: int,
                    rng: random.Random, *, arrival, exit_room: Room,
                    anchor_index: int, critical_route, rare_profile,
                    secret_exit: bool) -> SecretInstallation:
    """Carve the floor's sealed reward pockets and, when scheduled, its secret lift.

    Optionality is the contract. Every pocket here is off the mandatory route, has
    a complete rock shell, and is entered only through a pushwall, so a player who
    never finds one still finishes the floor. The secret elevator is budgeted *in
    addition* to the ordinary pockets -- discovering the route to floor 10 must not
    silently consume one of the floor's normal rewards.

    Takes a canvas rather than sixteen parameters. That count is not rhetorical: it
    was measured on this block before extraction, and it is the reason the phase
    could not simply become a function.
    """
    tiles, things = canvas.tiles, canvas.things
    rooms, roles, reserved = canvas.rooms, canvas.roles, canvas.reserved
    start = canvas.start
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
        ledger_reserve(reserved, [reward], "progression",
                       "secret-reward")
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
            ledger_reserve(reserved, [reward], "progression",
                           "secret-reward")
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
            rewards.append(reward)
            secret_variants.append(variant)
            ledger_reserve(reserved, [reward], "progression",
                           "secret-reward-fallback")
            if fallback_push is None:
                raise ValueError("fallback secret lost its pushwall metadata")
            secret_details.append(SecretDetail(
                variant, 7 if number == 9 else 3, -1, fallback_depth,
                fallback_push, False))
        else:
            break
    return SecretInstallation(
        rewards=tuple(rewards), variants=tuple(secret_variants),
        details=tuple(secret_details),
        shortcut_pushwalls=tuple(shortcut_pushwalls),
        protected=frozenset(secret_protected))
