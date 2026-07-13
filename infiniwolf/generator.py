"""Deterministic WL6 campaign generation and ECWolf package writing."""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
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
from .config import CampaignConfig

GRID = 64
WALL = 1
FLOOR = 108
ZONE_MAX = 143
DOOR_EW, DOOR_NS = 90, 91
DOOR_ELEVATOR = 100  # unlocked elevator door on an east/west axis
DOOR_GOLD_EW = 92
DOOR_GOLD_NS = 93
DOOR_SILVER_EW = 94
DOOR_SILVER_NS = 95
GOLD_DOORS = frozenset({DOOR_GOLD_EW, DOOR_GOLD_NS})
SILVER_DOORS = frozenset({DOOR_SILVER_EW, DOOR_SILVER_NS})
LOCKED_DOORS = GOLD_DOORS | SILVER_DOORS
DOORS = {DOOR_EW, DOOR_NS, *LOCKED_DOORS, DOOR_ELEVATOR, 101}
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
SILVER_KEY = 44
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
DOG_FOOD = 29
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
JAIL_CANDIDATE_PROBABILITY = 0.35
# BSTONEB (9) is mottled blue-stone masonry, distinct from the BLUWALL panel.
# Keep it out of the normal pool: floor 10 occasionally uses it as a rare
# material, while floors 1--9 never do. It needs its own DECOR_WALLS accent
# (the same BSTSIGN landmark the blue-stone theme uses) so a pushwall on this
# material can still be hinted -- validate_map requires every pushwall be
# hinted by a decor wall tile, and a theme with no accent at all has nothing
# in-family to hint with otherwise (see _hint_secrets).
FLOOR_TEN_STONE_THEME = (9, (41,))
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
    28,  # HangedMan
    30,  # WhitePillar
    31,  # GreenPlant (closest WL6 has to a tree)
    33,  # Sink
    34,  # BrownPlant
    35,  # Vase
    36,  # BareTable
    39,  # SuitOfArmor
    40,  # HangingCage
    41,  # SkeletonCage
    45,  # BunkBed
    58,  # Barrel
    59,  # Well
    60,  # EmptyWell
    62,  # Flag
    68,  # Stove
    69,  # Spears
)
STATIC_OPEN = (
    23,  # Puddle
    27,  # Chandelier
    32,  # SkeletonFlat
    37,  # CeilingLight
    38,  # KitchenStuff
    42,  # Bones1
    46,  # Basket
    57,  # Gibs
    61,  # Blood
    64, 65, 66,  # Bones2-4
    67,  # Pots
    70,  # Vines
)

# Decoration themes keyed by room role+tier, derived from community-map
# placement patterns: guard rooms get lamps and vases, storage closets get
# barrel clusters, grand anchor rooms get pillar pairs, barracks get tables.
_DECOR_BLOCKING: dict[str, tuple[int, ...]] = {
    "guardpost": (26, 35, 31, 62),       # FloorLamp, Vase, GreenPlant, Flag
    "armory":    (39, 62, 69, 58),       # Armor, Flag, Spears, Barrel
    "checkpoint": (26, 62, 35),          # Lamp, Flag, Vase
    "grand":     (30, 26, 35, 39),       # WhitePillar, FloorLamp, Vase, SuitOfArmor
    "war-room":  (39, 62, 30),           # Armor, Flag, WhitePillar
    "trophy-hall": (39, 62, 34),         # Armor, Flag, BrownPlant
    "courtyard": (30, 31, 34, 59),       # Pillar, Plants, Well
    "barracks":  (25, 36, 58, 45),       # TableWithChairs, BareTable, Barrel, BunkBed
    "ready-room": (45, 36, 58),          # BunkBed, BareTable, Barrel
    "training-room": (69, 36, 58),       # Spears, BareTable, Barrel
    "crypt":     (30, 40, 58),           # Pillar, HangingCage, Barrel
    "ossuary":   (30, 40, 41),           # Pillar, Cage, SkeletonCage
    "burial-chamber": (30, 35, 40),       # Pillar, Vase, HangingCage
    "storage":   (58, 24, 59, 60),       # Barrel, GreenBarrel, Well, EmptyWell
    "supply-cache": (58, 24, 60),         # Barrels, EmptyWell
    "workshop":  (36, 58, 69),           # Worktable, Barrel, Spears
    "lounge":    (25, 35, 34),           # TableWithChairs, Vase, BrownPlant
    "gallery":   (39, 62, 34),           # Armor, Flag, BrownPlant
    "dining-hall": (25, 36, 35),         # Tables and Vase
    "officers-quarters": (45, 25, 34),   # BunkBed, Table, BrownPlant
    "mess-kitchen": (36, 35),             # Appliances are placed explicitly
    "corridor":  (26,),                  # FloorLamp only
    "jail":      (58, 40, 41),           # Barrel, HangingCage, SkeletonCage
    "holding-cell": (40, 58, 36),         # Cage, Barrel, BareTable
    "interrogation-room": (36, 25, 26),  # Tables, FloorLamp
}
_DECOR_OPEN: dict[str, tuple[int, ...]] = {
    "guardpost": (37, 27),   # CeilingLight, Chandelier
    "armory":    (37, 46),   # CeilingLight, Basket
    "checkpoint": (37,),     # CeilingLight
    "grand":     (27, 37),   # Chandelier dominant
    "war-room":  (37,),      # CeilingLight
    "trophy-hall": (27, 37), # Chandelier, CeilingLight
    "courtyard": (37, 70),   # CeilingLight, Vines
    "barracks":  (46, 61),   # Basket, Blood (battle-worn)
    "ready-room": (46,),     # Basket
    "training-room": (37, 46),
    "crypt":     (42, 64, 65, 66),
    "ossuary":   (32, 42, 64, 65, 66),
    "burial-chamber": (27, 42, 64, 65, 66),
    "storage":   (46, 23),   # Basket, rare damp patch
    "supply-cache": (46,),   # Basket
    "workshop":  (46, 37),   # Basket, CeilingLight
    "lounge":    (27,),       # Chandelier
    "gallery":   (27, 37),   # Chandelier, CeilingLight
    "dining-hall": (27,),    # Chandelier
    "officers-quarters": (27, 37),
    "mess-kitchen": (37,),   # Loose kitchen props are placed explicitly
    "corridor":  (37,),      # CeilingLight
    "jail":      (61, 61, 42, 64, 65, 66),  # Blood, then bone variants
    "holding-cell": (42, 64, 65, 66),
    "interrogation-room": (37, 61),
}

# Purpose-built rooms split their furniture concepts across opposite halves.
# Storage and corridors remain deliberately single-purpose and use the
# scattered placement path below.
_DECOR_ZONES: dict[str, tuple[tuple[tuple[int, ...], tuple[int, ...]],
                              tuple[tuple[int, ...], tuple[int, ...]]]] = {
    # (zone A blocking, open), (zone B blocking, open)
    "barracks":  (((25, 36), (46,)),      ((58,), (61,))),
    "guardpost": (((26, 35), (37,)),      ((31,), (27,))),
    "grand":     (((26, 35), (27,)),      ((30,), (37,))),
    "lounge":    (((25,), (27,)),         ((35, 34), (46,))),
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


# A floor's "base variant": one named bundle of the parameters that used to
# be hard-coded module constants, so consecutive floors read as different
# places (a cramped catacomb, a stately hall) instead of re-rolls of one
# recipe. Every default equals the previous constant, so a default-valued
# variant reproduces the pre-variant generator's behavior knob-for-knob.
@dataclass(frozen=True, slots=True)
class FloorVariant:
    name: str
    notch_chance: float = 0.35        # _carve_notches
    alcove_chance: float = 0.30       # _carve_alcoves
    pillar_chance: float = 0.12       # rare structural _add_pillars landmark
    widen_chance: float = 0.80        # _widen_corridors
    hall_chance: float = 0.25         # _plan_floor spine-beat tier roll
    closet_weight: float = 0.45       # _plan_floor filler closet-vs-branch
    extra_motif_chance: float = 0.35  # _plan_floor motif budget roll
    motif_pref: tuple[str, ...] = ()  # motifs promoted ahead of the shuffle
    # Allowed WALL_THEMES base tiles; () = all. Must keep at least as many
    # bases as the floor has districts (up to 3) or the pool is ignored.
    theme_pool: tuple[int, ...] = ()
    jail_probability: float = JAIL_CANDIDATE_PROBABILITY
    decor_density: float = 1.0        # scales blocking/open decor budgets
    # Remaps applied to _decor_theme's result (never to jail rooms).
    decor_overrides: tuple[tuple[str, str], ...] = ()


FLOOR_VARIANT_ROTATION = (
    # Tidy military bunker: hard materials, sparse cells, guard fittings.
    FloorVariant("garrison", pillar_chance=0.10, jail_probability=0.15,
                 theme_pool=(1, 15, 17),
                 decor_overrides=(("barracks", "guardpost"),)),
    # Cramped dungeon: bitten-into rooms, narrow halls, cellblocks, gore.
    FloorVariant("catacombs", notch_chance=0.5, alcove_chance=0.5,
                 pillar_chance=0.14, widen_chance=0.55, jail_probability=0.6,
                 theme_pool=(8, 1, 17), decor_density=0.7,
                 decor_overrides=(("lounge", "barracks"),)),
    # Stately galleries: long halls, colonnades, wood and insignia panels.
    FloorVariant("grand-halls", hall_chance=0.4, extra_motif_chance=0.6,
                 motif_pref=("gallery",), pillar_chance=0.15, widen_chance=1.0,
                 theme_pool=(12, 40, 1), decor_density=1.25,
                 decor_overrides=(("barracks", "lounge"),)),
    # Supply depot: closet-heavy plan, loading niches, barrels everywhere.
    FloorVariant("storehouse", closet_weight=0.65, alcove_chance=0.45,
                 pillar_chance=0.08, jail_probability=0.0, theme_pool=(17, 15, 12),
                 decor_density=1.15,
                 decor_overrides=(("barracks", "storage"), ("lounge", "storage"))),
    # Officers' quarters: smooth walls, wide halls, lived-in furniture.
    FloorVariant("quarters", notch_chance=0.2, widen_chance=0.9,
                 pillar_chance=0.08,
                 theme_pool=(12, 40, 1), decor_density=1.1,
                 decor_overrides=(("guardpost", "lounge"),)),
)
# Floors 9 and 10 keep their purpose-built inline treatments (boss arena,
# treasure vault); the forced variants exist so every floor has a named
# identity in the manifest and the decoration hooks apply uniformly.
VARIANT_STRONGHOLD = FloorVariant("stronghold")
VARIANT_VAULT = FloorVariant("vault")

# In-game display flavor for mapinfo level names.
_VARIANT_TITLES = {
    "garrison": "The Garrison",
    "catacombs": "The Catacombs",
    "grand-halls": "Grand Halls",
    "storehouse": "The Storehouse",
    "quarters": "Officers' Quarters",
    "stronghold": "The Stronghold",
    "vault": "Treasure Vault",
}

DECORATION_MULTIPLIERS = (0.0, 0.70, 0.85, 1.00, 1.15, 1.30)
SHAPE_MULTIPLIERS = (0.0, 0.65, 0.82, 1.00, 1.10, 1.20)
PATROL_CHANCES = (0.0, 0.10, 0.22, 0.35, 0.45, 0.55)


def _variant_sequence(config: CampaignConfig) -> tuple[FloorVariant, ...]:
    """The campaign's per-floor variants, a pure function of the seed.

    Each pick draws from its own variant_seed and excludes the previous
    floor's pick, so consecutive floors always differ and floor N's variant
    is derivable without generating floors 1..N-1. Floors 9/10 are the
    forced boss/vault identities."""
    picks: list[FloorVariant] = []
    for floor in range(1, 9):
        rng = random.Random(config.variant_seed(floor))
        pool = [variant for variant in FLOOR_VARIANT_ROTATION
                if not picks or variant.name != picks[-1].name]
        bias = config.theme_bias.value
        if bias == "mixed":
            picks.append(rng.choice(pool))
        else:
            weights = [3 if variant.name == bias else 1 for variant in pool]
            picks.append(rng.choices(pool, weights=weights, k=1)[0])
    return tuple(picks) + (VARIANT_STRONGHOLD, VARIANT_VAULT)


@dataclass(frozen=True, slots=True)
class GatePlan:
    """Ordered key colors required by one floor's mandatory route."""
    colors: tuple[str, ...] = ()


def _lock_schedule(config: CampaignConfig) -> tuple[GatePlan, ...]:
    """Build a seeded campaign quota, weighted toward later floors.

    Floors 1--8 share a deliberate mixture of unlocked, single-key and
    dual-key maps. The seed chooses the exact quota and placement without
    permitting three gated floors in a row. Floor 9 always retains its gold
    boss-elevator gate and may add a silver pre-boss stage; floor 10 is open.
    """
    rng = random.Random(config.lock_seed())
    intensity = int(config.locked_doors)
    gated_ranges = {1: (0, 1), 2: (1, 2), 3: (3, 4),
                    4: (4, 5), 5: (5, 6)}
    dual_ranges = {1: (0, 0), 2: (0, 1), 3: (1, 2),
                   4: (2, 3), 5: (3, 4)}
    gated_count = rng.randint(*gated_ranges[intensity])
    dual_count = min(gated_count, rng.randint(*dual_ranges[intensity]))

    floor_sets = [choice for choice in combinations(range(1, 9), gated_count)
                  if not any(set(range(start, start + 3)) <= set(choice)
                             for start in range(1, 7))]
    if gated_count:
        weights = [math.prod(1.0 + floor * floor / 8.0 for floor in choice)
                   for choice in floor_sets]
        gated = set(rng.choices(floor_sets, weights=weights, k=1)[0])
    else:
        gated = set()
    if dual_count:
        choices = list(combinations(sorted(gated), dual_count))
        weights = [math.prod(floor for floor in choice) for choice in choices]
        dual = set(rng.choices(choices, weights=weights, k=1)[0])
    else:
        dual = set()

    plans = [GatePlan() for _ in range(10)]
    single_counts = {"gold": 0, "silver": 0}
    for floor in sorted(gated):
        if floor in dual:
            colors = (("gold", "silver") if rng.randrange(2)
                      else ("silver", "gold"))
        else:
            least = min(single_counts.values())
            available = [color for color, count in single_counts.items() if count == least]
            color = rng.choice(available)
            single_counts[color] += 1
            colors = (color,)
        plans[floor - 1] = GatePlan(colors)

    silver_boss_chance = (0.0, 0.0, 0.10, 0.25, 0.50, 0.70)[intensity]
    plans[8] = GatePlan(("silver", "gold") if rng.random() < silver_boss_chance
                        else ("gold",))
    plans[9] = GatePlan()
    return tuple(plans)


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


@dataclass(frozen=True, slots=True)
class RoomSpec:
    role: str
    tier: str
    district: int
    motif: str = "spine"


@dataclass(frozen=True, slots=True)
class RoomIdentity:
    """One semantic decision shared by wall, population and decor passes."""
    role: str
    tier: str
    motif: str
    district: int
    variant: str
    concept: str
    base_theme: str
    wall_base: int = WALL
    special: str = ""


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
    edges: tuple[tuple[int, int], ...] = ()
    jail_rooms: frozenset[int] = frozenset()
    variant: str = ""
    room_concepts: tuple[str, ...] = ()
    key_order: tuple[str, ...] = ()
    critical_route: tuple[int, ...] = ()
    room_districts: tuple[int, ...] = ()
    exit_depth_ratio: float = 0.0


def _room_identities(rooms: list[Room], specs: list[RoomSpec], districts: list[int],
                     edges: list[tuple[int, int]], variant: FloorVariant,
                     jail_rooms: frozenset[int],
                     component_of: dict[tuple[int, int], int],
                     group_theme: dict[int, tuple[int, tuple[int, ...]]],
                     exit_room: Room, boss_room: Room | None = None
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
        "corridor": ("corridor",),
        "jail": ("jail", "holding-cell", "interrogation-room"),
    }
    neighbors: dict[int, set[int]] = {index: set() for index in range(len(rooms))}
    for first, second in edges:
        neighbors[first].add(second)
        neighbors[second].add(first)
    concepts: list[str] = []
    counts: Counter[str] = Counter()
    for index, ((theme, special, _), spec, district) in enumerate(
            zip(resolved, specs, districts)):
        if index == kitchen_index:
            concept = "mess-kitchen"
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


def _at(plane: list[int], x: int, y: int) -> int:
    return plane[y * GRID + x] if 0 <= x < GRID and 0 <= y < GRID else -1


def _set(plane: list[int], x: int, y: int, value: int) -> None:
    if 0 <= x < GRID and 0 <= y < GRID:
        plane[y * GRID + x] = value


def _is_floor(value: int) -> bool:
    return FLOOR <= value <= ZONE_MAX or value == SECRET_EXIT_ZONE


def _path_bends(path: list[tuple[int, int]]) -> int:
    """Number of direction changes along a carved corridor path."""
    headings = [(end[0] - start[0], end[1] - start[1])
                for start, end in zip(path, path[1:])]
    return sum(current != previous for previous, current in zip(headings, headings[1:]))


def _overlaps(a: Room, b: Room, pad: int = 2) -> bool:
    return not (a.x + a.w + pad <= b.x or b.x + b.w + pad <= a.x or
                a.y + a.h + pad <= b.y or b.y + b.h + pad <= a.y)


def _plan_floor(rng: random.Random, complexity: int, number: int,
                variant: FloorVariant | None = None) -> FloorPlan:
    variant = variant or FloorVariant("default")
    target = min(20, 14 + 2 * complexity)
    # Roughly 55% of authored rooms belong to the mandatory progression
    # spine. Optional motifs and side rooms still provide exploration, but
    # the elevator can no longer sit only five rooms away in a twenty-room
    # floor. Floor 10's enlarged rooms need one fewer critical placement to
    # remain reliable inside the native 64x64 grid.
    spine_count = min(9 if number == 10 else 11,
                      max(7, round(target * 0.55)))
    beat_count = spine_count - 4
    tiers = ["standard"] + [("hall" if rng.random() < variant.hall_chance else "standard")
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
    budget = min(3, 1 + (complexity >= 3) + (rng.random() < variant.extra_motif_chance))
    motifs = ["ring"]
    remaining = ["hub", "wings", "gallery"]
    rng.shuffle(remaining)
    for preferred in reversed(variant.motif_pref):
        if preferred in remaining:
            remaining.remove(preferred)
            remaining.insert(0, preferred)
    motifs += remaining[:budget - 1]

    # The first motif always spends topology budget on a real reconvergence.
    pairs = [(i, j) for i in range(1, spine_count - 2)
             for j in range(i + 2, min(i + 4, spine_count - 1))]
    weights = [spine_count - abs((i + j) - (spine_count - 1)) for i, j in pairs]
    left, right = rng.choices(pairs, weights=weights, k=1)[0]
    parent = left
    # The reconvergence may add variety but must never be a faster route to
    # the elevator than the spine segment it parallels.
    ring_rooms = max(right - left - 1, rng.randrange(1, 3))
    for _ in range(ring_rooms):
        node = len(specs)
        specs.append(RoomSpec("ring", "standard", districts[left], "ring"))
        edges.append((parent, node))
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
            edges.append((hub, node))
    if "wings" in motifs:
        parent = rng.choice(middle_beats)
        wings = tuple(range(len(specs), len(specs) + 2))
        for node in wings:
            specs.append(RoomSpec("branch", "standard", districts[parent], "wings"))
            edges.append((parent, node))
        groups.append(wings)
    if "gallery" in motifs:
        parent = rng.choice(middle_beats)
        district = districts[parent]
        gallery = []
        for _ in range(rng.randrange(2, 4)):
            node = len(specs)
            specs.append(RoomSpec("closet", "closet", district, "gallery"))
            edges.append((parent, node)); gallery.append(node)
            parent = node
        groups.append(tuple(gallery))

    filler_tips: list[int] = []
    while len(specs) < target:
        if filler_tips and rng.random() < 0.35:
            parent = rng.choice(filler_tips)
            filler_tips.remove(parent)
        else:
            degrees = [sum(index in edge for edge in edges) for index in middle_beats]
            parent = rng.choices(middle_beats, weights=[1 / degree for degree in degrees], k=1)[0]
        role = "closet" if rng.random() < variant.closet_weight else "branch"
        tier = "closet" if role == "closet" else rng.choice(("standard", "standard", "hall"))
        node = len(specs)
        specs.append(RoomSpec(role, tier, specs[parent].district, "filler"))
        edges.append((parent, node))
        filler_tips.append(node)
    if sum(spec.tier == "anchor" for spec in specs) != 1:
        raise ValueError("floor plan must have exactly one anchor")
    return FloorPlan(specs, edges, loops, tuple(motifs), frozenset(critical), tuple(groups))


def _room_size(rng: random.Random, tier: str, number: int = 0) -> tuple[int, int]:
    bump = 2 if number == 10 else 0
    if tier == "anchor":
        if number == 9:
            return rng.randrange(14, 18), rng.randrange(14, 18)
        return rng.randrange(10 + bump, 14 + bump), rng.randrange(10 + bump, 14 + bump)
    if tier == "closet":
        return rng.randrange(4, 6), rng.randrange(4, 6)
    if tier == "hall":
        major, minor = rng.randrange(9 + bump, 14 + bump), rng.randrange(5 + bump, 8 + bump)
        return (major, minor) if rng.random() < 0.5 else (minor, major)
    return rng.randrange(6 + bump, 10 + bump), rng.randrange(6 + bump, 10 + bump)


def _place_planned_rooms(rng: random.Random, plan: FloorPlan, number: int = 0) -> PlacedPlan:
    spine_count = next(index for index, spec in enumerate(plan.specs)
                       if spec.role == "exit") + 1
    sizes = [_room_size(rng, spec.tier, number) for spec in plan.specs]
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
        for attempt in range(60):
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
            # Human mappers align rooms; jitter is the fallback once these
            # center and edge-flush placements have had a chance.
            jitters = (_snap_offsets(parent, *sizes[index], side, rng)
                       if attempt < 20 else
                       [rng.randrange(-6, 7) if index < spine_count else rng.randrange(-11, 12)])
            for jitter in jitters:
                candidate = adjacent(parent, sizes[index], side, gap, jitter)
                if legal(candidate):
                    room = candidate
                    if index < spine_count:
                        heading = side
                    else:
                        counts[side] = counts.get(side, 0) + 1
                    break
            if room is not None:
                break
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
                if legal(candidate):
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
        if ((a in dropped or b in dropped)
                and (plan.specs[a].motif == "ring" or plan.specs[b].motif == "ring")):
            continue
        a, b = survivor(a), survivor(b)
        edge = (remap[a], remap[b])
        if edge[0] != edge[1] and edge not in edges and edge[::-1] not in edges:
            edges.append(edge)
    loop_edges = [(remap[a], remap[b]) for a, b in plan.loop_edges
                  if a not in dropped and b not in dropped]
    return PlacedPlan(rooms, kept, edges, loop_edges)


def _carve_notches(tiles: list[int], rooms: list[Room], rng: random.Random,
                   chance: float = 0.35, mirrored_chance: float = 0.6) -> None:
    for room in rooms:
        if room.w < 6 or room.h < 6 or rng.random() >= chance:
            continue
        corners = [(False, False), (True, False), (False, True), (True, True)]
        if rng.random() < mirrored_chance:
            nw = rng.randint(2, min(3, (room.w - 2) // 2))
            nh = rng.randint(2, min(3, (room.h - 2) // 2))
            if rng.random() < 0.25:
                selected = corners
            elif rng.randrange(2):
                bottom = rng.randrange(2) == 1
                selected = [(False, bottom), (True, bottom)]
            else:
                right = rng.randrange(2) == 1
                selected = [(right, False), (right, True)]
            for right, bottom in selected:
                nx = room.x + room.w - nw if right else room.x
                ny = room.y + room.h - nh if bottom else room.y
                for y in range(ny, ny + nh):
                    for x in range(nx, nx + nw):
                        _set(tiles, x, y, WALL)
            continue
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
                   chance: float = 0.3, mirrored_chance: float = 0.35) -> None:
    established = list(rooms)
    for room in rooms:
        if rng.random() >= chance:
            continue
        span, depth = rng.randrange(2, 4), rng.randrange(2, 4)
        directions = [(0, -1), (0, 1), (-1, 0), (1, 0)]
        rng.shuffle(directions)

        def bump_for(dx: int, dy: int) -> Room:
            if dx:
                bx = room.x - depth if dx < 0 else room.x + room.w
                by = room.y + (room.h - span) // 2
                return Room(bx, by, depth, span)
            bx = room.x + (room.w - span) // 2
            by = room.y - depth if dy < 0 else room.y + room.h
            return Room(bx, by, span, depth)

        def legal(bump: Room) -> bool:
            return (1 <= bump.x and bump.x + bump.w <= GRID - 1 and
                    1 <= bump.y and bump.y + bump.h <= GRID - 1 and
                    not any(other != room and _overlaps(bump, other, pad=2)
                            for other in established))

        if rng.random() < mirrored_chance:
            dx, dy = rng.choice(((1, 0), (0, 1)))
            pair = (bump_for(dx, dy), bump_for(-dx, -dy))
            if all(legal(bump) for bump in pair):
                for bump in pair:
                    for y in range(bump.y, bump.y + bump.h):
                        for x in range(bump.x, bump.x + bump.w):
                            _set(tiles, x, y, FLOOR)
                    established.append(bump)
                continue
        for dx, dy in directions:
            bump = bump_for(dx, dy)
            # The normal rock buffer keeps a niche from quietly joining a
            # neighboring room or another room's niche into a shortcut.
            if not legal(bump):
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
                      avoid: set[tuple[int, int]] | None = None,
                      *, turn_penalty: int = 4) -> list[tuple[int, int]]:
    """Carve the shortest rock-backed route between two clean thresholds."""
    avoid = set() if avoid is None else avoid

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
                nxt = x + dx, y + dy
                if not (2 <= nxt[0] < GRID - 2 and 2 <= nxt[1] < GRID - 2):
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

    # Cheap clean thresholds are common; exhaust them before relaxing the
    # rock buffer around a crowded hub.
    for (outer_a, start, _, direction_a), (outer_b, goal, _, direction_b) in pairs:
        route = find_route(start, goal, direction_a, (-direction_b[0], -direction_b[1]))
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


def _lock_code(normal_code: int, color: str) -> int:
    if color == "gold":
        return DOOR_GOLD_EW if normal_code == DOOR_EW else DOOR_GOLD_NS
    if color == "silver":
        return DOOR_SILVER_EW if normal_code == DOOR_EW else DOOR_SILVER_NS
    raise ValueError(f"unknown key color: {color}")


def _codes_for_colors(colors: set[str] | frozenset[str]) -> frozenset[int]:
    codes: set[int] = set()
    if "gold" in colors:
        codes.update(GOLD_DOORS)
    if "silver" in colors:
        codes.update(SILVER_DOORS)
    return frozenset(codes)


def _key_spot_in_region(tiles: list[int], things: list[int], rooms: list[Room],
                        roles: list[str], allowed: set[tuple[int, int]],
                        excluded: set[tuple[int, int]], start: tuple[int, int],
                        lock_cells: set[tuple[int, int]],
                        occupied: set[tuple[int, int]] = frozenset()
                        ) -> tuple[int, int] | None:
    """Choose a deliberate key location inside one progression stage."""
    lock_sides = {(x + dx, y + dy) for x, y in lock_cells
                  for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))}
    candidates: list[tuple[tuple[int, int], str]] = []
    for room, role in zip(rooms, roles):
        cells = [room.center]
        cells += [(x, y) for y in range(room.y + 1, room.y + room.h - 1)
                  for x in range(room.x + 1, room.x + room.w - 1)]
        spot = next((cell for cell in cells
                     if cell in allowed and cell not in excluded
                     and cell not in occupied and cell != start
                     and _at(things, *cell) == 0), None)
        if spot is None:
            continue
        if lock_sides & _door_zone(tiles, spot):
            continue
        candidates.append((spot, role))
    candidates.sort(key=lambda item: (
        item[1] in ("branch", "relief"),
        abs(item[0][0] - start[0]) + abs(item[0][1] - start[1])),
        reverse=True)
    return candidates[0][0] if candidates else None


def _place_doors(tiles: list[int], things: list[int], rooms: list[Room],
                 edges: list[tuple[int, int]], paths: list[list[tuple[int, int]]],
                 rng: random.Random, start: tuple[int, int],
                 gate_target: tuple[int, int], roles: list[str],
                 reserved: set[tuple[int, int]], gate_plan: GatePlan,
                 critical_route: list[int]) -> tuple[int, tuple[str, ...]]:
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
        return 0, ()

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
        return 0, ()

    def reachable(open_colors: set[str]) -> set[tuple[int, int]]:
        return _reachable(tiles, start, locked_open=False,
                          extra_passable=pushwalls, blocked=rests,
                          open_lock_codes=_codes_for_colors(open_colors))

    def restore(trial: list[tuple[int, tuple[int, int, int], str]]) -> None:
        for _, (x, y, normal), _ in trial:
            _set(tiles, x, y, normal)

    def commit(trial: list[tuple[int, tuple[int, int, int], str]],
               key_spots: list[tuple[int, int]]) -> tuple[int, tuple[str, ...]]:
        colors = tuple(color for _, _, color in trial)
        for color, spot in zip(colors, key_spots):
            _set(things, *spot, GOLD_KEY if color == "gold" else SILVER_KEY)
            reserved.add(spot)
        return len(trial), colors

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
            lock_cells = {(candidate[0], candidate[1]) for _, candidate, _ in trial}
            first_key = _key_spot_in_region(
                tiles, things, rooms, roles, closed, set(), start, lock_cells)
            second_key = (_key_spot_in_region(
                tiles, things, rooms, roles, only_first, closed, start, lock_cells,
                {first_key} if first_key else frozenset()) if first_key else None)
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
                tiles, things, rooms, roles, closed, set(), start, {(x, y)})
            if key and gate_target not in closed and gate_target in opened:
                return commit([(progress, candidate, color)], [key])
            _set(tiles, x, y, normal)
    return 0, ()


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


def _reachable(tiles: list[int], start: tuple[int, int], locked_open: bool,
               extra_passable: set[tuple[int, int]] | None = None,
               blocked: set[tuple[int, int]] | None = None,
               open_lock_codes: set[int] | frozenset[int] | None = None
               ) -> set[tuple[int, int]]:
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
            if ((locked_open and tile in LOCKED_DOORS)
                    or (open_lock_codes is not None and tile in open_lock_codes)):
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
    return frozenset(selected)


def _apply_wall_theme(tiles: list[int], things: list[int], rooms: list[Room],
                      districts: list[int], component_of: dict[tuple[int, int], int],
                      group_theme: dict[int, tuple[int, tuple[int, ...]]],
                      rng: random.Random,
                      jail_rooms: frozenset[int] = frozenset()
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
        if not accents:
            continue
        accent = accents[district % len(accents)]
        other_accents = set(accents) - {accent}
        if accent in DECOR_WALLS:
            # Landmark tiles hang like pictures on the longest clean
            # (contiguous, same-base) wall run -- never the material for the
            # whole room. Short runs get one centered tile; longer runs get a
            # mirrored pair, and the longest a center-plus-pair triplet, so a
            # dressed wall reads as deliberately symmetric composition.
            runs: list[tuple[int, list[tuple[int, int]]]] = []
            for side_index, side in enumerate(sides):
                current: list[tuple[int, int]] = []
                for cell in side:
                    if _at(tiles, *cell) == base:
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
                for selected in selected_runs:
                    mid = len(selected) // 2
                    if accent == 7 or len(selected) < 9:
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
    return landmark_cells


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


def _room_graph_path(room_count: int, edges: list[tuple[int, int]],
                     target: int) -> list[int]:
    """Stable shortest room path from the start room to ``target``."""
    links: dict[int, list[int]] = {index: [] for index in range(room_count)}
    for first, second in edges:
        links[first].append(second)
        links[second].append(first)
    parent: dict[int, int | None] = {0: None}
    queue = deque([0])
    while queue:
        room = queue.popleft()
        if room == target:
            break
        for neighbor in links[room]:
            if neighbor not in parent:
                parent[neighbor] = room
                queue.append(neighbor)
    if target not in parent:
        return []
    path = []
    cursor: int | None = target
    while cursor is not None:
        path.append(cursor)
        cursor = parent[cursor]
    return list(reversed(path))


def _room_predecessor(room_count: int, edges: list[tuple[int, int]], target: int) -> int | None:
    """Return `target`'s BFS parent from room 0 in the structural graph.

    At a loop merge, an equally short predecessor resolves deterministically
    from `edges`' stable iteration order, matching `_rooms_by_distance`.
    """
    if target == 0:
        return None
    links: dict[int, list[int]] = {i: [] for i in range(room_count)}
    for a, b in edges:
        links[a].append(b); links[b].append(a)
    parent: dict[int, int | None] = {0: None}
    queue = deque([0])
    while queue:
        room = queue.popleft()
        for nxt in links[room]:
            if nxt not in parent:
                parent[nxt] = room
                queue.append(nxt)
    return parent.get(target)


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
                  protected: set[tuple[int, int]] | None = None
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
                                      variant, depth, reward_quality=reward_quality,
                                      protected=protected)
        if reward:
            return reward, variant, (px, py)
    return None


def _carve_secret_pocket(tiles: list[int], things: list[int], px: int, py: int,
                         rng: random.Random, secret_exit: bool,
                         variant: str = "square", depth: float = 0.5,
                         *, reward_quality: int = 3,
                         protected: set[tuple[int, int]] | None = None
                         ) -> tuple[int, int] | None:
    """Carve one purpose-built, rock-shelled secret east of its pushwall."""
    if _at(tiles, px, py) != WALL or not _is_floor(_at(tiles, px - 1, py)):
        return None

    if variant == "square":
        cells = {(px + dx, py + dy) for dx in range(1, 4) for dy in range(-1, 2)}
    elif variant == "vault":
        cells = {(px + dx, py + dy) for dx in range(1, 7) for dy in range(-1, 2)}
    elif variant == "reliquary":
        side = rng.choice((-1, 1))
        cells = ({(px + dx, py + dy) for dx in range(1, 4) for dy in range(-1, 2)}
                 | {(px + dx, py + side * dy) for dx in range(3, 6)
                    for dy in range(1, 4)})
    elif variant == "gallery":
        cells = ({(px + dx, py + dy) for dx in range(1, 4) for dy in range(-1, 2)}
                 | {(px + dx, py + dy) for dx in range(3, 6) for dy in range(-2, 3)})
    elif variant == "nested":
        cells = {(px + dx, py + dy) for dx in range(1, 8) for dy in range(-1, 2)}
        cells -= {(px + 4, py - 1), (px + 4, py), (px + 4, py + 1)}
    else:
        return None

    inner_wall = {(px + 4, py - 1), (px + 4, py), (px + 4, py + 1)} if variant == "nested" else set()
    entry = (px, py)
    shell = {neighbor for x, y in cells
             for neighbor in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))
             if neighbor not in cells}
    shell.discard(entry)
    shell |= inner_wall
    footprint = cells | shell | {entry}
    if any(not (1 <= x < GRID - 1 and 1 <= y < GRID - 1)
           or _at(tiles, x, y) != WALL or _at(things, x, y) != 0
           for x, y in footprint):
        return None

    east = max(x for x, _ in cells)
    if secret_exit and any(_at(tiles, east + 1, py + dy) != WALL for dy in (-1, 0, 1)):
        return None
    for cell in cells:
        _set(tiles, *cell, FLOOR)
    if variant == "nested":
        _set(things, px + 4, py, PUSHWALL)

    rests = {(px + 2, py)}
    if variant == "nested":
        rests.add((px + 6, py))
    candidates = sorted((cell for cell in cells if cell not in rests
                         and (variant != "nested" or cell[0] != px + 4)),
                        key=lambda cell: (-cell[0], abs(cell[1] - py), cell[1]))
    if secret_exit:
        candidates = [cell for cell in candidates if cell != (east, py)]
    if len(candidates) < 3:
        return None

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
    reward_cells = candidates[:3]
    for cell, item in zip(reward_cells, rewards):
        _set(things, *cell, item)

    if secret_exit:
        _set(tiles, east + 1, py - 1, ELEVATOR_TILE)
        _set(tiles, east + 1, py, ELEVATOR_TILE)
        _set(tiles, east + 1, py + 1, ELEVATOR_TILE)
        _set(tiles, east, py, SECRET_EXIT_ZONE)
    _set(things, *entry, PUSHWALL)
    if protected is not None:
        protected.update(footprint)
        protected.update(reward_cells)
        if secret_exit:
            protected.update({(east + 1, py + dy) for dy in (-1, 0, 1)})
    return reward_cells[0]


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


def _shortest_floor_path(tiles: list[int], start: tuple[int, int],
                         target: tuple[int, int]) -> list[tuple[int, int]]:
    """Shortest geometric route with ordinary and locked doors open."""
    parent: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
    queue = deque([start])
    while queue:
        cell = queue.popleft()
        if cell == target:
            break
        x, y = cell
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            neighbor = x + dx, y + dy
            if neighbor in parent:
                continue
            tile = _at(tiles, *neighbor)
            if _is_floor(tile) or tile in DOORS:
                parent[neighbor] = cell
                queue.append(neighbor)
    if target not in parent:
        return []
    path = []
    cursor: tuple[int, int] | None = target
    while cursor is not None:
        path.append(cursor)
        cursor = parent[cursor]
    return list(reversed(path))


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
                      exit_room: Room, *, patrol_chance: float = 0.35
                      ) -> tuple[int, int, int]:
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
    dog_cells: dict[Room, list[tuple[int, int]]] = {}

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
        if name == "dog" and room is not None:
            dog_cells.setdefault(room, []).append((x, y))
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
        if (budget and patrol is not None and rng.random() < patrol_chance):
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
            elif ridx % max(2, 7 - int(config.treasure) - (2 if number == 10 else 0)) == 1:
                _set(things, x, y, rng.choice(TREASURE))
                if number == 10 and len(candidates[cursor:]) > 1:
                    _set(things, *candidates[cursor + 1], rng.choice(TREASURE))
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
    health_now = things.count(DOG_FOOD) + things.count(FOOD) + things.count(FIRST_AID)
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
    if any(_at(things, x, y) in (DOG_FOOD, FOOD, FIRST_AID) for x, y in within):
        return
    candidates = [(x, y) for x, y in within
                  if (x, y) not in reserved and _at(things, x, y) == 0 and _inside_room(rooms, x, y)]
    if candidates:
        _set(things, *rng.choice(candidates), FOOD)


# Items that read as a deliberate matched pair when mirrored beside a door
# or under a landmark wall: plants, lamps, pillars, vases, barrels, suits
# of armor, and flags.
_FRAMEABLE = frozenset({26, 30, 31, 34, 35, 39, 62})


@dataclass(frozen=True, slots=True)
class RoomAnchors:
    """Composition anchors decoration builds around instead of free scatter."""
    # ((entry cell, inward unit vector), ...) for every doorway into the room.
    door_entries: tuple[tuple[tuple[int, int], tuple[int, int]], ...]
    # Entry cells plus one cell straight in: reachability alone still lets
    # furniture jam a doorway visually, so these ban all blocking decor.
    keep_clear: frozenset[tuple[int, int]]
    corners: tuple[tuple[int, int], ...]
    wall_midcells: tuple[tuple[int, int], ...]


def _room_anchors(room: Room, tiles: list[int]) -> RoomAnchors:
    entries: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for x in range(room.x, room.x + room.w):
        for y, outward in ((room.y, -1), (room.y + room.h - 1, 1)):
            if (_at(tiles, x, y + outward) in DOORS
                    and _is_floor(_at(tiles, x, y))):
                entries.append(((x, y), (0, -outward)))
    for y in range(room.y, room.y + room.h):
        for x, outward in ((room.x, -1), (room.x + room.w - 1, 1)):
            if (_at(tiles, x + outward, y) in DOORS
                    and _is_floor(_at(tiles, x, y))):
                entries.append(((x, y), (-outward, 0)))
    clear = set()
    for (ex, ey), (ix, iy) in entries:
        clear.add((ex, ey))
        clear.add((ex + ix, ey + iy))
    cx, cy = room.center
    corners = ((room.x + 1, room.y + 1), (room.x + room.w - 2, room.y + 1),
               (room.x + 1, room.y + room.h - 2),
               (room.x + room.w - 2, room.y + room.h - 2))
    midcells = ((cx, room.y + 1), (cx, room.y + room.h - 2),
                (room.x + 1, cy), (room.x + room.w - 2, cy))
    return RoomAnchors(tuple(entries), frozenset(clear), corners, midcells)


def _place_zoned(room: Room,
                 zones: tuple[tuple[tuple[int, ...], tuple[int, ...]],
                              tuple[tuple[int, ...], tuple[int, ...]]],
                 free: set[tuple[int, int]], blocked_cells: set[tuple[int, int]],
                 reserved: set[tuple[int, int]], things: list[int], rng: random.Random,
                 try_place, blocking_budget: int, place_open=None) -> None:
    """Cluster two compatible furniture concepts on opposite room halves."""
    cx, cy = room.center
    horizontal = room.w >= room.h

    def in_zone(cell: tuple[int, int], first: bool) -> bool:
        if horizontal:
            return cell[0] < cx if first else cell[0] >= cx
        return cell[1] < cy if first else cell[1] >= cy

    corners = [(room.x + 1, room.y + 1),
               (room.x + room.w - 2, room.y + 1),
               (room.x + 1, room.y + room.h - 2),
               (room.x + room.w - 2, room.y + room.h - 2)]
    corner_zones = ([corner for corner in corners if in_zone(corner, True)],
                    [corner for corner in corners if in_zone(corner, False)])
    cluster_budgets = ((blocking_budget + 1) // 2, blocking_budget // 2)

    for (blocking, _), corners, budget in zip(zones, corner_zones, cluster_budgets):
        if not budget:
            continue
        item = rng.choice(blocking)
        rng.shuffle(corners)
        for cornx, corny in corners:
            nx = cornx + (1 if cornx < cx else -1)
            ny = corny + (1 if corny < cy else -1)
            cluster = [cell for cell in ((cornx, corny), (nx, corny), (cornx, ny))
                       if cell in free][:2]
            if len(cluster) == 2 and try_place(cluster, item):
                break

    area = room.w * room.h
    open_budget = 3 if area >= 80 else 2 if area >= 45 else 1
    open_budgets = ((open_budget + 1) // 2, open_budget // 2)
    for zone_index, ((_, open_items), budget) in enumerate(zip(zones, open_budgets)):
        if not open_items:
            continue
        loose = [cell for cell in free - reserved
                 if in_zone(cell, zone_index == 0) and _at(things, *cell) == 0]

        # Prefer cells beside this zone's furniture cluster, then wall-hugging
        # cells, so themed clutter reads as attached to its concept rather
        # than sprinkled across the half.
        def _rank(cell: tuple[int, int]) -> tuple[int, int, float]:
            beside = any((cell[0] + dx, cell[1] + dy) in blocked_cells
                         for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
            inset = min(cell[0] - room.x, room.x + room.w - 1 - cell[0],
                        cell[1] - room.y, room.y + room.h - 1 - cell[1])
            return (0 if beside else 1, inset, rng.random())

        loose.sort(key=_rank)
        for cell in loose[:max(1, rng.randrange(0, budget + 1)) if budget else 0]:
            item = rng.choice(open_items)
            if place_open is not None:
                place_open(cell, item)
            else:
                _set(things, *cell, item)
                reserved.add(cell)
                free.discard(cell)


def _place_decorations(rooms: list[Room], tiles: list[int], things: list[int],
                       reserved: set[tuple[int, int]], start: tuple[int, int],
                       rng: random.Random,
                       roles: list[str] | None = None,
                       specs: list | None = None,
                       jail_rooms: frozenset[int] = frozenset(),
                       density: float = 1.0,
                       theme_overrides: tuple[tuple[str, str], ...] = (),
                       landmarks: dict[int, list[tuple[int, int]]] | None = None,
                       paths: list[list[tuple[int, int]]] | None = None,
                       identities: list[RoomIdentity] | None = None,
                       atmosphere: int = 3,
                       landmark_frame_chance: float = 0.15) -> None:
    """Place purposeful, themed furniture in rooms following community-map patterns.

    Blocking statics go in deliberate arrangements (landmark-wall frames,
    doorway flanks, pillar pairs, corner clusters, banquet rows, or occasional
    partial dividers) chosen to match the room's role and tier and anchored to
    the room's own features rather than scattered on free cells. Reachability
    is checked before any blocking item is committed, so furniture can never
    wall the player out of any area, and doorway approach cells stay clear so
    an entrance never reads furniture-jammed. Open (non-solid) items are
    anchored too: ceiling fixtures on the center axis, clutter beside
    furniture or wall midpoints, rhythm lights down long corridors, and one
    niche piece in each small dead-end alcove pocket.
    """
    baseline = len(_reachable(tiles, start, locked_open=True))
    blocked_cells: set[tuple[int, int]] = set()
    _roles = roles or ["beat"] * len(rooms)
    _tiers = [s.tier for s in specs] if specs else ["standard"] * len(rooms)
    overrides = dict(theme_overrides)
    # Engine statics soft cap (DESIGN §9.1): treasure, pickups, and keys
    # already on the plane count against it; decoration consumes only what
    # headroom remains instead of racing the 400 hard limit.
    static_headroom = 320 - sum(1 for thing in things if 23 <= thing <= 74)
    doorway_frames_placed = 0

    for ridx, room in enumerate(rooms):
        role = _roles[ridx] if ridx < len(_roles) else "beat"
        tier = _tiers[ridx] if ridx < len(_tiers) else "standard"
        identity = identities[ridx] if identities and ridx < len(identities) else None
        if identity is not None:
            theme = identity.base_theme
            concept = identity.concept
        elif ridx in jail_rooms:
            theme = "jail"
            concept = theme
        else:
            theme = _decor_theme(role, tier)
            theme = overrides.get(theme, theme)
            concept = theme

        if room.w < 5 or room.h < 5:
            continue

        blocking = _DECOR_BLOCKING.get(concept,
                                       _DECOR_BLOCKING.get(theme, STATIC_BLOCKING))
        open_items = _DECOR_OPEN.get(concept,
                                     _DECOR_OPEN.get(theme, STATIC_OPEN))
        if atmosphere <= 1:
            blocking = tuple(item for item in blocking if item not in (28, 41))
            open_items = tuple(item for item in open_items
                               if item not in (32, 42, 57, 61, 64, 65, 66))
        elif atmosphere == 2:
            blocking = tuple(item for item in blocking if item not in (28, 41))
            open_items = tuple(item for item in open_items if item != 57)
        elif atmosphere >= 4 and theme in ("jail", "barracks"):
            open_items += ((61,) if atmosphere == 4 else (57, 61))

        cx, cy = room.center
        interior = {(x, y) for x in range(room.x + 1, room.x + room.w - 1)
                    for y in range(room.y + 1, room.y + room.h - 1)
                    if _is_floor(_at(tiles, x, y))}
        free: set[tuple[int, int]] = {cell for cell in interior - reserved
                                      if _at(things, *cell) == 0}
        anchors = _room_anchors(room, tiles)
        keep_clear = set(anchors.keep_clear)
        # The outermost floor ring is excluded from `interior` (and thus from
        # every legacy pattern), but wall-flush anchors -- door flanks and
        # landmark frames -- live exactly there, so track it separately.
        ring = ({(x, y) for x in range(room.x, room.x + room.w)
                 for y in (room.y, room.y + room.h - 1)}
                | {(x, y) for x in (room.x, room.x + room.w - 1)
                   for y in range(room.y, room.y + room.h)})
        edge_free = {cell for cell in ring - reserved
                     if _is_floor(_at(tiles, *cell)) and _at(things, *cell) == 0}

        def _near_wall(x: int, y: int) -> bool:
            return (x <= room.x + 2 or x >= room.x + room.w - 3
                    or y <= room.y + 2 or y >= room.y + room.h - 3)

        def _try_place_items(pieces: list[tuple[tuple[int, int], int]]) -> bool:
            """Commit a blocking group if all cells are free, no doorway
            approach is jammed, statics headroom remains, and reachability
            holds."""
            nonlocal static_headroom
            cells = [cell for cell, _ in pieces]
            if static_headroom < len(cells):
                return False
            if not all((c in free or c in edge_free) and c not in keep_clear
                       for c in cells):
                return False
            candidate = blocked_cells | set(cells)
            if len(_reachable(tiles, start, locked_open=True, blocked=candidate)) < baseline - len(candidate):
                return False
            for c, item in pieces:
                _set(things, *c, item)
                reserved.add(c)
                blocked_cells.add(c)
                room_blocked.append(c)
                free.discard(c)
                edge_free.discard(c)
            static_headroom -= len(cells)
            return True

        def _try_place(cells: list[tuple[int, int]], item: int) -> bool:
            return _try_place_items([(cell, item) for cell in cells])

        def _place_open(cell: tuple[int, int], item: int) -> bool:
            """Commit one non-solid item; only occupancy and headroom apply."""
            nonlocal static_headroom
            if static_headroom <= 0 or _at(things, *cell) != 0:
                return False
            _set(things, *cell, item)
            reserved.add(cell)
            free.discard(cell)
            edge_free.discard(cell)
            static_headroom -= 1
            return True

        pair_budget = max(1, round((2 if room.w >= 8 and room.h >= 8 else 1) * density))
        pairs_placed = 0
        room_blocked: list[tuple[int, int]] = []
        concept_frames = {
            "war-room": (39, 62), "armory": (39, 62), "guardpost": (26,),
            "lounge": (31, 34, 35), "mess-kitchen": (35,),
            "courtyard": (31, 34), "checkpoint": (26, 62),
            "trophy-hall": (39, 62), "gallery": (34, 39, 62),
            "dining-hall": (35,), "officers-quarters": (34, 35),
        }
        frame_pool = concept_frames.get(
            concept, tuple(item for item in blocking if item in _FRAMEABLE)) or (26,)

        # --- Vignette: frame a landmark wall (portrait, banner, insignia) ---
        # The wall pass hangs its landmarks symmetrically; a matched pair of
        # plants/lamps beneath one turns that wall into a composed set piece
        # and keeps the furniture from floating mid-room. The cell directly
        # in front stays clear so the frame never hides the picture.
        room_landmarks = list((landmarks or {}).get(ridx, ()))
        if (room_landmarks and pairs_placed < pair_budget
                and rng.random() < landmark_frame_chance):
            by_side: dict[str, list[tuple[int, int]]] = {}
            for lx, ly in room_landmarks:
                side = ("north" if ly < room.y else "south" if ly >= room.y + room.h
                        else "west" if lx < room.x else "east")
                by_side.setdefault(side, []).append((lx, ly))
            for cells in by_side.values():
                cells.sort(key=lambda cell: (cell[0], cell[1]))
            selected: list[tuple[int, int]] = []
            for first, second in (("north", "south"), ("west", "east")):
                if first in by_side and second in by_side:
                    selected = [by_side[first][len(by_side[first]) // 2],
                                by_side[second][len(by_side[second]) // 2]]
                    break
            if not selected:
                cells = max(by_side.values(), key=len)
                selected = [cells[len(cells) // 2]]
            flanks: list[tuple[int, int]] = []
            fronts: list[tuple[int, int]] = []
            for lx, ly in selected:
                inward = next(((dx, dy) for dx, dy in
                               ((1, 0), (-1, 0), (0, 1), (0, -1))
                               if room.x <= lx + dx < room.x + room.w
                               and room.y <= ly + dy < room.y + room.h), None)
                if inward is None:
                    flanks = []
                    break
                ix, iy = inward
                front = (lx + ix, ly + iy)
                fronts.append(front)
                flanks.extend(((front[0] + iy, front[1] + ix),
                               (front[0] - iy, front[1] - ix)))
            keep_clear.update(fronts)
            if flanks and _try_place(flanks, rng.choice(frame_pool)):
                pairs_placed += len(selected)

        # Room signatures come from the same grammar/variant/material
        # identity that selected the room, not from a generic static pool.
        if room.w >= 6 and room.h >= 6 and pairs_placed < pair_budget:
            signatures: dict[str, list[tuple[tuple[int, int], int]]] = {
                "barracks": [((room.x + 1, room.y + 1), 45),
                              ((room.x + room.w - 2, room.y + room.h - 2), 45)],
                "ready-room": [((room.x + 1, room.y + 1), 45),
                               ((room.x + room.w - 2, room.y + 1), 36)],
                "training-room": [((room.x + 1, cy), 69),
                                  ((room.x + room.w - 2, cy), 36)],
                "armory": [((room.x + 1, cy), 69),
                            ((room.x + room.w - 2, cy), 69)],
                "guardpost": [((room.x + 1, room.y + 1), 26),
                               ((room.x + room.w - 2, room.y + 1), 26)],
                "checkpoint": [((room.x + 1, room.y + 1), 62)],
                "war-room": [((room.x + 1, room.y + 1), 39),
                              ((room.x + room.w - 2, room.y + 1), 39)],
                "trophy-hall": [((room.x + 1, room.y + 1), 39),
                                ((room.x + room.w - 2, room.y + 1), 62)],
                "courtyard": [((cx, cy), 59)],
                "storage": [((room.x + 1, room.y + 1), 58),
                             ((room.x + 2, room.y + 1), 58)],
                "supply-cache": [((room.x + 1, room.y + 1), 24),
                                  ((room.x + 2, room.y + 1), 58)],
                "workshop": [((room.x + 1, cy), 36),
                             ((room.x + room.w - 2, cy), 69)],
                "lounge": [((cx, cy), 25)],
                "gallery": [((room.x + 1, cy), 39)],
                "dining-hall": [((cx, cy), 25)],
                "officers-quarters": [((room.x + 1, room.y + 1), 45),
                                      ((room.x + room.w - 2,
                                        room.y + room.h - 2), 34)],
                "jail": [((room.x + 1, room.y + 1), 40 if atmosphere <= 2 else 41),
                         ((room.x + room.w - 2, room.y + 1),
                          40 if atmosphere <= 2 else 41)],
                "crypt": [((room.x + 1, room.y + 1), 30),
                           ((room.x + room.w - 2, room.y + 1), 30)],
                "ossuary": [((room.x + 1, room.y + 1), 40),
                             ((room.x + room.w - 2, room.y + 1), 41)],
                "burial-chamber": [((room.x + 1, room.y + 1), 30),
                                   ((room.x + room.w - 2, room.y + 1), 35)],
                "holding-cell": [((room.x + 1, room.y + 1), 40),
                                 ((room.x + room.w - 2, room.y + 1), 58)],
                "interrogation-room": [((cx, cy), 36),
                                       ((room.x + 1, cy), 26)],
            }
            if concept == "mess-kitchen":
                # Appliances belong against actual perimeter walls. They are
                # selected independently and kept apart, so a kitchen reads
                # as a room-sized work area instead of one repeated four-item
                # clump. The sink is optional rather than welded to the stove.
                def _wall_backed(cell: tuple[int, int]) -> bool:
                    x, y = cell
                    outward = []
                    if x == room.x:
                        outward.append((x - 1, y))
                    if x == room.x + room.w - 1:
                        outward.append((x + 1, y))
                    if y == room.y:
                        outward.append((x, y - 1))
                    if y == room.y + room.h - 1:
                        outward.append((x, y + 1))
                    return any(not _is_floor(_at(tiles, *neighbor))
                               and _at(tiles, *neighbor) not in DOORS
                               for neighbor in outward)

                wall_cells = [cell for cell in free | edge_free
                              if cell not in keep_clear and _wall_backed(cell)]
                rng.shuffle(wall_cells)
                used: list[tuple[int, int]] = []
                stove = next((cell for cell in wall_cells
                              if _try_place([cell], 68)), None)
                if stove is not None:
                    used.append(stove)
                    pairs_placed += 1
                    separation = max(3, min(room.w, room.h) // 2)
                    if rng.random() < 0.4:
                        sink = next((cell for cell in wall_cells
                                    if cell not in used
                                    and min(abs(cell[0] - x) + abs(cell[1] - y)
                                            for x, y in used) >= separation
                                    and _try_place([cell], 33)), None)
                        if sink is not None:
                            used.append(sink)
                    for item, chance in ((38, 0.65), (67, 0.45)):
                        if rng.random() >= chance:
                            continue
                        spot = next((cell for cell in wall_cells
                                     if cell not in used
                                     and min(abs(cell[0] - x) + abs(cell[1] - y)
                                             for x, y in used) >= separation
                                     and _at(things, *cell) == 0), None)
                        if spot is not None and _place_open(spot, item):
                            used.append(spot)
            else:
                signature = signatures.get(concept)
                if signature and _try_place_items(signature):
                    pairs_placed += 1

        # --- Vignette: matched pair flanking a doorway ---
        if (doorway_frames_placed < 3 and pairs_placed < pair_budget
                and anchors.door_entries and rng.random() < 0.15):
            entries = list(anchors.door_entries)
            rng.shuffle(entries)
            for (ex, ey), (ix, iy) in entries:
                flanks = [(ex + iy, ey + ix), (ex - iy, ey - ix)]
                if _try_place(flanks, rng.choice(frame_pool)):
                    pairs_placed += 1
                    doorway_frames_placed += 1
                    break

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

        # --- Pattern: corner stash cluster (storage always; battle-worn
        # barracks and bare jail cells occasionally) with a spill of loose
        # pots or blood beside it so the pile reads lived-in, not staged ---
        if pairs_placed < pair_budget and (
                theme == "storage"
                or (theme in ("barracks", "jail") and rng.random() < 0.35)):
            item = rng.choice(blocking)
            corners = list(anchors.corners)
            rng.shuffle(corners)
            for cornx, corny in corners:
                nx = cornx + (1 if cornx < cx else -1)
                ny = corny + (1 if corny < cy else -1)
                cluster = [(c) for c in [(cornx, corny), (nx, corny), (cornx, ny)]
                           if c in free][:2]
                if len(cluster) == 2 and _try_place(cluster, item):
                    pairs_placed += 1
                    spill = [(x + dx, y + dy) for x, y in cluster
                             for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
                             if (x + dx, y + dy) in free]
                    if spill:
                        spill_item = 61 if theme == "jail" else (46 if theme == "storage" else
                                     61 if atmosphere >= 3 else 46)
                        _place_open(rng.choice(spill), spill_item)
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

        # --- Vignette: banquet row along the center axis ---
        # Tables march down the room's long axis in mirrored pairs, the way
        # authored mess halls are dressed, instead of landing on random cells.
        if (pairs_placed < pair_budget and concept in ("barracks", "dining-hall")
                and max(room.w, room.h) >= 8 and rng.random() < 0.3):
            horizontal = room.w >= room.h
            cells: list[tuple[int, int]] = []
            for offset in (1, 3):
                pair = ([(cx - offset, cy), (cx + offset, cy)] if horizontal
                        else [(cx, cy - offset), (cx, cy + offset)])
                if all(cell in free for cell in pair):
                    cells += pair
            if cells and _try_place(cells, 25):   # TableWithChairs
                pairs_placed += 1

        # --- Vignette: courtyard centerpiece at the exact room center ---
        if (pairs_placed < pair_budget and concept in ("courtyard", "storage")
                and room.w >= 9 and room.h >= 9 and rng.random() < 0.3):
            if _try_place([(cx, cy)], 59 if concept == "storage" else 30):
                pairs_placed += 1

        zones = _DECOR_ZONES.get(concept) if concept == theme else None
        if zones and atmosphere <= 2:
            forbidden = ({32, 42, 57, 61, 64, 65, 66} if atmosphere == 1 else {57})
            zones = tuple((solid, tuple(item for item in open_ if item not in forbidden))
                          for solid, open_ in zones)
        themed_roll = (zones is not None and room.w >= 6 and room.h >= 6
                       and rng.random() < 0.75)

        if themed_roll:
            # _place_zoned's open-item placement always runs, independent of
            # remaining blocking budget -- an earlier pattern (colonnade,
            # divider) can already have spent pair_budget, and this room
            # must not lose its open decoration just because no blocking
            # budget is left for a themed cluster.
            _place_zoned(room, zones, free, blocked_cells, reserved, things, rng,
                         _try_place, max(0, pair_budget - pairs_placed), _place_open)
            pairs_placed = pair_budget
        else:
            # --- Pattern: symmetric wall pairs (general fallback) ---
            # Candidates come only from the wall-hugging band: a mirrored
            # pair mid-room reads as random clutter, the same pair against
            # opposite walls reads as furnishing.
            if pairs_placed < pair_budget:
                band = [cell for cell in free if _near_wall(*cell)]
                x_pairs = [((x, y), (2 * cx - x, y)) for x, y in band
                           if x < cx and (2 * cx - x, y) in free]
                y_pairs = [((x, y), (x, 2 * cy - y)) for x, y in band
                           if y < cy and (x, 2 * cy - y) in free]
                all_pairs = x_pairs + y_pairs
                rng.shuffle(all_pairs)
                for (ax, ay), (bx, by) in all_pairs:
                    if pairs_placed >= pair_budget:
                        break
                    if _try_place([(ax, ay), (bx, by)], rng.choice(blocking)):
                        pairs_placed += 1

            # --- Vignette: prisoner remains in a jail corner ---
            # Gore clusters where a body would lie instead of speckling
            # the whole cell uniformly.
            if theme == "jail" and atmosphere >= 2:
                corner_cells = [cell for cell in anchors.corners if cell in free]
                remains_chance = (0.0, 0.0, 0.35, 0.70, 0.85, 1.0)[atmosphere]
                if corner_cells and rng.random() < remains_chance:
                    corner = rng.choice(corner_cells)
                    if _place_open(corner, 32):   # SkeletonFlat
                        spots = [cell for cell in
                                 ((corner[0] + 1, corner[1]), (corner[0] - 1, corner[1]),
                                  (corner[0], corner[1] + 1), (corner[0], corner[1] - 1))
                                 if cell in free]
                        rng.shuffle(spots)
                        for cell in spots[:rng.randrange(1, 3)]:
                            _place_open(cell, 61)   # Blood

            # --- Open (non-solid) items, anchored instead of scattered ---
            # Ceiling fixtures hang on the room's center axis; floor clutter
            # sits beside furniture or hugs a wall midpoint. Nothing floats
            # on a random mid-room cell.
            area = room.w * room.h
            open_budget = max(1, round((3 if area >= 80 else 2 if area >= 45 else 1) * density))
            count = rng.randrange(0, open_budget + 1)
            ceiling = [item for item in open_items if item in (27, 37)]
            floor_clutter = [item for item in open_items if item not in (27, 37)]
            spots: list[tuple[tuple[int, int], int]] = []
            if ceiling:
                third = max(2, max(room.w, room.h) // 3)
                axis = [(cx, cy)]
                axis += ([(cx - third, cy), (cx + third, cy)] if room.w >= room.h
                         else [(cx, cy - third), (cx, cy + third)])
                spots += [(cell, rng.choice(ceiling)) for cell in axis if cell in free]
            if floor_clutter:
                beside = [(x + dx, y + dy) for x, y in room_blocked
                          for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
                          if (x + dx, y + dy) in free]
                rng.shuffle(beside)
                spots += [(cell, rng.choice(floor_clutter)) for cell in beside[:2]]
                mids = [cell for cell in anchors.wall_midcells if cell in free]
                rng.shuffle(mids)
                spots += [(cell, rng.choice(floor_clutter)) for cell in mids]
            for cell, item in spots[:count]:
                _place_open(cell, item)

    # --- Corridor rhythm: ceiling lights pace long straight halls ---
    # Open fixtures only, so nothing here can affect reachability, patrol
    # routes (in-room only), or actor facing.
    for path in paths or ():
        segments: list[list[tuple[int, int]]] = [[]]
        previous: tuple[int, int] | None = None
        heading: tuple[int, int] | None = None
        for cell in path:
            step = ((cell[0] - previous[0], cell[1] - previous[1])
                    if previous is not None else None)
            eligible = (not _inside_room(rooms, *cell)
                        and _is_floor(_at(tiles, *cell))
                        and not any(_at(tiles, cell[0] + dx, cell[1] + dy) in DOORS
                                    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))))
            if eligible and (step is None or heading is None or step == heading):
                segments[-1].append(cell)
            else:
                segments.append([cell] if eligible else [])
            if step is not None:
                heading = step
            previous = cell
        for segment in segments:
            if len(segment) < 5:
                continue
            for cell in segment[2:-1:4]:
                if static_headroom <= 0:
                    break
                if cell not in reserved and _at(things, *cell) == 0:
                    _set(things, *cell, 37)   # CeilingLight
                    reserved.add(cell)
                    static_headroom -= 1

    # --- Alcove niches: a dead-end pocket earns one deliberate piece ---
    if paths:
        path_cells = {cell for path in paths for cell in path}
        outside = {(index % GRID, index // GRID)
                   for index, tile in enumerate(tiles) if _is_floor(tile)}
        outside -= path_cells
        outside = {cell for cell in outside if not _inside_room(rooms, *cell)}
        while outside:
            component = {outside.pop()}
            queue = deque(component)
            while queue:
                x, y = queue.popleft()
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    neighbor = (x + dx, y + dy)
                    if neighbor in outside:
                        outside.discard(neighbor)
                        component.add(neighbor)
                        queue.append(neighbor)
            if len(component) > 9:
                continue
            mouths = [cell for cell in component
                      if any((cell[0] + dx, cell[1] + dy) in path_cells
                             or _inside_room(rooms, cell[0] + dx, cell[1] + dy)
                             for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))]
            if not mouths:
                continue   # sealed pocket (a secret) -- never decorate
            deep = max(component,
                       key=lambda c: min(abs(c[0] - m[0]) + abs(c[1] - m[1])
                                         for m in mouths))
            if deep in reserved or _at(things, *deep) != 0 or static_headroom <= 0:
                continue
            if deep not in mouths:
                candidate = blocked_cells | {deep}
                if len(_reachable(tiles, start, locked_open=True,
                                  blocked=candidate)) == baseline - len(candidate):
                    _set(things, *deep, rng.choice((35, 31, 26, 58)))
                    reserved.add(deep)
                    blocked_cells.add(deep)
                    static_headroom -= 1
                    continue
            # A pocket hanging off a corridor reads as hallway, and the only
            # ceiling decor that belongs in a hallway is a light -- hanging
            # pots or remains there look like kitchen props in a corridor.
            touches_room = any(_inside_room(rooms, m[0] + dx, m[1] + dy)
                               for m in mouths
                               for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
            _set(things, *deep, 32 if touches_room else 37)
            reserved.add(deep)
            static_headroom -= 1


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


def _place_preboss_cache(tiles: list[int], things: list[int], room: Room,
                         reserved: set[tuple[int, int]], rng: random.Random) -> None:
    """Place a modest stock-up cache before the boss arena.

    This intentionally sits outside `_guarantee_supplies`: that later pass
    counts the items already placed here before filling its floor-wide deficit.
    """
    candidates = [(x, y) for y in range(room.y + 1, room.y + room.h - 1)
                  for x in range(room.x + 1, room.x + room.w - 1)
                  if (x, y) not in reserved and _at(things, x, y) == 0
                  and _is_floor(_at(tiles, x, y))]
    rng.shuffle(candidates)
    loot = [FIRST_AID, AMMO]
    if rng.random() < 0.35:
        loot.append(rng.choice((MACHINE_GUN, CHAINGUN)))
    if rng.random() < 0.2:
        loot.append(ONE_UP)
    for thing, cell in zip(loot, candidates):
        _set(things, *cell, thing)
        reserved.add(cell)


def generate_map(config: CampaignConfig, number: int, attempt: int = 0,
                 secret_exit: bool = False) -> GeneratedMap:
    seed = config.floor_seed(number, attempt)
    rng = random.Random(seed)
    tiles = [WALL] * (GRID * GRID)
    things = [0] * (GRID * GRID)
    complexity = int(config.layout_complexity)
    floor_variant = _variant_sequence(config)[number - 1]
    scheduled_gate = _lock_schedule(config)[number - 1]
    plan = _plan_floor(rng, complexity, number, variant=floor_variant)
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
    _carve_notches(tiles, rooms, rng,
                   chance=min(1.0, floor_variant.notch_chance * shape_scale))
    _carve_alcoves(tiles, rooms, rng,
                   chance=min(1.0, floor_variant.alcove_chance * shape_scale))
    overrides = dict(floor_variant.decor_overrides)
    for room, spec in zip(rooms, specs):
        predicted = overrides.get(_decor_theme(spec.role, spec.tier),
                                  _decor_theme(spec.role, spec.tier))
        structural = (predicted == "grand"
                      or (floor_variant.name == "catacombs" and spec.tier == "anchor"))
        if structural:
            _add_pillars(tiles, room, rng, chance=floor_variant.pillar_chance)
    door_zones: set[tuple[int, int]] = set()
    paths = [_carve_connection(tiles, rooms[a], rooms[b], rng, complexity, door_zones)
             for a, b in edges]
    _widen_corridors(tiles, rooms, paths, rng, widen_chance=floor_variant.widen_chance)
    start = rooms[0].center
    _set(things, *start, PLAYER_START)
    is_boss = number == 9
    exit_room = None
    exit_stand = None
    # The elevator belongs near the deepest authored frontier, after the
    # anchor/climax room and at the end of a route containing most of the
    # mandatory spine. The old behavior always tried the nominal exit first,
    # even when a much deeper wing made it a trivial early solution.
    planned_exit_index = roles.index("exit")
    anchor_index = next(index for index, spec in enumerate(specs) if spec.tier == "anchor")
    preliminary_distances = _floor_distances(tiles, start)
    center_distances = {index: preliminary_distances.get(room.center, 0)
                        for index, room in enumerate(rooms)}
    deepest_center = max(center_distances.values(), default=1) or 1
    minimum_route_rooms = max(6, math.ceil(len(rooms) * 0.55))
    exit_candidates = []
    for room_index in range(1, len(rooms)):
        route = _room_graph_path(len(rooms), edges, room_index)
        if (anchor_index not in route[:-1] or room_index == anchor_index
                or len(route) < minimum_route_rooms
                or center_distances[room_index] / deepest_center < 0.75):
            continue
        exit_candidates.append((room_index, route))
    exit_candidates.sort(key=lambda item: (
        center_distances[item[0]], item[0] == planned_exit_index, len(item[1])),
        reverse=True)
    critical_route: list[int] = []
    exit_index = -1
    for room_index, route in exit_candidates:
        try:
            trial_tiles = tiles.copy()
            trial_stand = _place_elevator(trial_tiles, rooms[room_index], locked=is_boss)
            trial_distances = _floor_distances(trial_tiles, start)
            if trial_distances.get(trial_stand, 0) / deepest_center < 0.75:
                continue
            tiles[:] = trial_tiles
            exit_stand = trial_stand
            exit_room = rooms[room_index]
            exit_index = room_index
            critical_route = route
            break
        except ValueError:
            continue
    if exit_room is None:
        raise ValueError("no post-climax room satisfies the deep-exit route")
    if exit_index != planned_exit_index:
        roles[planned_exit_index] = "relief"
        roles[exit_index] = "exit"
    reserved = {start, exit_stand}
    rewards: list[tuple[int, int]] = []
    secret_variants: list[str] = []
    shortcut_pushwalls: list[tuple[int, int]] = []
    secret_protected: set[tuple[int, int]] = set()
    floor_distances = _floor_distances(tiles, start)
    room_distances = {room: floor_distances.get(room.center, 0) for room in rooms}
    max_room_distance = max(room_distances.values(), default=1) or 1
    # Report's secret budget is 2-6 per standard floor; scale directly with
    # the intensity dial instead of undershooting to 1 at the low end.
    target_secrets = max(2, int(config.secrets) + (1 if number == 10 else 0))
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
                                          secret_exit and not rewards,
                                          reward_quality=int(config.secret_reward_quality),
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
                                              secret_exit and not rewards,
                                              reward_quality=int(config.secret_reward_quality),
                                              protected=secret_protected)
                if placed_secret:
                    host = room
                    break
        if placed_secret:
            reward, realized_variant, push_cell = placed_secret
            rewards.append(reward); secret_variants.append(realized_variant)
            reserved.add(reward)
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
                min(1.0, approach_distance / max_room_distance),
                reward_quality=int(config.secret_reward_quality),
                protected=secret_protected)
            if reward:
                break
        if reward is None and variant != "square":
            variant = "square"
            for px, py in fallback_walls:
                approach_distance = floor_distances.get((px - 1, py), 0)
                reward = _carve_secret_pocket(
                    tiles, things, px, py, rng, secret_exit and not rewards, variant,
                    min(1.0, approach_distance / max_room_distance),
                    reward_quality=int(config.secret_reward_quality),
                    protected=secret_protected)
                if reward:
                    break
        if reward:
            rewards.append(reward); secret_variants.append(variant); reserved.add(reward)
        else:
            break
    reserved.update(secret_protected)
    reserved.update((index % GRID - 1, index // GRID)
                    for index, thing in enumerate(things) if thing == PUSHWALL)
    _place_bonus_rewards(tiles, things, rooms, reserved, rng, complexity)
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
    locks, key_order = _place_doors(
        tiles, things, rooms, edges, paths, rng, start, door_target, roles,
        reserved, door_gate_plan, door_route)
    _break_long_sightlines(tiles, things, rooms, reserved, rng, start)
    _split_oversized_zones(tiles, rooms, rng, reserved)
    if _remove_redundant_plain_doors(tiles):
        # A removed door can extend a floor-only sightline which the earlier
        # pass correctly treated as interrupted; repair only that new case.
        _break_long_sightlines(tiles, things, rooms, reserved, rng, start,
                               allow_doors=False, walls_for_redundant_doors=True)
    # With the gate probe below, seeds 0--19 on floors 2/5/8 placed at most
    # two doors (40 total doors at most), retried no maps, and left 41/60
    # samples with a second visible base material share of at least 10%.
    _limit_theme_merge_size(tiles, rooms, rng, reserved)
    if sum(tile in DOORS for tile in tiles) > 56:
        raise ValueError("door budget exceeded")
    boss_room = None
    if is_boss:
        boss_index = max((index for index in range(1, len(rooms)) if rooms[index] != exit_room),
                         key=lambda index: rooms[index].w * rooms[index].h)
        boss_room = rooms[boss_index]
        boss = _place_boss(tiles, things, boss_room, reserved, rng)
        preboss_index = _room_predecessor(len(rooms), edges, boss_index)
        if preboss_index is not None and rooms[preboss_index] != exit_room:
            _place_preboss_cache(tiles, things, rooms[preboss_index], reserved, rng)
        if boss not in KEY_DROP_BOSSES:
            locked = tuple((index % GRID, index // GRID, tile)
                           for index, tile in enumerate(tiles) if tile == DOOR_GOLD_EW)
            if "silver" in key_order:
                closed = _reachable(tiles, start, locked_open=False)
                after_silver = _reachable(
                    tiles, start, locked_open=False,
                    open_lock_codes=SILVER_DOORS)
                lock_cells = {(index % GRID, index // GRID)
                              for index, tile in enumerate(tiles)
                              if tile in LOCKED_DOORS}
                key = _key_spot_in_region(
                    tiles, things, rooms, roles, after_silver, closed, start,
                    lock_cells)
            else:
                key = _key_spot(tiles, things, rooms, roles, locked, start)
            if key is None:
                raise ValueError("boss elevator has no free gold-key location")
            _set(things, *key, GOLD_KEY)
            reserved.add(key)
        locks += 1
        key_order = key_order + ("gold",)
    enemy_tiers = _place_population(
        config, number, rooms, tiles, things, reserved, rng, start, exit_room,
        patrol_chance=PATROL_CHANCES[int(config.patrol_activity)])
    _ensure_early_heal(tiles, things, rooms, start, reserved, rng)
    _assign_sound_zones(tiles)
    component_of, group_theme = _assign_area_themes(tiles, rooms, districts, rng, number,
                                                    theme_pool=floor_variant.theme_pool)
    jail_rooms = _select_jail_rooms(rooms, districts, component_of, group_theme, tiles, rng,
                                    jail_probability=floor_variant.jail_probability)
    landmarks = _apply_wall_theme(tiles, things, rooms, districts, component_of, group_theme,
                                  rng, jail_rooms)
    _hint_secrets(tiles, things, component_of, group_theme, rng)
    identities = _room_identities(rooms, specs, districts, edges, floor_variant, jail_rooms,
                                  component_of, group_theme, exit_room, boss_room)
    _place_decorations(rooms, tiles, things, reserved, start, rng, roles=roles, specs=specs,
                       jail_rooms=jail_rooms,
                       density=(floor_variant.decor_density
                                * DECORATION_MULTIPLIERS[int(config.decoration_amount)]),
                       theme_overrides=floor_variant.decor_overrides, landmarks=landmarks,
                       paths=paths, identities=identities, atmosphere=int(config.atmosphere))
    final_distances = _floor_distances(tiles, start)
    deepest_room_distance = max((final_distances.get(room.center, 0) for room in rooms),
                                default=1) or 1
    exit_depth_ratio = final_distances.get(exit_stand, 0) / deepest_room_distance
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
                          exit_depth_ratio=exit_depth_ratio)
    validate_map(result)
    result.critique = _critique(result)
    return result


def validate_map(level: GeneratedMap) -> None:
    if len(level.tiles) != GRID * GRID or len(level.things) != GRID * GRID:
        raise ValueError("invalid plane dimensions")
    if 63 in level.things:
        raise ValueError("Call Apogee decoration is forbidden")
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
    if level.rooms and level.critical_route:
        distances = _floor_distances(level.tiles, level.start)
        deepest_room = max((distances.get(room.center, 0) for room in level.rooms),
                           default=1) or 1
        if distances.get(level.exit_stand, 0) / deepest_room < 0.75:
            raise ValueError("elevator route is shallower than 75% of the floor")
        minimum_route_rooms = max(6, math.ceil(len(level.rooms) * 0.55))
        if len(level.critical_route) < minimum_route_rooms:
            raise ValueError("critical route visits too few authored rooms")
        edge_sets = [{first, second} for first, second in level.edges]
        if any({first, second} not in edge_sets
               for first, second in zip(level.critical_route,
                                        level.critical_route[1:])):
            raise ValueError("critical route is not continuous")
        if (level.room_districts
                and len({level.room_districts[index]
                         for index in level.critical_route}) < 2):
            raise ValueError("elevator route never crosses a district boundary")
        route = _shortest_floor_path(level.tiles, level.start, level.exit_stand)
        if len(route) < 2 or _path_bends(route) < 2:
            raise ValueError("elevator route is too visually direct")
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
    # the cell right behind it is unreachable any other way.
    for wall in pushwalls:
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
    actual_locks = [(index % GRID, index // GRID, tile)
                    for index, tile in enumerate(level.tiles) if tile in LOCKED_DOORS]
    if len(actual_locks) != level.locked_doors:
        raise ValueError("locked-door count does not match the progression plan")
    if bool(actual_locks) != bool(level.key_order):
        raise ValueError("locked doors and key order disagree")
    if actual_locks:
        providers: dict[str, tuple[int, int]] = {}
        for color, thing in (("gold", GOLD_KEY), ("silver", SILVER_KEY)):
            positions = [(index % GRID, index // GRID)
                         for index, item in enumerate(level.things) if item == thing]
            if len(positions) > 1:
                raise ValueError(f"map has duplicate {color} keys")
            if positions:
                providers[color] = positions[0]
        boss_index = next((index for index, thing in enumerate(level.things)
                           if thing in BOSSES), None)
        if (level.boss and boss_index is not None
                and level.things[boss_index] in KEY_DROP_BOSSES):
            providers["gold"] = (boss_index % GRID, boss_index // GRID)

        opened_colors: set[str] = set()
        for color in level.key_order:
            if color not in providers:
                raise ValueError(f"locked map has no {color} key provider")
            open_codes = _codes_for_colors(opened_colors)
            normally_reachable = _reachable(
                level.tiles, level.start, locked_open=False,
                open_lock_codes=open_codes)
            bypass_reachable = _reachable(
                level.tiles, level.start, locked_open=False,
                extra_passable=set(pushwalls), blocked=rests,
                open_lock_codes=open_codes)
            if providers[color] not in normally_reachable:
                raise ValueError(f"{color} key is unreachable at its progression stage")
            if level.exit_stand in bypass_reachable:
                raise ValueError(f"exit bypasses the required {color} key")
            matching = GOLD_DOORS if color == "gold" else SILVER_DOORS
            lock_sides = {(x + dx, y + dy)
                          for x, y, tile in actual_locks if tile in matching
                          for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))}
            if (providers[color] in normally_reachable
                    and lock_sides & _door_zone(level.tiles, providers[color])):
                raise ValueError(f"{color} key shares a room with its lock")
            opened_colors.add(color)

        if level.exit_stand not in _reachable(
                level.tiles, level.start, locked_open=False,
                open_lock_codes=_codes_for_colors(opened_colors)):
            raise ValueError("exit is unreachable after obtaining every key")
        # Every color must independently remain necessary even if the other
        # key is treated as already collected and every secret is open.
        for color in opened_colors:
            other_colors = opened_colors - {color}
            if level.exit_stand in _reachable(
                    level.tiles, level.start, locked_open=False,
                    extra_passable=set(pushwalls), blocked=rests,
                    open_lock_codes=_codes_for_colors(other_colors)):
                raise ValueError(f"{color} lock is not individually necessary")
    if level.boss and sum(thing in BOSSES for thing in level.things) != 1:
        raise ValueError("boss floor must contain exactly one boss")
    if level.boss:
        boss_index = next(i for i, thing in enumerate(level.things) if thing in BOSSES)
        boss_position = boss_index % GRID, boss_index // GRID
        prior_colors = (set(level.key_order[:level.key_order.index("gold")])
                        if "gold" in level.key_order else set(level.key_order))
        if boss_position not in _reachable(
                level.tiles, level.start, locked_open=False,
                open_lock_codes=_codes_for_colors(prior_colors)):
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


def _mapinfo(secret_from: int, variants: tuple[str, ...] = ()) -> str:
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
        title = (_VARIANT_TITLES.get(variants[number - 1], "")
                 if number <= len(variants) else "")
        name = f"Floor {number}: {title}" if title else f"Random Floor {number}"
        # ECWolf's MAPINFO FloorNumber defaults to "1" for any map that does
        # not set it (g_mapinfo.cpp), so without this every floor reads
        # "Floor 1" on the status bar and the score tally.
        lines.append(f'map "IW{number:02d}" "{name}" {{ next = "{nxt}"{secret} '
                     f'levelnum = {number} floornumber = {number} par = {par} '
                     f'defaultceiling = "{ceiling}" music = "{music}" }}')
    lines.append(f'map "IW10" "Secret Floor" {{ next = "IW{secret_from + 1:02d}" levelnum = 10 '
                 f'floornumber = 10 par = 360 defaultceiling = "{CEILINGS[4]}" music = "{MUSIC[5]}" }}')
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
        "lock_schedule": [plan.colors for plan in _lock_schedule(config)],
        "floors": [{"number": level.number, "seed": level.seed,
                    "secrets": len(level.secret_rewards),
                    "locked_doors": level.locked_doors,
                    "key_order": level.key_order,
                    "critical_route_rooms": len(level.critical_route),
                    "exit_depth_ratio": round(level.exit_depth_ratio, 4),
                    "boss": level.boss,
                    "enemy_tiers": level.enemy_tiers,
                    "variant": level.variant,
                    "room_concepts": level.room_concepts,
                    "motifs": level.motifs,
                    "secret_variants": level.secret_variants,
                    "secret_details": [{"shape": shape, "reward_count": 3}
                                       for shape in level.secret_variants],
                    "critique": level.critique,
                    "validation": {
                        "passed": True,
                        "checks": ["bounds", "connectivity", "door_axes", "elevator",
                                   "exit_depth", "critical_route",
                                   "dual_key_progression", "key_room_separation",
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
            write("mapinfo.txt", _mapinfo(secret_from, tuple(level.variant for level in levels)))
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
