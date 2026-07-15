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

GRID = 64
WALL = 1
FLOOR = 108
ZONE_MAX = 143
DOOR_EW, DOOR_NS = 90, 91
DOOR_ELEVATOR = 100  # unlocked elevator door on an east/west axis
DOOR_ELEVATOR_NS = 101
DOOR_GOLD_EW = 92
DOOR_GOLD_NS = 93
DOOR_SILVER_EW = 94
DOOR_SILVER_NS = 95
GOLD_DOORS = frozenset({DOOR_GOLD_EW, DOOR_GOLD_NS})
SILVER_DOORS = frozenset({DOOR_SILVER_EW, DOOR_SILVER_NS})
LOCKED_DOORS = GOLD_DOORS | SILVER_DOORS
DOORS = {DOOR_EW, DOOR_NS, *LOCKED_DOORS, DOOR_ELEVATOR, DOOR_ELEVATOR_NS}
# ECWolf expands old thing 19 through four angles in engine order
# East/North/West/South. Keep the public planner convention N/E/S/W by
# explicitly reordering those codes, just as the actor families below do.
PLAYER_START_CODES = (20, 19, 22, 21)
PLAYER_START = PLAYER_START_CODES[0]
PUSHWALL = 98
# Tile 21 is the real elevator: its north/south faces render as plain
# elevator paneling and its east/west faces are the exit switch. ECWolf's
# wolf3d translator disables activation from north/south, so a usable
# switch must be approached along the east/west axis. Tile 85 renders the
# exact same ELEV1 faces but is an ordinary inert wall, which makes it the
# authentic non-functional arrival façade without introducing an exit trigger.
ELEVATOR_TILE = 21
DUMMY_ELEVATOR_TILE = 85
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
PICKUP_CODES = frozenset({DOG_FOOD, AMMO, FOOD, FIRST_AID, MACHINE_GUN,
                          CHAINGUN, ONE_UP, *TREASURE})
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

@dataclass(frozen=True, slots=True)
class WallMaterialFamily:
    """One coherent native WL6 material and its purposeful treatments.

    Plain variants may cover a room. Damage variants require a compatible
    atmosphere/concept. Landmarks are hung as sparse symmetric compositions;
    they are never selected as bulk wall fill.
    """
    name: str
    base: int
    plain_variants: tuple[int, ...] = ()
    damage_variants: tuple[int, ...] = ()
    landmarks: tuple[int, ...] = ()

    @property
    def accents(self) -> tuple[int, ...]:
        return self.plain_variants + self.damage_variants + self.landmarks


# Names and grouping come from ECWolf's bundled xlat/wolf3d.txt. These are
# material families rather than a flat texture lottery: the generator can use
# the full registered WL6 vocabulary while keeping every visible district
# coherent. FAKEDOR(13), SKY(16), stained GLASS(33), and inert ELEV1(85) are
# special compositions and intentionally remain outside the general roster.
WALL_MATERIALS = (
    WallMaterialFamily("grey-stone", 1, (2, 27), (), (3, 4, 6, 28)),
    # SIGN(28) carries a clean grey-stone surround, so it is not compatible
    # with the mossy/slimy face of SLIME(24).  Damp stone gets only its native
    # alternate course; signs remain exclusive to clean grey stone.
    WallMaterialFamily("damp-grey-stone", 24, (26,)),
    WallMaterialFamily("blue-stone", 8, (), (), (7, 41)),
    WallMaterialFamily("blue-panel", 40, (), (), (34, 36)),
    WallMaterialFamily("wood", 12, (), (), (10, 11, 23)),
    WallMaterialFamily("metal", 15, (), (), (14,)),
    WallMaterialFamily("brick", 17, (38,), (), (18, 20)),
    WallMaterialFamily("purple", 19, (), (25,), ()),
    WallMaterialFamily("chipped-stone", 29, (), (30, 31, 32), ()),
    WallMaterialFamily("grey-brick", 35, (), (39,), (37, 43, 49)),
    WallMaterialFamily("marble", 42, (46,), (), (47,)),
    WallMaterialFamily("brown-stone", 44, (45,), (), ()),
    WallMaterialFamily("plaster", 48),
)
MATERIAL_BY_BASE = {material.base: material for material in WALL_MATERIALS}
# Public compatibility view used by validation/tests and by secret hinting.
WALL_THEMES = tuple((material.base, material.accents)
                    for material in WALL_MATERIALS)
JAIL_CANDIDATE_PROBABILITY = 0.35
# BSTONEB (9) is mottled blue-stone masonry, distinct from the BLUWALL panel.
# Keep it out of the normal pool: floor 10 occasionally uses it as a rare
# material, while floors 1--9 never do. It needs its own DECOR_WALLS accent
# (the same BSTSIGN landmark the blue-stone theme uses) so a pushwall on this
# material can still be hinted -- validate_map requires every pushwall be
# hinted by a decor wall tile, and a theme with no accent at all has nothing
# in-family to hint with otherwise (see _hint_secrets).
FLOOR_TEN_STONE_THEME = (9, (41,))
PURPLE_MIN_FLOOR = 6
# Landmark decoration tiles (portraits, banners, insignia, signage/graffiti):
# these should read as a single accent set into an otherwise plain wall, the
# way they're used in the original game, never as the material of an entire
# room. Every other accent above is just an alternate plain material and is
# fine covering a whole room's walls.
DECOR_WALLS = frozenset(tile for material in WALL_MATERIALS
                        for tile in material.landmarks)
SPECIAL_WALL_TILES = frozenset({13, 16, 33, DUMMY_ELEVATOR_TILE})
SECRET_HINT_BY_BASE = {
    24: (26,),   # restrained mossy-stone course, never clean-grey signage
    19: (25,),   # blooded purple panel
    29: (30,),   # first, restrained blooded chipped-stone panel
    44: (45,),   # alternate brown-stone course
    48: (48,),   # plain plaster: fake elevator doors are not secret markers
}
SECRET_HINT_WALLS = DECOR_WALLS | frozenset(
    tile for hints in SECRET_HINT_BY_BASE.values() for tile in hints)

# A landmark is only credible where the room identity gives it a reason to
# exist. The fallback path used by low-level unit tests remains permissive;
# generated maps always have RoomIdentity metadata and use this routing.
WALL_LANDMARK_CONCEPTS = {
    3: frozenset({"guardpost", "checkpoint", "war-room", "trophy-hall"}),
    4: frozenset({"officers-quarters", "gallery", "trophy-hall", "war-room"}),
    6: frozenset({"guardpost", "checkpoint", "gallery", "war-room"}),
    7: frozenset({"jail", "holding-cell", "interrogation-room"}),
    10: frozenset({"gallery", "war-room", "trophy-hall"}),
    11: frozenset({"officers-quarters", "gallery", "lounge"}),
    14: frozenset({"guardpost", "checkpoint", "armory", "workshop"}),
    18: frozenset({"barracks", "ready-room", "training-room", "guardpost"}),
    20: frozenset({"gallery", "war-room", "trophy-hall"}),
    23: frozenset({"burial-chamber", "crypt", "gallery"}),
    28: frozenset({"guardpost", "checkpoint", "storage", "workshop"}),
    34: frozenset({"crypt", "ossuary", "trophy-hall"}),
    36: frozenset({"war-room", "guardpost", "trophy-hall"}),
    37: frozenset({"storage", "supply-cache", "workshop", "corridor"}),
    41: frozenset({"jail", "holding-cell", "interrogation-room", "checkpoint"}),
    43: frozenset({"war-room", "guardpost", "checkpoint", "workshop"}),
    47: frozenset({"gallery", "trophy-hall", "war-room"}),
    49: frozenset({"gallery", "trophy-hall", "officers-quarters"}),
}
DAMAGED_WALL_CONCEPTS = frozenset({
    "crypt", "ossuary", "burial-chamber", "jail", "holding-cell",
    "interrogation-room", "training-room", "workshop", "storage",
})

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
)

LIGHTING_ITEMS = frozenset({26, 27, 37})
LIGHTING_FAMILY_ITEMS = {
    "floor-lamp": frozenset({26}),
    "chandelier": frozenset({27}),
    "ceiling-lamp": frozenset({37}),
    "none": frozenset(),
}
SPEAR_CONCEPTS = frozenset({"armory", "training-room", "guardpost", "workshop"})
VINE_SCREEN_CONCEPTS = frozenset({"courtyard", "crypt", "burial-chamber"})

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
    "courtyard": (37,),      # CeilingLight; vines use complete screens only
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
    if tier in ("hall", "corridor") or role == "circulation":
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
    notch_chance: float = 0.22        # restrained _carve_notches
    pillar_chance: float = 0.12       # rare structural _add_pillars landmark
    widen_chance: float = 0.80        # _widen_corridors
    hall_chance: float = 0.25         # _plan_floor spine-beat tier roll
    closet_weight: float = 0.45       # _plan_floor filler closet-vs-branch
    extra_motif_chance: float = 0.35  # _plan_floor motif budget roll
    motif_pref: tuple[str, ...] = ()  # motifs promoted ahead of the shuffle
    # Allowed wall-material bases; () = all. Must keep at least as many bases
    # as the floor has districts (up to 3) or the pool is ignored.
    theme_pool: tuple[int, ...] = ()
    jail_probability: float = JAIL_CANDIDATE_PROBABILITY
    decor_density: float = 1.0        # scales blocking/open decor budgets
    # Remaps applied to _decor_theme's result (never to jail rooms).
    decor_overrides: tuple[tuple[str, str], ...] = ()


FLOOR_VARIANT_ROTATION = (
    # Tidy military bunker: hard materials, sparse cells, guard fittings.
    FloorVariant("garrison", pillar_chance=0.10, jail_probability=0.15,
                 theme_pool=(1, 15, 17, 35, 48),
                 decor_overrides=(("barracks", "guardpost"),)),
    # Cramped dungeon: bitten-into rooms, narrow halls, cellblocks, gore.
    FloorVariant("catacombs", notch_chance=0.32,
                 pillar_chance=0.14, widen_chance=0.55, jail_probability=0.6,
                 theme_pool=(8, 1, 24, 29, 44), decor_density=0.7,
                 decor_overrides=(("lounge", "barracks"),)),
    # Stately galleries: long halls, colonnades, wood and insignia panels.
    FloorVariant("grand-halls", hall_chance=0.4, extra_motif_chance=0.6,
                 motif_pref=("gallery",), pillar_chance=0.15, widen_chance=1.0,
                 theme_pool=(12, 40, 42, 48, 19), decor_density=1.25,
                 decor_overrides=(("barracks", "lounge"),)),
    # Supply depot: closet-heavy plan, loading niches, barrels everywhere.
    FloorVariant("storehouse", closet_weight=0.65,
                 pillar_chance=0.08, jail_probability=0.0,
                 theme_pool=(17, 15, 12, 35, 44, 48),
                 decor_density=1.15,
                 decor_overrides=(("barracks", "storage"), ("lounge", "storage"))),
    # Officers' quarters: smooth walls, wide halls, lived-in furniture.
    FloorVariant("quarters", notch_chance=0.14, widen_chance=0.9,
                 pillar_chance=0.08,
                 theme_pool=(12, 40, 1, 35, 48), decor_density=1.1,
                 decor_overrides=(("guardpost", "lounge"),)),
)
# Floors 9 and 10 keep their purpose-built inline treatments (boss arena,
# treasure vault); the forced variants exist so every floor has a named
# identity in the manifest and the decoration hooks apply uniformly.
VARIANT_STRONGHOLD = FloorVariant(
    "stronghold", theme_pool=(1, 15, 17, 19, 29, 35, 44))
VARIANT_VAULT = FloorVariant(
    "vault", theme_pool=(12, 19, 40, 42, 44, 48))

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
# Target share of ordinary actors that should visibly patrol. The old values
# were per-room attempt chances and produced only ~3% moving actors at the
# normal setting because most full-room loops failed geometry reservations.
PATROL_TARGETS = (0.0, 0.04, 0.09, 0.16, 0.23, 0.30)

# Floor-wide circulation and district-scale organization are separate choices.
# Themes weight these vocabularies but never own one fixed topology, avoiding
# a recognizable "one garrison plan, one catacomb plan" generator fingerprint.
CIRCULATION_SKELETONS = (
    "bent-spine", "parallel-cross", "central-wings",
    "forked", "perimeter-loop", "staggered-grid",
)
CIRCULATION_MODES = (
    "double-loaded", "single-loaded", "suite",
    "service-bays", "formal-axis", "tunnel-cluster",
)

PROGRESSION_GRAMMARS = (
    "axial-journey", "hub-relay", "offset-ladder",
    "clustered-chain", "nested-circuit", "bounded-perimeter",
)

SHAPE_TARGETS = (0.0, 0.15, 0.25, 0.40, 0.48, 0.55)
RARE_MOTIF_CHANCE = 0.03


def _variant_sequence(config: CampaignConfig) -> tuple[FloorVariant, ...]:
    """The campaign's per-floor variants, a pure function of the seed.

    Each pick draws from its own variant_seed and excludes the previous
    floor's pick, so consecutive floors always differ and floor N's variant
    is derivable without generating floors 1..N-1. Floors 9/10 are the
    forced boss/vault identities."""
    picks: list[FloorVariant] = []
    for floor in range(1, 9):
        seed = config.variant_seed(floor)
        if config.say_aardwolf:
            seed ^= config.aardwolf_seed(floor)
        rng = random.Random(seed)
        pool = [variant for variant in FLOOR_VARIANT_ROTATION
                if not picks or variant.name != picks[-1].name]
        if config.say_aardwolf and len(picks) > 1:
            distant = [variant for variant in pool
                       if variant.name != picks[-2].name]
            if distant:
                pool = distant
        bias = config.theme_bias.value
        if bias == "mixed":
            weights = ([1.0 + rng.random() * 2.5 for _ in pool]
                       if config.say_aardwolf else None)
            picks.append(rng.choices(pool, weights=weights, k=1)[0]
                         if weights else rng.choice(pool))
        else:
            weights = [(3 if variant.name == bias else 1)
                       * (1.0 + rng.random() * 1.5
                          if config.say_aardwolf else 1.0)
                       for variant in pool]
            picks.append(rng.choices(pool, weights=weights, k=1)[0])
    return tuple(picks) + (VARIANT_STRONGHOLD, VARIANT_VAULT)


def _aardwolf_variant(config: CampaignConfig, floor: int,
                      variant: FloorVariant) -> FloorVariant:
    if not config.say_aardwolf:
        return variant
    rng = random.Random(config.aardwolf_seed(floor))
    phase_rng = random.Random(config.aardwolf_seed(10) ^ config.seed)
    phase = phase_rng.random() * math.tau
    pulse = math.sin(phase + floor * (math.tau / 3.7))
    order = list(range(8))
    rng.shuffle(order)
    amplitudes = [0.15] * 8
    for index in order[:2]:
        amplitudes[index] = 1.0
    for index in order[2:5]:
        amplitudes[index] = 0.45
    blend = lambda index, value: 1.0 + (value - 1.0) * amplitudes[index]
    material = list(variant.theme_pool)
    if len(material) > 3 and amplitudes[6] >= 0.45:
        shift = rng.randrange(len(material))
        material = material[shift:] + material[:shift]
        material = material[:rng.randrange(3, min(5, len(material)) + 1)]
    motifs = list(("hub", "wings", "gallery"))
    rng.shuffle(motifs)
    echo = ("hub", "wings", "gallery")[phase_rng.randrange(3)]
    if floor in (1, 4, 7, 9):
        motifs.remove(echo)
        motifs.insert(0, echo)
    scale = lambda low, high, value: min(high, max(low, value))
    return replace(
        variant,
        notch_chance=scale(0.07, 0.38, variant.notch_chance
                           * blend(0, 0.72 + rng.random() * 0.62
                                   + pulse * 0.08)),
        pillar_chance=scale(0.04, 0.18, variant.pillar_chance
                            * blend(1, 0.65 + rng.random() * 0.80
                                    - pulse * 0.08)),
        widen_chance=scale(0.48, 1.0, variant.widen_chance
                           * blend(2, 0.72 + rng.random() * 0.55
                                   - pulse * 0.08)),
        hall_chance=scale(0.12, 0.48, variant.hall_chance
                          * blend(3, 0.62 + rng.random() * 0.95
                                  + pulse * 0.10)),
        closet_weight=scale(0.28, 0.72, variant.closet_weight
                            * blend(4, 0.68 + rng.random() * 0.72
                                    - pulse * 0.08)),
        extra_motif_chance=scale(0.18, 0.72, variant.extra_motif_chance
                                 * blend(5, 0.58 + rng.random() * 1.05)),
        motif_pref=(tuple(motifs[:2]) if amplitudes[7] >= 0.45
                    else variant.motif_pref),
        theme_pool=tuple(material),
        jail_probability=(0.0 if variant.jail_probability == 0 else
                          scale(0.08, 0.65, variant.jail_probability
                                * blend(6, 0.55 + rng.random() * 1.05))),
        decor_density=scale(0.72, 1.30, variant.decor_density
                            * blend(7, 0.72 + rng.random() * 0.58
                                    + pulse * 0.10)),
    )


def _circulation_sequence(config: CampaignConfig) -> tuple[str, ...]:
    """Choose varied skeletons; themes are preferences, never mandates."""
    variants = _variant_sequence(config)
    preferences = {
        "garrison": ("central-wings", "parallel-cross", "bent-spine"),
        "catacombs": ("bent-spine", "forked", "perimeter-loop"),
        "grand-halls": ("central-wings", "parallel-cross", "perimeter-loop"),
        "storehouse": ("parallel-cross", "staggered-grid", "central-wings"),
        "quarters": ("bent-spine", "staggered-grid", "parallel-cross"),
        "stronghold": ("central-wings", "forked", "parallel-cross"),
        "vault": ("perimeter-loop", "central-wings", "staggered-grid"),
    }
    result: list[str] = []
    for floor, variant in enumerate(variants, 1):
        seed = config.circulation_seed(floor)
        if config.say_aardwolf:
            seed ^= config.aardwolf_seed(11 - floor)
        rng = random.Random(seed)
        pool = [name for name in CIRCULATION_SKELETONS
                if not result or name != result[-1]]
        if config.say_aardwolf and len(result) > 1:
            distant = [name for name in pool if name != result[-2]]
            if distant:
                pool = distant
        favored = preferences[variant.name]
        weights = [(3 if name in favored else 1)
                   * (1.0 + rng.random() * 2.0
                      if config.say_aardwolf else 1.0)
                   for name in pool]
        result.append(rng.choices(pool, weights=weights, k=1)[0])
    return tuple(result)


def _progression_sequence(config: CampaignConfig) -> tuple[str, ...]:
    """Choose macro progression grammars independently of floor retries."""
    result: list[str] = []
    for floor in range(1, 11):
        seed = config.circulation_seed(floor) ^ 0x50524F4752455353
        if config.say_aardwolf:
            seed ^= config.aardwolf_seed(floor)
        rng = random.Random(seed)
        pool = [grammar for grammar in PROGRESSION_GRAMMARS
                if not result or grammar != result[-1]]
        if len(result) > 1:
            distant = [grammar for grammar in pool if grammar != result[-2]]
            if distant:
                pool = distant
        result.append(rng.choice(pool))
    return tuple(result)


def _rare_motif_schedule(config: CampaignConfig) -> int:
    """Return the nominated late floor for the rare plan motif, or zero."""
    rng = random.Random(config.rare_motif_seed())
    return rng.choice((6, 7, 8, 9)) if rng.random() < RARE_MOTIF_CHANCE else 0


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


@dataclass(frozen=True, slots=True)
class SpritePlacement:
    """Auditable proof that sprites belong to an authored composition."""
    reason: str
    template: str
    room_index: int
    cells: tuple[tuple[int, int, int], ...]


@dataclass(frozen=True, slots=True)
class VineScreen:
    """One complete, auditable vine pseudowall composition."""
    kind: str
    room_index: int
    cells: tuple[tuple[int, int], ...]
    ambush_anchor: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class KeyObjective:
    """A physical key staged as a measured exploration objective."""
    color: str
    cell: tuple[int, int]
    host_room: int
    stage: int
    detour: int
    treatment: str


@dataclass(frozen=True, slots=True)
class SecretDetail:
    """Host and progression metadata for one bespoke secret pocket."""
    shape: str
    reward_count: int
    host_room: int
    depth_ratio: float
    pushwall: tuple[int, int]
    secret_exit: bool = False
    hint_treatment: str = "single-landmark"
    return_floor: int = 0
    push_direction: int = 1


@dataclass(frozen=True, slots=True)
class RareMotifDetail:
    kind: str
    room_index: int
    realization: str
    endpoints: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class BossArenaDetail:
    family: str
    profile: str
    geometry: tuple[tuple[int, int], ...]
    decorations: tuple[tuple[int, int, int], ...]


@dataclass(frozen=True, slots=True)
class ArrivalDetail:
    """The inert elevator façade establishing how the player entered."""
    kind: str
    portal: tuple[int, int]
    player: tuple[int, int]
    facing: int
    footprint: tuple[tuple[int, int], ...]
    car_cells: tuple[tuple[int, int], ...] = ()
    clearance: tuple[tuple[int, int], ...] = ()
    item: tuple[int, int, int] | None = None


@dataclass(frozen=True, slots=True)
class GuardRecess:
    """A rare mirrored hallway composition built for a corner sentry."""
    room_index: int
    cells: tuple[tuple[int, int], tuple[int, int]]
    actor_cell: tuple[int, int]


@dataclass(frozen=True, slots=True)
class GuardGallery:
    """A symmetric, visible but physically inaccessible combat chamber."""
    room_index: int
    screen: tuple[tuple[int, int], ...]
    actor_cells: tuple[tuple[int, int], tuple[int, int]]
    rear_cells: tuple[tuple[int, int], ...]
    facing: int
    treatment: int = 30


@dataclass(frozen=True, slots=True)
class EncounterPlacement:
    """Auditable room-owned actor composition and its reveal behavior."""
    template: str
    room_index: int
    cells: tuple[tuple[int, int, int], ...]
    hidden_cells: tuple[tuple[int, int], ...] = ()
    patrol_kind: str = ""
    patrol_path: tuple[tuple[int, int], ...] = ()
    family: str = ""


@dataclass(frozen=True, slots=True)
class PatrolRoute:
    """Engine-valid path plus fixed direction changes at marker cells."""
    kind: str
    cells: tuple[tuple[int, int], ...]
    turns: tuple[tuple[tuple[int, int], int], ...]


@dataclass(slots=True)
class FloorPlan:
    specs: list[RoomSpec]
    edges: list[tuple[int, int]]
    loop_edges: list[tuple[int, int]]
    motifs: tuple[str, ...]
    # Realization metadata keeps grammar membership out of gameplay roles.
    critical: frozenset[int] = frozenset()
    size_groups: tuple[tuple[int, ...], ...] = ()
    skeleton: str = "bent-spine"
    district_circulation: tuple[str, ...] = ()
    special_family: str = "standard"
    progression_grammar: str = "axial-journey"
    motif_realizations: tuple[str, ...] = ()


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
    room_roles: tuple[str, ...] = ()
    room_tiers: tuple[str, ...] = ()
    circulation_skeleton: str = ""
    district_circulation: tuple[str, ...] = ()
    layout_signature: tuple[str, ...] = ()
    pickup_placements: tuple[SpritePlacement, ...] = ()
    room_shapes: tuple[str, ...] = ()
    lighting_families: tuple[str, ...] = ()
    vine_screens: tuple[VineScreen, ...] = ()
    key_objectives: tuple[KeyObjective, ...] = ()
    secret_details: tuple[SecretDetail, ...] = ()
    special_family: str = "standard"
    boss_arena_room: int = -1
    preboss_room: int = -1
    premium_room: int = -1
    expedition_rooms: tuple[int, ...] = ()
    secret_source: int = 0
    arrival: ArrivalDetail | None = None
    guard_recesses: tuple[GuardRecess, ...] = ()
    guard_galleries: tuple[GuardGallery, ...] = ()
    encounters: tuple[EncounterPlacement, ...] = ()
    patrol_target: float = 0.0
    progression_grammar: str = "axial-journey"
    motif_realizations: tuple[str, ...] = ()
    rare_motif: RareMotifDetail | None = None
    boss_arena: BossArenaDetail | None = None
    shape_target: float = 0.0


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
        "corridor": ("corridor",),
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
                variant: FloorVariant | None = None,
                skeleton: str | None = None,
                progression_grammar: str | None = None,
                rare_motif: bool = False) -> FloorPlan:
    variant = variant or FloorVariant("default")
    skeleton = skeleton or rng.choice(CIRCULATION_SKELETONS)
    if skeleton not in CIRCULATION_SKELETONS:
        raise ValueError("unknown circulation skeleton")
    progression_grammar = progression_grammar or rng.choice(PROGRESSION_GRAMMARS)
    if progression_grammar not in PROGRESSION_GRAMMARS:
        raise ValueError("unknown progression grammar")
    special_family = "standard"
    if number == 9:
        special_family = rng.choice((
            "throne-stronghold", "command-bunker", "laboratory-gauntlet",
            "columned-fortress", "central-duel"))
    elif number == 10:
        special_family = rng.choice((
            "central-vault", "museum-circuit", "nested-reliquary",
            "abandoned-armory", "treasure-palace"))
    # Density comes from additional destinations, not inflated room boxes.
    # The two highest settings now reach beyond Normal's twenty-room plan;
    # local filler recovery below raises realized density at every setting.
    target = min(24, 14 + 2 * complexity)
    if number == 10:
        # The reward expedition uses larger room footprints and consequently
        # loses more optional placements. Give it four additional destinations
        # to realize instead of making its already-large rooms even bigger.
        target = min(24, target + 4)
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
    if number == 9:
        # The final four beats are an explicit dramatic sequence. The arena
        # remains the unique anchor; the room before it is a readable pause,
        # and the room after it is a victory transition to the elevator.
        roles[-4:] = ["staging", "boss-arena", "victory", "exit"]
        tiers[-4] = "standard"
    elif number == 10:
        roles[0] = "arrival"
        roles[-3:] = ["premium-vault", "recovery", "exit"]

    # Convert selected spine beats into real narrow circulation spaces. The
    # floor skeleton controls their rhythm while later district modes decide
    # whether surrounding rooms form offices, suites, bays, or chambers.
    beat_indices = list(range(1, beat_count + 1))
    fractions = {
        "axial-journey": (0.32, 0.70),
        "hub-relay": (0.22, 0.50, 0.78),
        "offset-ladder": (0.27, 0.55, 0.82),
        "clustered-chain": (0.38, 0.68),
        "nested-circuit": (0.20, 0.48, 0.76),
        "bounded-perimeter": (0.18, 0.50, 0.82),
    }[progression_grammar]
    desired = 3 if beat_count >= 6 and len(fractions) >= 3 else 2
    corridor_indices: list[int] = []
    for fraction in fractions:
        index = beat_indices[min(len(beat_indices) - 1,
                                 round((len(beat_indices) - 1) * fraction))]
        if index not in corridor_indices:
            corridor_indices.append(index)
        if len(corridor_indices) == desired:
            break
    while len(corridor_indices) < desired:
        remaining = [index for index in beat_indices if index not in corridor_indices]
        index = max(remaining, key=lambda candidate: (
            min((abs(candidate - placed) for placed in corridor_indices),
                default=len(beat_indices)),
            -abs(candidate - beat_indices[len(beat_indices) // 2])))
        corridor_indices.append(index)
    for index in corridor_indices:
        roles[index] = "circulation"
        tiers[index] = "corridor"
    # Formal long rooms are destinations, not extra corridor segments. Keep
    # them away from circulation nodes and from one another so the spine does
    # not collapse into a visually repetitive run of the same concept.
    previous_hall = False
    for index in beat_indices:
        if tiers[index] != "hall":
            previous_hall = False
            continue
        beside_corridor = any(0 <= neighbor < len(tiers)
                              and tiers[neighbor] == "corridor"
                              for neighbor in (index - 1, index + 1))
        if beside_corridor or previous_hall:
            tiers[index] = "standard"
            previous_hall = False
        else:
            previous_hall = True

    district_count = 3 if spine_count >= 7 and rng.random() < 0.6 else 2
    cuts = sorted(rng.sample(range(2, spine_count - 1), district_count - 1))
    districts = [sum(index >= cut for cut in cuts) for index in range(spine_count)]
    mode_preferences = {
        "garrison": ("double-loaded", "formal-axis", "single-loaded"),
        "catacombs": ("tunnel-cluster", "suite", "single-loaded"),
        "grand-halls": ("formal-axis", "single-loaded", "suite"),
        "storehouse": ("service-bays", "double-loaded", "single-loaded"),
        "quarters": ("single-loaded", "suite", "double-loaded"),
        "stronghold": ("formal-axis", "double-loaded", "service-bays"),
        "vault": ("formal-axis", "suite", "service-bays"),
        "default": ("double-loaded", "single-loaded", "suite"),
    }[variant.name if variant.name in {
        "garrison", "catacombs", "grand-halls", "storehouse", "quarters",
        "stronghold", "vault"} else "default"]
    district_circulation: list[str] = []
    for _ in range(district_count):
        pool = [mode for mode in CIRCULATION_MODES
                if not district_circulation or mode != district_circulation[-1]]
        weights = [3 if mode in mode_preferences else 1 for mode in pool]
        district_circulation.append(rng.choices(pool, weights=weights, k=1)[0])
    specs = [RoomSpec(role, tier, district)
             for role, tier, district in zip(roles, tiers, districts)]
    edges = [(index, index + 1) for index in range(spine_count - 1)]
    loops: list[tuple[int, int]] = []
    critical = set(range(spine_count))
    groups: list[tuple[int, ...]] = []
    budget = min(3, 1 + (complexity >= 3) + (rng.random() < variant.extra_motif_chance))
    if number == 10:
        budget = max(2, budget)
    loop_realizations = {
        "axial-journey": ("asymmetric-detour", "short-room-loop"),
        "hub-relay": ("courtyard-circuit", "short-room-loop"),
        "offset-ladder": ("ladder-rung", "service-loop"),
        "clustered-chain": ("service-loop", "asymmetric-detour"),
        "nested-circuit": ("nested-room-loop", "courtyard-circuit"),
        "bounded-perimeter": ("bounded-perimeter",),
    }[progression_grammar]
    primary_loop = rng.choice(loop_realizations)
    primary_motif = ("courtyard" if "courtyard" in primary_loop else
                     "service" if "service" in primary_loop else
                     "ladder" if "ladder" in primary_loop else "ring")
    motifs = [primary_motif]
    motif_realizations = [primary_loop]
    remaining = ["hub", "wings", "gallery", "courtyard", "service"]
    rng.shuffle(remaining)
    family_preferences = {
        "throne-stronghold": ("hub", "wings"),
        "command-bunker": ("wings", "hub"),
        "laboratory-gauntlet": ("gallery", "wings"),
        "columned-fortress": ("hub", "gallery"),
        "central-duel": ("wings", "gallery"),
        "central-vault": ("hub", "wings"),
        "museum-circuit": ("gallery", "hub"),
        "nested-reliquary": ("gallery", "wings"),
        "abandoned-armory": ("wings", "gallery"),
        "treasure-palace": ("hub", "gallery"),
    }.get(special_family, ())
    for preferred in reversed(family_preferences):
        if preferred in remaining:
            remaining.remove(preferred)
            remaining.insert(0, preferred)
    for preferred in reversed(variant.motif_pref):
        if preferred in remaining:
            remaining.remove(preferred)
            remaining.insert(0, preferred)
    motifs += remaining[:budget - 1]
    motif_realizations += [f"{motif}-{rng.choice(('compact', 'offset', 'staggered'))}"
                           for motif in motifs[1:]]

    # The first motif always spends topology budget on a real reconvergence.
    pairs = [(i, j) for i in range(1, spine_count - 2)
             for j in range(i + 2, min(i + 4, spine_count - 1))]
    weights = [spine_count - abs((i + j) - (spine_count - 1)) for i, j in pairs]
    left, right = rng.choices(pairs, weights=weights, k=1)[0]
    parent = left
    # The reconvergence may add variety but must never be a faster route to
    # the elevator than the spine segment it parallels.
    ring_rooms = max(right - left - 1, rng.randrange(1, 3))
    if primary_loop in ("courtyard-circuit", "nested-room-loop", "bounded-perimeter"):
        ring_rooms = min(3, ring_rooms + 1)
    for _ in range(ring_rooms):
        node = len(specs)
        tier = "hall" if primary_loop == "courtyard-circuit" else "standard"
        specs.append(RoomSpec("ring", tier, districts[left], primary_motif))
        edges.append((parent, node))
        parent = node
    edges.append((parent, right)); loops.append((parent, right))

    middle_beats = list(range(1, 1 + beat_count))
    if "hub" in motifs:
        if number in (9, 10):
            # Special floors own a fixed dramatic anchor on the mandatory
            # route. A hub family grows optional wings from that anchor
            # without moving the boss arena or premium vault earlier.
            hub = next(index for index, spec in enumerate(specs)
                       if spec.tier == "anchor")
        else:
            hub = rng.choice([index for index in middle_beats
                              if specs[index].tier != "corridor"])
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

    if "courtyard" in motifs and primary_motif != "courtyard":
        parent = rng.choice(middle_beats)
        node = len(specs)
        specs.append(RoomSpec("branch", "hall", districts[parent], "courtyard"))
        edges.append((parent, node))
    if "service" in motifs and primary_motif != "service":
        left = rng.choice(middle_beats[:-2])
        right = min(middle_beats[-1], left + rng.randrange(2, 4))
        node = len(specs)
        specs.append(RoomSpec("branch", "corridor", districts[left], "service"))
        edges.extend(((left, node), (node, right)))
        loops.append((node, right))

    if rare_motif and number in (6, 7, 8, 9):
        parent = rng.choice(middle_beats)
        node = len(specs)
        specs.append(RoomSpec("branch", "motif", districts[parent], "swastika"))
        edges.append((parent, node))
        motifs.append("swastika")
        motif_realizations.append("swastika-room-profile")

    filler_tips: list[int] = []
    while len(specs) < target:
        suite_tips = [tip for tip in filler_tips
                      if district_circulation[specs[tip].district]
                      in ("suite", "tunnel-cluster")]
        if suite_tips and rng.random() < 0.35:
            parent = rng.choice(suite_tips)
            filler_tips.remove(parent)
        else:
            degrees = [sum(index in edge for edge in edges) for index in middle_beats]
            weights = []
            for index, degree in zip(middle_beats, degrees):
                mode = district_circulation[specs[index].district]
                corridor_bias = (3.0 if specs[index].tier == "corridor"
                                 and mode not in ("suite", "tunnel-cluster")
                                 else 1.6 if specs[index].tier == "corridor" else 1.0)
                weights.append(corridor_bias / degree)
            parent = rng.choices(middle_beats, weights=weights, k=1)[0]
        role = "closet" if rng.random() < variant.closet_weight else "branch"
        tier = "closet" if role == "closet" else rng.choice(("standard", "standard", "hall"))
        node = len(specs)
        specs.append(RoomSpec(role, tier, specs[parent].district, "filler"))
        edges.append((parent, node))
        filler_tips.append(node)
    if sum(spec.tier == "anchor" for spec in specs) != 1:
        raise ValueError("floor plan must have exactly one anchor")
    return FloorPlan(specs, edges, loops, tuple(motifs), frozenset(critical), tuple(groups),
                     skeleton, tuple(district_circulation), special_family,
                     progression_grammar, tuple(motif_realizations))


def _room_size(rng: random.Random, tier: str, number: int = 0) -> tuple[int, int]:
    bump = 2 if number == 10 else 0
    if tier == "anchor":
        if number == 9:
            return rng.randrange(14, 18), rng.randrange(14, 18)
        return rng.randrange(10 + bump, 14 + bump), rng.randrange(10 + bump, 14 + bump)
    if tier == "motif":
        return 15, 15
    if tier == "closet":
        return rng.randrange(4, 6), rng.randrange(4, 6)
    if tier == "corridor":
        # A circulation node is a traversable hallway, not a long combat
        # room. The major axis varies while the minor axis stays readable.
        major, minor = rng.randrange(8 + bump, 14 + bump), rng.randrange(3, 5)
        return (major, minor) if rng.random() < 0.5 else (minor, major)
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
    planned_parents = dict(parents)
    reparented: dict[int, int] = {}
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
            0 if plan.specs[item].motif == "swastika" else
            1 if plan.specs[parents[item]].role == "hub" else
            2 if item in grouped else 3 if plan.specs[item].role == "ring" else 4,
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
                if plan.specs[parent_index].tier == "corridor" and mode in (
                        "double-loaded", "single-loaded", "service-bays", "formal-axis"):
                    cross = ((0, 1), (0, -1)) if parent.w >= parent.h else ((1, 0), (-1, 0))
                    favored = ({cross[0]} if mode == "single-loaded" else set(cross))
                else:
                    favored = set(sides)
                side = rng.choices(
                    sides, weights=[(3 if s in favored else 0.5)
                                    / (1 + 5 * counts.get(s, 0)) for s in sides], k=1)[0]
                gap = rng.randrange(1, 4)
            candidate_size = sizes[index]
            if plan.specs[index].tier == "corridor":
                rw, rh = candidate_size
                if side[0] and rw < rh:
                    candidate_size = rh, rw
                elif side[1] and rh < rw:
                    candidate_size = rh, rw
            # Human mappers align rooms; jitter is the fallback once these
            # center and edge-flush placements have had a chance.
            jitters = (_snap_offsets(parent, *candidate_size, side, rng)
                       if attempt < 20 else
                       [rng.randrange(-6, 7) if index < spine_count else rng.randrange(-11, 12)])
            for jitter in jitters:
                candidate = adjacent(parent, candidate_size, side, gap, jitter)
                if legal(candidate):
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
    fake_door_placed = False
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

        # A fake door is rare architectural misdirection in a service room.
        # It is allowed only when one face is visible and solid rock continues
        # behind it, so it cannot leak into a neighboring space or progression.
        special_fake = (identity is not None and not fake_door_placed
                        and concept in {"storage", "supply-cache", "workshop",
                                        "checkpoint"}
                        and rng.random() < 0.035)

        if special_glass or place_landmark or special_fake:
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
            if special_fake:
                backed_runs = []
                for run_side, candidate in runs:
                    backed = []
                    for x, y in candidate:
                        floor_neighbors = [(nx, ny) for nx, ny in
                                           ((x + 1, y), (x - 1, y),
                                            (x, y + 1), (x, y - 1))
                                           if _is_floor(_at(tiles, nx, ny))]
                        if len(floor_neighbors) != 1:
                            continue
                        ix, iy = floor_neighbors[0]
                        outward = (x + (x - ix), y + (y - iy))
                        if (_is_floor(_at(tiles, *outward))
                                or _at(tiles, *outward) in DOORS):
                            continue
                        backed.append((x, y))
                    if backed:
                        backed_runs.append((run_side, backed))
                if backed_runs:
                    _, backed = max(backed_runs, key=lambda item: len(item[1]))
                    _set(tiles, *backed[len(backed) // 2], 13)
                    fake_door_placed = True
                    continue
                special_fake = False
                if not special_glass and not place_landmark:
                    continue

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
                  special_pushwall: tuple[int, int] | None = None
                  ) -> dict[tuple[int, int], str]:
    """Hang a landmark decor tile (banner, portrait, insignia) on every
    pushwall, the way the original episodes telegraph most of theirs. Runs
    after _apply_wall_theme so the theme can't repaint the hint, and prefers
    the floor theme's own decor accents so the hint matches the material.
    Falls back to a same-base sibling theme's accents rather than a hardcoded
    cross-family constant, so a hint tile can never mix material families."""
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
        if hints:
            hint = rng.choice(hints)
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
    return treatments


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


def _minimum_critical_route_rooms(roles: list[str] | tuple[str, ...]) -> int:
    """Require most of the progression spine, independent of side-room count.

    Optional density must not make a valid exit mathematically impossible.
    Roles used exclusively by optional graph nodes are excluded; a reassigned
    optional exit still adds itself to the requirement and its realized route.
    """
    optional_roles = {"ring", "branch", "closet"}
    spine_rooms = sum(role not in optional_roles for role in roles)
    return max(6, math.ceil(spine_rooms * 0.90))


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
    sides = [preferred, *[side for side in facings if side != preferred]]
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
        if name in ("officer", "ss") and near_door(x, y):
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
        budget = max(0, round(per_room * (0.4 if room == exit_room else pacing(depth))))
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
        cursor = 0
        for x, y in candidates[cursor:cursor + budget]:
            record = place_enemy(x, y, 0, name, family, room)
            placed_cells.append(record)
            if not any(_line_visible(entry, (x, y)) for entry in entries):
                hidden_cells.append((x, y))
            tier_counts[0] += 1
        cursor += budget
        # ECWolf's base translator treats +36 as the next cumulative skill
        # tier: skill 2 actors join the easy population on medium, and skill 3
        # actors join both on hard. They require their own cells in plane 2.
        extra = max(0, round(base_budget * (0.20 + progression * 0.12)))
        for tier in (1, 2):
            for x, y in candidates[cursor:cursor + extra]:
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
    if novelty is not None and rng.random() < 0.1:
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


@dataclass(frozen=True, slots=True)
class TraversalFrame:
    """The dominant path a player is expected to take through one room."""
    entries: tuple[tuple[int, int], ...]
    axis: tuple[int, int]
    stations: tuple[tuple[int, int], ...]
    station_axes: tuple[tuple[int, int], ...]
    path: tuple[tuple[int, int], ...]


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


def _room_traversal_frame(room: Room, tiles: list[int],
                          anchors: RoomAnchors | None = None) -> TraversalFrame:
    """Resolve doors into a stable visual axis and balanced decor stations.

    Opposing, widely separated doors win in multi-door rooms. A single door
    projects inward toward the room center; a doorless room falls back to its
    major axis. Stations are ordered midpoint-first, then at one-third and
    two-thirds, so the first matched pair bisects the most visible crossing.
    """
    anchors = anchors or _room_anchors(room, tiles)
    door_entries = list(anchors.door_entries)
    cx, cy = room.center
    if len(door_entries) >= 2:
        choices = list(combinations(door_entries, 2))

        def pair_score(pair):
            (first, first_in), (second, second_in) = pair
            opposite = first_in == (-second_in[0], -second_in[1])
            separation = abs(first[0] - second[0]) + abs(first[1] - second[1])
            midpoint_offset = abs(first[0] + second[0] - 2 * cx) + abs(
                first[1] + second[1] - 2 * cy)
            return opposite, separation, -midpoint_offset, first, second

        (start, start_in), (end, end_in) = max(choices, key=pair_score)
        if start_in == (-end_in[0], -end_in[1]):
            axis = start_in
        else:
            dx, dy = end[0] - start[0], end[1] - start[1]
            axis = ((1 if dx >= 0 else -1), 0) if abs(dx) >= abs(dy) else (
                0, (1 if dy >= 0 else -1))
        entries = (start, end)
    elif door_entries:
        start, axis = door_entries[0]
        end = (cx, cy)
        entries = (start,)
    else:
        axis = (1, 0) if room.w >= room.h else (0, 1)
        start = (room.x, cy) if axis[0] else (cx, room.y)
        end = (room.x + room.w - 1, cy) if axis[0] else (cx, room.y + room.h - 1)
        entries = ()

    previous: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
    queue = deque([start])
    while queue and end not in previous:
        x, y = queue.popleft()
        directions = sorted(((1, 0), (-1, 0), (0, 1), (0, -1)),
                            key=lambda step: abs(x + step[0] - end[0])
                            + abs(y + step[1] - end[1]))
        for dx, dy in directions:
            nxt = x + dx, y + dy
            if (nxt in previous
                    or not (room.x <= nxt[0] < room.x + room.w
                            and room.y <= nxt[1] < room.y + room.h)
                    or not _is_floor(_at(tiles, *nxt))):
                continue
            previous[nxt] = (x, y)
            queue.append(nxt)
    if end in previous:
        path = []
        cell: tuple[int, int] | None = end
        while cell is not None:
            path.append(cell)
            cell = previous[cell]
        path.reverse()
    else:
        path = [start, end]

    stations = []
    station_axes = []
    for numerator, denominator in ((1, 2), (1, 3), (2, 3)):
        index = min(len(path) - 1,
                    ((len(path) - 1) * numerator + denominator // 2) // denominator)
        station = path[index]
        if station not in stations:
            stations.append(station)
            before = path[max(0, index - 1)]
            after = path[min(len(path) - 1, index + 1)]
            dx, dy = after[0] - before[0], after[1] - before[1]
            local_axis = (((1 if dx >= 0 else -1), 0) if abs(dx) >= abs(dy) and dx
                          else (0, (1 if dy >= 0 else -1)) if dy else axis)
            station_axes.append(local_axis)
    return TraversalFrame(entries, axis, tuple(stations), tuple(station_axes),
                          tuple(path))


def _traversal_pair_candidates(room: Room, tiles: list[int], frame: TraversalFrame
                               ) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """Matched cells on opposite sides of the player's travel line.

    Larger offsets are preferred so lamps and furniture read as two balanced
    sides of an aisle. Exact floor/occupancy checks remain the caller's job.
    """
    pairs = []
    max_offset = max(room.w, room.h)
    for (sx, sy), local_axis in zip(frame.stations, frame.station_axes):
        # A doorless even-sized room has its visual axis between tiles. Keep
        # that half-tile center exact so opposite-wall pairs remain possible.
        if local_axis[0]:
            center2 = 2 * sy if frame.entries else 2 * room.y + room.h - 1
        else:
            center2 = 2 * sx if frame.entries else 2 * room.x + room.w - 1
        for offset in range(max_offset, -1, -1):
            low = center2 // 2 - offset
            high = (center2 + 1) // 2 + offset
            first, second = (((sx, low), (sx, high)) if local_axis[0]
                             else ((low, sy), (high, sy)))
            if first == second:
                continue
            if not all(room.x <= x < room.x + room.w
                       and room.y <= y < room.y + room.h
                       and _is_floor(_at(tiles, x, y))
                       for x, y in (first, second)):
                continue
            pair = (first, second)
            if pair not in pairs and pair[::-1] not in pairs:
                pairs.append(pair)
    return pairs


AUTHORED_PICKUP_TEMPLATES = frozenset({
    "wall-cache", "entry-staging", "recovery-station",
    "treasure-display", "corner-cache", "center-dais",
    "kennel-wall", "boss-arena-cross",
})


class _PlacementGrammar:
    """Commit sprites only through named, geometry-aware compositions.

    Randomness may select a valid template and its orientation, but this API
    never accepts a raw coordinate from a caller. Failed compositions move to
    another compatible room instead of falling back to scatter placement.
    """

    def __init__(self, rooms: list[Room], tiles: list[int], things: list[int],
                 reserved: set[tuple[int, int]], identities: list[RoomIdentity],
                 rng: random.Random, placements: list[SpritePlacement]):
        self.rooms = rooms
        self.tiles = tiles
        self.things = things
        self.reserved = reserved
        self.identities = identities
        self.rng = rng
        self.placements = placements
        self.last_template_by_district: dict[int, str] = {}

    @staticmethod
    def _line_offsets(count: int) -> tuple[int, ...]:
        return tuple(2 * index - (count - 1) for index in range(count))

    def _wall_backed(self, room: Room, cell: tuple[int, int]) -> bool:
        x, y = cell
        outside = []
        if x == room.x:
            outside.append((x - 1, y))
        if x == room.x + room.w - 1:
            outside.append((x + 1, y))
        if y == room.y:
            outside.append((x, y - 1))
        if y == room.y + room.h - 1:
            outside.append((x, y + 1))
        return any(not _is_floor(_at(self.tiles, *neighbor))
                   and _at(self.tiles, *neighbor) not in DOORS
                   for neighbor in outside)

    def _wall_lines(self, room: Room, count: int) -> list[tuple[tuple[int, int], ...]]:
        offsets = self._line_offsets(count)
        cx, cy = room.center
        return [
            tuple((cx + offset, room.y) for offset in offsets),
            tuple((cx + offset, room.y + room.h - 1) for offset in offsets),
            tuple((room.x, cy + offset) for offset in offsets),
            tuple((room.x + room.w - 1, cy + offset) for offset in offsets),
        ]

    def _corner_clusters(self, room: Room, count: int
                         ) -> list[tuple[tuple[int, int], ...]]:
        patterns = (
            ((room.x, room.y + 1), (room.x + 1, room.y),
             (room.x, room.y + 2), (room.x + 2, room.y)),
            ((room.x + room.w - 1, room.y + 1),
             (room.x + room.w - 2, room.y),
             (room.x + room.w - 1, room.y + 2),
             (room.x + room.w - 3, room.y)),
            ((room.x, room.y + room.h - 2),
             (room.x + 1, room.y + room.h - 1),
             (room.x, room.y + room.h - 3),
             (room.x + 2, room.y + room.h - 1)),
            ((room.x + room.w - 1, room.y + room.h - 2),
             (room.x + room.w - 2, room.y + room.h - 1),
             (room.x + room.w - 1, room.y + room.h - 3),
             (room.x + room.w - 3, room.y + room.h - 1)),
        )
        return [tuple(pattern[:count]) for pattern in patterns]

    def _center_daises(self, room: Room, count: int
                       ) -> list[tuple[tuple[int, int], ...]]:
        cx, cy = room.center
        patterns = (
            ((cx, cy), (cx - 1, cy), (cx + 1, cy), (cx, cy + 1)),
            ((cx, cy), (cx, cy - 1), (cx, cy + 1), (cx + 1, cy)),
        )
        return [tuple(pattern[:count]) for pattern in patterns]

    def _formations(self, room: Room, template: str, count: int
                    ) -> list[tuple[tuple[int, int], ...]]:
        anchors = _room_anchors(room, self.tiles)
        entries = [cell for cell, _ in anchors.door_entries] or [room.center]
        wall_lines = self._wall_lines(room, count)
        corners = self._corner_clusters(room, count)

        def entry_distance(cells: tuple[tuple[int, int], ...]) -> int:
            return min(abs(x - ex) + abs(y - ey)
                       for x, y in cells for ex, ey in entries)

        if template == "wall-cache":
            return wall_lines
        if template == "entry-staging":
            return sorted(wall_lines, key=lambda cells: (entry_distance(cells), cells))
        if template == "recovery-station":
            return sorted(corners, key=lambda cells: (-entry_distance(cells), cells))
        if template == "treasure-display":
            return wall_lines
        if template == "corner-cache":
            return corners
        if template == "center-dais":
            return self._center_daises(room, count)
        return []

    def place(self, room_index: int, items: tuple[int, ...], reason: str,
              templates: tuple[str, ...]) -> SpritePlacement | None:
        if not items or len(items) > 4:
            return None
        room = self.rooms[room_index]
        identity = self.identities[room_index]
        anchors = _room_anchors(room, self.tiles)
        ordered = [template for template in templates
                   if template in AUTHORED_PICKUP_TEMPLATES]
        previous = self.last_template_by_district.get(identity.district)
        if previous in ordered and len(ordered) > 1:
            ordered.remove(previous)
            ordered.append(previous)
        if len(ordered) > 1:
            offset = self.rng.randrange(len(ordered))
            ordered = ordered[offset:] + ordered[:offset]
        for template in ordered:
            valid = []
            for cells in self._formations(room, template, len(items)):
                if (len(set(cells)) != len(cells)
                        or any(cell in self.reserved or cell in anchors.keep_clear
                               or _at(self.things, *cell) != 0
                               or not _is_floor(_at(self.tiles, *cell))
                               for cell in cells)):
                    continue
                if template != "center-dais" and not all(
                        self._wall_backed(room, cell) for cell in cells):
                    continue
                valid.append(cells)
            if not valid:
                continue
            cells = self.rng.choice(valid)
            pieces = tuple((x, y, item) for (x, y), item in zip(cells, items))
            for x, y, item in pieces:
                _set(self.things, x, y, item)
                self.reserved.add((x, y))
            placement = SpritePlacement(reason, template, room_index, pieces)
            self.placements.append(placement)
            self.last_template_by_district[identity.district] = template
            return placement
        return None


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
    target_ratio = 1.15 + 0.05 * int(config.supplies)
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
        if not budget or not blocking:
            continue
        item = rng.choice(blocking)
        rng.shuffle(corners)
        for cornx, corny in corners:
            # A pair of identical potted plants packed into one corner reads
            # like a placement accident.  Plant concepts still own their
            # intended half of the room, but use one deliberate specimen;
            # the general mirrored-pair pass remains free to put plants on
            # opposing sides where a pair reads as composition.
            if item in (31, 34):
                if (cornx, corny) in free and try_place([(cornx, corny)], item):
                    break
                continue
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


_LIGHTING_OPTIONS: dict[str, tuple[tuple[str, float], ...]] = {
    "war-room": (("chandelier", 3.0), ("ceiling-lamp", 2.0), ("none", 0.5)),
    "trophy-hall": (("chandelier", 4.0), ("ceiling-lamp", 1.0), ("none", 0.5)),
    "gallery": (("chandelier", 3.0), ("ceiling-lamp", 1.0), ("none", 0.5)),
    "dining-hall": (("chandelier", 5.0), ("none", 0.5)),
    "officers-quarters": (("chandelier", 2.0), ("floor-lamp", 2.0), ("none", 0.5)),
    "lounge": (("chandelier", 2.0), ("floor-lamp", 3.0), ("none", 0.5)),
    "guardpost": (("floor-lamp", 3.0), ("ceiling-lamp", 2.0), ("none", 0.5)),
    "checkpoint": (("ceiling-lamp", 4.0), ("floor-lamp", 1.0), ("none", 0.5)),
    "armory": (("ceiling-lamp", 4.0), ("none", 1.0)),
    "training-room": (("ceiling-lamp", 4.0), ("none", 1.0)),
    "ready-room": (("ceiling-lamp", 2.0), ("floor-lamp", 1.0), ("none", 1.0)),
    "workshop": (("ceiling-lamp", 4.0), ("none", 1.0)),
    "mess-kitchen": (("ceiling-lamp", 5.0), ("none", 0.5)),
    "corridor": (("ceiling-lamp", 4.0), ("floor-lamp", 1.0), ("none", 1.0)),
    "interrogation-room": (("floor-lamp", 3.0), ("ceiling-lamp", 1.0), ("none", 1.0)),
    "courtyard": (("ceiling-lamp", 2.0), ("none", 2.0)),
    "grand": (("chandelier", 3.0), ("ceiling-lamp", 1.0), ("none", 0.5)),
}


def _lighting_family(concept: str, room: Room, rng: random.Random,
                     counts: Counter[str]) -> str:
    """Resolve one coherent fixture language for an authored room."""
    options = list(_LIGHTING_OPTIONS.get(concept, (("none", 1.0),)))
    if room.w < 6 or room.h < 6:
        options = [(family, weight) for family, weight in options
                   if family not in ("chandelier", "floor-lamp")]
        if not options:
            return "none"
    families = [family for family, _ in options]
    # Repetition is legal when the identity calls for it, but a floor-wide
    # monoculture receives a soft penalty rather than a deterministic cycle.
    weights = [weight / (1.0 + 0.35 * counts[family])
               for family, weight in options]
    chosen = rng.choices(families, weights=weights, k=1)[0]
    counts[chosen] += 1
    return chosen


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
                       landmark_frame_chance: float = 0.15,
                       notch_anchors: dict[int, tuple[tuple[int, int], ...]] | None = None,
                       traversal_pair_chance: float | None = None,
                       hallway_vine_budget: int = 0,
                       ) -> tuple[tuple[str, ...], tuple[VineScreen, ...]]:
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
    lighting_counts: Counter[str] = Counter()
    lighting_families = ["none"] * len(rooms)
    vine_screens: list[VineScreen] = []
    sky_composition_placed = False

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

        existing_lights = {
            _at(things, x, y)
            for y in range(room.y, room.y + room.h)
            for x in range(room.x, room.x + room.w)
            if _at(things, x, y) in LIGHTING_ITEMS}
        if existing_lights:
            compatible = [family for family, items in LIGHTING_FAMILY_ITEMS.items()
                          if existing_lights <= items]
            if not compatible:
                raise ValueError("authored room mixes incompatible lighting families")
            lighting = compatible[0]
            lighting_counts[lighting] += 1
        else:
            lighting = _lighting_family(concept, room, rng, lighting_counts)
        lighting_families[ridx] = lighting

        if room.w < 5 or room.h < 5:
            continue

        blocking = _DECOR_BLOCKING.get(concept,
                                       _DECOR_BLOCKING.get(theme, STATIC_BLOCKING))
        open_items = _DECOR_OPEN.get(concept,
                                     _DECOR_OPEN.get(theme, STATIC_OPEN))
        allowed_lights = LIGHTING_FAMILY_ITEMS[lighting]
        blocking = tuple(item for item in blocking
                         if item not in LIGHTING_ITEMS or item in allowed_lights)
        open_items = tuple(item for item in open_items
                           if item not in LIGHTING_ITEMS or item in allowed_lights)
        if lighting == "floor-lamp" and 26 not in blocking:
            blocking += (26,)
        elif lighting == "chandelier" and 27 not in open_items:
            open_items += (27,)
        elif lighting == "ceiling-lamp" and 37 not in open_items:
            open_items += (37,)
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
        traversal = _room_traversal_frame(room, tiles, anchors)
        travel_pairs = _traversal_pair_candidates(room, tiles, traversal)
        keep_clear = set(anchors.keep_clear)
        # Reachability alone still permits a table/barrel composition to
        # occupy the obvious route between two doors when a cramped detour
        # remains around it. Preserve the complete authored traversal lane;
        # blocking decor belongs beside that path, never on top of it.
        if traversal.entries:
            keep_clear.update(traversal.path)
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

        def _wall_backed(cell: tuple[int, int]) -> bool:
            """True only on the room perimeter with a solid wall behind it."""
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

        room_blocked: list[tuple[int, int]] = []

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
            if any(item in LIGHTING_ITEMS and item not in allowed_lights
                   for _, item in pieces):
                return False
            # Suits of armor, flags, and spear racks are wall displays,
            # never freestanding furniture.
            if any(item in (39, 62, 69) and not _wall_backed(cell)
                   for cell, item in pieces):
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
            if item in LIGHTING_ITEMS and item not in allowed_lights:
                return False
            if static_headroom <= 0 or _at(things, *cell) != 0:
                return False
            _set(things, *cell, item)
            reserved.add(cell)
            free.discard(cell)
            edge_free.discard(cell)
            static_headroom -= 1
            return True

        # A sky tile is a view beyond the building, never ordinary wallpaper.
        # Every tile is completely covered by its own blocking pillar and the
        # ray behind the wall must remain solid all the way to the map edge,
        # proving this is an outside-most face rather than a leak into another
        # room. One symmetric colonnade is enough to establish the motif.
        if (not sky_composition_placed
                and concept in {"courtyard", "gallery", "trophy-hall"}
                and min(room.w, room.h) >= 7
                and rng.random() < (0.26 if concept == "courtyard" else 0.08)):
            side_candidates: list[list[tuple[tuple[int, int], tuple[int, int]]]] = []
            side_specs = (
                [((x, room.y), (x, room.y - 1))
                 for x in range(room.x + 1, room.x + room.w - 1)],
                [((x, room.y + room.h - 1), (x, room.y + room.h))
                 for x in range(room.x + 1, room.x + room.w - 1)],
                [((room.x, y), (room.x - 1, y))
                 for y in range(room.y + 1, room.y + room.h - 1)],
                [((room.x + room.w - 1, y), (room.x + room.w, y))
                 for y in range(room.y + 1, room.y + room.h - 1)],
            )
            for side in side_specs:
                eligible = []
                for interior_cell, wall_cell in side:
                    ix, iy = interior_cell
                    wx, wy = wall_cell
                    dx, dy = wx - ix, wy - iy
                    if (interior_cell not in edge_free
                            or _at(tiles, wx, wy) in DECOR_WALLS | SPECIAL_WALL_TILES
                            or _is_floor(_at(tiles, wx, wy))
                            or _at(tiles, wx, wy) in DOORS):
                        continue
                    outside_clear = True
                    ox, oy = wx + dx, wy + dy
                    while 0 <= ox < GRID and 0 <= oy < GRID:
                        if (_is_floor(_at(tiles, ox, oy))
                                or _at(tiles, ox, oy) in DOORS):
                            outside_clear = False
                            break
                        ox += dx
                        oy += dy
                    if outside_clear:
                        eligible.append((interior_cell, wall_cell))
                if len(eligible) >= 4:
                    side_candidates.append(eligible)
            rng.shuffle(side_candidates)
            for side in side_candidates:
                mid = len(side) // 2
                offset = max(1, len(side) // 4)
                pair = [side[mid - offset], side[mid + offset]]
                pillars = [interior for interior, _ in pair]
                if _try_place(pillars, 30):
                    for _, wall_cell in pair:
                        _set(tiles, *wall_cell, 16)
                    sky_composition_placed = True
                    break

        # Vines are complete architectural screens, never loose foliage. A
        # room screen spans from one bounding wall to the opposite wall and
        # crosses the dominant travel axis. Placement is atomic.
        if (concept in VINE_SCREEN_CONCEPTS and room.w >= 9 and room.h >= 8
                and rng.random() < 0.24):
            if traversal.axis[0]:
                offsets = (room.x + room.w // 3,
                           room.x + (2 * room.w) // 3)
                screen_candidates = [tuple((x, y)
                                           for y in range(room.y, room.y + room.h))
                                     for x in offsets]
            else:
                offsets = (room.y + room.h // 3,
                           room.y + (2 * room.h) // 3)
                screen_candidates = [tuple((x, y)
                                           for x in range(room.x, room.x + room.w))
                                     for y in offsets]
            rng.shuffle(screen_candidates)
            for cells in screen_candidates:
                if (static_headroom < len(cells)
                        or any(not _is_floor(_at(tiles, *cell))
                               or _at(things, *cell) != 0
                               or cell in reserved or cell in keep_clear
                               for cell in cells)):
                    continue
                for cell in cells:
                    _set(things, *cell, 70)
                    reserved.add(cell)
                    free.discard(cell)
                    edge_free.discard(cell)
                static_headroom -= len(cells)
                vine_screens.append(VineScreen("room-divider", ridx, cells))
                break

        # Mirrored notches are architectural display bays, never empty bites.
        # Every anchor in a room receives the same compact, theme-compatible
        # prop so the geometry and decoration read as one authored motif.
        room_notches = list((notch_anchors or {}).get(ridx, ()))
        if room_notches:
            compact = tuple(item for item in blocking
                            if item in (26, 31, 34, 35, 58, 62, 69))
            notch_item = rng.choice(compact or (35,))
            if not _try_place(room_notches, notch_item):
                # A non-blocking ground accent preserves the mirrored intent
                # when traffic or reachability makes solid props unsuitable.
                ground = tuple(item for item in open_items
                               if item in (23, 32, 42, 46, 61, 64, 65, 66, 70))
                if ground:
                    accent = rng.choice(ground)
                    if all(cell not in keep_clear and _at(things, *cell) == 0
                           for cell in room_notches):
                        for cell in room_notches:
                            _place_open(cell, accent)

        pair_budget = max(1, round((2 if room.w >= 8 and room.h >= 8 else 1) * density))
        pairs_placed = 0
        concept_frames = {
            "war-room": (39, 62), "armory": (39, 62), "guardpost": (26,),
            "lounge": (31, 34, 35), "mess-kitchen": (35,),
            "courtyard": (31, 34), "checkpoint": (26, 62),
            "trophy-hall": (39, 62), "gallery": (34, 39, 62),
            "dining-hall": (35,), "officers-quarters": (34, 35),
        }
        frame_pool = tuple(
            item for item in concept_frames.get(
                concept, tuple(item for item in blocking if item in _FRAMEABLE))
            if item not in LIGHTING_ITEMS or item in allowed_lights)
        if not frame_pool:
            frame_pool = tuple(item for item in blocking
                               if item not in LIGHTING_ITEMS) or (35,)

        # The primary matched composition follows the route through the room,
        # not an arbitrary room half. In a two-door hall this places one prop
        # on each side of the aisle at the same travel depth. Formal and
        # circulation spaces use the rule strongly; irregular utility rooms
        # retain more freedom so the result does not become formulaic.
        formal_concepts = {
            "corridor", "checkpoint", "guardpost", "war-room",
            "trophy-hall", "gallery", "dining-hall", "courtyard",
        }
        travel_chance = (traversal_pair_chance
                         if traversal_pair_chance is not None else
                         0.90 if len(traversal.entries) >= 2
                         and (tier in ("corridor", "hall")
                              or concept in formal_concepts)
                         else 0.45 if len(traversal.entries) >= 2 else 0.20)
        if (travel_pairs and pairs_placed < pair_budget
                and rng.random() < travel_chance):
            # Traversal pairs are room furniture, not landmark frames.  Use
            # the room's own blocking palette when no concept-specific pair
            # palette exists; the landmark-frame fallback is a floor lamp,
            # which would otherwise leak into themes such as jails.
            pair_items = list(dict.fromkeys(
                concept_frames.get(concept, blocking)))
            rng.shuffle(pair_items)
            placed_travel_pair = False
            for pair in travel_pairs:
                for item in pair_items:
                    if item in (39, 62, 69) and not all(
                            _wall_backed(cell) for cell in pair):
                        continue
                    if _try_place(list(pair), item):
                        pairs_placed += 1
                        placed_travel_pair = True
                        break
                if placed_travel_pair:
                    break

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
                "training-room": [((room.x, cy), 69),
                                  ((room.x + room.w - 2, cy), 36)],
                "armory": [((room.x, cy), 69),
                            ((room.x + room.w - 1, cy), 69)],
                "guardpost": [((room.x + 1, room.y + 1), 26),
                               ((room.x + room.w - 2, room.y + 1), 26)],
                "checkpoint": [((room.x, cy), 62)],
                "war-room": [((room.x, room.y + 1), 39),
                              ((room.x + room.w - 1, room.y + 1), 39)],
                "trophy-hall": [((room.x, cy), 39),
                                ((room.x + room.w - 1, cy), 62)],
                "courtyard": [((cx, cy), 59)],
                "storage": [((room.x + 1, room.y + 1), 58),
                             ((room.x + 2, room.y + 1), 58)],
                "supply-cache": [((room.x + 1, room.y + 1), 24),
                                  ((room.x + 2, room.y + 1), 58)],
                "workshop": [((room.x + 1, cy), 36),
                             ((room.x + room.w - 1, cy), 69)],
                "lounge": [((cx, cy), 25)],
                "gallery": [((room.x, cy), 39)],
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
                if signature:
                    if any(item in LIGHTING_ITEMS and item not in allowed_lights
                           for _, item in signature):
                        signature = None
                if signature:
                    matched_item = (signature[0][1] if len(signature) == 2
                                    and signature[0][1] == signature[1][1]
                                    else None)
                    if matched_item is not None and travel_pairs:
                        # A matched signature is still a matched composition:
                        # it may move to the traversal frame, but may not fall
                        # back to two pieces stranded on one side of the aisle.
                        placed_signature = False
                        for pair in travel_pairs:
                            if _try_place(list(pair), matched_item):
                                pairs_placed += 1
                                placed_signature = True
                                break
                        if not placed_signature and _try_place_items(signature):
                            pairs_placed += 1
                    elif _try_place_items(signature):
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
        if blocking and pairs_placed < pair_budget and (
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
        if zones:
            zones = tuple((tuple(item for item in solid
                                 if item not in LIGHTING_ITEMS
                                 or item in allowed_lights),
                           tuple(item for item in open_
                                 if item not in LIGHTING_ITEMS
                                 or item in allowed_lights))
                          for solid, open_ in zones)
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
            if pairs_placed < pair_budget and blocking:
                band = [cell for cell in free if _near_wall(*cell)]
                x_pairs = [((x, y), (2 * cx - x, y)) for x, y in band
                           if x < cx and (2 * cx - x, y) in free]
                y_pairs = [((x, y), (x, 2 * cy - y)) for x, y in band
                           if y < cy and (x, 2 * cy - y) in free]
                geometric_pairs = x_pairs + y_pairs
                rng.shuffle(geometric_pairs)
                # Travel-aware pairs stay ahead of room-center symmetry. The
                # latter remains a fallback when doors are absent or the aisle
                # is occupied by a stronger room signature.
                all_pairs = travel_pairs + [pair for pair in geometric_pairs
                                            if pair not in travel_pairs
                                            and pair[::-1] not in travel_pairs]
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
    straight_segments: list[tuple[int, list[tuple[int, int]]]] = []
    for path_index, path in enumerate(paths or ()):
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
            straight_segments.append((path_index, segment))

    # The campaign scheduler nominates at most one floor for hallway
    # overgrowth. A composition fills the longitudinal safe center of a
    # one-cell-wide corridor; it never degrades to the old isolated singleton.
    # Existing actors beyond an endpoint or around a bend rank the corridor
    # first, turning the foliage into sightline cover without inventing an
    # encounter in the decoration pass.
    enemy_cells = [(index % GRID, index // GRID)
                   for index, item in enumerate(things) if item in ENEMY_CODES]
    vine_candidates: list[tuple[int, int, tuple[tuple[int, int], ...],
                                tuple[int, int] | None]] = []
    for path_index, segment in straight_segments:
        cells = tuple(segment[1:-1])
        if len(cells) < 3 or static_headroom < len(cells):
            continue
        horizontal = cells[0][1] == cells[-1][1]
        if any(cell in reserved or _at(things, *cell) != 0 for cell in cells):
            continue
        if any(any(_is_floor(_at(tiles, *side)) or _at(tiles, *side) in DOORS
                   for side in (((x, y - 1), (x, y + 1)) if horizontal else
                                ((x - 1, y), (x + 1, y))))
               for x, y in cells):
            continue
        if any(any(_at(tiles, x + dx, y + dy) in DOORS
                   for dx in range(-2, 3) for dy in range(-2, 3)
                   if abs(dx) + abs(dy) <= 2) for x, y in cells):
            continue
        nearby = [(abs(actor[0] - endpoint[0]) + abs(actor[1] - endpoint[1]), actor)
                  for actor in enemy_cells for endpoint in (cells[0], cells[-1])
                  if actor not in cells
                  and abs(actor[0] - endpoint[0]) + abs(actor[1] - endpoint[1]) <= 6]
        anchor = min(nearby, default=(99, None))[1]
        around_bend = bool(anchor and not (
            anchor[1] == cells[0][1] if horizontal else anchor[0] == cells[0][0]))
        score = 2 if around_bend else 1 if anchor else 0
        vine_candidates.append((score, path_index, cells, anchor))

    rng.shuffle(vine_candidates)
    vine_candidates.sort(key=lambda item: (-item[0], -len(item[2])))
    chosen_path: int | None = None
    hallway_runs_placed = 0
    for score, path_index, cells, anchor in vine_candidates:
        if hallway_runs_placed >= hallway_vine_budget:
            break
        if chosen_path is not None and path_index != chosen_path:
            continue
        if static_headroom < len(cells) or any(_at(things, *cell) for cell in cells):
            continue
        for cell in cells:
            _set(things, *cell, 70)
            reserved.add(cell)
        static_headroom -= len(cells)
        vine_screens.append(VineScreen("hallway-run", -1, cells, anchor))
        chosen_path = path_index
        hallway_runs_placed += 1

    for _, segment in straight_segments:
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

    return tuple(lighting_families), tuple(vine_screens)


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
    door_zones: set[tuple[int, int]] = set()
    paths = [_carve_connection(tiles, rooms[a], rooms[b], rng, complexity, door_zones)
             for a, b in edges]
    _widen_corridors(tiles, rooms, paths, rng, widen_chance=floor_variant.widen_chance)
    first_neighbor = next((second if first == 0 else first
                           for first, second in edges if 0 in (first, second)), 1)
    arrival = _place_arrival_elevator(
        tiles, rooms[0], rooms[first_neighbor].center, rng, floor_variant.name)
    start = arrival.player
    _set(things, *start, PLAYER_START_CODES[arrival.facing])
    if arrival.item is not None:
        _set(things, *arrival.item)
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
    minimum_route_rooms = _minimum_critical_route_rooms(roles)
    required_post_anchor = next((index for index, role in enumerate(roles)
                                 if role in ("victory", "recovery")), None)
    exit_candidates = []
    for room_index in range(1, len(rooms)):
        route = _room_graph_path(len(rooms), edges, room_index)
        if (anchor_index not in route[:-1] or room_index == anchor_index
                or len(route) < minimum_route_rooms
                or center_distances[room_index] / deepest_center < 0.75):
            continue
        if required_post_anchor is not None and required_post_anchor not in route[:-1]:
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
    notch_cells = {cell for cells in notch_anchors.values() for cell in cells}
    reserved = {start, *arrival.clearance, *arrival.car_cells,
                exit_stand, *notch_cells}
    rewards: list[tuple[int, int]] = []
    secret_variants: list[str] = []
    secret_details: list[SecretDetail] = []
    shortcut_pushwalls: list[tuple[int, int]] = []
    secret_protected: set[tuple[int, int]] = set(arrival.footprint)
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
    pickup_placements: list[SpritePlacement] = []
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
    secret_hints = _hint_secrets(tiles, things, component_of, group_theme, rng,
                                 special_pushwall=exit_pushwall)
    if exit_pushwall is not None and secret_hints.get(exit_pushwall) != "symmetric-landmark":
        raise ValueError("secret elevator host cannot support its landmark hint")
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
        notch_anchors=notch_anchors, hallway_vine_budget=hallway_vine_budget)
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
                          shape_target=shape_target)
    validate_map(result)
    result.critique = _critique(result)
    return result


def validate_map(level: GeneratedMap) -> None:
    if len(level.tiles) != GRID * GRID or len(level.things) != GRID * GRID:
        raise ValueError("invalid plane dimensions")
    if 63 in level.things:
        raise ValueError("Call Apogee decoration is forbidden")
    if (level.number < PURPLE_MIN_FLOOR
            and any(tile in {19, 25} for tile in level.tiles)):
        raise ValueError("purple wall material appears before the late campaign")
    if level.arrival is None:
        raise ValueError("floor has no arrival elevator")
    arrival = level.arrival
    facings = ((0, -1), (1, 0), (0, 1), (-1, 0))
    inward = facings[arrival.facing]
    outward = (-inward[0], -inward[1])
    inside = arrival.kind.startswith("inside-")
    expected_player = (arrival.portal[0] + 2 * (outward[0] if inside else inward[0]),
                       arrival.portal[1] + 2 * (outward[1] if inside else inward[1]))
    if (arrival.kind not in {"outside-empty", "outside-supply", "inside-closed"}
            or arrival.player != level.start or level.start != expected_player
            or _at(level.things, *level.start) != PLAYER_START_CODES[arrival.facing]
            or sum(thing in PLAYER_START_CODES for thing in level.things) != 1):
        raise ValueError("player does not face away from the arrival elevator")
    if (any(_at(level.tiles, *cell) == ELEVATOR_TILE
            for cell in arrival.footprint)
            or not all(_is_floor(_at(level.tiles, *cell))
                       and _at(level.things, *cell) == 0
                       for cell in arrival.clearance)):
        raise ValueError("arrival elevator contains a switch or blocked threshold")
    car_cells = tuple((arrival.portal[0] + depth * outward[0],
                       arrival.portal[1] + depth * outward[1])
                      for depth in (1, 2))
    if (arrival.car_cells != car_cells
            or any(not _is_floor(_at(level.tiles, *cell)) for cell in car_cells)):
        raise ValueError("arrival elevator car has invalid floor geometry")
    expected_portal = DOOR_ELEVATOR if inward[0] else DOOR_ELEVATOR_NS
    if _at(level.tiles, *arrival.portal) != expected_portal:
        raise ValueError("arrival car lacks its normal elevator door")
    px, py = -outward[1], outward[0]
    dressed = {
        (arrival.portal[0] + depth * outward[0] + side * px,
         arrival.portal[1] + depth * outward[1] + side * py)
        for depth in (1, 2, 3) for side in (-1, 1)}
    dressed.add((arrival.portal[0] + 3 * outward[0],
                 arrival.portal[1] + 3 * outward[1]))
    if any(_at(level.tiles, *cell) != DUMMY_ELEVATOR_TILE
           for cell in dressed):
        raise ValueError("arrival car does not use inert native panels")
    if arrival.item is not None:
        if (arrival.kind != "outside-supply"
                or arrival.item[:2] not in arrival.car_cells
                or arrival.item[2] not in PICKUP_CODES
                or _at(level.things, *arrival.item[:2]) != arrival.item[2]):
            raise ValueError("arrival car item lacks contextual provenance")
    elif arrival.kind == "outside-supply":
        raise ValueError("staged arrival car has no item")
    allowed_things = {level.start}
    if arrival.item is not None:
        allowed_things.add(arrival.item[:2])
    if any(_at(level.things, *cell) and cell not in allowed_things
           for cell in arrival.car_cells):
        raise ValueError("arrival car contains an unexplained object")
    for cell in arrival.footprint:
        if cell in dressed or cell in arrival.car_cells or cell == arrival.portal:
            continue
        if _is_floor(_at(level.tiles, *cell)) or _at(level.tiles, *cell) in DOORS:
            raise ValueError("arrival elevator is not rock bounded")

    if len(level.guard_recesses) > 1:
        raise ValueError("guard recesses dominate the floor")
    for recess in level.guard_recesses:
        if (not 0 <= recess.room_index < len(level.rooms)
                or level.room_tiers[recess.room_index] not in ("corridor", "hall")
                or recess.actor_cell not in recess.cells
                or any(not _is_floor(_at(level.tiles, *cell)) for cell in recess.cells)
                or _at(level.things, *recess.actor_cell) not in ENEMY_CODES):
            raise ValueError("guard recess lacks its owned hallway ambush")
        room = level.rooms[recess.room_index]
        first, second = recess.cells
        mirrored = ((first[0] == second[0]
                     and {first[1], second[1]} == {room.y - 1, room.y + room.h})
                    or (first[1] == second[1]
                        and {first[0], second[0]} == {room.x - 1,
                                                      room.x + room.w}))
        if not mirrored:
            raise ValueError("hallway guard recesses are not mirrored")
        if any(sum(_is_floor(_at(level.tiles, cell[0] + dx, cell[1] + dy))
                   or _at(level.tiles, cell[0] + dx, cell[1] + dy) in DOORS
                   for dx, dy in facings) != 1 for cell in recess.cells):
            raise ValueError("guard recess is not a blind one-cell pocket")

    if len(level.guard_galleries) > 1:
        raise ValueError("guard galleries dominate the floor")
    for gallery in level.guard_galleries:
        if (not 0 <= gallery.room_index < len(level.rooms)
                or level.room_shapes[gallery.room_index] != "rectangle"
                or gallery.treatment != 30
                or len(gallery.actor_cells) != 2):
            raise ValueError("guard gallery lacks a symmetric architectural host")
        room = level.rooms[gallery.room_index]
        horizontal = len({y for _, y in gallery.screen}) == 1
        vertical = len({x for x, _ in gallery.screen}) == 1
        expected_screen = (
            {(x, gallery.screen[0][1]) for x in range(room.x, room.x + room.w)}
            if horizontal else
            {(gallery.screen[0][0], y) for y in range(room.y, room.y + room.h)})
        if (not (horizontal or vertical)
                or set(gallery.screen) != expected_screen
                or any(_at(level.things, *cell) != gallery.treatment
                       for cell in gallery.screen)):
            raise ValueError("guard gallery does not have one complete matched screen")
        first, second = gallery.actor_cells
        mirrored = ((horizontal and first[1] == second[1]
                     and first[0] + second[0] == 2 * room.center[0])
                    or (vertical and first[0] == second[0]
                        and first[1] + second[1] == 2 * room.center[1]))
        if (not mirrored or any(_at(level.things, *cell) not in ENEMY_CODES
                                for cell in gallery.actor_cells)
                or any(cell not in gallery.rear_cells for cell in gallery.actor_cells)):
            raise ValueError("guard gallery actors are not a matched firing pair")
        collision_reachable = _reachable(
            level.tiles, level.start, locked_open=True, blocked=set(gallery.screen))
        if any(cell in collision_reachable for cell in gallery.rear_cells):
            raise ValueError("guard gallery is physically accessible")
        if any(_at(level.things, *cell) in PICKUP_CODES | {GOLD_KEY, SILVER_KEY}
               for cell in gallery.rear_cells):
            raise ValueError("inaccessible guard gallery contains a pickup")
        dx, dy = facings[gallery.facing]
        for actor in gallery.actor_cells:
            x, y = actor
            crossed = None
            for _ in range(max(room.w, room.h)):
                x, y = x + dx, y + dy
                if (x, y) in gallery.screen:
                    crossed = (x, y)
                    break
                if not _is_floor(_at(level.tiles, x, y)):
                    break
            if crossed is None:
                raise ValueError("guard gallery actor cannot fire through its screen")

    allowed_encounters = {"visible-sentry", "blind-corner-ambush",
                          "staggered-flank", "strongpoint", "objective-guard",
                          "patrol", "boss-support", "novelty", "guard-gallery"}
    tracked_actors: dict[tuple[int, int], int] = {}
    for encounter in level.encounters:
        if (encounter.template not in allowed_encounters or not encounter.cells
                or encounter.family not in ("guard", "dog", "officer", "ss", "novelty")
                or not -1 <= encounter.room_index < len(level.rooms)):
            raise ValueError("actor has invalid encounter provenance")
        for x, y, actor in encounter.cells:
            if (actor not in ENEMY_CODES or _at(level.things, x, y) != actor
                    or (x, y) in tracked_actors):
                raise ValueError("encounter provenance disagrees with the things plane")
            tracked_actors[(x, y)] = actor
        if not set(encounter.hidden_cells) <= {
                (x, y) for x, y, _ in encounter.cells}:
            raise ValueError("encounter records a hidden actor it does not own")
        if bool(encounter.patrol_kind) != bool(encounter.patrol_path):
            raise ValueError("patrol encounter metadata is incomplete")
    expected_actors = {(index % GRID, index // GRID): thing
                       for index, thing in enumerate(level.things)
                       if thing in ENEMY_CODES and thing not in BOSSES}
    if tracked_actors != expected_actors:
        raise ValueError("floor contains an actor outside an encounter composition")
    critical_ambushes = sorted(
        level.critical_route.index(encounter.room_index)
        for encounter in level.encounters
        if encounter.template == "blind-corner-ambush"
        and encounter.room_index in level.critical_route)
    if any(second - first <= 1 for first, second in zip(
            critical_ambushes, critical_ambushes[1:])):
        raise ValueError("critical route repeats ambushes without a recovery beat")
    ordinary_count = len(expected_actors)
    moving_count = sum(_patrol_actor_direction(actor) is not None
                       for actor in expected_actors.values())
    if (level.patrol_target >= 0.08 and ordinary_count >= 8 and moving_count == 0):
        raise ValueError("patrol setting produced an entirely stationary floor")
    if (level.patrol_target >= 0.15 and ordinary_count >= 12
            and moving_count / ordinary_count < level.patrol_target * 0.45):
        raise ValueError("realized patrol activity is far below its target")
    if level.room_tiers:
        if (len(level.room_tiers) != len(level.rooms)
                or len(level.room_roles) != len(level.rooms)):
            raise ValueError("room circulation metadata is incomplete")
        if level.circulation_skeleton not in CIRCULATION_SKELETONS:
            raise ValueError("unknown circulation skeleton")
        if level.progression_grammar not in PROGRESSION_GRAMMARS:
            raise ValueError("unknown progression grammar")
        if (not level.district_circulation
                or any(mode not in CIRCULATION_MODES
                       for mode in level.district_circulation)):
            raise ValueError("unknown district circulation mode")
        corridor_indices = {index for index, tier in enumerate(level.room_tiers)
                            if tier == "corridor"}
        if len(corridor_indices) < 2:
            raise ValueError("floor has no meaningful circulation hierarchy")
        if any(max(level.rooms[index].w, level.rooms[index].h)
               < 2 * min(level.rooms[index].w, level.rooms[index].h)
               for index in corridor_indices):
            raise ValueError("circulation node reads as a room, not a hallway")
        degrees = Counter(index for edge in level.edges for index in edge)
        if any(degrees[index] < 2 for index in corridor_indices):
            raise ValueError("circulation hallway ends without a destination")
        mediated = sum(first in corridor_indices or second in corridor_indices
                       for first, second in level.edges) / max(1, len(level.edges))
        if not 0.25 <= mediated <= 0.70:
            raise ValueError("corridor-mediated connection ratio is outside its quality band")

    if level.room_shapes:
        if len(level.room_shapes) != len(level.rooms):
            raise ValueError("room-shape metadata is incomplete")
        allowed_shapes = {"rectangle", "mirrored-notch", "stepped-cross",
                          "single-chamfer", "l-shaped", "shallow-t",
                          "paired-side-bays", "paired-end-bays",
                          "offset-side-bay", "swastika-profile"}
        if any(shape not in allowed_shapes and not shape.startswith("boss-")
               for shape in level.room_shapes):
            raise ValueError("unknown shaped-room family")
        shaped = sum(shape != "rectangle" for shape in level.room_shapes)
        if shaped > math.ceil(len(level.rooms) * 0.60):
            raise ValueError("non-rectangular rooms dominate the floor")
        counts = Counter(shape for shape in level.room_shapes
                         if shape != "rectangle" and not shape.startswith("boss-"))
        if shaped >= 4 and counts and max(counts.values()) / shaped > 0.50:
            raise ValueError("one shaped-room family dominates the floor")

    if level.rare_motif is not None:
        detail = level.rare_motif
        if (detail.kind != "swastika" or level.number not in (6, 7, 8, 9)
                or detail.room_index in level.critical_route
                or detail.room_index == level.boss_arena_room
                or any(objective.host_room == detail.room_index
                       for objective in level.key_objectives)
                or len(detail.endpoints) != 4):
            raise ValueError("rare plan motif owns progression or invalid geometry")
        if any(not _is_floor(_at(level.tiles, *cell)) for cell in detail.endpoints):
            raise ValueError("rare plan motif endpoint is not traversable")

    if level.lighting_families:
        if len(level.lighting_families) != len(level.rooms):
            raise ValueError("room-lighting metadata is incomplete")
        for room, family in zip(level.rooms, level.lighting_families):
            if family not in LIGHTING_FAMILY_ITEMS:
                raise ValueError("unknown room lighting family")
            fixtures = {_at(level.things, x, y)
                        for y in range(room.y, room.y + room.h)
                        for x in range(room.x, room.x + room.w)
                        if _at(level.things, x, y) in LIGHTING_ITEMS}
            if not fixtures <= LIGHTING_FAMILY_ITEMS[family]:
                raise ValueError("room mixes incompatible lighting families")

    # Special wall graphics are complete architectural compositions, never
    # members of the ordinary material scatter.
    fake_doors = [(index % GRID, index // GRID)
                  for index, tile in enumerate(level.tiles)
                  if tile == 13 and level.things[index] != PUSHWALL]
    if len(fake_doors) > 1:
        raise ValueError("fake doors dominate the floor")
    for x, y in fake_doors:
        visible_from = [(nx, ny) for nx, ny in
                        ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))
                        if _is_floor(_at(level.tiles, nx, ny))]
        if len(visible_from) != 1:
            raise ValueError("fake door exposes more than one wall face")
        ix, iy = visible_from[0]
        backing = (x + (x - ix), y + (y - iy))
        if (_is_floor(_at(level.tiles, *backing))
                or _at(level.tiles, *backing) in DOORS):
            raise ValueError("fake door has no solid architectural backing")

    sky_walls = [(index % GRID, index // GRID)
                 for index, tile in enumerate(level.tiles) if tile == 16]
    if sky_walls and (len(sky_walls) < 2 or len(sky_walls) % 2):
        raise ValueError("sky vista is not a matched pillar composition")
    for x, y in sky_walls:
        visible_from = [(nx, ny) for nx, ny in
                        ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))
                        if _is_floor(_at(level.tiles, nx, ny))]
        if len(visible_from) != 1:
            raise ValueError("sky wall is not on an outside-most face")
        interior = visible_from[0]
        if _at(level.things, *interior) != 30:
            raise ValueError("sky wall is not completely hidden by its pillar")
        dx, dy = x - interior[0], y - interior[1]
        ox, oy = x + dx, y + dy
        while 0 <= ox < GRID and 0 <= oy < GRID:
            if (_is_floor(_at(level.tiles, ox, oy))
                    or _at(level.tiles, ox, oy) in DOORS):
                raise ValueError("sky vista leaks into interior architecture")
            ox += dx
            oy += dy

    glass_walls = {(index % GRID, index // GRID)
                   for index, tile in enumerate(level.tiles) if tile == 33}
    accounted_glass: set[tuple[int, int]] = set()
    for room in level.rooms:
        sides = (
            {(x, room.y - 1) for x in range(room.x - 1, room.x + room.w + 1)},
            {(x, room.y + room.h) for x in range(room.x - 1, room.x + room.w + 1)},
            {(room.x - 1, y) for y in range(room.y, room.y + room.h)},
            {(room.x + room.w, y) for y in range(room.y, room.y + room.h)},
        )
        room_glass = glass_walls & set().union(*sides)
        if not room_glass:
            continue
        if len(room_glass) % 2:
            raise ValueError("stained glass is not a matched wall composition")
        if any(not any(other != cell and
                       (other[0] == cell[0] or other[1] == cell[1])
                       for other in room_glass) for cell in room_glass):
            raise ValueError("stained glass has an isolated panel")
        accounted_glass.update(room_glass)
    if accounted_glass != glass_walls:
        raise ValueError("stained glass has no owning formal room")

    spear_positions = [(index % GRID, index // GRID)
                       for index, thing in enumerate(level.things) if thing == 69]
    for cell in spear_positions:
        owners = [index for index, room in enumerate(level.rooms)
                  if room.x <= cell[0] < room.x + room.w
                  and room.y <= cell[1] < room.y + room.h]
        if not owners:
            raise ValueError("spear display has no owning room")
        owner = owners[0]
        room = level.rooms[owner]
        if level.room_concepts and level.room_concepts[owner] not in SPEAR_CONCEPTS:
            raise ValueError("spear display does not fit its room identity")
        x, y = cell
        outside = []
        if x == room.x:
            outside.append((x - 1, y))
        if x == room.x + room.w - 1:
            outside.append((x + 1, y))
        if y == room.y:
            outside.append((x, y - 1))
        if y == room.y + room.h - 1:
            outside.append((x, y + 1))
        if not any(not _is_floor(_at(level.tiles, *neighbor))
                   and _at(level.tiles, *neighbor) not in DOORS
                   for neighbor in outside):
            raise ValueError("spear display is not purposefully wall backed")

    flag_positions = [(index % GRID, index // GRID)
                      for index, thing in enumerate(level.things) if thing == 62]
    for x, y in flag_positions:
        owners = [room for room in level.rooms
                  if room.x <= x < room.x + room.w
                  and room.y <= y < room.y + room.h]
        if not owners:
            raise ValueError("flag has no owning room")
        room = owners[0]
        outside = []
        if x == room.x:
            outside.append((x - 1, y))
        if x == room.x + room.w - 1:
            outside.append((x + 1, y))
        if y == room.y:
            outside.append((x, y - 1))
        if y == room.y + room.h - 1:
            outside.append((x, y + 1))
        if not any(not _is_floor(_at(level.tiles, *neighbor))
                   and _at(level.tiles, *neighbor) not in DOORS
                   for neighbor in outside):
            raise ValueError("flag is not purposefully wall backed")

    vine_positions = {(index % GRID, index // GRID)
                      for index, thing in enumerate(level.things) if thing == 70}
    documented_vines = {cell for screen in level.vine_screens for cell in screen.cells}
    if vine_positions != documented_vines:
        raise ValueError("vine exists outside a complete screen composition")
    for screen in level.vine_screens:
        if not screen.cells or any(_at(level.things, *cell) != 70
                                   for cell in screen.cells):
            raise ValueError("vine screen metadata disagrees with the things plane")
        if screen.kind == "room-divider":
            if not 0 <= screen.room_index < len(level.rooms):
                raise ValueError("vine divider has no host room")
            room = level.rooms[screen.room_index]
            xs = {x for x, _ in screen.cells}
            ys = {y for _, y in screen.cells}
            vertical = (len(xs) == 1 and ys == set(range(room.y, room.y + room.h)))
            horizontal = (len(ys) == 1
                          and xs == set(range(room.x, room.x + room.w)))
            if not (vertical or horizontal):
                raise ValueError("vine divider does not span the complete room")
        elif screen.kind == "hallway-run":
            if len(screen.cells) < 3 or screen.room_index != -1:
                raise ValueError("hallway vine run has invalid extent")
            horizontal = len({y for _, y in screen.cells}) == 1
            vertical = len({x for x, _ in screen.cells}) == 1
            ordered = sorted(screen.cells,
                             key=lambda cell: cell[0] if horizontal else cell[1])
            if (not (horizontal or vertical)
                    or any(abs(first[0] - second[0]) + abs(first[1] - second[1]) != 1
                           for first, second in zip(ordered, ordered[1:]))):
                raise ValueError("hallway vines do not fill one continuous length")
            for x, y in screen.cells:
                sides = (((x, y - 1), (x, y + 1)) if horizontal else
                         ((x - 1, y), (x + 1, y)))
                if any(_is_floor(_at(level.tiles, *side))
                       or _at(level.tiles, *side) in DOORS for side in sides):
                    raise ValueError("hallway vine escaped its one-tile corridor")
            if screen.ambush_anchor is not None:
                if (_at(level.things, *screen.ambush_anchor) not in ENEMY_CODES
                        or min(abs(screen.ambush_anchor[0] - endpoint[0])
                               + abs(screen.ambush_anchor[1] - endpoint[1])
                               for endpoint in (screen.cells[0], screen.cells[-1])) > 6):
                    raise ValueError("hallway vine ambush anchor is not credible")
        else:
            raise ValueError("unknown vine-screen composition")

    if level.pickup_placements:
        tracked: dict[tuple[int, int], int] = {}
        for placement in level.pickup_placements:
            if (placement.template not in AUTHORED_PICKUP_TEMPLATES
                    or not 0 <= placement.room_index < len(level.rooms)
                    or not placement.cells):
                raise ValueError("pickup has invalid placement provenance")
            room = level.rooms[placement.room_index]
            for x, y, item in placement.cells:
                if item not in PICKUP_CODES or _at(level.things, x, y) != item:
                    raise ValueError("pickup provenance disagrees with the things plane")
                if not (room.x <= x < room.x + room.w
                        and room.y <= y < room.y + room.h):
                    raise ValueError("authored pickup escaped its owning room")
                if (x, y) in tracked:
                    raise ValueError("pickup belongs to multiple authored compositions")
                tracked[(x, y)] = item
        expected = {(index % GRID, index // GRID): item
                    for index, item in enumerate(level.things)
                    if item in PICKUP_CODES
                    and _inside_room(list(level.rooms), index % GRID, index // GRID)}
        if tracked != expected:
            raise ValueError("room contains an untracked pickup sprite")
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
    switch_dx = next(dx for dx in (1, -1)
                     if _at(level.tiles, sx + dx, sy) == ELEVATOR_TILE)
    threshold = (sx - switch_dx, sy)
    if any(_at(level.tiles, threshold[0], threshold[1] + side) != ELEVATOR_TILE
           for side in (-1, 1)):
        raise ValueError("elevator exposes a non-elevator wall inside the doorway")
    if level.rooms and level.critical_route:
        distances = _floor_distances(level.tiles, level.start)
        deepest_room = max((distances.get(room.center, 0) for room in level.rooms),
                           default=1) or 1
        if distances.get(level.exit_stand, 0) / deepest_room < 0.75:
            raise ValueError("elevator route is shallower than 75% of the floor")
        minimum_route_rooms = _minimum_critical_route_rooms(level.room_roles)
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
    if (len(level.secret_details) != len(level.secret_rewards)
            or tuple(detail.shape for detail in level.secret_details)
            != level.secret_variants):
        raise ValueError("secret planning metadata is incomplete")
    exit_details = [detail for detail in level.secret_details if detail.secret_exit]
    if level.has_secret_exit:
        if len(exit_details) != 1:
            raise ValueError("secret elevator is not a separately planned pocket")
        detail = exit_details[0]
        if (detail.shape == "square" or detail.host_room < 0
                or detail.depth_ratio < 0.45
                or detail.hint_treatment != "symmetric-landmark"
                or detail.return_floor != level.number + 1):
            raise ValueError("secret elevator lacks a substantial deep host")
        px, py = detail.pushwall
        if (_at(level.tiles, px, py) not in DECOR_WALLS
                or not any(_at(level.tiles, px, py - offset)
                           == _at(level.tiles, px, py)
                           == _at(level.tiles, px, py + offset)
                           for offset in (1, 2))):
            raise ValueError("secret elevator lacks its symmetric landmark hint")
    elif exit_details:
        raise ValueError("ordinary floor reports a secret-elevator host")
    for detail in level.secret_details:
        if (_at(level.things, *detail.pushwall) != PUSHWALL
                or detail.reward_count != (7 if level.number == 9 else 3)):
            raise ValueError("secret detail disagrees with its realized pocket")
    pushwalls = [(i % GRID, i // GRID) for i, thing in enumerate(level.things)
                 if thing == PUSHWALL]
    if len(pushwalls) < len(level.secret_rewards):
        raise ValueError("secret reward has no pushwall")
    # Most pockets open eastward, while a secret elevator may use either
    # horizontal face of its host room. Track both its outer wall and the
    # second wall of a nested pocket so reachability models the real push.
    push_directions = {detail.pushwall: detail.push_direction
                       for detail in level.secret_details}
    for detail in level.secret_details:
        if detail.shape == "nested":
            x, y = detail.pushwall
            push_directions[(x + 4 * detail.push_direction, y)] = detail.push_direction
    rests = {(x + 2 * push_directions.get((x, y), 1), y)
             for x, y in pushwalls}
    for x, y in pushwalls:
        direction = push_directions.get((x, y), 1)
        if _is_floor(_at(level.tiles, x, y)):
            raise ValueError("pushwall trigger is not on a solid wall")
        if not _is_floor(_at(level.tiles, x - direction, y)):
            raise ValueError("pushwall has no movement clearance")
        if not all(_is_floor(_at(level.tiles, x + direction * step, y))
                   for step in (1, 2)):
            raise ValueError("pushwall has no two-tile backstop")
        if _at(level.tiles, x, y) not in SECRET_HINT_WALLS:
            raise ValueError("pushwall is not hinted by a decor wall tile")
    # Nested secrets become approachable in sequence. Simulate each push at
    # its real two-tile travel distance rather than pretending the inner wall
    # must be reachable while its outer wall is still closed.
    pending = set(pushwalls)
    pushed: set[tuple[int, int]] = set()
    while pending:
        opened = _reachable(level.tiles, level.start, locked_open=True,
                            extra_passable=pushed,
                            blocked={(x + 2 * push_directions.get((x, y), 1), y)
                                     for x, y in pushed})
        ready = sorted((wall for wall in pending
                        if (wall[0] - push_directions.get(wall, 1), wall[1])
                        in opened),
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
                            blocked={(x + 2 * push_directions.get((x, y), 1), y)
                                     for x, y in others})
        direction = push_directions.get(wall, 1)
        if (wall[0] + direction, wall[1]) in bypass:
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
        direction = exit_details[0].push_direction
        if (_at(level.tiles, zx + direction, zy) != ELEVATOR_TILE
                or not _is_floor(_at(level.tiles, zx - direction, zy))
                or _at(level.tiles, zx - 2 * direction, zy) != DOOR_ELEVATOR):
            raise ValueError("secret exit is not inside a real elevator car")
        if any(_at(level.tiles, x, zy + side) != ELEVATOR_TILE
               for x in range(zx - 1, zx + 2) for side in (-1, 1)):
            raise ValueError("secret elevator is missing its side rails")
        outer_shell = ({(zx + 2 * direction, zy + side)
                        for side in range(-2, 3)}
                       | {(x, zy + side) for x in range(zx - 2, zx + 3)
                          for side in (-2, 2)})
        if any(_is_floor(_at(level.tiles, *cell))
               or _at(level.tiles, *cell) in DOORS for cell in outer_shell):
            raise ValueError("secret elevator is not rock bounded")
        closed_secret = _reachable(level.tiles, level.start, locked_open=True)
        if (zx, zy) in closed_secret:
            raise ValueError("secret elevator is reachable without its pushwall")
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
        if (len(level.key_objectives) != len(level.key_order)
                or tuple(objective.color for objective in level.key_objectives)
                != level.key_order):
            raise ValueError("key-objective metadata disagrees with progression order")
        physical_hosts = []
        for objective in level.key_objectives:
            if not 0 <= objective.host_room < len(level.rooms):
                raise ValueError("key objective has no host room")
            if objective.treatment == "boss-drop":
                if (_at(level.things, *objective.cell) not in KEY_DROP_BOSSES
                        or objective.color != "gold" or not level.boss):
                    raise ValueError("boss key objective is not backed by a native drop")
                continue
            expected_key = GOLD_KEY if objective.color == "gold" else SILVER_KEY
            if _at(level.things, *objective.cell) != expected_key:
                raise ValueError("physical key objective disagrees with things plane")
            if objective.detour < 2:
                raise ValueError("physical key lies directly on its lock route")
            if objective.cell == level.rooms[objective.host_room].center:
                raise ValueError("physical key defaults to the room center")
            clear_neighbors = sum(
                _is_floor(_at(level.tiles, objective.cell[0] + dx,
                              objective.cell[1] + dy))
                and _at(level.things, objective.cell[0] + dx,
                        objective.cell[1] + dy) == 0
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
            if clear_neighbors < 1:
                raise ValueError("physical key has no clear pickup approach")
            physical_hosts.append(objective.host_room)
        if len(physical_hosts) != len(set(physical_hosts)):
            raise ValueError("dual physical keys repeat the same host room")

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

    boss_families = {"throne-stronghold", "command-bunker",
                     "laboratory-gauntlet", "columned-fortress", "central-duel"}
    vault_families = {"central-vault", "museum-circuit", "nested-reliquary",
                      "abandoned-armory", "treasure-palace"}
    if level.number == 9:
        if level.special_family not in boss_families:
            raise ValueError("floor 9 has no boss-stronghold family")
        if (not 0 <= level.boss_arena_room < len(level.rooms)
                or level.boss_arena_room not in level.critical_route
                or level.room_roles[level.boss_arena_room] != "boss-arena"):
            raise ValueError("floor 9 arena is not a mandatory planned destination")
        arena = level.rooms[level.boss_arena_room]
        if min(arena.w, arena.h) < 14:
            raise ValueError("boss arena is too small for its encounter")
        if (level.boss_arena is None
                or level.boss_arena.family != level.special_family
                or len(level.boss_arena.geometry) < 2
                or len(level.boss_arena.decorations) < 3):
            raise ValueError("boss arena lacks its family-owned composition")
        interior_cover = sum(
            not _is_floor(_at(level.tiles, x, y))
            for y in range(arena.y + 2, arena.y + arena.h - 2)
            for x in range(arena.x + 2, arena.x + arena.w - 2))
        if interior_cover < 2:
            raise ValueError("boss arena has no symmetric sightline cover")
        if (not 0 <= level.preboss_room < len(level.rooms)
                or level.room_roles[level.preboss_room] != "staging"
                or _room_predecessor(len(level.rooms), list(level.edges),
                                     level.boss_arena_room) != level.preboss_room):
            raise ValueError("floor 9 lacks an immediate pre-boss staging room")
        if not any(placement.reason == "preboss-stockup"
                   and placement.room_index == level.preboss_room
                   for placement in level.pickup_placements):
            raise ValueError("pre-boss staging room has no authored stock-up")
        victory = next((index for index, role in enumerate(level.room_roles)
                        if role == "victory"), None)
        if (victory is None or victory not in level.critical_route
                or level.critical_route.index(victory)
                <= level.critical_route.index(level.boss_arena_room)):
            raise ValueError("boss arena does not lead into a victory space")
        victory_room = level.rooms[victory]
        if any(_at(level.things, x, y) in ENEMY_CODES
               for y in range(victory_room.y, victory_room.y + victory_room.h)
               for x in range(victory_room.x, victory_room.x + victory_room.w)):
            raise ValueError("floor 9 victory room is not a calm transition")
        if _at(level.things, *next(objective.cell for objective in level.key_objectives
                                   if objective.color == "gold")) not in KEY_DROP_BOSSES:
            raise ValueError("floor 9 completion is not boss gated")
    elif level.number == 10:
        if level.special_family not in vault_families:
            raise ValueError("floor 10 has no reward-expedition family")
        if level.secret_source and not 1 <= level.secret_source <= 6:
            raise ValueError("floor 10 has an invalid secret-exit source floor")
        if (level.locked_doors or level.key_order
                or not 0 <= level.premium_room < len(level.rooms)
                or level.premium_room not in level.critical_route
                or level.room_roles[level.premium_room] != "premium-vault"):
            raise ValueError("floor 10 premium chamber is not on its open route")
        premium = [placement for placement in level.pickup_placements
                   if placement.reason == "floor-ten-premium"
                   and placement.room_index == level.premium_room]
        expeditions = [placement for placement in level.pickup_placements
                       if placement.reason == "floor-ten-expedition"]
        if (len(premium) != 1 or not any(item in (ONE_UP, CHAINGUN, TREASURE[3])
                                        for _, _, item in premium[0].cells)
                or len({placement.room_index for placement in expeditions}) < 2):
            raise ValueError("floor 10 lacks its premium and expedition rewards")
        expedition_concepts = {level.room_concepts[placement.room_index]
                               for placement in expeditions}
        if len(expedition_concepts) < 2:
            raise ValueError("floor 10 reward expeditions repeat one concept")
        arrival = next((index for index, role in enumerate(level.room_roles)
                        if role == "arrival"), None)
        if arrival is None:
            raise ValueError("floor 10 lacks a calm arrival room")
        arrival_room = level.rooms[arrival]
        if any(_at(level.things, x, y) in ENEMY_CODES
               for y in range(arrival_room.y, arrival_room.y + arrival_room.h)
               for x in range(arrival_room.x, arrival_room.x + arrival_room.w)):
            raise ValueError("floor 10 arrival reveal is not calm")
        for index, thing in enumerate(level.things):
            if thing not in GHOSTS:
                continue
            x, y = index % GRID, index // GRID
            owner = next((room_index for room_index, room in enumerate(level.rooms)
                          if room.x <= x < room.x + room.w
                          and room.y <= y < room.y + room.h), None)
            if owner is None or owner in level.critical_route:
                raise ValueError("floor 10 ghost controls the mandatory route")
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
    route_by_actor: dict[tuple[int, int], EncounterPlacement] = {}
    claimed_markers: set[tuple[int, int]] = set()
    allowed_kinds = {"room-loop", "compact-loop",
                     "hall-shuttle", "doorway-shuttle"}
    for encounter in level.encounters:
        if not encounter.patrol_kind:
            continue
        if (encounter.template != "patrol" or len(encounter.cells) != 1
                or encounter.patrol_kind not in allowed_kinds
                or len(encounter.patrol_path) < 3
                or len(set(encounter.patrol_path)) != len(encounter.patrol_path)):
            raise ValueError("patrol route has invalid encounter provenance")
        x, y, actor = encounter.cells[0]
        origin = (x, y)
        if (_patrol_actor_direction(actor) is None
                or origin not in encounter.patrol_path
                or origin in route_by_actor):
            raise ValueError("patrol actor does not own one declared route")
        route_by_actor[origin] = encounter
        path = encounter.patrol_path
        route_markers = {cell for cell in path
                         if _at(level.things, *cell) in PATROL_POINT_DIRECTIONS}
        expected_count = (2 if encounter.patrol_kind in
                          ("hall-shuttle", "doorway-shuttle") else 4)
        if len(route_markers) != expected_count or claimed_markers & route_markers:
            raise ValueError("patrol markers are missing, duplicated, or shared")
        claimed_markers.update(route_markers)
        for cell in route_markers:
            index = path.index(cell)
            if encounter.patrol_kind in ("hall-shuttle", "doorway-shuttle"):
                next_cell = path[1] if index == 0 else path[-2]
            else:
                next_cell = path[(index + 1) % len(path)]
            delta = next_cell[0] - cell[0], next_cell[1] - cell[1]
            expected = ((0, -1), (1, 0), (0, 1), (-1, 0)).index(delta)
            actual = PATROL_POINT_DIRECTIONS[_at(level.things, *cell)]
            if actual != expected:
                raise ValueError(f"patrol marker at {cell} points off its route")

    plane_markers = {(index % GRID, index // GRID)
                     for index, thing in enumerate(level.things)
                     if thing in PATROL_POINT_DIRECTIONS}
    if plane_markers != claimed_markers:
        raise ValueError("things plane contains an unowned patrol marker")

    for index, thing in enumerate(level.things):
        direction = _patrol_actor_direction(thing)
        if direction is None:
            continue
        x, y = index % GRID, index // GRID
        origin = (x, y)
        encounter = route_by_actor.get(origin)
        if encounter is None:
            raise ValueError(f"patrol actor at {origin} has no declared route")
        path_cells = set(encounter.patrol_path)
        for _ in range(steps):
            dx, dy = ((0, -1), (1, 0), (0, 1), (-1, 0))[direction]
            x, y = x + dx, y + dy
            if (x, y) not in path_cells:
                raise ValueError(f"patrol actor at {origin} walks off its declared route")
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


def _display_name(number: int, variant: str = "") -> str:
    if number == 10:
        return "Secret Floor"
    title = _VARIANT_TITLES.get(variant, "")
    return f"Floor {number}: {title}" if title else f"Random Floor {number}"


def _reproducibility_text(config: CampaignConfig, secret_from: int) -> str:
    """Human-readable, copyable record of every campaign input."""
    settings = json.loads(config.to_json())
    commit = BUILD_COMMIT or "unknown"
    lines = [
        "InfiniWolf campaign reproducibility record",
        "==========================================",
        "",
        f"version = {__version__}",
        f"commit = {commit}",
        "seed_source = LittleEntropyMachine",
        f"seed = {config.seed}",
        f"secret_floor_source = {secret_from}",
        "",
        "Resolved settings",
        "-----------------",
    ]
    for name, value in settings.items():
        if name != "seed":
            lines.append(f"{name} = {value}")
    arguments = [f"--seed {config.seed}"]
    for name, value in settings.items():
        if name == "seed":
            continue
        if name == "say_aardwolf":
            if value:
                arguments.append("--say-aardwolf")
            continue
        arguments.append(f"--{name.replace('_', '-')} {value}")
    lines.extend((
        "",
        "Reproduction command",
        "--------------------",
        "python3 -m infiniwolf " + " ".join(arguments)
        + " --output infiniwolf.pk3",
        "",
        "The same InfiniWolf version and commit are required for byte-identical output.",
        "",
    ))
    return "\n".join(lines)


def _set_distance(first: tuple[str, ...], second: tuple[str, ...]) -> float:
    left, right = set(first), set(second)
    union = left | right
    return len(left ^ right) / max(1, len(union))


def _candidate_score(level: GeneratedMap, previous: list[GeneratedMap],
                     config: CampaignConfig) -> float:
    score = 0.0
    for offset, other in enumerate(reversed(previous[-4:])):
        weight = 4.0 / (offset + 1)
        score += weight * (
            2.5 * (level.variant != other.variant)
            + 2.5 * (level.circulation_skeleton != other.circulation_skeleton)
            + 1.5 * _set_distance(level.layout_signature,
                                  other.layout_signature)
            + 1.4 * _set_distance(level.room_concepts,
                                  other.room_concepts)
            + 1.2 * _set_distance(level.motifs, other.motifs)
            + 1.1 * _set_distance(level.room_shapes, other.room_shapes)
            + 0.9 * _set_distance(level.district_circulation,
                                  other.district_circulation)
            + 0.8 * _set_distance(level.secret_variants,
                                  other.secret_variants)
            + 0.7 * _set_distance(
                tuple(encounter.family for encounter in level.encounters),
                tuple(encounter.family for encounter in other.encounters))
            + 0.6 * _set_distance(level.lighting_families,
                                  other.lighting_families)
            + 0.5 * ((level.arrival.kind if level.arrival else "")
                     != (other.arrival.kind if other.arrival else ""))
            + min(1.0, abs(len(level.rooms) - len(other.rooms)) / 5.0)
            + min(1.0, abs(sum(bool(thing) for thing in level.things)
                               - sum(bool(thing) for thing in other.things)) / 24.0)
        )
    score += _set_distance(level.room_concepts[:-1],
                           level.room_concepts[1:])
    score += 0.35 * len(set(level.room_concepts))
    pairs = {
        frozenset(("storage", "ready-room")),
        frozenset(("supply-cache", "checkpoint")),
        frozenset(("armory", "training-room")),
        frozenset(("barracks", "mess-kitchen")),
        frozenset(("officers-quarters", "war-room")),
        frozenset(("gallery", "trophy-hall")),
        frozenset(("crypt", "ossuary")),
        frozenset(("holding-cell", "interrogation-room")),
    }
    score += 0.6 * sum(
        frozenset((level.room_concepts[first], level.room_concepts[second]))
        in pairs for first, second in level.edges)
    rhythm = random.Random(config.aardwolf_seed(10) ^ 0xA4D0F)
    phase = rhythm.random() * math.tau
    tension = math.sin(phase + level.number * math.tau / 4.5)
    actor_density = sum(level.enemy_tiers) / max(1, len(level.rooms))
    target_actors = 0.45 + int(config.guard_density) * 0.20 + tension * 0.18
    score -= abs(actor_density - target_actors) * 1.8
    object_density = sum(bool(thing) for thing in level.things) / max(1, len(level.rooms))
    target_objects = 3.0 + int(config.decoration_amount) * 0.55 - tension * 0.35
    score -= abs(object_density - target_objects) * 0.20
    center = sum(room.center[0] for room in level.rooms) / max(1, len(level.rooms))
    handedness = -1 if config.aardwolf_seed(level.number) & 1 else 1
    score += 0.5 if (center - level.start[0]) * handedness > 0 else 0.0
    return score


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
                                         rare_motif_enabled=(number == rare_motif_floor))
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
    if any(first.progression_grammar == second.progression_grammar
           for first, second in zip(levels, levels[1:])):
        raise RuntimeError("campaign repeated the same progression grammar consecutively")
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
        if not {"mapinfo.txt", "infiniwolf-manifest.json",
                "infiniwolf-settings.txt"}.issubset(names):
            raise ValueError("package is missing required reproducibility metadata")
        forbidden = (".wl6", ".png", ".wav", ".ogg", ".voc")
        if any(name.lower().endswith(forbidden) for name in names):
            raise ValueError("package contains an asset file instead of map metadata")
        manifest = json.loads(package.read("infiniwolf-manifest.json"))
        if len(manifest.get("floors", ())) != 10:
            raise ValueError("manifest does not describe ten floors")
        settings_text = package.read("infiniwolf-settings.txt").decode("utf-8")
        settings_lines = set(settings_text.splitlines())
        required_settings = {
            f"version = {manifest.get('version')}",
            f"commit = {manifest.get('commit')}",
            f"seed = {manifest.get('seed')}",
            f"secret_floor_source = {manifest.get('secret_from')}",
            "seed_source = LittleEntropyMachine",
        }
        required_settings.update(
            f"{name} = {value}"
            for name, value in manifest.get("settings", {}).items()
            if name != "seed")
        if not required_settings <= settings_lines:
            raise ValueError("reproducibility text disagrees with the manifest")
        for name in expected_maps:
            wad = package.read(name)
            if len(wad) < 46 or wad[:4] != b"PWAD" or wad[12:18] != b"WDC3.1":
                raise ValueError(f"{name} has an invalid ECWolf WAD header")
            width, height = struct.unpack_from("<HH", wad, 42)
            if (width, height) != (GRID, GRID):
                raise ValueError(f"{name} is not a {GRID}x{GRID} map")
        # Provenance is part of the installed artifact contract, not merely
        # descriptive manifest data. Recompute it from the map planes before
        # the temporary package is allowed to replace the previous campaign.
        from .watermark import (floor_target, plane_residue,
                                plane_residue_secondary, secondary_target,
                                _parse_wad)
        primary = []
        for number in range(1, 11):
            record = _parse_wad(package.read(f"maps/iw{number:02d}.wad"))
            first = plane_residue(record.tiles, record.things, number)
            second = plane_residue_secondary(record.tiles, record.things, number)
            if first != floor_target(number) or second != secondary_target(number):
                raise ValueError(f"IW{number:02d} provenance watermark is invalid")
            primary.append(first)
        if sum(primary) % 43 != 42:
            raise ValueError("campaign provenance residue is not 42")
        return manifest
