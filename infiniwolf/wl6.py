"""Native WL6 map code vocabulary.

Every number ECWolf's wolf3d translator interprets: tile codes, door and
elevator codes, actor/pickup thing codes, wall material families, and the
static-decoration registries. Pure data with no local imports -- this is the
leaf every other generation module builds on.
"""

from __future__ import annotations

from dataclasses import dataclass

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
    ("guard", GUARDS, 8, 1.5),
    ("dog", DOGS, 6, 0.5),
    ("officer", OFFICERS, 4, 3.0),
    ("ss", SS, 6, 6.0),
)
NOVELTY_SPAWN_CHANCE = 0.20
PATROLS_BY_FAMILY = dict(zip((GUARDS, OFFICERS, SS, DOGS),
                             (PATROL_GUARDS, PATROL_OFFICERS, PATROL_SS, PATROL_DOGS)))
FAMILY_BY_CODE = {code + 36 * tier: family
                  for _, family, _, _ in ENEMY_FAMILIES
                  for variant in (family, PATROLS_BY_FAMILY[family])
                  for code in variant for tier in range(3)}
AMMO_COST = {family: cost for _, family, _, cost in ENEMY_FAMILIES}
# The placed-clip budget models bullets spent but not bullets recovered: in WL6
# a downed guard, officer, or SS leaves its own clip behind, so a floor stocked
# to its full expected sink reads as a floor drowning in ammo. Corpse drops are
# the primary supply; staged clips are a thin top-up for expensive stretches.
AMMO_SUPPLY_SCALE = 0.25
# The boss stronghold is exempt. Its staging cache, pre-arena secrets, and arena
# supplies exist to make one unavoidable, expensive fight survivable, and there
# is no corpse-drop stream from a boss to fall back on.
AMMO_SUPPLY_EXEMPT_FLOORS = frozenset({9})
# Encounter frequency. `guard_density` sets the raw appetite, but realized
# rooms were reading as wall-to-wall resistance, so trim the per-room budget.
ACTOR_BUDGET_SCALE = 0.85
# Chebyshev separation an encounter tries to keep between its own actors before
# relaxing (see `_spread_actor_cells`).
ACTOR_SPACING = 3

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
# coherent. FAKEDOR(13) is deliberately excluded from decorative wall
# dressing.  The inert ELEV1(85) elevator façade remains reserved for the
# player's purpose-built arrival car; SKY(16) and stained GLASS(33) are the
# other special compositions outside the general roster.
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


def _codes_for_colors(colors: set[str] | frozenset[str]) -> frozenset[int]:
    codes: set[int] = set()
    if "gold" in colors:
        codes.update(GOLD_DOORS)
    if "silver" in colors:
        codes.update(SILVER_DOORS)
    return frozenset(codes)
