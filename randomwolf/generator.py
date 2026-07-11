"""Deterministic WL6 campaign generation and ECWolf package writing."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
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
from .config import CampaignConfig

GRID = 64
WALL = 1
FLOOR = 108
ZONE_MAX = 143
DOOR_EW, DOOR_NS = 90, 91
DOOR_ELEVATOR = 100  # unlocked elevator door on an east/west axis
DOOR_GOLD_EW = 92
DOORS = {DOOR_EW, DOOR_NS, DOOR_GOLD_EW, 93, 94, 95, DOOR_ELEVATOR, 101}
PLAYER_START = 19  # north-facing player start (things 19-22 are N/E/S/W)
PUSHWALL = 98
# Tile 21 is the real elevator: its north/south faces render as plain
# elevator paneling and its east/west faces are the exit switch. ECWolf's
# wolf3d translator disables activation from north/south, so a usable
# switch must be approached along the east/west axis. Tile 22 (the "fake
# elevator", a decoy switch that does nothing) is deliberately never used.
ELEVATOR_TILE = 21
# Floor code 107 is not a wall: the translator's "modzone 107" converts an
# adjacent tile-21 Exit_Normal trigger into Exit_Secret. Placing it on the
# floor cell in front of a hidden elevator switch is how native maps route
# to the secret floor.
SECRET_EXIT_ZONE = 107
GOLD_KEY = 43
HANS_GROSSE = 214

# Native Wolf3D map object numbers, interpreted by ECWolf's base translator.
GUARDS = (108, 109, 110, 111)
OFFICERS = (116, 117, 118, 119)
SS = (126, 127, 128, 129)
DOGS = (134, 135, 136, 137)
AMMO, FOOD, FIRST_AID, MACHINE_GUN, CHAINGUN = 49, 47, 48, 50, 51
TREASURE = (52, 53, 54, 55)
ENEMY_CODES = frozenset(code + tier * 36 for family in (GUARDS, OFFICERS, SS, DOGS)
                        for code in family for tier in range(3)) | {HANS_GROSSE}

# Threat-weighted roster: (name, thing codes, base frequency weight, expected
# bullets to down one at routine range). Guards stay the common baseline
# filler and SS stays rare/expensive, matching the report's threat-budget
# model (guard 1.0 up to SS 3.0) instead of a flat or inverted mix.
ENEMY_FAMILIES = (
    ("guard", GUARDS, 10, 1.5),
    ("dog", DOGS, 6, 0.5),
    ("officer", OFFICERS, 3, 3.0),
    ("ss", SS, 1, 6.0),
)
FAMILY_BY_CODE = {code + 36 * tier: family
                  for _, family, _, _ in ENEMY_FAMILIES
                  for code in family for tier in range(3)}
AMMO_COST = {family: cost for _, family, _, cost in ENEMY_FAMILIES}

# Native WL6 wall tiles only. Each tuple is (base, room accents).
# Accent 13 (FAKEDOR, a sealed door/lift-shutter graphic) and 16 (SKY1, an
# outdoor-only texture) are deliberately excluded: neither reads as a normal
# room wall, so they're replaced with plain material variants below.
WALL_THEMES = (
    (1, (2, 3, 4)), (5, (6, 7, 8)), (8, (9, 14, 15)),
    (12, (10, 11, 17)), (15, (14, 17, 18)), (1, (9, 19, 20)),
)
# Landmark decoration tiles (portraits, banners, insignia, signage/graffiti):
# these should read as a single accent set into an otherwise plain wall, the
# way they're used in the original game, never as the material of an entire
# room. Every other accent above is just an alternate plain material and is
# fine covering a whole room's walls.
DECOR_WALLS = frozenset({3, 4, 6, 10, 11, 14, 18, 20})
# Fallback secret hints when a floor's theme has no decor accent of its own:
# the grey-stone swastika banner and Hitler portrait, the two tiles the
# original episodes most often hang on a pushwall.
SECRET_HINTS = (3, 4)

# Native WL6 furniture (things-plane old-num, from wolf3d.txt's xlat things
# table). BLOCKING entries are +SOLID actors (verified against
# actors/wolf/decorations.txt) -- placement always re-checks full-map
# reachability with the candidate cells blocked before committing, so these
# can never wall off part of a level. OPEN entries have no collision at all
# (floor/ceiling decor) and can be dropped anywhere free.
STATIC_BLOCKING = (
    24,  # GreenBarrel
    25,  # TableWithChairs
    26,  # FloorLamp
    30,  # WhitePillar
    31,  # GreenPlant (closest WL6 has to a tree)
    34,  # BrownPlant
    35,  # Vase
    36,  # BareTable
    58,  # Barrel
    59,  # Well
)
STATIC_OPEN = (
    27,  # Chandelier
    32,  # SkeletonFlat
    37,  # CeilingLight
    61,  # Blood
    67,  # Pots
)
CEILINGS = ("#383838", "#202840", "#402828", "#303820", "#382840")
MUSIC = ("GETTHEM", "SEARCHN", "POW", "SUSPENSE", "WARMARCH", "NAZI_OMI")


class GenerationCancelled(RuntimeError):
    """Raised when a caller cancels before atomic package installation."""


@dataclass(frozen=True, slots=True)
class Room:
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


@dataclass(slots=True)
class GeneratedMap:
    number: int
    tiles: list[int]
    things: list[int]
    start: tuple[int, int]
    exit_stand: tuple[int, int]
    secret_rewards: list[tuple[int, int]]
    seed: int
    has_secret_exit: bool = False
    locked_doors: int = 0
    boss: bool = False
    enemy_tiers: tuple[int, int, int] = (0, 0, 0)


def _at(plane: list[int], x: int, y: int) -> int:
    return plane[y * GRID + x] if 0 <= x < GRID and 0 <= y < GRID else -1


def _set(plane: list[int], x: int, y: int, value: int) -> None:
    if 0 <= x < GRID and 0 <= y < GRID:
        plane[y * GRID + x] = value


def _is_floor(value: int) -> bool:
    return FLOOR <= value <= ZONE_MAX or value == SECRET_EXIT_ZONE


def _overlaps(a: Room, b: Room, pad: int = 2) -> bool:
    return not (a.x + a.w + pad <= b.x or b.x + b.w + pad <= a.x or
                a.y + a.h + pad <= b.y or b.y + b.h + pad <= a.y)


def _rooms(rng: random.Random, count: int) -> list[Room]:
    result: list[Room] = []
    for _ in range(count * 100):
        if len(result) == count:
            break
        # Rooms stay compact combat cells (the report's door-delimited room
        # model); the rock left between them is what corridors wind through.
        room = Room(rng.randrange(3, 51), rng.randrange(3, 52),
                    rng.randrange(6, 10), rng.randrange(6, 10))
        if room.x + room.w >= 61 or room.y + room.h >= 61:
            continue
        if not any(_overlaps(room, other) for other in result):
            result.append(room)
    if len(result) < 8:
        raise ValueError("could not place enough rooms")
    return result


def _tree(rooms: list[Room]) -> list[tuple[int, int]]:
    connected = {0}
    remaining = set(range(1, len(rooms)))
    edges = []
    while remaining:
        _, a, b = min(
            ((rooms[a].center[0] - rooms[b].center[0]) ** 2 +
             (rooms[a].center[1] - rooms[b].center[1]) ** 2, a, b)
            for a in connected for b in remaining
        )
        edges.append((a, b))
        connected.add(b)
        remaining.remove(b)
    return edges


def _loop_edges(rooms: list[Room], tree: list[tuple[int, int]], count: int) -> list[tuple[int, int]]:
    existing = {tuple(sorted(edge)) for edge in tree}
    pairs = sorted(
        (((rooms[a].center[0] - rooms[b].center[0]) ** 2 +
          (rooms[a].center[1] - rooms[b].center[1]) ** 2, a, b)
         for a in range(len(rooms)) for b in range(a + 1, len(rooms))
         if (a, b) not in existing),
        key=lambda item: item[0],
    )
    return [(a, b) for _, a, b in pairs[:count]]


def _carve_segment(tiles: list[int], path: list[tuple[int, int]],
                   x: int, y: int, tx: int, ty: int, x_first: bool) -> tuple[int, int]:
    """Carve one L between two points, axis order chosen by the caller."""
    for horizontal in ((True, False) if x_first else (False, True)):
        if horizontal:
            while x != tx:
                _set(tiles, x, y, FLOOR)
                path.append((x, y))
                x += 1 if tx > x else -1
        else:
            while y != ty:
                _set(tiles, x, y, FLOOR)
                path.append((x, y))
                y += 1 if ty > y else -1
    return x, y


def _carve_connection(tiles: list[int], a: Room, b: Room,
                      rng: random.Random, complexity: int) -> list[tuple[int, int]]:
    """Carve a winding corridor between two room centers.

    A single L-hall makes every connection read as two straight runs, which
    is why the floors navigate too easily. Threading the route through
    deterministic jittered waypoints turns most connections into doglegs:
    more corners, shorter straight runs (the report wants common straights
    of 4-12 tiles and long sightlines only by explicit intent), and a more
    maze-like read, while every segment stays orthogonal and fully carved.
    """
    ax, ay = a.center
    bx, by = b.center
    span = abs(ax - bx) + abs(ay - by)
    waypoints: list[tuple[int, int]] = []
    if span >= 8:
        # Waypoints offset perpendicular to the travel axis, alternating
        # sides: the corridor becomes an S-curve that genuinely detours
        # instead of wandering the map and accidentally merging with other
        # corridors into a shortcut web (which would make routes shorter,
        # not mazier).
        bends = 2 if complexity >= 3 and span >= 16 else 1
        horizontal = abs(bx - ax) >= abs(by - ay)
        amplitude = 3 + rng.randint(0, complexity + 2)
        sign = rng.choice((-1, 1))
        for step in range(1, bends + 1):
            along = step / (bends + 1)
            wx = round(ax + (bx - ax) * along)
            wy = round(ay + (by - ay) * along)
            if horizontal:
                wy += sign * amplitude
            else:
                wx += sign * amplitude
            sign = -sign
            waypoints.append((max(2, min(GRID - 3, wx)), max(2, min(GRID - 3, wy))))
    waypoints.append((bx, by))
    path: list[tuple[int, int]] = []
    x, y = ax, ay
    for tx, ty in waypoints:
        x, y = _carve_segment(tiles, path, x, y, tx, ty, rng.random() < 0.5)
    _set(tiles, x, y, FLOOR)
    path.append((x, y))
    return path


def _inside_room(rooms: list[Room], x: int, y: int) -> bool:
    return any(room.x <= x < room.x + room.w and room.y <= y < room.y + room.h
               for room in rooms)


def _adjacent_to_room(rooms: list[Room], x: int, y: int) -> bool:
    return any(_inside_room(rooms, nx, ny)
               for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))


def _widen_corridors(tiles: list[int], rooms: list[Room], paths: list[list[tuple[int, int]]],
                     rng: random.Random, widen_chance: float = 0.75) -> None:
    """A map built entirely from 1-tile halls reads as door-camping and rush
    traps. Default corridors to 2 tiles wide by adding floor to one side of
    each interior cell, but leave the doorway threshold (any cell touching a
    room) and short connectors pinched to 1 tile, so door placement still
    finds an unambiguous bottleneck and secret pushwall pockets are untouched.
    Widening only ever turns a solid WALL cell into FLOOR, so it can only add
    connectivity, never remove or corrupt anything already carved."""
    for path in paths:
        if len(path) < 6 or rng.random() > widen_chance:
            continue
        side = rng.choice((-1, 1))
        for i in range(1, len(path) - 1):
            x, y = path[i]
            if _inside_room(rooms, x, y) or _adjacent_to_room(rooms, x, y):
                continue
            px, py = path[i - 1]
            nx, ny = path[i + 1]
            horizontal = (px != x) or (nx != x)
            vertical = (py != y) or (ny != y)
            if horizontal and not vertical:
                wx, wy = x, y + side
            elif vertical and not horizontal:
                wx, wy = x + side, y
            else:
                continue
            if _inside_room(rooms, wx, wy) or _adjacent_to_room(rooms, wx, wy):
                continue
            if _at(tiles, wx, wy) == WALL:
                _set(tiles, wx, wy, FLOOR)


def _carve_reward_stubs(tiles: list[int], things: list[int], rooms: list[Room],
                        paths: list[list[tuple[int, int]]], reserved: set[tuple[int, int]],
                        rng: random.Random, complexity: int) -> None:
    """Branch short dead-end spurs off corridors, each ending in a reward.

    The report calls for shallow-but-meaningful branching (mean forward
    choices of roughly 1.2-1.8 on the traversed route): junctions where the
    player must choose between progress and a visible side pocket. Spurs are
    1 tile wide but only 3-6 tiles long, which the guidelines explicitly
    allow for short connectors and reward runs. Each spur is dug entirely
    from solid rock with a one-tile rock margin on every flank, so it can
    connect to nothing but its own junction and never alters existing
    geometry or connectivity.
    """
    junctions = [cell for path in paths for cell in path
                 if not _inside_room(rooms, *cell) and not _adjacent_to_room(rooms, *cell)]
    rng.shuffle(junctions)
    target = max(1, complexity)
    carved = 0
    for x, y in junctions:
        if carved >= target:
            break
        if not _is_floor(_at(tiles, x, y)):
            continue
        directions = [(1, 0), (-1, 0), (0, 1), (0, -1)]
        rng.shuffle(directions)
        for dx, dy in directions:
            length = rng.randrange(3, 7)
            strip = [(x + dx * step, y + dy * step) for step in range(1, length + 1)]
            margin = [(sx - dy, sy - dx) for sx, sy in strip]
            margin += [(sx + dy, sy + dx) for sx, sy in strip]
            margin.append((x + dx * (length + 1), y + dy * (length + 1)))
            if any(not (1 <= cx < GRID - 1 and 1 <= cy < GRID - 1) or _at(tiles, cx, cy) != WALL
                   for cx, cy in strip + margin):
                continue
            for cell in strip:
                _set(tiles, *cell, FLOOR)
            end = strip[-1]
            _set(things, *end, rng.choice(TREASURE + (AMMO, FOOD)))
            reserved.add(end)
            carved += 1
            break


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


def _place_doors(config: CampaignConfig, tiles: list[int], things: list[int],
                 rooms: list[Room], paths: list[list[tuple[int, int]]],
                 rng: random.Random, start: tuple[int, int],
                 exit_stand: tuple[int, int], allow_locks: bool = True) -> int:
    candidates = [candidate for path in paths if (candidate := _door_candidate(tiles, rooms, path))]
    rng.shuffle(candidates)
    # Every viable room-to-room junction gets a door: sound zones (see
    # _assign_sound_zones) only split at door tiles, so leaving most
    # candidates doorless silently merges most of the floor into one giant
    # zone and one gunshot wakes almost the whole map. This still misses
    # incidental adjacency where two unrelated corridors happen to run flush
    # against each other away from their own intended junction -- see
    # _split_oversized_zones, which catches what's left. Locked-door
    # intensity independently controls how many of these doors require the
    # single reusable gold key.
    placed = candidates
    for x, y, code in placed:
        _set(tiles, x, y, code)
    requested_locks = (min(len(placed), max(0, (int(config.locked_doors) - 1) // 2))
                       if allow_locks else 0)
    locked: tuple[tuple[int, int, int], ...] = ()
    key: tuple[int, int] | None = None
    # Secrets are carved before doors, so gating must also hold with every
    # pushwall already pushed: otherwise a secret pocket can quietly open a
    # route around the lock and the key becomes optional.
    pushwalls = {(i % GRID, i // GRID) for i, thing in enumerate(things) if thing == PUSHWALL}
    rests = {(x + 2, y) for x, y in pushwalls}
    # Choose a set that really separates the start from the exit. This makes
    # loop corridors useful exploration rather than accidental lock bypasses.
    for size in range(requested_locks, 0, -1):
        for trial in combinations(placed, size):
            for x, y, code in trial:
                _set(tiles, x, y, 92 if code == DOOR_EW else 93)
            gated = exit_stand not in _reachable(tiles, start, locked_open=False,
                                                 extra_passable=pushwalls, blocked=rests)
            key = _key_spot(tiles, rooms, trial, start) if gated else None
            if key:
                locked = trial
                break
            for x, y, code in trial:
                _set(tiles, x, y, code)
        if locked:
            break
    if key:
        _set(things, *key, GOLD_KEY)
    return len(locked)


def _door_zone(tiles: list[int], cell: tuple[int, int]) -> set[tuple[int, int]]:
    """The door-bounded floor region containing cell -- one 'room' as the
    player experiences it, since every zone boundary is a door tile."""
    seen = {cell}
    queue = deque([cell])
    while queue:
        x, y = queue.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nxt = x + dx, y + dy
            if nxt not in seen and _is_floor(_at(tiles, *nxt)):
                seen.add(nxt); queue.append(nxt)
    return seen


def _key_spot(tiles: list[int], rooms: list[Room],
              locked: tuple[tuple[int, int, int], ...],
              start: tuple[int, int]) -> tuple[int, int] | None:
    """Farthest reachable room center whose door-bounded region touches no
    locked door: finding the key beside the very door it opens is a
    non-puzzle, so such rooms never host it."""
    pre_lock = _reachable(tiles, start, locked_open=False)
    lock_sides = {(x + dx, y + dy) for x, y, _ in locked
                  for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))}
    candidates = sorted((room.center for room in rooms
                         if room.center in pre_lock and room.center != start),
                        key=lambda p: abs(p[0] - start[0]) + abs(p[1] - start[1]),
                        reverse=True)
    for center in candidates:
        if not lock_sides & _door_zone(tiles, center):
            return center
    return None


def _reachable(tiles: list[int], start: tuple[int, int], locked_open: bool,
               extra_passable: set[tuple[int, int]] | None = None,
               blocked: set[tuple[int, int]] | None = None) -> set[tuple[int, int]]:
    extra_passable = extra_passable or set()
    blocked = blocked or set()
    seen = {start}
    queue = deque([start])
    while queue:
        x, y = queue.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nxt = x + dx, y + dy
            if nxt in seen or nxt in blocked:
                continue
            tile = _at(tiles, *nxt)
            passable = _is_floor(tile) or tile in (DOOR_EW, DOOR_NS, DOOR_ELEVATOR, 101)
            if locked_open and tile in (DOOR_GOLD_EW, 93, 94, 95):
                passable = True
            if passable or nxt in extra_passable:
                seen.add(nxt); queue.append(nxt)
    return seen


def _floor_components(tiles: list[int]) -> list[set[tuple[int, int]]]:
    """Connected components of plain floor -- the same partition
    _assign_sound_zones turns into zone ids. Doors and the secret-exit
    modzone (107) are boundaries and never join a component."""
    unassigned = {(x, y) for y in range(GRID) for x in range(GRID)
                  if _is_floor(_at(tiles, x, y)) and _at(tiles, x, y) != SECRET_EXIT_ZONE}
    components = []
    while unassigned:
        start = min(unassigned, key=lambda point: (point[1], point[0]))
        component = {start}
        queue = deque([start])
        unassigned.remove(start)
        while queue:
            x, y = queue.popleft()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxt = x + dx, y + dy
                if nxt in unassigned and _is_floor(_at(tiles, *nxt)):
                    unassigned.remove(nxt); component.add(nxt); queue.append(nxt)
        components.append(component)
    return components


def _split_oversized_zones(tiles: list[int], rooms: list[Room], rng: random.Random,
                           cap: int = 160, min_piece: int = 20) -> int:
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
    while True:
        component = next((c for c in _floor_components(tiles)
                          if len(c) > cap and frozenset(c) not in stuck), None)
        if component is None:
            break
        candidates = [(x, y) for x, y in component
                     if not _inside_room(rooms, x, y) and _door_axis(tiles, x, y)]
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
                placed += 1
                split = True
                break
        if not split:
            stuck.add(frozenset(component))
    return placed


def _assign_sound_zones(tiles: list[int]) -> int:
    """Give each door-separated floor component its own ECWolf MapZone.

    Floor code 107 is skipped: it is the secret-exit modzone and must keep
    its exact value for the translator to rewrite the adjacent switch."""
    components = _floor_components(tiles)
    for zone_count, component in enumerate(components):
        zone = FLOOR + (zone_count % (ZONE_MAX - FLOOR + 1))
        for x, y in component:
            _set(tiles, x, y, zone)
    return len(components)


def _apply_wall_theme(tiles: list[int], rooms: list[Room], number: int,
                      rng: random.Random) -> None:
    """Apply native WL6 materials without changing traversable geometry."""
    base, accents = WALL_THEMES[(number - 1) % len(WALL_THEMES)]
    for index, tile in enumerate(tiles):
        if tile == WALL:
            tiles[index] = base
    for room_index, room in enumerate(rooms):
        accent = accents[room_index % len(accents)]
        other_accents = set(accents) - {accent}
        sides = (
            [(x, room.y - 1) for x in range(room.x - 1, room.x + room.w + 1)],
            [(x, room.y + room.h) for x in range(room.x - 1, room.x + room.w + 1)],
            [(room.x - 1, y) for y in range(room.y, room.y + room.h)],
            [(room.x + room.w, y) for y in range(room.y, room.y + room.h)],
        )
        if accent in DECOR_WALLS:
            # A single landmark tile, centered on its longest clean wall run
            # so it reads as a deliberately hung picture, not a random
            # jitter -- never the material for the whole room.
            runs = [[cell for cell in side if _at(tiles, *cell) == base] for side in sides]
            run = max(runs, key=len, default=[])
            if run:
                x, y = run[len(run) // 2]
                _set(tiles, x, y, accent)
            continue
        for side in sides:
            for x, y in side:
                if _at(tiles, x, y) != base:
                    continue
                # Two rooms can sit close enough that their wall rings land
                # a single rock tile apart. Painting both with a different
                # accent flips the material within a couple of corridor
                # tiles; leaving this cell in the shared base material keeps
                # a neutral buffer instead of an abrupt seam.
                if any(_at(tiles, x + dx, y + dy) in other_accents
                       for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                    continue
                _set(tiles, x, y, accent)


def _hint_secrets(tiles: list[int], things: list[int], number: int,
                  rng: random.Random) -> None:
    """Hang a landmark decor tile (banner, portrait, insignia) on every
    pushwall, the way the original episodes telegraph most of theirs. Runs
    after _apply_wall_theme so the theme can't repaint the hint, and prefers
    the floor theme's own decor accents so the hint matches the material."""
    _, accents = WALL_THEMES[(number - 1) % len(WALL_THEMES)]
    hints = tuple(accent for accent in accents if accent in DECOR_WALLS) or SECRET_HINTS
    for index, thing in enumerate(things):
        if thing == PUSHWALL:
            tiles[index] = rng.choice(hints)


def _rooms_by_distance(rooms: list[Room], edges: list[tuple[int, int]]) -> list[int]:
    """Room indices ordered farthest-first from the start room's graph node."""
    links = {i: [] for i in range(len(rooms))}
    for a, b in edges:
        links[a].append(b); links[b].append(a)
    distance = {0: 0}
    queue = deque([0])
    while queue:
        room = queue.popleft()
        for nxt in links[room]:
            if nxt not in distance:
                distance[nxt] = distance[room] + 1
                queue.append(nxt)
    return sorted((i for i in distance if i != 0), key=distance.get, reverse=True)


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
        footprint = [(wx + dx * depth, wy + side)
                     for depth in range(4) for side in (-1, 0, 1)]
        if any(not (1 <= x < GRID - 1 and 1 <= y < GRID - 1) or _at(tiles, x, y) != WALL
               for x, y in footprint):
            continue
        for depth in (1, 2):
            _set(tiles, wx + dx * depth, wy, FLOOR)
        for depth in (1, 2, 3):
            for side in (-1, 1):
                _set(tiles, wx + dx * depth, wy + side, ELEVATOR_TILE)
        _set(tiles, wx + dx * 3, wy, ELEVATOR_TILE)
        _set(tiles, wx, wy, DOOR_GOLD_EW if locked else DOOR_ELEVATOR)
        return wx + dx * 2, wy
    raise ValueError("terminal room has no clear east/west wall for an elevator")


def _place_secret(tiles: list[int], things: list[int], room: Room,
                  rng: random.Random, secret_exit: bool = False) -> tuple[int, int] | None:
    px = room.x + room.w
    if px + 4 >= GRID - 1:
        return None
    # Sweep rows outward from the wall's midline so one crossing corridor
    # doesn't doom the whole room's secret.
    mid = room.y + room.h // 2
    for py in sorted(range(room.y + 1, room.y + room.h - 1), key=lambda y: abs(y - mid)):
        reward = _carve_secret_pocket(tiles, things, px, py, rng, secret_exit)
        if reward:
            return reward
    return None


def _carve_secret_pocket(tiles: list[int], things: list[int], px: int, py: int,
                         rng: random.Random, secret_exit: bool) -> tuple[int, int] | None:
    # Corridors and elevator bays may already cross this nominal room wall.
    # Never turn their floor back into a pushwall.
    if _at(tiles, px, py) != WALL or not _is_floor(_at(tiles, px - 1, py)):
        return None
    cells = [(x, y) for x in range(px + 1, px + 4) for y in range(py - 1, py + 2)]
    # The pocket keeps a one-tile rock margin on every exposed flank so it
    # connects to nothing but its own pushwall. Without it, a pocket brushing
    # a passing corridor leaks the reward -- and pushing the wall opens a
    # route that can bypass a locked door entirely.
    margin = [(px + 4, y) for y in range(py - 1, py + 2)]
    margin += [(x, py - 2) for x in range(px + 1, px + 4)]
    margin += [(x, py + 2) for x in range(px + 1, px + 4)]
    margin += [(px, py - 1), (px, py + 1)]
    if any(_at(tiles, x, y) != WALL for x, y in cells + margin):
        return None
    for x, y in cells:
        _set(tiles, x, y, FLOOR)
    _set(tiles, px, py, WALL)
    _set(things, px, py, PUSHWALL)
    # The pushed wall slides two tiles east and settles on (px + 2, py), so
    # nothing collectible may sit on that track: the reward goes one cell to
    # the side, where it stays visible and reachable around the settled slab.
    reward = (px + 2, py + rng.choice((-1, 1)))
    _set(things, *reward, rng.choice(TREASURE + (MACHINE_GUN, CHAINGUN)))
    if secret_exit:
        # Native secret exits are an elevator switch with floor code 107 in
        # front of it: the translator's modzone rewrites that switch's
        # Exit_Normal trigger into Exit_Secret. The switch goes into the
        # pocket's back wall, one step past the settled pushwall.
        _set(tiles, px + 4, py, ELEVATOR_TILE)
        _set(tiles, px + 3, py, SECRET_EXIT_ZONE)
    return reward


def _place_population(config: CampaignConfig, number: int, rooms: list[Room],
                      tiles: list[int], things: list[int], reserved: set[tuple[int, int]],
                      rng: random.Random) -> tuple[int, int, int]:
    progression = (number - 1) / 8
    per_room = max(1, round(config.guard_density * .7 + progression * 2))
    toughness = int(config.enemy_toughness)
    unlocked = ENEMY_FAMILIES[:max(1, min(len(ENEMY_FAMILIES), toughness))]
    names = [name for name, *_ in unlocked]
    families = [family for _, family, *_ in unlocked]
    base_weights = [weight for _, _, weight, _ in unlocked]
    # A fresh door breach must not reveal a high-tier shooter at point-blank
    # range (report checklist #5): anything picked as officer/SS within a
    # short walk of a door tile is downgraded to the safe guard filler.
    doors = {(x, y) for y in range(GRID) for x in range(GRID) if _at(tiles, x, y) in DOORS}

    def near_door(x: int, y: int, radius: int = 3) -> bool:
        return any(abs(x - dx) + abs(y - dy) <= radius for dx, dy in doors)

    def pick_family() -> tuple[str, tuple[int, ...]]:
        # Guards stay the common baseline; officers/SS grow more common as
        # the campaign progresses instead of matching the pistol-start opener.
        weights = [weight * (1 + progression) if name in ("officer", "ss") else weight
                   for name, weight in zip(names, base_weights)]
        index = rng.choices(range(len(families)), weights=weights, k=1)[0]
        return names[index], families[index]

    tier_counts = [0, 0, 0]
    for ridx, room in enumerate(rooms[1:], 1):
        candidates = [(x, y) for y in range(room.y + 2, room.y + room.h - 1)
                      for x in range(room.x + 2, room.x + room.w - 1)
                      if (x, y) not in reserved and _at(things, x, y) == 0]
        rng.shuffle(candidates)
        cursor = 0
        for x, y in candidates[cursor:cursor + per_room]:
            name, family = pick_family()
            if name in ("officer", "ss") and near_door(x, y):
                family = GUARDS
            _set(things, x, y, rng.choice(family))
            tier_counts[0] += 1
        cursor += per_room
        # ECWolf's base translator treats +36 as the next cumulative skill
        # tier: skill 2 actors join the easy population on medium, and skill 3
        # actors join both on hard. They require their own cells in plane 2.
        extra = max(0, round(per_room * (0.20 + progression * 0.12)))
        for tier in (1, 2):
            for x, y in candidates[cursor:cursor + extra]:
                name, family = pick_family()
                if name in ("officer", "ss") and near_door(x, y):
                    family = GUARDS
                _set(things, x, y, rng.choice(family) + 36 * tier)
                tier_counts[tier] += 1
            cursor += extra
        if candidates[cursor:]:
            x, y = candidates[cursor]
            if ridx % max(2, 7 - int(config.supplies)) == 0:
                _set(things, x, y, rng.choice((AMMO, FOOD, FIRST_AID)))
            elif ridx % max(2, 7 - int(config.treasure)) == 1:
                _set(things, x, y, rng.choice(TREASURE))
    _guarantee_supplies(config, rooms, things, reserved, rng)
    return tuple(tier_counts)


def _guarantee_supplies(config: CampaignConfig, rooms: list[Room], things: list[int],
                        reserved: set[tuple[int, int]], rng: random.Random) -> None:
    """Ammo floor follows the report's expected-bullet-sink model: sum each
    placed enemy's AMMO_COST, then require a fresh pistol start (8 rounds
    plus 8 per map clip) to clear that total with a 1.2-1.4x margin. Health
    keeps a simpler total-opposition heuristic scaled by the supplies dial."""
    expected_need = sum(AMMO_COST.get(FAMILY_BY_CODE.get(code), 0.0) for code in things if code)
    target_ratio = 1.15 + 0.05 * int(config.supplies)
    ammo_now = things.count(AMMO)
    accessible = 8 + 8 * ammo_now
    deficit = expected_need * target_ratio - accessible
    ammo_target = math.ceil(deficit / 8) if deficit > 0 else 0
    total_enemies = sum(1 for code in things if code in FAMILY_BY_CODE)
    health_target = max(1, total_enemies // max(6, 14 - int(config.supplies)))
    health_now = things.count(FOOD) + things.count(FIRST_AID)
    available = [(x, y) for room in rooms for y in range(room.y + 1, room.y + room.h - 1)
                 for x in range(room.x + 1, room.x + room.w - 1)
                 if (x, y) not in reserved and _at(things, x, y) == 0]
    rng.shuffle(available)
    for thing in [AMMO] * ammo_target + [FIRST_AID] * max(0, health_target - health_now):
        if not available:
            break
        _set(things, *available.pop(), thing)


def _ensure_early_heal(tiles: list[int], things: list[int], rooms: list[Room],
                       start: tuple[int, int], reserved: set[tuple[int, int]],
                       rng: random.Random, max_tiles: int = 20) -> None:
    """Report health-pacing rule: the first visible heal should be reachable
    within about 20 traversed tiles of a pistol-start spawn."""
    seen = {start: 0}
    queue = deque([start])
    within: set[tuple[int, int]] = set()
    while queue:
        x, y = queue.popleft()
        dist = seen[(x, y)]
        within.add((x, y))
        if dist >= max_tiles:
            continue
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nxt = x + dx, y + dy
            if nxt in seen:
                continue
            tile = _at(tiles, *nxt)
            if _is_floor(tile) or tile in DOORS:
                seen[nxt] = dist + 1
                queue.append(nxt)
    if any(_at(things, x, y) in (FOOD, FIRST_AID) for x, y in within):
        return
    candidates = [(x, y) for x, y in within
                  if (x, y) not in reserved and _at(things, x, y) == 0 and _inside_room(rooms, x, y)]
    if candidates:
        _set(things, *rng.choice(candidates), FOOD)


def _place_decorations(rooms: list[Room], tiles: list[int], things: list[int],
                       reserved: set[tuple[int, int]], start: tuple[int, int],
                       rng: random.Random) -> None:
    """Sprinkle native WL6 furniture into rooms for visual variety.

    Blocking statics (barrels, tables, plants, pillars...) go in mirrored
    left/right pairs so a decorated room reads as deliberately laid out
    rather than cluttered, but only on interior cells inset from every wall,
    and only ever committed once a full-map reachability check confirms
    blocking them doesn't shrink what's reachable from spawn -- so furniture
    can never wall the player out of part of a level. Open (non-solid)
    decorations carry no such risk and are just sprinkled in loosely.
    """
    baseline = len(_reachable(tiles, start, locked_open=True))
    blocked_cells: set[tuple[int, int]] = set()
    for room in rooms:
        if room.w < 5 or room.h < 5:
            continue
        cx, _ = room.center
        interior = {(x, y) for x in range(room.x + 1, room.x + room.w - 1)
                    for y in range(room.y + 1, room.y + room.h - 1)}
        free = {cell for cell in interior - reserved if _at(things, *cell) == 0}
        pairs = [((x, y), (2 * cx - x, y)) for x, y in free
                 if x < cx and (2 * cx - x, y) in free]
        rng.shuffle(pairs)
        for (ax, ay), (bx, by) in pairs:
            candidate = blocked_cells | {(ax, ay), (bx, by)}
            # A blocked cell is never counted as "reachable" even when it's
            # perfectly safe to occupy, so compare against baseline minus the
            # blocked cells themselves -- only a shortfall beyond that means
            # some other cell got cut off.
            if len(_reachable(tiles, start, locked_open=True, blocked=candidate)) < baseline - len(candidate):
                continue
            static = rng.choice(STATIC_BLOCKING)
            _set(things, ax, ay, static)
            _set(things, bx, by, static)
            reserved.add((ax, ay)); reserved.add((bx, by))
            blocked_cells |= {(ax, ay), (bx, by)}
            break  # one deliberate symmetric pair per room, not clutter
        loose = [cell for cell in free - reserved if _at(things, *cell) == 0]
        rng.shuffle(loose)
        for cell in loose[:rng.randrange(0, 2)]:
            _set(things, *cell, rng.choice(STATIC_OPEN))
            reserved.add(cell)


def _place_boss(things: list[int], room: Room, reserved: set[tuple[int, int]]) -> None:
    cx, cy = room.center
    positions = [(cx, cy), (cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)]
    bx, by = next(((x, y) for x, y in positions
                   if (x, y) not in reserved and _at(things, x, y) == 0), (cx, cy))
    _set(things, bx, by, HANS_GROSSE)
    reserved.add((bx, by))
    supplies = ((cx - 2, cy - 2, FIRST_AID), (cx + 2, cy - 2, FIRST_AID),
                (cx - 2, cy + 2, AMMO), (cx + 2, cy + 2, AMMO))
    for x, y, thing in supplies:
        if _at(things, x, y) == 0:
            _set(things, x, y, thing)
            reserved.add((x, y))


def generate_map(config: CampaignConfig, number: int, attempt: int = 0,
                 secret_exit: bool = False) -> GeneratedMap:
    seed = config.floor_seed(number, attempt)
    rng = random.Random(seed)
    tiles = [WALL] * (GRID * GRID)
    things = [0] * (GRID * GRID)
    complexity = int(config.layout_complexity)
    count = min(17, 9 + complexity + number // 3)
    rooms = _rooms(rng, count)
    for room in rooms:
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                _set(tiles, x, y, FLOOR)
    tree_edges = _tree(rooms)
    # A few loop corridors give real route choices, but every loop is also
    # a shortcut that shrinks the critical path, so they stay scarce.
    loops = _loop_edges(rooms, tree_edges, max(0, complexity - 2))
    edges = tree_edges + loops
    paths = [_carve_connection(tiles, rooms[a], rooms[b], rng, complexity) for a, b in edges]
    _widen_corridors(tiles, rooms, paths, rng)
    start = rooms[0].center
    _set(things, *start, PLAYER_START)
    is_boss = number == 9
    exit_room = None
    exit_stand = None
    # Prefer the farthest room, but fall back through the distance order:
    # a corridor may have crossed every east/west wall of the first choice.
    for room_index in _rooms_by_distance(rooms, edges):
        try:
            exit_stand = _place_elevator(tiles, rooms[room_index], locked=is_boss)
            exit_room = rooms[room_index]
            break
        except ValueError:
            continue
    if exit_room is None:
        raise ValueError("no room can host the exit elevator")
    reserved = {start, exit_stand}
    rewards: list[tuple[int, int]] = []
    # Report's secret budget is 2-6 per standard floor; scale directly with
    # the intensity dial instead of undershooting to 1 at the low end.
    target_secrets = max(2, int(config.secrets))
    # A secret pocket must never reuse or seal the terminal room's elevator
    # wall after the elevator has been carved.
    candidates = [room for room in rooms[1:] if room != exit_room]
    rng.shuffle(candidates)
    for room in candidates:
        if len(rewards) >= target_secrets:
            break
        reward = _place_secret(tiles, things, room, rng, secret_exit and not rewards)
        if reward:
            rewards.append(reward); reserved.add(reward)
    _carve_reward_stubs(tiles, things, rooms, paths, reserved, rng, complexity)
    locks = _place_doors(config, tiles, things, rooms, paths, rng, start, exit_stand,
                         allow_locks=not is_boss)
    _split_oversized_zones(tiles, rooms, rng)
    if is_boss:
        boss_room = max((room for room in rooms[1:] if room != exit_room), key=lambda room: room.w * room.h)
        _place_boss(things, boss_room, reserved)
        locks += 1
    enemy_tiers = _place_population(config, number, rooms, tiles, things, reserved, rng)
    _ensure_early_heal(tiles, things, rooms, start, reserved, rng)
    _place_decorations(rooms, tiles, things, reserved, start, rng)
    _assign_sound_zones(tiles)
    _apply_wall_theme(tiles, rooms, number, rng)
    _hint_secrets(tiles, things, number, rng)
    result = GeneratedMap(number, tiles, things, start, exit_stand, rewards, seed,
                          secret_exit, locks, is_boss, enemy_tiers)
    validate_map(result)
    return result


def validate_map(level: GeneratedMap) -> None:
    if len(level.tiles) != GRID * GRID or len(level.things) != GRID * GRID:
        raise ValueError("invalid plane dimensions")
    seen = {level.start}
    queue = deque([level.start])
    while queue:
        x, y = queue.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nxt = x + dx, y + dy
            if nxt in seen:
                continue
            tile = _at(level.tiles, *nxt)
            if _is_floor(tile) or tile in DOORS:
                seen.add(nxt); queue.append(nxt)
    if level.exit_stand not in seen:
        raise ValueError("elevator standing position is unreachable")
    sx, sy = level.exit_stand
    # The switch must sit on the stand's east/west axis with the shaft floor
    # opposite it: tile 21 cannot be activated from north or south.
    if not any(_at(level.tiles, sx + dx, sy) == ELEVATOR_TILE
               and _is_floor(_at(level.tiles, sx - dx, sy))
               for dx in (1, -1)):
        raise ValueError("elevator switch is not usable from its standing position")
    if not level.secret_rewards:
        raise ValueError("map has no rewarded secret")
    pushwalls = [(i % GRID, i // GRID) for i, thing in enumerate(level.things)
                 if thing == PUSHWALL]
    if len(pushwalls) < len(level.secret_rewards):
        raise ValueError("secret reward has no pushwall")
    # Each pushed wall slides two tiles east and permanently occupies its
    # resting cell, so rewards are validated against the post-push layout.
    rests = {(x + 2, y) for x, y in pushwalls}
    baseline = _reachable(level.tiles, level.start, locked_open=True)
    for x, y in pushwalls:
        if _is_floor(_at(level.tiles, x, y)):
            raise ValueError("pushwall trigger is not on a solid wall")
        if not _is_floor(_at(level.tiles, x - 1, y)):
            raise ValueError("pushwall has no movement clearance")
        if (x - 1, y) not in baseline:
            raise ValueError("pushwall cannot be approached")
        if _at(level.tiles, x, y) not in DECOR_WALLS:
            raise ValueError("pushwall is not hinted by a decor wall tile")
    opened = _reachable(level.tiles, level.start, locked_open=True,
                        extra_passable=set(pushwalls), blocked=rests)
    for reward in level.secret_rewards:
        if _at(level.things, *reward) not in TREASURE + (MACHINE_GUN, CHAINGUN):
            raise ValueError("secret is missing a valuable reward")
        if reward in rests:
            raise ValueError("secret reward sits on the pushwall's resting cell")
        if reward not in opened:
            raise ValueError("secret reward is unreachable after opening pushwall")
    zone_count = level.tiles.count(SECRET_EXIT_ZONE)
    if level.has_secret_exit:
        if zone_count != 1:
            raise ValueError("designated secret-route map needs exactly one secret exit")
        index = level.tiles.index(SECRET_EXIT_ZONE)
        zx, zy = index % GRID, index // GRID
        if _at(level.tiles, zx + 1, zy) != ELEVATOR_TILE:
            raise ValueError("secret exit zone has no elevator switch east of it")
        if (zx, zy) not in opened:
            raise ValueError("secret elevator is unusable after opening its pushwall")
    elif zone_count:
        raise ValueError("secret exit zone on a floor with no secret route")
    validate_door_axes(level.tiles)
    if level.locked_doors:
        if not level.boss and GOLD_KEY not in level.things:
            raise ValueError("locked map has no gold key")
        if not any(tile in (92, 93) for tile in level.tiles):
            raise ValueError("locked map has no locked door")
        if not level.boss:
            key_index = level.things.index(GOLD_KEY)
            key_position = key_index % GRID, key_index // GRID
            if key_position not in _reachable(level.tiles, level.start, locked_open=False):
                raise ValueError("gold key is unreachable before its lock")
            lock_sides = {(x + dx, y + dy)
                          for i, tile in enumerate(level.tiles) if tile in (92, 93)
                          for x, y in ((i % GRID, i // GRID),)
                          for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))}
            if lock_sides & _door_zone(level.tiles, key_position):
                raise ValueError("gold key shares a room with a locked door")
        # The lock must hold even with every secret pushwall already pushed:
        # a pocket may never double as a route around the key.
        if level.exit_stand in _reachable(level.tiles, level.start, locked_open=False,
                                          extra_passable=set(pushwalls), blocked=rests):
            raise ValueError("locked elevator route can be bypassed")
        if level.exit_stand not in _reachable(level.tiles, level.start, locked_open=True):
            raise ValueError("exit is unreachable after obtaining the key")
    if level.boss and level.things.count(HANS_GROSSE) != 1:
        raise ValueError("boss floor must contain exactly one boss")
    if level.boss:
        boss_index = level.things.index(HANS_GROSSE)
        boss_position = boss_index % GRID, boss_index // GRID
        if boss_position not in _reachable(level.tiles, level.start, locked_open=False):
            raise ValueError("boss is unreachable before the boss elevator lock")
    validate_objects(level)


def validate_objects(level: GeneratedMap) -> None:
    for index, thing in enumerate(level.things):
        if thing == 0:
            continue
        x, y = index % GRID, index // GRID
        tile = _at(level.tiles, x, y)
        if thing == PUSHWALL:
            if _is_floor(tile):
                raise ValueError(f"pushwall at {(x, y)} is not on a solid tile")
            continue
        if not _is_floor(tile):
            raise ValueError(f"thing {thing} at {(x, y)} is not on floor")
        if thing in ENEMY_CODES and thing != HANS_GROSSE:
            distance = abs(x - level.start[0]) + abs(y - level.start[1])
            if distance < 6:
                raise ValueError(f"enemy at {(x, y)} is too close to player start")


def validate_door_axes(tiles: list[int]) -> None:
    for y in range(GRID):
        for x in range(GRID):
            tile = _at(tiles, x, y)
            if tile not in DOORS:
                continue
            ew = tile % 2 == 0
            along = ((_at(tiles, x - 1, y), _at(tiles, x + 1, y)) if ew else
                     (_at(tiles, x, y - 1), _at(tiles, x, y + 1)))
            across = ((_at(tiles, x, y - 1), _at(tiles, x, y + 1)) if ew else
                      (_at(tiles, x - 1, y), _at(tiles, x + 1, y)))
            if not all(_is_floor(value) or value in DOORS for value in along):
                raise ValueError(f"door at {(x, y)} is blocked on its opening axis")
            if any(_is_floor(value) or value in DOORS for value in across):
                raise ValueError(f"door at {(x, y)} can be bypassed around its jamb")


def _wad_bytes(name: str, tiles: list[int], things: list[int]) -> bytes:
    planes = (tiles, things, [0] * (GRID * GRID))
    map_name = name.encode("ascii")[:15].ljust(16, b"\0")
    payload = b"WDC3.1" + struct.pack("<IHH", 1, 3, 16) + map_name + struct.pack("<HH", GRID, GRID)
    payload += b"".join(struct.pack("<4096H", *plane) for plane in planes)
    marker = name.encode("ascii")[:8].ljust(8, b"\0")
    directory = struct.pack("<II8s", 12, 0, marker) + struct.pack("<II8s", 12, len(payload), b"PLANES\0\0")
    return b"PWAD" + struct.pack("<II", 2, 12 + len(payload)) + payload + directory


def _mapinfo(secret_from: int) -> str:
    lines = [
        'gameinfo { drawreadthis = false }',
        'clearepisodes',
        'episode "RW01" { name = "Random Wolf" key = "R" }',
    ]
    for number in range(1, 10):
        # ECWolf only recognizes "EndSequence:<id>" or "EndTitle" as a real
        # end-of-game next-map value (see wl_game.cpp); anything else,
        # including the Doom/ZDoom-only "EndGameC" cast-call keyword this
        # used to say, is treated as a literal (nonexistent) map name and
        # crashes on exit once LevelInfo::Find fails to resolve it.
        nxt = f'RW{number + 1:02d}' if number < 9 else 'EndTitle'
        secret = ' secretnext = "RW10"' if number == secret_from else ''
        ceiling = CEILINGS[(number - 1) % len(CEILINGS)]
        music = MUSIC[(number - 1) % len(MUSIC)]
        par = 90 + number * 30
        lines.append(f'map "RW{number:02d}" "Random Floor {number}" {{ next = "{nxt}"{secret} '
                     f'levelnum = {number} par = {par} defaultceiling = "{ceiling}" music = "{music}" }}')
    lines.append(f'map "RW10" "Secret Floor" {{ next = "RW{secret_from + 1:02d}" levelnum = 10 '
                 f'par = 360 defaultceiling = "{CEILINGS[4]}" music = "{MUSIC[5]}" }}')
    return "\n".join(lines) + "\n"


def generate_campaign(config: CampaignConfig, output: Path,
                      progress: Callable[[int, int], None] | None = None,
                      cancelled: Callable[[], bool] | None = None) -> Path:
    levels = []
    secret_from = 1 + config.floor_seed(10) % 6
    for number in range(1, 11):
        if cancelled and cancelled():
            raise GenerationCancelled("campaign generation cancelled")
        last_error = None
        for attempt in range(50):
            try:
                levels.append(generate_map(config, number, attempt, number == secret_from))
                break
            except ValueError as error:
                last_error = error
        else:
            raise RuntimeError(f"floor {number} failed generation: {last_error}")
        if progress:
            progress(number, 10)
    manifest = {
        "generator": "randomwolf", "version": __version__, "seed": config.seed,
        "settings": json.loads(config.to_json()), "secret_from": secret_from,
        "floors": [{"number": level.number, "seed": level.seed,
                    "secrets": len(level.secret_rewards),
                    "locked_doors": level.locked_doors,
                    "boss": level.boss,
                    "enemy_tiers": level.enemy_tiers,
                    "validation": {
                        "passed": True,
                        "checks": ["bounds", "connectivity", "door_axes", "elevator",
                                   "key_lock_progression", "key_room_separation",
                                   "pushwall_clearance", "rewarded_secrets",
                                   "secret_hints", "secret_route", "boss"],
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
            write("mapinfo.txt", _mapinfo(secret_from))
            write("randomwolf-manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
            for level in levels:
                write(f"maps/rw{level.number:02d}.wad",
                      _wad_bytes(f"RW{level.number:02d}", level.tiles, level.things))
        validate_package(temp_path)
        if cancelled and cancelled():
            raise GenerationCancelled("campaign generation cancelled")
        temp_path.replace(output)
    finally:
        temp_path.unlink(missing_ok=True)
    return output


def read_manifest(package_path: Path) -> dict[str, object]:
    with zipfile.ZipFile(package_path) as package:
        return json.loads(package.read("randomwolf-manifest.json"))


def validate_package(package_path: Path) -> dict[str, object]:
    """Reopen and parse a completed temporary package before installation."""
    with zipfile.ZipFile(package_path) as package:
        corrupt = package.testzip()
        if corrupt:
            raise ValueError(f"corrupt package entry: {corrupt}")
        names = set(package.namelist())
        expected_maps = {f"maps/rw{number:02d}.wad" for number in range(1, 11)}
        if not expected_maps.issubset(names):
            raise ValueError("package is missing one or more campaign maps")
        forbidden = (".wl6", ".png", ".wav", ".ogg", ".voc")
        if any(name.lower().endswith(forbidden) for name in names):
            raise ValueError("package contains an asset file instead of map metadata")
        manifest = json.loads(package.read("randomwolf-manifest.json"))
        if len(manifest.get("floors", ())) != 10:
            raise ValueError("manifest does not describe ten floors")
        for name in expected_maps:
            wad = package.read(name)
            if len(wad) < 46 or wad[:4] != b"PWAD" or wad[12:18] != b"WDC3.1":
                raise ValueError(f"{name} has an invalid ECWolf WAD header")
            width, height = struct.unpack_from("<HH", wad, 42)
            if (width, height) != (GRID, GRID):
                raise ValueError(f"{name} is not a {GRID}x{GRID} map")
        return manifest
