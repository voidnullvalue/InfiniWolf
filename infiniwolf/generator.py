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
SCHABBS = 196
GRETEL = 197
GIFT = 215
FAT_FACE = 179
MECHA_HITLER = 178
FAKE_HITLER = 160
GHOSTS = (224, 225, 226, 227)
# Native WL6 boss roster only (wolfbosses.txt): Hans Grosse, Dr. Schabbs,
# Gretel Grosse, Otto Giftmacher, Fat Face, and MechaHitler. FakeHitler is
# an ordinary actor: unlike the real bosses it neither drops a key nor calls
# A_BossDeath (hitler.txt), so it is a boss-floor novelty spawn below instead.
# Trans Grosse/UberMutant/Wilhelm/DeathKnight are Spear of Destiny actors
# (spearbosses.txt) whose sprites live in SOD's VSWAP, not wl6's -- placing
# them here would show up as missing graphics under "--data wl6".
BOSSES = (HANS_GROSSE, SCHABBS, GRETEL, GIFT, FAT_FACE, MECHA_HITLER)
# Only these native bosses declare DropItem GoldKey (wolfbosses.txt).
KEY_DROP_BOSSES = frozenset({HANS_GROSSE, GRETEL})

# Native Wolf3D map object numbers, interpreted by ECWolf's base translator.
# ECWolf's old-format loader computes each thing's facing angle as
# (oldnum - base) * 90 and casts it straight to MapTile::Side, whose enum
# order is {East, North, West, South} (gamemap.h) -- NOT the North-first
# order these arrays are indexed in throughout this file (facings tuple,
# _pick_stationary_facing, etc. all reason in (N, E, S, W) order). Every
# tuple here is reordered so index 0/1/2/3 already lines up with N/E/S/W,
# i.e. GUARDS[0] (base+1) faces north in-engine, GUARDS[1] (base+0) faces
# east, GUARDS[2] (base+3) faces south, GUARDS[3] (base+2) faces west.
# Without this reorder, every facing decision computed correctly against
# the floor plan still placed the wrong one of the 4 thing-codes, so
# actors could face a wall the generator had explicitly avoided -- a bug
# invisible to any test that (like the generator) assumes N/E/S/W order.
GUARDS = (109, 108, 111, 110)
OFFICERS = (117, 116, 119, 118)
SS = (127, 126, 129, 128)
DOGS = (135, 134, 137, 136)
PATROL_GUARDS = tuple(code + 4 for code in GUARDS)
PATROL_OFFICERS = tuple(code + 4 for code in OFFICERS)
PATROL_SS = tuple(code + 4 for code in SS)
PATROL_DOGS = tuple(code + 4 for code in DOGS)
# Old-format PatrolPoint codes, indexed in this file's N/E/S/W convention.
PATROL_POINT_CODES = (92, 90, 96, 94)
PATROL_POINT_DIRECTIONS = {code: index for index, code in enumerate(PATROL_POINT_CODES)}
AMMO, FOOD, FIRST_AID, MACHINE_GUN, CHAINGUN, ONE_UP = 49, 47, 48, 50, 51, 56
TREASURE = (52, 53, 54, 55)
ENEMY_CODES = frozenset(code + tier * 36
                        for family in (GUARDS, OFFICERS, SS, DOGS,
                                       PATROL_GUARDS, PATROL_OFFICERS, PATROL_SS, PATROL_DOGS)
                        for code in family for tier in range(3)) | set(BOSSES) | {FAKE_HITLER, *GHOSTS}

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
PATROLS_BY_FAMILY = dict(zip((GUARDS, OFFICERS, SS, DOGS),
                             (PATROL_GUARDS, PATROL_OFFICERS, PATROL_SS, PATROL_DOGS)))
FAMILY_BY_CODE = {code + 36 * tier: family
                  for _, family, _, _ in ENEMY_FAMILIES
                  for variant in (family, PATROLS_BY_FAMILY[family])
                  for code in variant for tier in range(3)}
AMMO_COST = {family: cost for _, family, _, cost in ENEMY_FAMILIES}

# Native WL6 wall tiles only. Each tuple is (base, room accents), and every
# tile in an entry is verified (by prefix in wolf3d.txt's xlat table, e.g.
# GSTONE*/BSTONE*/WOD*/METAL*/BRICK*) to belong to the SAME texture family as
# that entry's base. Mixing families within one theme (e.g. a blue stone base
# with metal or grey accents) used to be silently possible here -- floor
# number picked a fixed theme index, so only one entry's mismatch was ever on
# screen at a time and it went unnoticed. Once theme selection was randomized
# per floor (any floor can land on any entry), every entry needs to be
# internally coherent on its own, not just "looks fine on floor N".
# Accent 13 (FAKEDOR, a sealed door/lift-shutter graphic) and 16 (SKY1, an
# outdoor-only texture) are deliberately excluded: neither reads as a normal
# room wall, so they're replaced with plain material variants below.
# Tile 7 (BSTCELB1, the skeleton-in-a-cage prison-cell wall) reads as a specific set
# piece, not a generic wall -- it must never be the base fill for an entire
# floor. It's demoted to a room accent here so only the districts that land
# on it get a cellblock-styled room; the base stays the plain blue stone (8).
# BLUSKUL (34) and BLUSWAS (36) are likewise DECOR_WALLS landmarks, kept in
# their own blue-panel theme rather than mixed into the plain BLUWALL material.
WALL_THEMES = (
    (1, (2, 3, 4)),        # grey stone: GSTONEA/B + flag/portrait decor
    (8, (7, 41)),          # blue stone: BSTONEA + cellblock accent + sign decor
    (40, ()),              # blue wall: BLUWALL plain panel/brick only
    (40, (34, 36)),        # blue insignia: BLUWALL + BLUSKUL/BLUSWAS landmark decor
    (12, (10, 11, 23)),    # wood: WOOD1 + eagle/portrait/cross decor
    (15, (14,)),           # metal: METAL1 + sign decor
    (17, (18, 20)),        # brick: BRICK1 + writing/eagle decor
)
# BSTONEB (9) is mottled blue-stone masonry, distinct from the BLUWALL panel.
# Keep it out of the normal pool: floor 10 occasionally uses it as a rare bare
# material, while floors 1--9 never do.
FLOOR_TEN_STONE_THEME = (9, ())
# Landmark decoration tiles (portraits, banners, insignia, signage/graffiti):
# these should read as a single accent set into an otherwise plain wall, the
# way they're used in the original game, never as the material of an entire
# room. Every other accent above is just an alternate plain material and is
# fine covering a whole room's walls.
DECOR_WALLS = frozenset({3, 4, 7, 10, 11, 14, 18, 20, 23, 34, 36, 41})

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

# Decoration themes keyed by room role+tier, derived from community-map
# placement patterns: guard rooms get lamps and vases, storage closets get
# barrel clusters, grand anchor rooms get pillar pairs, barracks get tables.
_DECOR_BLOCKING: dict[str, tuple[int, ...]] = {
    "guardpost": (26, 35, 31),   # FloorLamp, Vase, GreenPlant
    "grand":     (30, 26, 35),   # WhitePillar, FloorLamp, Vase
    "barracks":  (25, 36, 58),   # TableWithChairs, BareTable, Barrel
    "storage":   (58, 24, 59),   # Barrel, GreenBarrel, Well
    "lounge":    (25, 35, 34),   # TableWithChairs, Vase, BrownPlant
    "corridor":  (26,),          # FloorLamp only
}
_DECOR_OPEN: dict[str, tuple[int, ...]] = {
    "guardpost": (37, 27),   # CeilingLight, Chandelier
    "grand":     (27, 37),   # Chandelier dominant
    "barracks":  (61, 67),   # Blood, Pots  (battle-worn)
    "storage":   (67, 61),   # Pots, Blood
    "lounge":    (27, 67),   # Chandelier, Pots
    "corridor":  (37,),      # CeilingLight
}


def _decor_theme(role: str, tier: str) -> str:
    if tier == "closet":
        return "storage"
    if tier == "hall":
        return "corridor"
    if tier == "anchor" or role in ("climax",):
        return "grand"
    if role == "start":
        return "guardpost"
    if role == "relief":
        return "lounge"
    return "barracks"   # beat, branch, ring, hub, filler
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


@dataclass(frozen=True, slots=True)
class RoomSpec:
    role: str
    tier: str
    district: int
    motif: str = "spine"


@dataclass(slots=True)
class FloorPlan:
    specs: list[RoomSpec]
    edges: list[tuple[int, int]]
    loop_edges: list[tuple[int, int]]
    motifs: tuple[str, ...]
    # Realization metadata keeps grammar membership out of gameplay roles.
    critical: frozenset[int] = frozenset()
    size_groups: tuple[tuple[int, ...], ...] = ()


@dataclass(slots=True)
class PlacedPlan:
    rooms: list[Room]
    spec_indices: list[int]
    edges: list[tuple[int, int]]
    loop_edges: list[tuple[int, int]]


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
    motifs: tuple[str, ...] = ()
    motif_rooms: tuple[str, ...] = ()
    secret_variants: tuple[str, ...] = ()
    shortcut_pushwalls: tuple[tuple[int, int], ...] = ()
    critique: tuple[str, ...] = ()
    rooms: tuple[Room, ...] = ()


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


def _plan_floor(rng: random.Random, complexity: int, number: int) -> FloorPlan:
    spine_count = min(8, 5 + (complexity + 1) // 2 + (number >= 6))
    beat_count = spine_count - 4
    tiers = ["standard"] + [("hall" if rng.random() < 0.25 else "standard")
                            for _ in range(beat_count)]
    tiers += ["anchor", rng.choice(("closet", "standard")), "standard"]
    roles = ["start"] + ["beat"] * beat_count + ["climax", "relief", "exit"]
    district_count = 3 if spine_count >= 7 and rng.random() < 0.6 else 2
    cuts = sorted(rng.sample(range(2, spine_count - 1), district_count - 1))
    districts = [sum(index >= cut for cut in cuts) for index in range(spine_count)]
    specs = [RoomSpec(role, tier, district)
             for role, tier, district in zip(roles, tiers, districts)]
    edges = [(index, index + 1) for index in range(spine_count - 1)]
    loops: list[tuple[int, int]] = []
    critical = set(range(spine_count))
    groups: list[tuple[int, ...]] = []
    budget = min(3, 1 + (complexity >= 3) + (rng.random() < 0.35))
    motifs = ["ring"]
    remaining = ["hub", "wings", "gallery"]
    rng.shuffle(remaining)
    motifs += remaining[:budget - 1]

    # The first motif always spends topology budget on a real reconvergence.
    pairs = [(i, j) for i in range(1, spine_count - 2)
             for j in range(i + 2, spine_count - 1)]
    weights = [spine_count - abs((i + j) - (spine_count - 1)) for i, j in pairs]
    left, right = rng.choices(pairs, weights=weights, k=1)[0]
    parent = left
    for _ in range(rng.randrange(1, 3)):
        node = len(specs)
        specs.append(RoomSpec("ring", "standard", districts[left], "ring"))
        edges.append((parent, node)); critical.add(node)
        parent = node
    edges.append((parent, right)); loops.append((parent, right))

    middle_beats = list(range(1, 1 + beat_count))
    if "hub" in motifs:
        hub = rng.choice(middle_beats)
        specs[hub] = RoomSpec("hub", "anchor", districts[hub], "hub")
        climax = roles.index("climax")
        specs[climax] = RoomSpec("climax", "standard", districts[climax])
        for role in ["branch", "branch"] + ["closet"] * rng.randrange(1, 3):
            node = len(specs)
            specs.append(RoomSpec(role, "closet" if role == "closet" else "standard",
                                  districts[hub], "hub"))
            edges.append((hub, node)); critical.add(node)
    if "wings" in motifs:
        parent = rng.choice(middle_beats)
        wings = tuple(range(len(specs), len(specs) + 2))
        for node in wings:
            specs.append(RoomSpec("branch", "standard", districts[parent], "wings"))
            edges.append((parent, node)); critical.add(node)
        groups.append(wings)
    if "gallery" in motifs:
        parent = rng.choice(middle_beats)
        district = districts[parent]
        gallery = []
        for _ in range(rng.randrange(2, 4)):
            node = len(specs)
            specs.append(RoomSpec("closet", "closet", district, "gallery"))
            edges.append((parent, node)); gallery.append(node); critical.add(node)
            parent = node
        groups.append(tuple(gallery))

    target = min(20, 14 + 2 * complexity)
    filler_tips: list[int] = []
    while len(specs) < target:
        if filler_tips and rng.random() < 0.35:
            parent = rng.choice(filler_tips)
            filler_tips.remove(parent)
        else:
            degrees = [sum(index in edge for edge in edges) for index in middle_beats]
            parent = rng.choices(middle_beats, weights=[1 / degree for degree in degrees], k=1)[0]
        role = "closet" if rng.random() < 0.45 else "branch"
        tier = "closet" if role == "closet" else rng.choice(("standard", "standard", "hall"))
        node = len(specs)
        specs.append(RoomSpec(role, tier, specs[parent].district, "filler"))
        edges.append((parent, node))
        filler_tips.append(node)
    if sum(spec.tier == "anchor" for spec in specs) != 1:
        raise ValueError("floor plan must have exactly one anchor")
    return FloorPlan(specs, edges, loops, tuple(motifs), frozenset(critical), tuple(groups))


def _room_size(rng: random.Random, tier: str) -> tuple[int, int]:
    if tier == "anchor":
        return rng.randrange(10, 14), rng.randrange(10, 14)
    if tier == "closet":
        return rng.randrange(4, 6), rng.randrange(4, 6)
    if tier == "hall":
        major, minor = rng.randrange(9, 14), rng.randrange(5, 8)
        return (major, minor) if rng.random() < 0.5 else (minor, major)
    return rng.randrange(6, 10), rng.randrange(6, 10)


def _place_planned_rooms(rng: random.Random, plan: FloorPlan) -> PlacedPlan:
    spine_count = next(index for index, spec in enumerate(plan.specs)
                       if spec.role == "exit") + 1
    sizes = [_room_size(rng, spec.tier) for spec in plan.specs]
    for group in plan.size_groups:
        shared = sizes[group[0]]
        for index in group[1:]:
            sizes[index] = shared
    parents: dict[int, int] = {}
    for child in range(1, len(plan.specs)):
        parents[child] = next(other for a, b in plan.edges
                              for other in ((b,) if a == child else (a,) if b == child else ())
                              if other < child)
    rooms: list[Room] = []
    kept: list[int] = []
    room_by_spec: dict[int, Room] = {}
    dropped: set[int] = set()
    used_sides: dict[int, dict[tuple[int, int], int]] = {}
    quadrant = rng.randrange(4)
    heading = ((1, 0), (-1, 0), (0, 1), (0, -1))[quadrant]
    w, h = sizes[0]
    sx = rng.randrange(4, 10) if heading[0] > 0 else (GRID - w - rng.randrange(4, 10)
         if heading[0] < 0 else rng.randrange(4, GRID - w - 3))
    sy = rng.randrange(4, 10) if heading[1] > 0 else (GRID - h - rng.randrange(4, 10)
         if heading[1] < 0 else rng.randrange(4, GRID - h - 3))
    start = Room(sx, sy, w, h)
    rooms.append(start); kept.append(0); room_by_spec[0] = start

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
                and not any(_overlaps(room, other) for other in rooms))

    grouped = {index for group in plan.size_groups for index in group}
    order = list(range(1, spine_count))
    pending = set(range(spine_count, len(plan.specs)))
    while pending:
        available = [index for index in pending if parents[index] not in pending]
        index = min(available, key=lambda item: (
            0 if plan.specs[parents[item]].role == "hub" else
            1 if item in grouped else 2 if plan.specs[item].role == "ring" else 3,
            item))
        order.append(index); pending.remove(index)
    for index in order:
        parent_index = parents[index]
        while parent_index in dropped:
            parent_index = parents[parent_index]
        parent = room_by_spec[parent_index]
        room = None
        for _ in range(60):
            if index < spine_count:
                dx, dy = heading
                sides = [(dx, dy), (-dy, dx), (dy, -dx)]
                weights = (6, 2, 2)
                side = rng.choices(sides, weights=weights, k=1)[0]
                gap = rng.randrange(1, 4)
            else:
                counts = used_sides.setdefault(parent_index, {})
                sides = ((1, 0), (-1, 0), (0, 1), (0, -1))
                side = rng.choices(sides, weights=[1 / (1 + 5 * counts.get(s, 0))
                                                   for s in sides], k=1)[0]
                gap = rng.randrange(1, 4)
            jitter = rng.randrange(-6, 7) if index < spine_count else rng.randrange(-11, 12)
            candidate = adjacent(parent, sizes[index], side, gap, jitter)
            if legal(candidate):
                room = candidate
                if index < spine_count:
                    heading = side
                else:
                    counts[side] = counts.get(side, 0) + 1
                break
        if room is None:
            # A crowded beat may need a second ring beyond its first wings;
            # keep the graph parent local before conceding to global scatter.
            for _ in range(120):
                side = rng.choice(((1, 0), (-1, 0), (0, 1), (0, -1)))
                candidate = adjacent(parent, sizes[index], side, rng.randrange(6, 13),
                                     rng.randrange(-18, 19))
                if legal(candidate):
                    room = candidate
                    break
        if room is None:
            rw, rh = sizes[index]
            for _ in range(200):
                candidate = Room(rng.randrange(3, 61 - rw), rng.randrange(3, 61 - rh), rw, rh)
                if legal(candidate):
                    room = candidate
                    break
        if room is None:
            if index in plan.critical or index < spine_count:
                raise ValueError("could not realize critical planned room")
            dropped.add(index)
            continue
        rooms.append(room); kept.append(index); room_by_spec[index] = room

    remap = {spec_index: room_index for room_index, spec_index in enumerate(kept)}

    def survivor(index: int) -> int:
        while index in dropped:
            index = parents[index]
        return index

    edges = []
    for a, b in plan.edges:
        a, b = survivor(a), survivor(b)
        edge = (remap[a], remap[b])
        if edge[0] != edge[1] and edge not in edges and edge[::-1] not in edges:
            edges.append(edge)
    loop_edges = [(remap[survivor(a)], remap[survivor(b)]) for a, b in plan.loop_edges]
    return PlacedPlan(rooms, kept, edges, loop_edges)


def _carve_notches(tiles: list[int], rooms: list[Room], rng: random.Random,
                   chance: float = 0.35) -> None:
    for room in rooms:
        if room.w < 6 or room.h < 6 or rng.random() >= chance:
            continue
        corners = [(False, False), (True, False), (False, True), (True, True)]
        rng.shuffle(corners)
        # The untouched center row and column join every remaining quadrant;
        # even two bites therefore cannot split the room.
        count = 2 if rng.random() < 0.25 else 1
        for right, bottom in corners[:count]:
            nw = rng.randint(2, min(3, (room.w - 2) // 2))
            nh = rng.randint(2, min(3, (room.h - 2) // 2))
            nx = room.x + room.w - nw if right else room.x
            ny = room.y + room.h - nh if bottom else room.y
            for y in range(ny, ny + nh):
                for x in range(nx, nx + nw):
                    _set(tiles, x, y, WALL)


def _carve_alcoves(tiles: list[int], rooms: list[Room], rng: random.Random,
                   chance: float = 0.3) -> None:
    established = list(rooms)
    for room in rooms:
        if rng.random() >= chance:
            continue
        span, depth = rng.randrange(2, 4), rng.randrange(2, 4)
        directions = [(0, -1), (0, 1), (-1, 0), (1, 0)]
        rng.shuffle(directions)
        for dx, dy in directions:
            if dx:
                bx = room.x - depth if dx < 0 else room.x + room.w
                by = room.y + (room.h - span) // 2
                bump = Room(bx, by, depth, span)
            else:
                bx = room.x + (room.w - span) // 2
                by = room.y - depth if dy < 0 else room.y + room.h
                bump = Room(bx, by, span, depth)
            if not (1 <= bump.x and bump.x + bump.w <= GRID - 1 and
                    1 <= bump.y and bump.y + bump.h <= GRID - 1):
                continue
            # The normal rock buffer keeps a niche from quietly joining a
            # neighboring room or another room's niche into a shortcut.
            if any(other != room and _overlaps(bump, other, pad=2)
                   for other in established):
                continue
            for y in range(bump.y, bump.y + bump.h):
                for x in range(bump.x, bump.x + bump.w):
                    _set(tiles, x, y, FLOOR)
            established.append(bump)
            break


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
                      avoid: set[tuple[int, int]] | None = None) -> list[tuple[int, int]]:
    """Carve the shortest rock-backed route between two clean thresholds."""
    avoid = set() if avoid is None else avoid

    def portals(room: Room) -> list[tuple[tuple[int, int], tuple[int, int],
                                           tuple[int, int]]]:
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
                result.append((outer, beyond, inner))
        return result

    pairs = [(pa, pb) for pa in portals(a) for pb in portals(b)]
    rng.shuffle(pairs)
    pairs.sort(key=lambda pair: abs(pair[0][0][0] - pair[1][0][0])
               + abs(pair[0][0][1] - pair[1][0][1]))
    directions = [(1, 0), (-1, 0), (0, 1), (0, -1)]
    rng.shuffle(directions)
    def find_route(start: tuple[int, int], goal: tuple[int, int]) -> list[tuple[int, int]] | None:
        previous: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
        queue = deque([start])
        while queue and goal not in previous:
            x, y = queue.popleft()
            for dx, dy in directions:
                nxt = x + dx, y + dy
                if nxt in previous or not (2 <= nxt[0] < GRID - 2 and 2 <= nxt[1] < GRID - 2):
                    continue
                if _at(tiles, *nxt) != WALL:
                    continue
                # A one-rock buffer stops unrelated routes and rooms from
                # silently fusing before their planned door can separate them.
                if (nxt != goal
                        and any(_is_floor(_at(tiles, nxt[0] + sx, nxt[1] + sy))
                                or _at(tiles, nxt[0] + sx, nxt[1] + sy) in DOORS
                                for sx, sy in directions)):
                    continue
                previous[nxt] = (x, y); queue.append(nxt)
        if goal not in previous:
            return None
        route = []
        cell: tuple[int, int] | None = goal
        while cell is not None:
            route.append(cell); cell = previous[cell]
        route.reverse()
        return route

    # Cheap clean thresholds are common; exhaust them before relaxing the
    # rock buffer around a crowded hub.
    for (outer_a, start, _), (outer_b, goal, _) in pairs:
        route = find_route(start, goal)
        if route is None:
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
            if _at(tiles, x, y) != WALL:
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
                if (nxt in previous or not (2 <= nxt[0] < GRID - 2
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
            if not nxt_open and _at(tiles, *nxt) != WALL:
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


def _inside_room(rooms: list[Room], x: int, y: int) -> bool:
    return any(room.x <= x < room.x + room.w and room.y <= y < room.y + room.h
               for room in rooms)


def _adjacent_to_room(rooms: list[Room], x: int, y: int) -> bool:
    return any(_inside_room(rooms, nx, ny)
               for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))


def _widen_corridors(tiles: list[int], rooms: list[Room], paths: list[list[tuple[int, int]]],
                     rng: random.Random, widen_chance: float = 0.8) -> None:
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
            if any(_at(tiles, x + dx, y + dy) in DOORS
                   for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
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
            if any(_at(tiles, wx + dx, wy + dy) in DOORS
                   for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                continue
            if _at(tiles, wx, wy) == WALL:
                _set(tiles, wx, wy, FLOOR)


def _place_bonus_rewards(tiles: list[int], things: list[int], rooms: list[Room],
                         reserved: set[tuple[int, int]], rng: random.Random,
                         complexity: int) -> None:
    """A handful of extra treasure/ammo/food pickups on floor that already
    exists, instead of carving a dead-end spur to hold every one of them.
    A hallway that only exists because it has a pickup at the end reads as
    artificial -- no human level author dug a corridor just to hold a candy
    bar. Existing room floor is plentiful this early in the pipeline (before
    population and decorations claim it), so there's no need to add rock.
    """
    target = max(1, complexity)
    candidates = [(x, y) for room in rooms
                  for y in range(room.y + 1, room.y + room.h - 1)
                  for x in range(room.x + 1, room.x + room.w - 1)
                  if (x, y) not in reserved and _at(things, x, y) == 0
                  and _is_floor(_at(tiles, x, y))]
    rng.shuffle(candidates)
    for cell in candidates[:target]:
        _set(things, *cell, rng.choice(TREASURE + (AMMO, FOOD)))
        reserved.add(cell)


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
                 exit_stand: tuple[int, int], roles: list[str],
                 reserved: set[tuple[int, int]],
                 allow_locks: bool = True) -> int:
    candidates = [candidate for path in paths
                  if (candidate := _door_candidate(tiles, rooms, path))
                  and candidate[:2] not in reserved]
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
            key = _key_spot(tiles, rooms, roles, trial, start) if gated else None
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


def _key_spot(tiles: list[int], rooms: list[Room], roles: list[str],
              locked: tuple[tuple[int, int, int], ...],
              start: tuple[int, int]) -> tuple[int, int] | None:
    """Farthest reachable room center whose door-bounded region touches no
    locked door: finding the key beside the very door it opens is a
    non-puzzle, so such rooms never host it."""
    pre_lock = _reachable(tiles, start, locked_open=False)
    lock_sides = {(x + dx, y + dy) for x, y, _ in locked
                  for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))}
    candidates = [(room.center, role) for room, role in zip(rooms, roles)
                  if room.center in pre_lock and room.center != start]
    candidates.sort(key=lambda item: (item[1] == "branch",
                                      abs(item[0][0] - start[0]) + abs(item[0][1] - start[1])),
                    reverse=True)
    for center, _ in candidates:
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
            and set(level.secret_variants) == {"closet"}):
        flags.append("secret_monotony")
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
                        rng: random.Random, number: int
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
    if number == 10 and rng.random() < 0.25:
        chosen = [FLOOR_TEN_STONE_THEME] + rng.sample(
            deduped, k=len(distinct_districts) - 1)
    else:
        chosen = rng.sample(deduped, k=len(distinct_districts))
    rng.shuffle(chosen)
    theme_by_district = dict(zip(distinct_districts, chosen))
    group_theme = {group: theme_by_district[assigned[group]] for group in groups}
    return component_of, group_theme


def _apply_wall_theme(tiles: list[int], things: list[int], rooms: list[Room],
                      districts: list[int], component_of: dict[tuple[int, int], int],
                      group_theme: dict[int, tuple[int, tuple[int, ...]]],
                      rng: random.Random) -> None:
    """Apply native WL6 materials without changing traversable geometry."""
    for index, tile in enumerate(tiles):
        if tile != WALL:
            continue
        x, y = index % GRID, index // GRID
        group = next((component_of[cell] for cell in ((x + 1, y), (x - 1, y),
                                                       (x, y + 1), (x, y - 1))
                      if cell in component_of), None)
        if group is not None:
            tiles[index] = group_theme[group][0]
    for room, district in zip(rooms, districts):
        base, accents = group_theme[component_of[room.center]]
        if not accents:
            continue
        accent = accents[district % len(accents)]
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
                landmark = rng.choices((5, 7), weights=(9, 1))[0] if accent == 7 else accent
                _set(tiles, x, y, landmark)
                # Plain bars sometimes get the loose remains that distinguish a
                # neglected cellblock without turning the wall texture itself
                # into a room-wide skeleton set piece.
                if landmark == 5 and rng.random() < 0.3:
                    interior = [(nx, ny) for nx, ny in ((x - 1, y), (x + 1, y),
                                                         (x, y - 1), (x, y + 1))
                                if room.x <= nx < room.x + room.w
                                and room.y <= ny < room.y + room.h
                                and _is_floor(_at(tiles, nx, ny))
                                and _at(things, nx, ny) == 0]
                    if interior:
                        _set(things, *rng.choice(interior), rng.choice((42, 64, 65, 66)))
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


def _hint_secrets(tiles: list[int], things: list[int],
                  component_of: dict[tuple[int, int], int],
                  group_theme: dict[int, tuple[int, tuple[int, ...]]],
                  rng: random.Random) -> None:
    """Hang a landmark decor tile (banner, portrait, insignia) on every
    pushwall, the way the original episodes telegraph most of theirs. Runs
    after _apply_wall_theme so the theme can't repaint the hint, and prefers
    the floor theme's own decor accents so the hint matches the material.
    Falls back to a same-base sibling theme's accents rather than a hardcoded
    cross-family constant, so a hint tile can never mix material families."""
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
        if hints:
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
        for depth in (1, 2, 3):
            for side in (-1, 1):
                _set(tiles, wx + dx * depth, wy + side, ELEVATOR_TILE)
        _set(tiles, wx + dx * 3, wy, ELEVATOR_TILE)
        _set(tiles, wx, wy, DOOR_GOLD_EW if locked else DOOR_ELEVATOR)
        return wx + dx * 2, wy
    raise ValueError("terminal room has no clear east/west wall for an elevator")


def _pick_secret_variant(rng: random.Random, used: list[str]) -> str:
    variants = [("closet", 0.40), ("vault", 0.25),
                ("nested", 0.20), ("shortcut", 0.15)]
    available = [(name, weight) for name, weight in variants
                 if not (name in ("nested", "shortcut") and name in used)]
    return rng.choices([name for name, _ in available],
                       weights=[weight for _, weight in available], k=1)[0]


def _secret_reward(rng: random.Random, depth: float,
                   premium: bool = False, lesser: bool = False) -> int:
    if lesser:
        return rng.choices((AMMO, TREASURE[0], TREASURE[1]),
                           weights=(4.0 - 2.5 * depth, 2.0, 1.5 + depth), k=1)[0]
    if premium:
        choices = (TREASURE[2], TREASURE[3], MACHINE_GUN, CHAINGUN, ONE_UP)
        weights = (2.0, 1.5 + depth, 1.0 + depth, 0.4 + 2.5 * depth,
                   0.05 + 0.8 * depth * depth)
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
                  secret_exit: bool = False
                  ) -> tuple[tuple[int, int], str, tuple[int, int]] | None:
    px = room.x + room.w
    if px + 4 >= GRID - 1:
        return None
    # Sweep rows outward from the wall's midline so one crossing corridor
    # doesn't doom the whole room's secret.
    mid = room.y + room.h // 2
    rows = sorted(range(room.y + 1, room.y + room.h - 1), key=lambda y: abs(y - mid))
    for py in rows:
        reward = _carve_secret_pocket(tiles, things, px, py, rng, secret_exit,
                                      variant, depth)
        if reward:
            return reward, variant, (px, py)
    return None


def _carve_secret_pocket(tiles: list[int], things: list[int], px: int, py: int,
                         rng: random.Random, secret_exit: bool,
                         variant: str = "closet", depth: float = 0.5) -> tuple[int, int] | None:
    # Corridors and elevator bays may already cross this nominal room wall.
    # Never turn their floor back into a pushwall.
    if _at(tiles, px, py) != WALL or not _is_floor(_at(tiles, px - 1, py)):
        return None
    if secret_exit and variant == "shortcut":
        return None

    def chamber(length: int) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
        cells = [(x, y) for x in range(px + 1, px + length + 1)
                 for y in range(py - 1, py + 2)]
        margin = [(px + length + 1, y) for y in range(py - 1, py + 2)]
        margin += [(x, py - 2) for x in range(px + 1, px + length + 1)]
        margin += [(x, py + 2) for x in range(px + 1, px + length + 1)]
        margin += [(px, py - 1), (px, py + 1)]
        return cells, margin

    if variant == "shortcut":
        layout = None
        lengths = list(range(4, 9))
        rng.shuffle(lengths)
        for length in lengths:
            cells = ([(x, y) for x in range(px + 1, px + 4)
                      for y in range(py - 1, py + 2)]
                     + [(x, py) for x in range(px + 4, px + length + 1)])
            margin = [(x, py + side) for x in range(px + 4, px + length + 1)
                      for side in (-1, 1)]
            margin += [(x, py - 2) for x in range(px + 1, px + 4)]
            margin += [(x, py + 2) for x in range(px + 1, px + 4)]
            margin += [(px, py - 1), (px, py + 1)]
            target = px + length + 1, py
            if (_is_floor(_at(tiles, *target)) and _at(things, *target) == 0
                    and all(_at(tiles, x, y) == WALL for x, y in cells + margin)):
                layout = cells, target, length
                break
        if layout is None:
            return None
        cells, _, length = layout
        for cell in cells:
            _set(tiles, *cell, FLOOR)
        reward = (px + length, py)
        _set(things, *reward, _secret_reward(rng, depth))
    elif variant == "nested":
        cells, margin = chamber(7)
        # The wall between the two chambers must stay solid on every row
        # but the pushwall's own cell. Only removing the center cell here
        # left the flanking rows (py-1/py+1) as already-carved open floor,
        # so the second wall was just a bypassable prop: a player could
        # walk straight around it, and pushing it revealed nothing new.
        for wall_y in (py - 1, py, py + 1):
            cells.remove((px + 4, wall_y))
        margin += [(px + 4, py - 1), (px + 4, py + 1)]
        if any(_at(tiles, x, y) != WALL for x, y in cells + margin):
            return None
        # The east face (back) of the elevator switch must be covered by solid
        # wall; the margin only checks up to px+8, not one step beyond.
        if secret_exit and _at(tiles, px + 9, py) != WALL:
            return None
        for cell in cells:
            _set(tiles, *cell, FLOOR)
        _set(things, px + 4, py, PUSHWALL)
        lesser = (px + 2, py + rng.choice((-1, 1)))
        reward = (px + 7, py + rng.choice((-1, 1)))
        _set(things, *lesser, _secret_reward(rng, depth, lesser=True))
        _set(things, *reward, _secret_reward(rng, depth, premium=True))
        if secret_exit:
            # Frame the switch with ELEVATOR_TILE on all non-approach faces so
            # later passes (reward stubs, sightline breakers) cannot carve
            # through the surrounding wall and expose the switch from the side.
            _set(tiles, px + 8, py - 1, ELEVATOR_TILE)
            _set(tiles, px + 8, py, ELEVATOR_TILE)
            _set(tiles, px + 8, py + 1, ELEVATOR_TILE)
            _set(tiles, px + 7, py, SECRET_EXIT_ZONE)
    else:
        length = rng.randrange(4, 6) if variant == "vault" else 3
        cells, margin = chamber(length)
        if any(_at(tiles, x, y) != WALL for x, y in cells + margin):
            return None
        # The east face (back) of the elevator switch must be covered by solid
        # wall; the margin only checks up to px+length+1, not one step beyond.
        if secret_exit and _at(tiles, px + length + 2, py) != WALL:
            return None
        for cell in cells:
            _set(tiles, *cell, FLOOR)
        if variant == "vault":
            guard = (px + length - (1 if secret_exit else 0), py)
            if abs(guard[0] - px) < 3:
                guard = (px + length, py)
            _set(things, *guard, GUARDS[3])  # west-facing: pocket opens eastward
            spots = [(px + length, py - 1), (px + length, py + 1),
                     (px + length - 1, py + rng.choice((-1, 1)))]
            count = rng.randrange(2, 4)
            for spot in spots[:count]:
                _set(things, *spot, _secret_reward(rng, depth, premium=depth > 0.65))
            reward = spots[0]
        else:
            # The pushed wall settles on the center track; the reward stays
            # visible and reachable one cell to the side of that backstop.
            reward = (px + 2, py + rng.choice((-1, 1)))
            _set(things, *reward, _secret_reward(rng, depth))
        if secret_exit:
            # Native secret exits are an elevator switch with floor code 107
            # in front; the translator rewrites it into Exit_Secret. Frame the
            # switch with ELEVATOR_TILE on all non-approach faces so later
            # passes cannot carve through and expose a side or back face.
            _set(tiles, px + length + 1, py - 1, ELEVATOR_TILE)
            _set(tiles, px + length + 1, py, ELEVATOR_TILE)
            _set(tiles, px + length + 1, py + 1, ELEVATOR_TILE)
            _set(tiles, px + length, py, SECRET_EXIT_ZONE)
    _set(tiles, px, py, WALL)
    _set(things, px, py, PUSHWALL)
    return reward


def _floor_distances(tiles: list[int], start: tuple[int, int]) -> dict[tuple[int, int], int]:
    distances = {start: 0}
    queue = deque([start])
    while queue:
        x, y = queue.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nxt = x + dx, y + dy
            if nxt in distances:
                continue
            tile = _at(tiles, *nxt)
            if _is_floor(tile) or tile in DOORS:
                distances[nxt] = distances[(x, y)] + 1
                queue.append(nxt)
    return distances


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


def _place_population(config: CampaignConfig, number: int, rooms: list[Room],
                      tiles: list[int], things: list[int], reserved: set[tuple[int, int]],
                      rng: random.Random, start: tuple[int, int],
                      exit_room: Room) -> tuple[int, int, int]:
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

    def pick_family(depth: float) -> tuple[str, tuple[int, ...]]:
        # Guards stay the common baseline; officers/SS grow more common as
        # the campaign progresses, then concentrate where the floor peaks.
        elite_scale = 0.45 if depth < 0.2 else (1.35 if 0.6 <= depth <= 0.85 else 1.0)
        weights = [weight * (1 + progression) * elite_scale
                   if name in ("officer", "ss") else weight
                   for name, weight in zip(names, base_weights)]
        index = rng.choices(range(len(families)), weights=weights, k=1)[0]
        return names[index], families[index]

    facings = ((0, -1), (1, 0), (0, 1), (-1, 0))

    def place_enemy(x: int, y: int, depth: float, tier: int,
                    room: Room | None = None, patrol_facing: int | None = None) -> None:
        name, family = pick_family(depth)
        if name in ("officer", "ss") and near_door(x, y):
            name, family = "guard", GUARDS
        if patrol_facing is not None:
            facing = patrol_facing
            family = PATROLS_BY_FAMILY[family]
        elif room is not None:
            facing = _pick_stationary_facing(x, y, room)
        elif open_facings := [i for i in range(4)
                              if _is_floor(_at(tiles, x + facings[i][0],
                                               y + facings[i][1]))]:
            facing = rng.choice(open_facings)
        else:
            facing = rng.randrange(4)
        _set(things, x, y, family[facing] + 36 * tier)
        # Decoration placement runs after population and only checks that a
        # cell is empty, not who's facing it; reserve the tile directly ahead
        # so a later pillar/barrel/table can't get dropped in a stationary
        # actor's face.
        dx, dy = facings[facing]
        reserved.add((x + dx, y + dy))

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

    def patrol_loop(room: Room) -> tuple[list[tuple[int, int]], dict[tuple[int, int], int]] | None:
        """Return a clockwise, two-tile-inset loop and its outgoing turn directions.

        PatrolPoint changes an actor's direction only when it reaches the
        marker tile, so the four corners carry the direction of their
        outgoing leg.  Reserving the full loop below also keeps other actors
        and later decoration from turning a valid route into a blockage.
        """
        if room.w < 7 or room.h < 7:
            return None
        left, right = room.x + 2, room.x + room.w - 3
        top, bottom = room.y + 2, room.y + room.h - 3
        path = ([(x, top) for x in range(left, right + 1)]
                + [(right, y) for y in range(top + 1, bottom + 1)]
                + [(x, bottom) for x in range(right - 1, left - 1, -1)]
                + [(left, y) for y in range(bottom - 1, top, -1)])
        corners = {(left, top), (right, top), (right, bottom), (left, bottom)}
        directions = {cell: next(i for i, delta in enumerate(facings)
                                 if (cell[0] + delta[0], cell[1] + delta[1])
                                 == path[(index + 1) % len(path)])
                      for index, cell in enumerate(path) if cell in corners}
        return path, directions

    tier_counts = [0, 0, 0]
    for ridx, room in enumerate(rooms[1:], 1):
        depth = depth_of(room)
        budget = max(0, round(per_room * (0.4 if room == exit_room else pacing(depth))))
        patrol = patrol_loop(room)
        patrol_count = 0
        if (budget and patrol is not None and rng.random() < 0.35):
            path, turns = patrol
            path_cells = set(path)
            if (all(_is_floor(_at(tiles, x, y)) and _at(things, x, y) == 0
                    and (x, y) not in reserved for x, y in path)
                    and len(path_cells) > len(turns)):
                for cell, direction in turns.items():
                    _set(things, *cell, PATROL_POINT_CODES[direction])
                reserved.update(path_cells)
                spawn = rng.choice([cell for cell in path if cell not in turns])
                spawn_index = path.index(spawn)
                successor = path[(spawn_index + 1) % len(path)]
                facing = next(i for i, delta in enumerate(facings)
                              if (spawn[0] + delta[0], spawn[1] + delta[1]) == successor)
                place_enemy(*spawn, depth, 0, room, patrol_facing=facing)
                tier_counts[0] += 1
                patrol_count = 1
        candidates = [(x, y) for y in range(room.y + 2, room.y + room.h - 1)
                      for x in range(room.x + 2, room.x + room.w - 1)
                      if (x, y) not in reserved and _at(things, x, y) == 0
                      and _is_floor(_at(tiles, x, y))]
        rng.shuffle(candidates)
        cursor = 0
        for x, y in candidates[cursor:cursor + budget - patrol_count]:
            place_enemy(x, y, depth, 0, room)
            tier_counts[0] += 1
        cursor += budget - patrol_count
        # ECWolf's base translator treats +36 as the next cumulative skill
        # tier: skill 2 actors join the easy population on medium, and skill 3
        # actors join both on hard. They require their own cells in plane 2.
        extra = max(0, round(budget * (0.20 + progression * 0.12)))
        for tier in (1, 2):
            for x, y in candidates[cursor:cursor + extra]:
                place_enemy(x, y, depth, tier, room)
                tier_counts[tier] += 1
            cursor += extra
        if candidates[cursor:]:
            x, y = candidates[cursor]
            if ridx % max(2, 7 - int(config.supplies)) == 0:
                _set(things, x, y, rng.choice((AMMO, FOOD, FIRST_AID)))
            elif ridx % max(2, 7 - int(config.treasure)) == 1:
                _set(things, x, y, rng.choice(TREASURE))
    recovery_rooms = sorted((room for room in rooms if 0.85 < depth_of(room) < 1.0
                             and room != exit_room), key=depth_of)
    if recovery_rooms:
        room = recovery_rooms[0]
        nearby = _floor_distances(tiles, room.center)
        if not any(thing in (AMMO, FIRST_AID) and nearby.get((index % GRID, index // GRID), 13) <= 12
                   for index, thing in enumerate(things)):
            candidates = [(x, y) for y in range(room.y + 1, room.y + room.h - 1)
                          for x in range(room.x + 1, room.x + room.w - 1)
                          if (x, y) not in reserved and _at(things, x, y) == 0
                          and _is_floor(_at(tiles, x, y))]
            if candidates:
                _set(things, *rng.choice(candidates), rng.choice((AMMO, FIRST_AID)))
    novelty = FAKE_HITLER if number == 9 else rng.choice(GHOSTS) if number == 10 else None
    if novelty is not None and rng.random() < 0.1:
        candidates = [(x, y) for room in rooms for y in range(room.y + 1, room.y + room.h - 1)
                      for x in range(room.x + 1, room.x + room.w - 1)
                      if (x, y) not in reserved and _at(things, x, y) == 0
                      and _is_floor(_at(tiles, x, y))
                      and abs(x - start[0]) + abs(y - start[1]) >= 6]
        if candidates:
            cell = rng.choice(candidates)
            _set(things, *cell, novelty)
            reserved.add(cell)
    _guarantee_supplies(config, rooms, tiles, things, reserved, rng)
    return tuple(tier_counts)


def _guarantee_supplies(config: CampaignConfig, rooms: list[Room], tiles: list[int],
                        things: list[int], reserved: set[tuple[int, int]],
                        rng: random.Random) -> None:
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
                 if (x, y) not in reserved and _at(things, x, y) == 0
                 and _is_floor(_at(tiles, x, y))]
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
                       rng: random.Random,
                       roles: list[str] | None = None,
                       specs: list | None = None) -> None:
    """Place purposeful, themed furniture in rooms following community-map patterns.

    Blocking statics go in deliberate arrangements (pillar pairs, corner
    clusters, entry flanking, or occasional partial dividers) chosen to match
    the room's role and tier.  Reachability is checked before any blocking item
    is committed, so furniture can never wall the player out of any area.
    Open (non-solid) items are placed loosely but still theme-appropriate.
    """
    baseline = len(_reachable(tiles, start, locked_open=True))
    blocked_cells: set[tuple[int, int]] = set()
    _roles = roles or ["beat"] * len(rooms)
    _tiers = [s.tier for s in specs] if specs else ["standard"] * len(rooms)

    for ridx, room in enumerate(rooms):
        role = _roles[ridx] if ridx < len(_roles) else "beat"
        tier = _tiers[ridx] if ridx < len(_tiers) else "standard"
        theme = _decor_theme(role, tier)

        if room.w < 5 or room.h < 5:
            continue

        blocking = _DECOR_BLOCKING.get(theme, STATIC_BLOCKING)
        open_items = _DECOR_OPEN.get(theme, STATIC_OPEN)

        cx, cy = room.center
        interior = {(x, y) for x in range(room.x + 1, room.x + room.w - 1)
                    for y in range(room.y + 1, room.y + room.h - 1)
                    if _is_floor(_at(tiles, x, y))}
        free: set[tuple[int, int]] = {cell for cell in interior - reserved
                                      if _at(things, *cell) == 0}

        def _near_wall(x: int, y: int) -> bool:
            return (x <= room.x + 2 or x >= room.x + room.w - 3
                    or y <= room.y + 2 or y >= room.y + room.h - 3)

        def _try_place(cells: list[tuple[int, int]], item: int) -> bool:
            """Commit a blocking group if all cells are free and reachability holds."""
            if not all(c in free for c in cells):
                return False
            candidate = blocked_cells | set(cells)
            if len(_reachable(tiles, start, locked_open=True, blocked=candidate)) < baseline - len(candidate):
                return False
            for c in cells:
                _set(things, *c, item)
                reserved.add(c)
                blocked_cells.add(c)
                free.discard(c)
            return True

        pair_budget = 2 if room.w >= 8 and room.h >= 8 else 1
        pairs_placed = 0

        # --- Pattern: partial divider (community-map technique, rare) ---
        # A row of pillars or plants that visually subdivides a large room
        # while a 2-tile gap keeps it fully traversable.  Appears in ~8% of
        # eligible rooms -- enough to read as intentional, not as clutter.
        if (room.w >= 10 and room.h >= 10
                and theme in ("grand", "barracks", "guardpost")
                and rng.random() < 0.08):
            div_item = 30 if theme == "grand" else (31 if theme == "guardpost" else 25)
            if room.w >= room.h:
                span = list(range(room.y + 2, room.y + room.h - 2))
                if len(span) >= 4:
                    gap = rng.randrange(1, len(span) - 2)
                    cells = [(cx, span[i]) for i in range(len(span))
                             if not (gap <= i <= gap + 1) and (cx, span[i]) in free]
                    if len(cells) >= 2 and _try_place(cells, div_item):
                        pairs_placed = pair_budget
            else:
                span = list(range(room.x + 2, room.x + room.w - 2))
                if len(span) >= 4:
                    gap = rng.randrange(1, len(span) - 2)
                    cells = [(span[i], cy) for i in range(len(span))
                             if not (gap <= i <= gap + 1) and (span[i], cy) in free]
                    if len(cells) >= 2 and _try_place(cells, div_item):
                        pairs_placed = pair_budget

        # --- Pattern: corner barrel cluster (storage rooms) ---
        if pairs_placed < pair_budget and theme == "storage":
            item = rng.choice(blocking)
            corners = [
                (room.x + 1, room.y + 1),
                (room.x + room.w - 2, room.y + 1),
                (room.x + 1, room.y + room.h - 2),
                (room.x + room.w - 2, room.y + room.h - 2),
            ]
            rng.shuffle(corners)
            for cornx, corny in corners:
                nx = cornx + (1 if cornx < cx else -1)
                ny = corny + (1 if corny < cy else -1)
                cluster = [(c) for c in [(cornx, corny), (nx, corny), (cornx, ny)]
                           if c in free][:2]
                if len(cluster) == 2 and _try_place(cluster, item):
                    pairs_placed += 1
                    break

        # --- Pattern: pillar colonnade (grand / anchor rooms) ---
        if pairs_placed < pair_budget and theme == "grand" and room.w >= 8 and room.h >= 8:
            depth = max(2, min(room.w // 3, room.h // 3))
            for offset in (0, -1, 1):
                if pairs_placed >= pair_budget:
                    break
                a = (room.x + depth, cy + offset)
                b = (room.x + room.w - 1 - depth, cy + offset)
                if _try_place([a, b], 30):   # WhitePillar
                    pairs_placed += 1

        # --- Pattern: symmetric wall pairs (general fallback) ---
        if pairs_placed < pair_budget:
            x_pairs = [((x, y), (2 * cx - x, y)) for x, y in free
                       if x < cx and (2 * cx - x, y) in free]
            y_pairs = [((x, y), (x, 2 * cy - y)) for x, y in free
                       if y < cy and (x, 2 * cy - y) in free]
            all_pairs = x_pairs + y_pairs
            all_pairs.sort(key=lambda p: (0 if _near_wall(*p[0]) or _near_wall(*p[1]) else 1,
                                          rng.random()))
            for (ax, ay), (bx, by) in all_pairs:
                if pairs_placed >= pair_budget:
                    break
                if _try_place([(ax, ay), (bx, by)], rng.choice(blocking)):
                    pairs_placed += 1

        # --- Open (non-solid) items, theme-appropriate ---
        area = room.w * room.h
        open_budget = 3 if area >= 80 else 2 if area >= 45 else 1
        loose = [c for c in free - reserved if _at(things, *c) == 0]
        rng.shuffle(loose)
        for cell in loose[:rng.randrange(0, open_budget + 1)]:
            _set(things, *cell, rng.choice(open_items))
            reserved.add(cell)
            free.discard(cell)


def _place_boss(tiles: list[int], things: list[int], room: Room,
                reserved: set[tuple[int, int]], rng: random.Random) -> int:
    cx, cy = room.center
    positions = [(cx, cy), (cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)]
    bx, by = next(((x, y) for x, y in positions
                   if (x, y) not in reserved and _at(things, x, y) == 0
                   and _is_floor(_at(tiles, x, y))), (cx, cy))
    boss = rng.choice(BOSSES)
    _set(things, bx, by, boss)
    reserved.add((bx, by))
    supplies = ((cx - 2, cy - 2, FIRST_AID), (cx + 2, cy - 2, FIRST_AID),
                (cx - 2, cy + 2, AMMO), (cx + 2, cy + 2, AMMO))
    for x, y, thing in supplies:
        if _at(things, x, y) == 0 and _is_floor(_at(tiles, x, y)):
            _set(things, x, y, thing)
            reserved.add((x, y))
    return boss


def generate_map(config: CampaignConfig, number: int, attempt: int = 0,
                 secret_exit: bool = False) -> GeneratedMap:
    seed = config.floor_seed(number, attempt)
    rng = random.Random(seed)
    tiles = [WALL] * (GRID * GRID)
    things = [0] * (GRID * GRID)
    complexity = int(config.layout_complexity)
    plan = _plan_floor(rng, complexity, number)
    placed = _place_planned_rooms(rng, plan)
    rooms = placed.rooms
    edges = placed.edges
    specs = [plan.specs[index] for index in placed.spec_indices]
    roles = [spec.role for spec in specs]
    districts = [spec.district for spec in specs]
    for room in rooms:
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                _set(tiles, x, y, FLOOR)
    _carve_notches(tiles, rooms, rng)
    _carve_alcoves(tiles, rooms, rng)
    for room in rooms:
        _add_pillars(tiles, room, rng)
    door_zones: set[tuple[int, int]] = set()
    paths = [_carve_connection(tiles, rooms[a], rooms[b], rng, complexity, door_zones)
             for a, b in edges]
    _widen_corridors(tiles, rooms, paths, rng)
    start = rooms[0].center
    _set(things, *start, PLAYER_START)
    is_boss = number == 9
    exit_room = None
    exit_stand = None
    # The planned terminus is legible; crossed walls still need a safe sweep.
    exit_index = roles.index("exit")
    exit_order = [exit_index] + [index for index in _rooms_by_distance(rooms, edges)
                                 if index != exit_index]
    for room_index in exit_order:
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
    secret_variants: list[str] = []
    shortcut_pushwalls: list[tuple[int, int]] = []
    floor_distances = _floor_distances(tiles, start)
    room_distances = {room: floor_distances.get(room.center, 0) for room in rooms}
    max_room_distance = max(room_distances.values(), default=1) or 1
    # Report's secret budget is 2-6 per standard floor; scale directly with
    # the intensity dial instead of undershooting to 1 at the low end.
    target_secrets = max(2, int(config.secrets))
    # A secret pocket must never reuse or seal the terminal room's elevator
    # wall after the elevator has been carved.
    candidates = [room for room in rooms[1:] if room != exit_room]
    rng.shuffle(candidates)
    while len(rewards) < target_secrets and candidates:
        variant = _pick_secret_variant(rng, secret_variants)
        placed_secret = None
        host = None
        for room in candidates:
            placed_secret = _place_secret(tiles, things, room, rng, variant,
                                          room_distances[room] / max_room_distance,
                                          secret_exit and not rewards)
            if placed_secret:
                host = room
                break
        # A slot whose larger footprint fits nowhere still gets the proven
        # baseline experience rather than silently shrinking the budget.
        if placed_secret is None and variant != "closet":
            for room in candidates:
                placed_secret = _place_secret(tiles, things, room, rng, "closet",
                                              room_distances[room] / max_room_distance,
                                              secret_exit and not rewards)
                if placed_secret:
                    host = room
                    break
        if placed_secret:
            reward, realized_variant, push_cell = placed_secret
            rewards.append(reward); secret_variants.append(realized_variant)
            reserved.add(reward)
            if realized_variant == "shortcut":
                shortcut_pushwalls.append(push_cell)
            candidates.remove(host)
        else:
            break
    # Dense motifs can consume every nominal east wall; a rock-backed hall
    # threshold is a safe last host with the same push direction and margin.
    reachable_walls = _reachable(tiles, start, locked_open=True)
    fallback_walls = [(x, y) for y in range(3, GRID - 3) for x in range(3, GRID - 4)
                      if _at(tiles, x, y) == WALL and (x - 1, y) in reachable_walls]
    rng.shuffle(fallback_walls)
    while len(rewards) < target_secrets:
        variant = _pick_secret_variant(rng, secret_variants)
        reward = None
        for px, py in fallback_walls:
            approach_distance = floor_distances.get((px - 1, py), 0)
            reward = _carve_secret_pocket(
                tiles, things, px, py, rng, secret_exit and not rewards, variant,
                min(1.0, approach_distance / max_room_distance))
            if reward:
                break
        if reward is None and variant != "closet":
            variant = "closet"
            for px, py in fallback_walls:
                approach_distance = floor_distances.get((px - 1, py), 0)
                reward = _carve_secret_pocket(
                    tiles, things, px, py, rng, secret_exit and not rewards, variant,
                    min(1.0, approach_distance / max_room_distance))
                if reward:
                    break
        if reward:
            rewards.append(reward); secret_variants.append(variant); reserved.add(reward)
            if variant == "shortcut":
                shortcut_pushwalls.append((px, py))
        else:
            break
    reserved.update((index % GRID - 1, index // GRID)
                    for index, thing in enumerate(things) if thing == PUSHWALL)
    _place_bonus_rewards(tiles, things, rooms, reserved, rng, complexity)
    locks = _place_doors(config, tiles, things, rooms, paths, rng, start, exit_stand, roles,
                         reserved,
                         allow_locks=not is_boss)
    _break_long_sightlines(tiles, things, rooms, reserved, rng, start)
    _split_oversized_zones(tiles, rooms, rng, reserved)
    if _remove_redundant_plain_doors(tiles):
        # A removed door can extend a floor-only sightline which the earlier
        # pass correctly treated as interrupted; repair only that new case.
        _break_long_sightlines(tiles, things, rooms, reserved, rng, start,
                               allow_doors=False, walls_for_redundant_doors=True)
    if sum(tile in DOORS for tile in tiles) > 56:
        raise ValueError("door budget exceeded")
    if is_boss:
        boss_room = max((room for room in rooms[1:] if room != exit_room), key=lambda room: room.w * room.h)
        boss = _place_boss(tiles, things, boss_room, reserved, rng)
        if boss not in KEY_DROP_BOSSES:
            locked = tuple((index % GRID, index // GRID, tile)
                           for index, tile in enumerate(tiles) if tile == DOOR_GOLD_EW)
            key = _key_spot(tiles, rooms, roles, locked, start)
            if key is None or _at(things, *key) != 0:
                raise ValueError("boss elevator has no free gold-key location")
            _set(things, *key, GOLD_KEY)
            reserved.add(key)
        locks += 1
    enemy_tiers = _place_population(config, number, rooms, tiles, things, reserved, rng,
                                    start, exit_room)
    _ensure_early_heal(tiles, things, rooms, start, reserved, rng)
    _place_decorations(rooms, tiles, things, reserved, start, rng, roles=roles, specs=specs)
    _assign_sound_zones(tiles)
    component_of, group_theme = _assign_area_themes(tiles, rooms, districts, rng, number)
    _apply_wall_theme(tiles, things, rooms, districts, component_of, group_theme, rng)
    _hint_secrets(tiles, things, component_of, group_theme, rng)
    result = GeneratedMap(number=number, tiles=tiles, things=things, start=start,
                          exit_stand=exit_stand, secret_rewards=rewards, seed=seed,
                          has_secret_exit=secret_exit, locked_doors=locks, boss=is_boss,
                          enemy_tiers=enemy_tiers, motifs=plan.motifs,
                          motif_rooms=tuple(spec.motif for spec in specs),
                          secret_variants=tuple(secret_variants),
                          shortcut_pushwalls=tuple(shortcut_pushwalls), rooms=tuple(rooms))
    validate_map(result)
    result.critique = _critique(result)
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
    for x, y in pushwalls:
        if _is_floor(_at(level.tiles, x, y)):
            raise ValueError("pushwall trigger is not on a solid wall")
        if not _is_floor(_at(level.tiles, x - 1, y)):
            raise ValueError("pushwall has no movement clearance")
        if not all(_is_floor(_at(level.tiles, x + step, y)) for step in (1, 2)):
            raise ValueError("pushwall has no two-tile backstop")
        if _at(level.tiles, x, y) not in DECOR_WALLS:
            raise ValueError("pushwall is not hinted by a decor wall tile")
    # Nested secrets become approachable in sequence. Simulate each push at
    # its real two-tile travel distance rather than pretending the inner wall
    # must be reachable while its outer wall is still closed.
    pending = set(pushwalls)
    pushed: set[tuple[int, int]] = set()
    while pending:
        opened = _reachable(level.tiles, level.start, locked_open=True,
                            extra_passable=pushed,
                            blocked={(x + 2, y) for x, y in pushed})
        ready = sorted((wall for wall in pending
                        if (wall[0] - 1, wall[1]) in opened),
                       key=lambda cell: (cell[1], cell[0]))
        if not ready:
            raise ValueError("pushwall cannot be approached")
        wall = ready[0]
        pending.remove(wall); pushed.add(wall)
    # A pushwall guarding nothing is worse than no secret at all: the player
    # either finds a bypass and the "secret" never needed pushing, or pushes
    # it and sees no new floor because everything past it was already open.
    # Check each wall in isolation -- every OTHER wall already pushed (best
    # case for a bypass to exist) but this one still solid -- and confirm
    # the cell right behind it is unreachable any other way. "shortcut"
    # secrets are exempt: connecting to floor that's already reachable from
    # the far side is the whole point of that variant, not a bug.
    for wall in pushwalls:
        if wall in level.shortcut_pushwalls:
            continue
        others = pushed - {wall}
        bypass = _reachable(level.tiles, level.start, locked_open=True,
                            extra_passable=others,
                            blocked={(x + 2, y) for x, y in others})
        if (wall[0] + 1, wall[1]) in bypass:
            raise ValueError("pushwall is bypassable without being pushed")
    opened = _reachable(level.tiles, level.start, locked_open=True,
                        extra_passable=pushed, blocked=rests)
    for reward in level.secret_rewards:
        if _at(level.things, *reward) not in (AMMO,) + TREASURE + (MACHINE_GUN, CHAINGUN, ONE_UP):
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
        boss_drops_key = level.boss and any(thing in KEY_DROP_BOSSES for thing in level.things)
        if not boss_drops_key and GOLD_KEY not in level.things:
            raise ValueError("locked map has no gold key")
        if not any(tile in (92, 93) for tile in level.tiles):
            raise ValueError("locked map has no locked door")
        if not boss_drops_key:
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
    if level.boss and sum(thing in BOSSES for thing in level.things) != 1:
        raise ValueError("boss floor must contain exactly one boss")
    if level.boss:
        boss_index = next(i for i, thing in enumerate(level.things) if thing in BOSSES)
        boss_position = boss_index % GRID, boss_index // GRID
        if boss_position not in _reachable(level.tiles, level.start, locked_open=False):
            raise ValueError("boss is unreachable before the boss elevator lock")
    validate_objects(level)
    validate_patrols(level)


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
        if thing in ENEMY_CODES and thing not in BOSSES:
            distance = abs(x - level.start[0]) + abs(y - level.start[1])
            if distance < 6:
                raise ValueError(f"enemy at {(x, y)} is too close to player start")


def _patrol_actor_direction(code: int) -> int | None:
    """Decode a patrol actor's old-format code into this module's N/E/S/W index."""
    for patrol_family in PATROLS_BY_FAMILY.values():
        for tier in range(3):
            candidate = code - 36 * tier
            if candidate in patrol_family:
                return patrol_family.index(candidate)
    return None


def validate_patrols(level: GeneratedMap, steps: int = 512) -> None:
    """Simulate ECWolf's pathing movement and reject a route that dead-ends.

    In ECWolf, ``TryWalk`` tries only the actor's current direction; a
    PatrolPoint must set a new direction before the next tile would be solid.
    This deliberately models that limited algorithm rather than a helpful
    path finder, making the historical walking-in-place failure reproducible.
    """
    for index, thing in enumerate(level.things):
        direction = _patrol_actor_direction(thing)
        if direction is None:
            continue
        x, y = index % GRID, index // GRID
        origin = (x, y)
        for _ in range(steps):
            dx, dy = ((0, -1), (1, 0), (0, 1), (-1, 0))[direction]
            x, y = x + dx, y + dy
            tile = _at(level.tiles, x, y)
            if not (_is_floor(tile) or tile in DOORS):
                raise ValueError(f"patrol actor at {origin} dead-ends at {(x, y)}")
            occupant = _at(level.things, x, y)
            if occupant and occupant not in PATROL_POINT_DIRECTIONS and (x, y) != origin:
                raise ValueError(f"patrol actor at {origin} is blocked by thing at {(x, y)}")
            if occupant in PATROL_POINT_DIRECTIONS:
                direction = PATROL_POINT_DIRECTIONS[occupant]


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
        'episode "IW01" { name = "InfiniWolf" key = "I" }',
    ]
    for number in range(1, 10):
        # ECWolf only recognizes "EndSequence:<id>" or "EndTitle" as a real
        # end-of-game next-map value (see wl_game.cpp); anything else,
        # including the Doom/ZDoom-only "EndGameC" cast-call keyword this
        # used to say, is treated as a literal (nonexistent) map name and
        # crashes on exit once LevelInfo::Find fails to resolve it.
        nxt = f'IW{number + 1:02d}' if number < 9 else 'EndTitle'
        secret = ' secretnext = "IW10"' if number == secret_from else ''
        ceiling = CEILINGS[(number - 1) % len(CEILINGS)]
        music = MUSIC[(number - 1) % len(MUSIC)]
        par = 90 + number * 30
        lines.append(f'map "IW{number:02d}" "Random Floor {number}" {{ next = "{nxt}"{secret} '
                     f'levelnum = {number} par = {par} defaultceiling = "{ceiling}" music = "{music}" }}')
    lines.append(f'map "IW10" "Secret Floor" {{ next = "IW{secret_from + 1:02d}" levelnum = 10 '
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
        candidates: list[GeneratedMap] = []
        for attempt in range(50):
            try:
                candidate = generate_map(config, number, attempt, number == secret_from)
            except ValueError as error:
                last_error = error
                continue
            candidates.append(candidate)
            if not candidate.critique:
                levels.append(candidate)
                break
            if len(candidates) == 3:
                levels.append(min(candidates, key=lambda level: len(level.critique)))
                break
        else:
            if candidates:
                levels.append(min(candidates, key=lambda level: len(level.critique)))
            else:
                raise RuntimeError(f"floor {number} failed generation: {last_error}")
        if progress:
            progress(number, 10)
    manifest = {
        "generator": "infiniwolf", "version": __version__, "seed": config.seed,
        "settings": json.loads(config.to_json()), "secret_from": secret_from,
        "floors": [{"number": level.number, "seed": level.seed,
                    "secrets": len(level.secret_rewards),
                    "locked_doors": level.locked_doors,
                    "boss": level.boss,
                    "enemy_tiers": level.enemy_tiers,
                    "motifs": level.motifs,
                    "secret_variants": level.secret_variants,
                    "critique": level.critique,
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
            write("infiniwolf-manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
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


def read_manifest(package_path: Path) -> dict[str, object]:
    with zipfile.ZipFile(package_path) as package:
        return json.loads(package.read("infiniwolf-manifest.json"))


def validate_package(package_path: Path) -> dict[str, object]:
    """Reopen and parse a completed temporary package before installation."""
    with zipfile.ZipFile(package_path) as package:
        corrupt = package.testzip()
        if corrupt:
            raise ValueError(f"corrupt package entry: {corrupt}")
        names = set(package.namelist())
        expected_maps = {f"maps/iw{number:02d}.wad" for number in range(1, 11)}
        if not expected_maps.issubset(names):
            raise ValueError("package is missing one or more campaign maps")
        forbidden = (".wl6", ".png", ".wav", ".ogg", ".voc")
        if any(name.lower().endswith(forbidden) for name in names):
            raise ValueError("package contains an asset file instead of map metadata")
        manifest = json.loads(package.read("infiniwolf-manifest.json"))
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
