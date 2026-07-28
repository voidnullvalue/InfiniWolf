"""Static decoration: what furnishes a room, and where it stands.

`_place_decorations` is the terminal pass of `generate_map` -- nothing after it
draws from the main RNG stream, so decoration composition can be retuned
without perturbing layout, population, or patrols. Every blocking commit
re-runs full-map reachability with the candidate cells blocked
(`_try_place_items`), so furniture can never seal off part of a floor.

Concept tables (`_DECOR_BLOCKING` / `_DECOR_OPEN`) decide which item vocabulary
a room is eligible for; the placement geometry in `placement` decides where a
composition may key off.

Its recessed sky-vista writes are category 2 feature-owned bounded geometry: they
exist solely for that decoration feature and are not general realization.
"""

from __future__ import annotations

from collections import Counter, deque
import random

from .grid import (_at, _inside_room, _is_floor, _reachable, _set,
                   qualifying_dead_end_alcoves)
from .ledger import release as ledger_release, reserve as ledger_reserve
from .model import Room, RoomIdentity, VineScreen
from .placement import _room_anchors, _room_traversal_frame, _traversal_pair_candidates
from .room_policy import base_theme as _decor_theme
from .wl6 import (DECOR_WALLS, DOORS, ENEMY_CODES, GRID, LIGHTING_FAMILY_ITEMS,
                    LIGHTING_ITEMS, PLAYER_START_CODES, SPECIAL_WALL_TILES, STATIC_BLOCKING, STATIC_OPEN,
                    VINE_SCREEN_CONCEPTS, WALL_MATERIALS)


# Decoration themes keyed by room role+tier, derived from community-map
# placement patterns: guard rooms get lamps and vases, storage closets get
# barrel clusters, grand anchor rooms get pillar pairs, barracks get tables.
_DECOR_BLOCKING: dict[str, tuple[int, ...]] = {
    # Solid props per concept. Potted plants and pillars are generic authored
    # decor rather than courtyard specials: GreenPlant is the corpus's second
    # most common item at 19.5 per map and WhitePillar its third at 18.0, behind
    # only ceiling lights. They were reachable in 2 and 6 of these 25 concepts,
    # which capped them at roughly 8 and 7 per map however often the mined fill
    # distribution asked for them -- fill can only offer what a concept permits.
    #
    # Deliberately still absent from the carceral and funerary concepts: a cell
    # block does not keep houseplants, and those concepts have their own
    # vocabulary of bones, chains and barrels.
    "guardpost": (26, 35, 31, 62, 30),
    "armory": (39, 62, 69, 58, 34),
    "checkpoint": (26, 62, 35),
    "grand": (30, 26, 35, 39, 31),
    "war-room": (39, 62, 30, 31, 36),
    "trophy-hall": (39, 62, 34, 30, 31),
    "courtyard": (30, 31, 34, 59),
    "barracks": (25, 36, 58, 45, 34),
    "ready-room": (45, 36, 58, 34),
    "training-room": (69, 36, 58, 30),
    "crypt": (30, 40, 58, 28),
    "ossuary": (30, 40, 41, 28),
    "burial-chamber": (30, 35, 40),
    "storage": (58, 24, 59, 60),
    "supply-cache": (58, 24, 60),
    "workshop": (36, 58, 69, 30),
    "lounge": (25, 35, 34, 31),
    "gallery": (39, 62, 34, 30, 31),
    "dining-hall": (25, 36, 35, 34),
    "officers-quarters": (45, 25, 34, 31, 36),
    "mess-kitchen": (36, 35, 34),
    "corridor": (26, 30),
    "jail": (58, 40, 41, 28),
    "holding-cell": (40, 58, 36),
    "interrogation-room": (36, 25, 26),
}

_DECOR_OPEN: dict[str, tuple[int, ...]] = {
    # Non-solid clutter per concept, widened from measurement. The authored
    # corpus carries 8.3 TableChairs, 5.3 Pots, 4.7 Blood, 4.4 Bones2 and 3.9
    # Puddles per map; this generator produced 4.2, 0.1, 1.4, 1.2 and 0.2. Pots
    # appeared in no palette at all, so the density fill -- which intersects the
    # mined corpus distribution with what a concept permits -- could never place
    # one however often the corpus said to. Thin palettes are also why distinct
    # item types sat at 15.8 against the corpus's 19.3: fill had nothing to draw.
    #
    # Assigned per concept rather than added uniformly. Pots belong in a kitchen,
    # a workshop and a store room; puddles where damp would collect; bones and
    # blood stay in the carceral and funerary concepts, where the atmosphere
    # setting can still strip them.
    "guardpost": (37, 27),
    "armory": (37, 46, 67),
    "checkpoint": (37,),
    "grand": (27, 37, 67),
    "war-room": (37, 67, 46),
    "trophy-hall": (27, 37, 46),
    "courtyard": (37, 23, 67),
    "barracks": (46, 61, 67),
    "ready-room": (46, 67),
    "training-room": (37, 67),
    "crypt": (42, 64, 65, 66, 23),
    "ossuary": (32, 42, 64, 65, 66, 23),
    "burial-chamber": (27, 42, 64, 65, 66, 23),
    "storage": (46, 23, 67),
    "supply-cache": (46, 67, 23),
    "workshop": (37, 67, 23),
    "lounge": (27,),
    "gallery": (27, 37, 46),
    "dining-hall": (27, 67),
    "officers-quarters": (27, 37, 46, 67),
    "mess-kitchen": (37, 67, 23),
    "corridor": (37, 23),
    "jail": (61, 61, 42, 64, 65, 66, 23),
    "holding-cell": (42, 64, 65, 66, 61, 23),
    "interrogation-room": (37, 61, 64, 23),
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

# Recessed exterior vistas retain the original wall plane as matching pillar
# supports. Name the rates so this landmark can be deliberately tuned.
SKY_VISTA_COURTYARD_CHANCE = 0.36

SKY_VISTA_INTERIOR_CHANCE = 0.18

# Items that read as a deliberate matched pair when mirrored beside a door
# or under a landmark wall: plants, lamps, pillars, vases, barrels, suits
# of armor, and flags.
_FRAMEABLE = frozenset({26, 30, 31, 34, 39, 62})

# One motif per room, chosen deliberately.
#
# These compositions used to be a sequence of independent probability gates that
# all drew against a shared budget of one or two, so whichever appeared earliest in
# this file won. Source order became priority order, and the effect was measurable:
# across 1840 rooms the travel pair fired in 19.6% while the room *signature* --
# the matched bunks in a barracks, the spear rack in an armory, the one composition
# that is specific to what the room is for -- reached 1.0%. Nearly 70% of rooms got
# no composition at all.
#
# Weights say how often a motif should win when it is eligible, and eligibility is
# a structural question answered before any roll. Concept-specific motifs are
# weighted highest because they are what makes a room read as built for a purpose;
# the travel pair is generic and applies almost everywhere, so it needs the lowest
# weight to stop it crowding the rest out.
# Weight is inverse to how widely a motif is eligible. A narrowly-scoped motif can
# only ever appear in the rooms it fits, so weighting it high cannot crowd anything
# out elsewhere -- a colonnade needs a grand 8x8 room, and if it loses there it has
# nowhere else to go. Generic motifs get the lowest weights for the same reason:
# they are eligible almost everywhere, so any weight at all makes them common.
#
# Tuned against measurement. A first pass weighted colonnade at 3.0 against
# signature's 6.0 and dropped it from 4.9% of rooms to 0.8%, trading one starvation
# for another: the grand-hall look is the point of the grand-halls variant, and it
# has only those rooms to appear in.
_MOTIF_WEIGHTS = {
    # Narrow eligibility: concept- or theme-specific, and few rooms qualify.
    "colonnade": 6.0,              # grand theme, 8x8
    "courtyard-centerpiece": 6.0,  # courtyard/storage, 9x9
    "signature": 6.0,              # the room concept's own composition
    "banquet-row": 5.0,            # barracks/dining-hall, long axis
    "divider": 4.0,                # 10x10, grand/barracks/guardpost
    "landmark-frame": 3.0,         # needs a landmark wall
    # Broad eligibility: applies in most rooms, so it must ask for less.
    # Lowered from 2.5: this motif exists to heap props in a corner, so it is
    # the one composition that raises the clustering measure by design. At 2.5
    # it reached 14% of rooms.
    "corner-stash": 1.5,
    "doorway-frame": 2.0,
    "travel-pair": 1.5,
}

# Rooms that receive no motif at all. Some emptiness is correct -- a corridor or a
# small connector should stay quiet, and a floor where every room is composed reads
# as busy rather than designed. This is the share of *eligible* rooms deliberately
# left plain.
_MOTIF_ABSTENTION = 0.18

# A weight at or above this means "choose this if it is eligible", bypassing
# both the weighted draw and the abstention.
_MOTIF_FORCED = 100.0


def _choose_motif(eligible: dict[str, bool], rng: random.Random,
                  overrides: dict[str, float] | None = None) -> str:
    """Pick one motif for a room from those structurally possible.

    Returns "" when the room is left plain, either because nothing was eligible or
    because it drew the abstention. Deciding once, up front, is the whole point:
    it replaces a race in which the winner was determined by line number.

    `overrides` replaces a motif's weight. It exists because the caller's
    `traversal_pair_chance` and `landmark_frame_chance` parameters used to be the
    probabilities of those two gates directly, and callers -- tests especially --
    still need to force or suppress one composition to examine it. A weight of zero
    removes the motif from consideration entirely; a large weight makes it certain
    when it is eligible.
    """
    weights_by_name = dict(_MOTIF_WEIGHTS)
    weights_by_name.update(overrides or {})
    names = [name for name, ok in eligible.items()
             if ok and weights_by_name.get(name, 0.0) > 0.0]
    if not names:
        return ""
    forced = [name for name in names if weights_by_name[name] >= _MOTIF_FORCED]
    if forced:
        return rng.choice(forced)
    if rng.random() < _MOTIF_ABSTENTION:
        return ""
    return rng.choices(names, weights=[weights_by_name[n] for n in names], k=1)[0]


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
            # Leave a gap between the two members unless they are barrels. A
            # shoulder-to-shoulder pair of anything else reads as a heap, and the
            # corpus agrees: barrels are in contact 51-57% of the time while brown
            # plants manage 8% and vases 13%. Ledger attribution showed 71% of this
            # generator's floor-prop clustering came from multi-cell composition
            # commits, of which this was the widest-reaching.
            step = 1 if item in (24, 58) else 2
            nx = cornx + (step if cornx < cx else -step)
            ny = corny + (step if corny < cy else -step)
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
                ledger_reserve(reserved, [cell], "decorations",
                               "zoned-open-accent")
                free.discard(cell)

# Every concept carries a fixture language, and none of them may resolve to
# "none". A room the player cannot see is not a stylistic choice: the corpus
# runs ~60 ceiling lights per map across 94% of maps, and the community guides
# single out unlit rooms as the clearest "unfinished" tell. Missing entries used
# to default to (("none", 1.0),), which silently left `barracks` -- the theme
# every beat/branch/ring/hub/filler room gets -- and `storage`, every closet,
# permanently dark.
_LIGHTING_OPTIONS: dict[str, tuple[tuple[str, float], ...]] = {
    "war-room": (("chandelier", 3.0), ("ceiling-lamp", 2.0)),
    "trophy-hall": (("chandelier", 4.0), ("ceiling-lamp", 1.0)),
    "gallery": (("chandelier", 3.0), ("ceiling-lamp", 1.0)),
    "dining-hall": (("chandelier", 5.0), ("ceiling-lamp", 1.0)),
    "officers-quarters": (("chandelier", 2.0), ("floor-lamp", 2.0)),
    "lounge": (("chandelier", 2.0), ("floor-lamp", 3.0)),
    "guardpost": (("floor-lamp", 3.0), ("ceiling-lamp", 2.0)),
    "checkpoint": (("ceiling-lamp", 4.0), ("floor-lamp", 1.0)),
    "armory": (("ceiling-lamp", 4.0),),
    "training-room": (("ceiling-lamp", 4.0),),
    "ready-room": (("ceiling-lamp", 2.0), ("floor-lamp", 1.0)),
    "workshop": (("ceiling-lamp", 4.0),),
    "mess-kitchen": (("ceiling-lamp", 5.0),),
    # Even weight, not 4:1. A corridor's whole blocking vocabulary is the floor
    # lamp, so a ceiling-lamp corridor has no furniture composition available at
    # all -- the 4:1 weight this used to carry left four corridors in five bare
    # of the matched pair that bisects door-to-door travel. Keep ceiling lamps a
    # real alternative so hallway lighting still varies down a floor.
    "corridor": (("ceiling-lamp", 1.0), ("floor-lamp", 1.0)),
    "interrogation-room": (("floor-lamp", 3.0), ("ceiling-lamp", 1.0)),
    "courtyard": (("ceiling-lamp", 3.0),),
    "grand": (("chandelier", 3.0), ("ceiling-lamp", 1.0)),
    # Previously unreachable concepts. Utility and dungeon spaces get the plain
    # ceiling lamp; only the quarters-like barracks earns a floor lamp.
    "barracks": (("ceiling-lamp", 4.0), ("floor-lamp", 1.0)),
    "storage": (("ceiling-lamp", 1.0),),
    "supply-cache": (("ceiling-lamp", 1.0),),
    "jail": (("ceiling-lamp", 1.0),),
    "holding-cell": (("ceiling-lamp", 1.0),),
    "crypt": (("ceiling-lamp", 1.0),),
    "ossuary": (("ceiling-lamp", 1.0),),
    "burial-chamber": (("ceiling-lamp", 2.0), ("chandelier", 1.0)),
}
# What an unlisted concept gets. Chandeliers and floor lamps carry styling the
# caller has not asked for, so the neutral ceiling lamp is the safe default.
_DEFAULT_LIGHTING = "ceiling-lamp"

def _lighting_family(concept: str, room: Room, rng: random.Random,
                     counts: Counter[str]) -> str:
    """Resolve one coherent fixture language for an authored room.

    Always returns a real family. Every room gets light; the only question is
    which fixture vocabulary it uses.
    """
    options = list(_LIGHTING_OPTIONS.get(concept, ((_DEFAULT_LIGHTING, 1.0),)))
    # Each family has its own minimum. A chandelier needs a hall to read as one.
    # A standing lamp does not -- floor lamps are 13.7% of the corpus's
    # corner/tight bucket, its third most common item -- but it does need a
    # corner the decoration pass will actually visit, and rooms below 5x5 are
    # closets that the pass skips wholesale. Requiring 6x6 for both was what left
    # only 3.5% of rooms on the floor-lamp family; dropping the floor-lamp
    # minimum to 5x5 raises that to ~20% without leaving closets dark, because
    # only the non-solid ceiling lamp can be placed in a room that gets skipped.
    if room.w < 6 or room.h < 6:
        options = [(family, weight) for family, weight in options
                   if family != "chandelier"]
    if room.w < 5 or room.h < 5:
        options = [(family, weight) for family, weight in options
                   if family != "floor-lamp"]
    if not options:
        return _DEFAULT_LIGHTING
    families = [family for family, _ in options]
    # Repetition is legal when the identity calls for it, but a floor-wide
    # monoculture receives a soft penalty rather than a deterministic cycle.
    weights = [weight / (1.0 + 0.35 * counts[family])
               for family, weight in options]
    chosen = rng.choices(families, weights=weights, k=1)[0]
    counts[chosen] += 1
    return chosen

# Ceiling fixtures are laid on a grid, not scattered and not strung along the
# room's centre axis. Measured over the 1,211 straight CeilingLight runs in the
# authored corpus, stride 4 x length 3 is modal (452 runs) followed by stride 3
# x length 3 (250), i.e. authored maps light a hall on a 3-4 tile lattice.
_FIXTURE_STRIDES = (4, 3)
# The Wolf3D renderer starts dropping sprites somewhere around 56-64 visible
# objects, and one room's lattice is the worst case for that because it is all
# in view at once. A 24x24 hall on stride 3 would ask for 64 fixtures by itself.
_MAX_FIXTURES_PER_ROOM = 12


def _fixture_lattice(room: Room, tiles: list[int], stride: int
                     ) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Candidate fixture cells as (rhythm, fallback), both centre-outward.

    `rhythm` is the centred `stride` lattice, ordered from the room centre
    outward, so a room that can only afford part of its lattice still reads as
    deliberately lit rather than lit in one corner. `fallback` is every other
    interior floor cell, and exists to guarantee a minimum rather than to be
    filled: a 5x4 room has exactly one cell on a stride-3 lattice, so without it
    a single reserved cell -- an actor, a pickup, a patrol node -- would leave
    the whole room dark. Ceiling fixtures have no collision, so there is never a
    reason to refuse one while any free floor cell remains.
    """
    xs = range(room.x + 1, room.x + room.w - 1)
    ys = range(room.y + 1, room.y + room.h - 1)
    if not xs or not ys:
        # A 1- or 2-wide room has no interior ring; light its floor directly.
        xs, ys = range(room.x, room.x + room.w), range(room.y, room.y + room.h)
    # Centre the lattice inside the room so the rhythm looks intentional at both
    # walls instead of flush to the low corner.
    x0 = xs.start + ((len(xs) - 1) % stride) // 2
    y0 = ys.start + ((len(ys) - 1) % stride) // 2
    cx, cy = room.center
    interior = [(x, y) for x in xs for y in ys if _is_floor(_at(tiles, x, y))]
    on_lattice = [c for c in interior
                  if (c[0] - x0) % stride == 0 and (c[1] - y0) % stride == 0]
    # The fallback spans the whole room, wall ring included, not just the
    # interior: a small room can have every interior cell held in `reserved` for
    # an actor and its facing clearance, and a things-plane cell holds exactly
    # one code, so those cells are genuinely unavailable. Wall-adjacent ceiling
    # lights are unremarkable in the corpus anyway (21% of CeilingLight sits on
    # a wall, corner, or slot cell).
    ring = [(x, y)
            for x in range(room.x, room.x + room.w)
            for y in range(room.y, room.y + room.h)
            if _is_floor(_at(tiles, x, y))]
    taken = set(on_lattice)
    on_lattice.sort(key=lambda c: (abs(c[0] - cx) + abs(c[1] - cy), c))
    spare = sorted((c for c in ring if c not in taken),
                   key=lambda c: (abs(c[0] - cx) + abs(c[1] - cy), c))
    return on_lattice, spare


# --------------------------------------------------------------------- Stage C
# The geometry of a candidate cell predicts the authored item better than the
# room's theme does. Mined from all 43,122 decoration instances in the 207-map
# hand-authored corpus (docs/decor-corpus-patterns.md; regenerate with
# tools/mine_decor_patterns.py). This is a prior, not a replacement for theming:
# the concept still chooses the eligible item set, and this only ranks within it
# and decides which cells are candidates for which item at all.

def _cell_geometry(tiles: list[int], cell: tuple[int, int]) -> str:
    """Classify a cell by its orthogonal wall neighbours.

    free   0 walls        mid-floor
    wall   1 wall         a plain wall line
    corner 2 perpendicular   the single largest destination for solid props
    slot   2 opposite     a one-wide gap; the corpus fills these with pillars
    nook   3+ walls       a one-cell alcove; armor and bunks live here
    """
    x, y = cell
    solid = []
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        value = _at(tiles, x + dx, y + dy)
        # Off-map reads as wall; a door is an opening, not a backing.
        if value == -1 or (not _is_floor(value) and value not in DOORS):
            solid.append((dx, dy))
    if len(solid) >= 3:
        return "nook"
    if len(solid) == 2:
        (adx, ady), (bdx, bdy) = solid
        return "slot" if (adx == -bdx and ady == -bdy) else "corner"
    return "wall" if solid else "free"


def _wall_orientation(tiles: list[int], cell: tuple[int, int]) -> str | None:
    """Classify a one-wall cell by the sightline away from its backing wall.

    The normal run starts beyond the prop cell; the along run is the longer of
    the two directions parallel to the wall.  This distinguishes a corridor
    terminus from its plentiful long-side wall cells without changing the
    original count-based geometry classes.
    """
    x, y = cell
    solid = [(dx, dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
             if (value := _at(tiles, x + dx, y + dy)) == -1
             or (not _is_floor(value) and value not in DOORS)]
    if len(solid) != 1:
        return None
    wx, wy = solid[0]
    nx, ny = -wx, -wy

    def run(dx: int, dy: int) -> int:
        length = 0
        while _is_floor(_at(tiles, x + dx * (length + 1), y + dy * (length + 1))):
            length += 1
        return length

    normal = run(nx, ny)
    along = max(run(-ny, nx), run(ny, -nx))
    if normal >= 4 and normal >= 2 * along:
        return "terminus"
    if along >= 2 * normal:
        return "flank"
    return "neutral"


# These probabilities select a terminus candidate instead of the ordinary
# shuffled candidate.  They close the measured generated-to-authored gap for
# upright display families without forcing every such item into a dead end.
_TERMINUS_PREFERENCE = {
    26: 0.41,  # FloorLamp
    31: 0.34,  # GreenPlant
    34: 0.37,  # BrownPlant
    35: 0.23,  # Vase
    36: 0.19,  # BareTable
    39: 0.22,  # SuitOfArmor
    62: 0.22,  # Flag
    69: 0.22,  # SpearRack
}


def _prefer_terminus(cells: list[tuple[int, int]], tiles: list[int],
                      rng: random.Random, chance: float) -> list[tuple[int, int]]:
    """Shuffle candidates, then occasionally put terminus cells first.

    Partitioning after the shuffle keeps positions random within each
    orientation while making the orientation decision a cell-selection rule.
    """
    rng.shuffle(cells)
    if rng.random() < chance:
        return ([cell for cell in cells
                 if _wall_orientation(tiles, cell) == "terminus"]
                + [cell for cell in cells
                   if _wall_orientation(tiles, cell) != "terminus"])
    return cells


def _cell_openness(tiles: list[int], cell: tuple[int, int]) -> str:
    """How open the surroundings are: floor cells in the 5x5 box around it."""
    x, y = cell
    count = sum(1 for dx in range(-2, 3) for dy in range(-2, 3)
                if _is_floor(_at(tiles, x + dx, y + dy)))
    return "tight" if count <= 9 else "medium" if count <= 17 else "open"


def occupy_dead_end_alcoves(tiles: list[int], things: list[int], rooms: list[Room],
                            identities: list[RoomIdentity], reserved: set[tuple[int, int]],
                            rng: random.Random, start: tuple[int, int]
                            ) -> tuple[tuple[int, int], ...]:
    """Put a themed open prop in every qualifying gameplay dead-end alcove.

    A one-cell dead end with nothing in it reads as a hole cut in a wall for no
    reason, so `validate_map` refuses one. That makes occupancy mandatory
    rather than budgeted: this pass is allowed past the 320 soft cap, because
    the alternative is rejecting an otherwise good floor over a handful of
    props. Only ECWolf's own 400-static limit is a genuine wall.
    """
    static_count = sum(23 <= thing <= 74 for thing in things)
    occupied = []
    for cell in qualifying_dead_end_alcoves(tiles, things, start):
        x, y = cell
        if static_count >= 400:
            raise ValueError("engine static limit prevents dead-end alcove occupancy")
        owner = next((index for index, room in enumerate(rooms)
                      if room.x <= x < room.x + room.w and room.y <= y < room.y + room.h), None)
        if owner is None:
            owner = next((index for index, room in enumerate(rooms)
                          if any(room.x <= nx < room.x + room.w and room.y <= ny < room.y + room.h
                                 for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))), None)
        concept = identities[owner].concept if owner is not None and owner < len(identities) else "corridor"
        open_items = _DECOR_OPEN.get(concept, STATIC_OPEN)
        nook_items = [item for item, _ in
                      _FILL_BUCKETS.get(("nook", _cell_openness(tiles, cell)), ())
                      if item in open_items]
        item = rng.choice(nook_items or list(open_items))
        _set(things, x, y, item)
        ledger_reserve(reserved, [cell], "decorations", "dead-end-alcove")
        static_count += 1
        occupied.append(cell)
    return tuple(occupied)


# P(item | geo, space), as (item, weight) pairs per bucket. Weights are the
# corpus percentages, so a bucket's mix is reproduced directly. Ceiling fixtures
# (27/37) are deliberately absent: Stage B owns lighting on its own rhythm, and
# letting fill re-add them would break that spacing.
_FILL_BUCKETS: dict[tuple[str, str], tuple[tuple[int, float], ...]] = {
    # corner is 22.1% of all authored decoration -- the single biggest target.
    # FloorLamp (26) is absent from every bucket even though the corpus puts it
    # in corners 76% of the time: it is a LIGHTING_ITEM, so the dedicated
    # floor-lamp pass owns it outright and fill must not stack more beside it.
    ("corner", "medium"): ((31, 28.1), (34, 14.5), (30, 12.0), (35, 6.5),
                           (62, 6.1), (24, 4.9), (58, 3.6), (25, 1.7)),
    ("corner", "tight"): ((31, 19.0), (34, 6.8), (30, 5.0), (58, 4.0)),
    ("corner", "open"): ((31, 24.0), (34, 12.0), (30, 14.0), (62, 6.0)),
    # Alcoves: suits of armor sit in a nook 80% of the time, bunk beds 51%.
    ("nook", "medium"): ((39, 29.0), (30, 19.0), (28, 12.0), (45, 11.0), (33, 4.0)),
    ("nook", "tight"): ((39, 16.0), (28, 11.0), (30, 11.0), (45, 8.0),
                        (68, 5.7), (33, 4.7)),
    ("nook", "open"): ((39, 20.0), (30, 16.0), (45, 10.0)),
    # A one-wide gap in an open room is a pillar, 67% of the time.
    ("slot", "open"): ((30, 67.0), (24, 5.0), (58, 4.0)),
    ("slot", "medium"): ((30, 27.0), (24, 7.2), (58, 5.0), (36, 3.0)),
    ("slot", "tight"): ((30, 20.0), (24, 5.0)),
    ("wall", "medium"): ((24, 10.1), (25, 8.9), (58, 8.5), (30, 5.5), (31, 5.5),
                         (61, 5.0), (42, 4.0), (36, 3.5), (62, 3.0)),
    ("wall", "open"): ((30, 15.0), (24, 11.0), (25, 9.8), (58, 6.8), (36, 4.0)),
    ("wall", "tight"): ((24, 8.0), (58, 8.0), (30, 5.0), (61, 5.0), (42, 4.0),
                        (64, 2.5), (65, 2.0)),
    # Free cells are mostly ceiling in the corpus; with lighting excluded only
    # the genuinely free-standing furniture remains, which is why this is thin.
    ("free", "open"): ((25, 7.4), (30, 6.1), (67, 3.0), (61, 2.9), (46, 2.1)),
    ("free", "medium"): ((67, 5.9), (46, 5.0), (25, 1.5), (64, 2.5), (65, 1.5),
                         (61, 1.4), (23, 1.1), (66, 1.0)),
    ("free", "tight"): ((67, 2.0),),
}
# Order fill visits geometry classes in. Corner first is the headline finding.
_FILL_ORDER = ("corner", "nook", "slot", "wall", "free")
# Wall-material affinities worth encoding, as a soft multiplier. The corpus
# signal here is the weakest of the four and most tail items are too thin to
# trust, so only the wood-domestic and dungeon-gore families get a nudge.
_MATERIAL_AFFINITY: dict[str, frozenset[int]] = {
    "wood": frozenset({38, 36, 45, 33, 68}),
    "blue-stone": frozenset({42, 64, 65, 66, 32, 57, 61}),
    "damp-grey-stone": frozenset({42, 64, 65, 66, 32, 57, 61, 59, 60}),
    "chipped-stone": frozenset({41, 40}),
    "metal": frozenset({67, 24}),
}
_MATERIAL_MULTIPLIER = 1.75
# Authored decoration sits at 0.134 items per floor cell. Fill aims at that and
# stops; it is a target, not a floor to be saturated toward.
_TARGET_DECOR_DENSITY = 0.134
# Keep fill spaced so it reads as furnishing rather than a pile. The corpus
# clusters 18.6% of decorations against a neighbour, so some contact is correct
# -- barrels are shoulder to shoulder -- but it must not be the default.
_FILL_MIN_SPACING = 2
# Every static decoration code, for counting what a room already carries.
_ALL_DECOR = frozenset(STATIC_BLOCKING) | frozenset(STATIC_OPEN)

# Decoration that visually occupies the floor. Ceiling fixtures are excluded: a
# barrel standing under a light is not two props crowded together, and counting it
# as such would make every lattice cell look clustered.
_FLOOR_DECOR = _ALL_DECOR - LIGHTING_ITEMS

# Items allowed to sit shoulder to shoulder with a floor neighbour. Barrels are the
# genuine case -- stride 1 dominates their runs in the corpus, which clusters them
# at 53% and 47% against our 38% and 34%, so they are if anything under-stacked.
# Ceiling fixtures are exempt because they are overhead and already spaced by their
# own lattice.
_MAY_ABUT = frozenset({24, 58}) | LIGHTING_ITEMS
# Open items may sit against a floor lamp (an edge fixture that reads as "lit
# corner") but not against a ceiling light, chandelier, or floor prop.  Ceiling
# lights were the primary clustering source; chandeliers are now also excluded
# because gallery/officers-quarters rooms carry both Basket and Chandelier and
# the two tend to land adjacent when both are permitted.
_NO_ABUT_DECOR = _ALL_DECOR - frozenset({26})


def _spaced_from_neighbours(things: list[int], cell: tuple[int, int],
                            item: int) -> bool:
    """Whether a single prop may go here without crowding a floor neighbour.

    Measured per item against the authored corpus, the passes that commit one prop
    at a time had no spacing rule at all and clustered badly: Pots 74% against the
    corpus's 21%, TableChairs 51% against 11%, SuitOfArmor 40% against 2%. Only the
    density fill was checking, so everything the open-item, accent, alcove and niche
    passes placed piled against whatever was already there.

    Deliberately not applied to multi-cell commits. A matched pair, a signature or
    a banquet row is *meant* to read as one group, and spacing those apart would
    dismantle the compositions this generator exists to make.
    """
    if item in _MAY_ABUT:
        return True
    x, y = cell
    return not any(_at(things, x + dx, y + dy) in _NO_ABUT_DECOR
                   for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))


def _material_behind(tiles: list[int], cell: tuple[int, int]) -> str | None:
    """Name of the wall material family backing a cell, if any."""
    x, y = cell
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        value = _at(tiles, x + dx, y + dy)
        if value != -1 and not _is_floor(value) and value not in DOORS:
            for family in WALL_MATERIALS:
                if value == family.base or value in family.accents:
                    return family.name
    return None


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
                       allow_sky_vista: bool = True,
                       force_motif: str = "",
                       ) -> tuple[tuple[str, ...], tuple[VineScreen, ...],
                                  tuple[str, ...]]:
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
    # Flags and suits of armour are punctuation, not texture. Authored maps average
    # 4.6 flags and 3.2 suits; with no floor-level budget the concept frames, the
    # signatures and the density fill each placed their own and the total reached
    # 15.6 flags per map, which reads as a parade ground rather than a castle.
    wall_display_budget = 8
    doorway_frames_placed = 0
    lighting_counts: Counter[str] = Counter()
    lighting_families = ["none"] * len(rooms)
    # Indexed by room, not appended: rooms too small to compose are skipped
    # before the motif is chosen, so an append would misalign every entry
    # after the first closet.
    room_motifs = [""] * len(rooms)
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

        # --- Stage B: ambient lighting, before any furniture spends budget ---
        # Lighting is allocated first on purpose: when a floor is close to the
        # statics cap it should lose filler props, never its lights. Ceiling
        # fixtures (27/37) have no collision at all, so this pass cannot affect
        # reachability, patrols, or combat -- it is purely what the room looks
        # like. Solid floor lamps are left to the furniture passes below, which
        # already re-check reachability, so a floor-lamp room is guaranteed its
        # ceiling-free minimum here only when it has no lamp yet.
        if not existing_lights:
            fixture = next(iter(LIGHTING_FAMILY_ITEMS[lighting]), None)
            if fixture is not None and fixture not in STATIC_BLOCKING:
                stride = _FIXTURE_STRIDES[0] if max(room.w, room.h) >= 10 else _FIXTURE_STRIDES[1]
                rhythm, spare = _fixture_lattice(room, tiles, stride)
                placed = 0
                for cell in rhythm:
                    if placed >= _MAX_FIXTURES_PER_ROOM or static_headroom <= 0:
                        break
                    if cell in reserved or _at(things, *cell) != 0:
                        continue
                    _set(things, *cell, fixture)
                    ledger_reserve(reserved, [cell], "decorations",
                                   "ceiling-fixture-lattice")
                    static_headroom -= 1
                    placed += 1
                # The lattice can be fully occupied (small rooms have only one
                # cell on it). Take the nearest free cell instead of going dark,
                # but only enough to light the room, never the whole fallback.
                for cell in spare if not placed else ():
                    if static_headroom <= 0:
                        break
                    if cell in reserved or _at(things, *cell) != 0:
                        continue
                    _set(things, *cell, fixture)
                    ledger_reserve(reserved, [cell], "decorations",
                                   "ceiling-fixture-spare")
                    static_headroom -= 1
                    placed += 1
                    break

        # Area, not minimum dimension. The old `w < 5 or h < 5` test excluded
        # every hallway-first corridor from furniture entirely, because
        # validate_map pins those at exactly 3 cells wide -- so a hallway got
        # ambient light and nothing else, and the traversal-pair composition
        # named for hallways only ever ran in wide hall-tier rooms.
        #
        # A 3-wide corridor is structurally safe to furnish: `interior` reduces
        # to the single middle lane, which is already in `keep_clear` as the
        # travel path, so props can only reach the two flanking lanes through
        # `edge_free`. The heavy vignettes exclude themselves on their own size
        # and concept guards (divider 10x10, colonnade 8x8, centerpiece 9x9,
        # signatures 6x6, and "corridor" has no zoning entry), and pair_budget
        # is 1 at this height, so what a hallway gains is one matched pair, its
        # corner lamps, and a modest density fill along the edges.
        if room.w * room.h < 20:
            continue

        blocking = _DECOR_BLOCKING.get(concept,
                                       _DECOR_BLOCKING.get(theme, STATIC_BLOCKING))
        open_items = _DECOR_OPEN.get(concept,
                                     _DECOR_OPEN.get(theme, STATIC_OPEN))
        # Barrels are a room-level material language, not an independent prop
        # roll. Damp/crypt contexts use green barrels, formal military rooms
        # use blue, and neutral storage chooses once for the whole room.
        green_barrel_concepts = {
            "jail", "crypt", "ossuary", "burial-chamber", "holding-cell",
            "courtyard", "storage", "supply-cache", "workshop",
        }
        # Blue reads as issued military stock, green as older or organic stores.
        # The corpus carries more green than blue (9.5 against 6.7), and store
        # rooms are where barrels actually pile up, so stores take green while the
        # formal military spaces keep blue.
        blue_barrel_concepts = {
            "guardpost", "checkpoint", "armory", "war-room",
            "barracks", "ready-room", "training-room", "officers-quarters",
        }
        existing_barrels = {
            _at(things, x, y)
            for y in range(room.y, room.y + room.h)
            for x in range(room.x, room.x + room.w)
            if _at(things, x, y) in {24, 58}
        }
        if len(existing_barrels) == 1:
            barrel_item = next(iter(existing_barrels))
        elif concept in green_barrel_concepts or theme in {"jail", "crypt"}:
            barrel_item = 24
        elif concept in blue_barrel_concepts or theme in {"grand", "barracks"}:
            barrel_item = 58
        elif any(item in (24, 58) for item in blocking):
            # Corpus ratio rather than a guess: green barrels appear 9.5 times per
            # authored map against blue's 6.7. The old 3:2 favoured blue and gave
            # 16.1 blue against 2.3 green -- the right total, the wrong colour
            # almost every time.
            barrel_item = rng.choices((24, 58), weights=(10, 7), k=1)[0]
        else:
            # Preserve the established decoration RNG stream for concepts
            # that cannot place a barrel.  Choosing an unused material here
            # used to perturb unrelated optional signatures such as sinks.
            barrel_item = 58
        blocking = tuple(dict.fromkeys(
            barrel_item if item in (24, 58) else item for item in blocking))
        wants_vase = 35 in blocking
        # Vases are placed by the dedicated wall-accent pass below. Removing
        # them here prevents zone clusters, pairs, signatures, and fallback
        # compositions from independently adding more.
        blocking = tuple(item for item in blocking if item != 35)
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
        # A room only three cells across has exactly one through-route: its
        # centre lane. Two gaps let props onto it -- traversal.path covers the
        # lane only when the frame actually found entries, and the lane's end
        # cells lie on the perimeter ring, so edge_free reaches them even when
        # it does. Reachability does not catch this: the flood fill still finds
        # a way through the doorway, so the corridor validates while being
        # miserable to walk. Protect the lane outright.
        if min(room.w, room.h) <= 3:
            if room.h <= room.w:
                keep_clear.update((x, room.y + room.h // 2)
                                  for x in range(room.x, room.x + room.w))
            else:
                keep_clear.update((room.x + room.w // 2, y)
                                  for y in range(room.y, room.y + room.h))
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
        # Floor lamps are rationed at this one choke point, because every
        # blocking path commits through here. Eight paths can ask for item 26 --
        # the guardpost/checkpoint signatures, concept frames, _FRAMEABLE
        # landmark and doorway frames, traversal pairs, the zoned blocking sets,
        # notch accents and Stage C fill -- and because each rolled
        # independently, one 9x9 room came out with six lamps, two of them side
        # by side, against a corpus rate of 6.2 per *map*.
        #
        # The rule is about the shape of the request, not about which pass is
        # asking. A matched pair flanking a landmark wall, or bisecting
        # door-to-door travel, is exactly how authored maps use floor lamps away
        # from corners (24% of the 1,283 in the corpus are not in a corner). So
        # any owner may commit an atomic *pair*; only the dedicated corner pass
        # below may commit singles. What the corpus never does is stack them --
        # 3 runs in all 43,122 instances -- or crowd a room, so pairs must not
        # abut and the room total is capped either way.
        placing_lamps = False
        room_lamps: list[tuple[int, int]] = []
        # Area, not minimum dimension. A hallway is exactly three cells wide
        # (validate_map enforces it), so a min-dimension rule capped every real
        # corridor at one lamp and silently rejected the matched pair that
        # bisects door-to-door travel -- the one composition a corridor has.
        lamp_cap = 2 if room.w * room.h >= 20 else 1
        composed_cells: set[tuple[int, int]] = set()

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
            nonlocal wall_display_budget
            displays = sum(item in (39, 62) for _, item in pieces)
            if displays > wall_display_budget:
                return False
            lamp_cells = [cell for cell, item in pieces if item == 26]
            if lamp_cells:
                # Never in open floor. The corpus puts 76% of its 1,283 floor
                # lamps in a true corner, 17% along a wall and 1% free, so a
                # lamp with floor on all four sides is the one arrangement
                # authored maps essentially never use -- and it is what reads as
                # a lamp abandoned mid-room. This rejects the guardpost
                # signature's (room.x + 1, room.y + 1) diagonal slots and any
                # mid-aisle traversal pair; those compositions fall back to a
                # non-lamp prop from the same palette.
                if any(_cell_geometry(tiles, cell) == "free"
                       for cell in lamp_cells):
                    return False
                if not placing_lamps and len(lamp_cells) != 2:
                    return False
                if len(room_lamps) + len(lamp_cells) > lamp_cap:
                    return False
                occupied = set(room_lamps) | set(lamp_cells)
                if any((lx + dx, ly + dy) in occupied
                       for lx, ly in lamp_cells
                       for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                    return False
            # Suits of armor, flags, and spear racks are wall displays,
            # never freestanding furniture.
            if any(item in (39, 62, 69) and not _wall_backed(cell)
                   for cell, item in pieces):
                return False
            # Spacing applies to every commit, but a composition is only exempt
            # from *its own* members. A matched pair is meant to read as a group;
            # it is not meant to be planted beside an unrelated barrel. Exempting
            # multi-cell groups wholesale left that second case unchecked, and it
            # was the diffuse remainder of this generator's excess clustering
            # after the obvious shoulder-to-shoulder shapes were fixed.
            own = set(cells)
            for cell, item in pieces:
                if item in _MAY_ABUT:
                    continue
                x, y = cell
                if any((x + dx, y + dy) not in own
                       and _at(things, x + dx, y + dy) in _FLOOR_DECOR
                       for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                    return False
            candidate = blocked_cells | set(cells)
            if len(_reachable(tiles, start, locked_open=True, blocked=candidate)) < baseline - len(candidate):
                return False
            for c, item in pieces:
                _set(things, *c, item)
                ledger_reserve(reserved, [c], "decorations",
                               "blocking-composition")
                blocked_cells.add(c)
                room_blocked.append(c)
                if item == 26:
                    room_lamps.append(c)
                free.discard(c)
                edge_free.discard(c)
            # Anything committed as a multi-cell group is a composition, and
            # its members' positions are load-bearing relative to each other.
            # Record that here so the flush-to-wall repair below cannot pull
            # one half of a matched pair a cell sideways and silently break the
            # symmetry the pass was placed to create.
            if len(cells) >= 2:
                composed_cells.update(cells)
            wall_display_budget -= displays
            static_headroom -= len(cells)
            return True

        def _try_place(cells: list[tuple[int, int]], item: int) -> bool:
            return _try_place_items([(cell, item) for cell in cells])

        def _place_open(cell: tuple[int, int], item: int,
                        spaced: bool = True) -> bool:
            """Commit one non-solid item.

            `spaced=False` marks a deliberate attachment -- spill beside a crate,
            an accent under a landmark -- which is authored-looking and must be
            allowed to touch its anchor. Everything else keeps its distance.
            """
            nonlocal static_headroom, wall_display_budget
            if item in (39, 62):
                if wall_display_budget <= 0:
                    return False
                wall_display_budget -= 1
            if item in LIGHTING_ITEMS and item not in allowed_lights:
                return False
            if static_headroom <= 0 or _at(things, *cell) != 0:
                return False
            if spaced and not _spaced_from_neighbours(things, cell, item):
                return False
            _set(things, *cell, item)
            ledger_reserve(reserved, [cell], "decorations",
                           "open-prop")
            free.discard(cell)
            edge_free.discard(cell)
            static_headroom -= 1
            return True

        # --- Stage B, solid half: guarantee the floor-lamp rooms their light ---
        # The lattice pass above only places non-solid ceiling fixtures, so a
        # room whose family is the standing lamp would still be dark. Corpus
        # floor lamps are corner furniture (76% sit in a corner) and essentially
        # never run in rows, so one or two corners is the authored shape. This
        # goes through _try_place, so reachability is re-checked as usual.
        # Floor-lamp rooms are handled *after* the composition instead of before
        # it, by the guarantee pass at the end of this loop. A dedicated
        # up-front lamp pass was tried and reverted: floor lamps are already
        # placed deliberately by the guardpost/checkpoint signatures, the
        # concept frames, _FRAMEABLE landmark and doorway frames, the zoned
        # blocking sets, the notch accents and the alcove niche. Adding a
        # seventh source on top produced six lamps in one 9x9 room with two side
        # by side, against a corpus rate of 6.2 per *map* that forms 3 runs in
        # all 43,122 instances. The authored placements were fine; the problem
        # was competing with them.

        if wants_vase and static_headroom > 0:
            vase_cells = [cell for cell in edge_free
                          if cell not in keep_clear and _wall_backed(cell)]
            vase_cells = _prefer_terminus(vase_cells, tiles, rng,
                                          _TERMINUS_PREFERENCE[35])
            for cell in vase_cells:
                if _try_place([cell], 35):
                    break

        # A sky tile is a view beyond the building, never ordinary wallpaper.
        # Open one broad, odd-span bay through the former wall plane and put
        # the sky on the next plane outward. Pillars stay on that original
        # wall line, so the visible one-cell recess supplies the depth cue the
        # old pillar-directly-on-sky trick lacked.
        if (allow_sky_vista and not sky_composition_placed
                and concept in {"courtyard", "gallery", "trophy-hall"}
                and min(room.w, room.h) >= 7
                and rng.random() < (SKY_VISTA_COURTYARD_CHANCE
                                    if concept == "courtyard"
                                    else SKY_VISTA_INTERIOR_CHANCE)):
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
            side_candidates = list(side_specs)
            rng.shuffle(side_candidates)
            for side in side_candidates:
                spans = [span for span in (9, 7, 5) if span <= len(side)]
                rng.shuffle(spans)
                for span in spans:
                    starts = list(range(len(side) - span + 1))
                    center_start = (len(side) - span) / 2
                    starts.sort(key=lambda value: (abs(value - center_start), value))
                    for start_index in starts:
                        aperture = side[start_index:start_index + span]
                        geometry = []
                        valid = True
                        for interior_cell, wall_cell in aperture:
                            ix, iy = interior_cell
                            wx, wy = wall_cell
                            dx, dy = wx - ix, wy - iy
                            sky_cell = (wx + dx, wy + dy)
                            if (interior_cell not in edge_free
                                    or wall_cell in reserved
                                    or _at(things, *wall_cell) != 0
                                    or not (1 <= sky_cell[0] < GRID - 1
                                            and 1 <= sky_cell[1] < GRID - 1)
                                    or _at(tiles, *wall_cell) in (
                                        DECOR_WALLS | SPECIAL_WALL_TILES | DOORS)
                                    or _is_floor(_at(tiles, *wall_cell))
                                    or _at(tiles, *sky_cell) in (
                                        DECOR_WALLS | SPECIAL_WALL_TILES | DOORS)
                                    or _is_floor(_at(tiles, *sky_cell))):
                                valid = False
                                break
                            ox, oy = sky_cell[0] + dx, sky_cell[1] + dy
                            while 0 <= ox < GRID and 0 <= oy < GRID:
                                if (_is_floor(_at(tiles, ox, oy))
                                        or _at(tiles, ox, oy) in DOORS):
                                    valid = False
                                    break
                                ox += dx
                                oy += dy
                            if not valid:
                                break
                            geometry.append((interior_cell, wall_cell, sky_cell))
                        # Recessing the original wall makes the two end-cap
                        # cells newly visible from this floor component. A
                        # bay at a district seam can therefore expose the
                        # neighboring district's material even though it
                        # does not connect to that district's floor. Keep the
                        # complete reveal in the aperture wall's family.
                        if valid and len(geometry) >= 2:
                            wall_tiles = {
                                _at(tiles, *wall_cell)
                                for _, wall_cell, _ in geometry
                            }
                            material_family = next((
                                {material.base, *material.accents}
                                for material in WALL_MATERIALS
                                if wall_tiles <= {
                                    material.base, *material.accents}
                            ), None)
                            tx = geometry[1][1][0] - geometry[0][1][0]
                            ty = geometry[1][1][1] - geometry[0][1][1]
                            flanks = (
                                (geometry[0][1][0] - tx,
                                 geometry[0][1][1] - ty),
                                (geometry[-1][1][0] + tx,
                                 geometry[-1][1][1] + ty),
                            )
                            theme_tiles = {
                                tile for material in WALL_MATERIALS
                                for tile in (material.base, *material.accents)
                            }
                            if (material_family is None
                                    or any(_at(tiles, *cell) in theme_tiles
                                           and _at(tiles, *cell)
                                           not in material_family
                                           for cell in flanks)):
                                valid = False
                        supports = tuple(range(span))
                        if not valid or static_headroom < len(supports):
                            continue
                        for interior_cell, wall_cell, sky_cell in geometry:
                            _set(tiles, *wall_cell, _at(tiles, *interior_cell))
                            _set(tiles, *sky_cell, 16)
                            # The complete original wall plane belongs to the
                            # vista, including its open gaps. Reserving it
                            # prevents the later tiny-alcove fallback from
                            # scattering an unrelated skeleton/plant between
                            # the architectural supports.
                            ledger_reserve(reserved, [wall_cell], "decorations",
                                           "sky-vista-support")
                        for support in supports:
                            cell = geometry[support][1]
                            _set(things, *cell, 30)
                            blocked_cells.add(cell)
                            room_blocked.append(cell)
                        static_headroom -= len(supports)
                        sky_composition_placed = True
                        break
                    if sky_composition_placed:
                        break
                if sky_composition_placed:
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
                    ledger_reserve(reserved, [cell], "decorations",
                                   "landmark-frame-clearance")
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
                            if item in (24, 26, 31, 34, 58, 62, 69))
            notch_item = rng.choice(compact or (31,))
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
        room_landmarks = list((landmarks or {}).get(ridx, ()))
        # Structural eligibility only -- no rolls here. Each condition mirrors the
        # one its motif's own block still applies, so a motif can decline once
        # chosen (a signature whose lighting family conflicts, a divider with no
        # clean span) and the room simply stays plain rather than falling through
        # to whichever composition happened to be next in the file.
        eligible_motifs = {
            "signature": room.w >= 6 and room.h >= 6,
            "travel-pair": bool(travel_pairs),
            "landmark-frame": bool(room_landmarks),
            "doorway-frame": bool(anchors.door_entries) and doorway_frames_placed < 3,
            "divider": (room.w >= 10 and room.h >= 10
                        and theme in ("grand", "barracks", "guardpost")),
            "corner-stash": bool(blocking) and theme in ("storage", "barracks", "jail"),
            "colonnade": theme == "grand" and room.w >= 8 and room.h >= 8,
            "banquet-row": (concept in ("barracks", "dining-hall")
                            and max(room.w, room.h) >= 8),
            "courtyard-centerpiece": (concept in ("courtyard", "storage")
                                      and room.w >= 9 and room.h >= 9),
        }
        # The two chance parameters predate motif selection: they used to be the
        # probabilities of those specific gates. Preserved as weight overrides so a
        # caller can still force or suppress one composition -- 1.0 forces, 0.0
        # suppresses, anything else leaves the normal weighting alone.
        motif_overrides: dict[str, float] = {}
        if traversal_pair_chance is not None:
            if traversal_pair_chance >= 1.0:
                motif_overrides["travel-pair"] = _MOTIF_FORCED
            elif traversal_pair_chance <= 0.0:
                motif_overrides["travel-pair"] = 0.0
        if landmark_frame_chance >= 1.0:
            motif_overrides["landmark-frame"] = _MOTIF_FORCED
        elif landmark_frame_chance <= 0.0:
            motif_overrides["landmark-frame"] = 0.0
        # A named seam for examining one composition in isolation. Selection is a
        # weighted draw, so a test that wants to inspect the barracks signature
        # would otherwise have to hunt seeds until that motif happened to win --
        # which passes for the wrong reason the moment the weights change.
        if force_motif:
            motif_overrides[force_motif] = _MOTIF_FORCED
        motif = _choose_motif(eligible_motifs, rng, motif_overrides)
        room_motifs[ridx] = motif
        concept_frames = {
            # Guardpost keeps a green-plant companion so its matched pair can
            # still land when the candidate cells are open floor and the lamp is
            # therefore refused. Not a vase: item 35 is a singular wall accent
            # owned by its own pass, and `blocking` strips it for exactly this
            # reason -- concept_frames bypasses that filter, so naming it here
            # would let a guardpost mount two. Plants are legitimately paired
            # (_place_zoned says so in as many words) and are corner furniture
            # in the corpus at 72-84%.
            "war-room": (39, 62), "armory": (39, 62), "guardpost": (26, 31),
            "lounge": (31, 34),
            "courtyard": (31, 34), "checkpoint": (26, 62),
            "trophy-hall": (39, 62), "gallery": (34, 39, 62),
            "officers-quarters": (34,),
        }
        frame_pool = tuple(
            item for item in concept_frames.get(
                concept, tuple(item for item in blocking if item in _FRAMEABLE))
            if item not in LIGHTING_ITEMS or item in allowed_lights)
        if not frame_pool:
            frame_pool = tuple(item for item in blocking
                               if item not in LIGHTING_ITEMS) or (31,)

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
        if motif == "travel-pair" and travel_pairs and pairs_placed < pair_budget:
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
        if motif == "landmark-frame" and room_landmarks and pairs_placed < pair_budget:
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
        if motif == "signature" and room.w >= 6 and room.h >= 6 and pairs_placed < pair_budget:
            signatures: dict[str, list[tuple[tuple[int, int], int]]] = {
                "barracks": [((room.x + 1, room.y + 1), 45),
                              ((room.x + room.w - 2, room.y + room.h - 2), 45)],
                "ready-room": [((room.x + 1, room.y + 1), 45),
                               ((room.x + room.w - 2, room.y + 1), 36)],
                "training-room": [((room.x, cy), 69),
                                  ((room.x + room.w - 2, cy), 36)],
                "armory": [((room.x, cy), 69),
                            ((room.x + room.w - 1, cy), 69)],
                # True corners, not the (+1, +1) diagonal slot this used to
                # use: that cell has floor on all four sides, so the lamp read
                # as abandoned mid-room. room.x/room.y are already the first
                # floor column and row, with wall immediately outside.
                "guardpost": [((room.x, room.y), 26),
                               ((room.x + room.w - 1, room.y), 26)],
                "checkpoint": [((room.x, cy), 62)],
                "war-room": [((room.x, room.y + 1), 39),
                              ((room.x + room.w - 1, room.y + 1), 39)],
                "trophy-hall": [((room.x, cy), 39),
                                ((room.x + room.w - 1, cy), 62)],
                "courtyard": [((cx, cy), 59)],
                "storage": [((room.x + 1, room.y + 1), barrel_item),
                             ((room.x + 2, room.y + 1), barrel_item)],
                "supply-cache": [((room.x + 1, room.y + 1), barrel_item),
                                  ((room.x + 2, room.y + 1), 59)],
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
                                   ((room.x + room.w - 2, room.y + 1), 30)],
                "holding-cell": [((room.x + 1, room.y + 1), 40),
                                 ((room.x + room.w - 2, room.y + 1), barrel_item)],
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
                    # Signatures are a major source of flags and armour.  Keep
                    # their composition identity, but when a terminus is drawn
                    # from the per-item stream, choose that wall-backed cell
                    # before committing the signature.
                    candidates = [cell for cell in free | edge_free
                                  if cell not in keep_clear and _wall_backed(cell)]
                    for index, (_, item) in enumerate(signature):
                        if (item not in _TERMINUS_PREFERENCE
                                or rng.random() >= _TERMINUS_PREFERENCE[item]):
                            continue
                        terminus = [cell for cell in candidates
                                    if _wall_orientation(tiles, cell) == "terminus"]
                        rng.shuffle(terminus)
                        if terminus:
                            chosen = terminus.pop()
                            signature[index] = (chosen, item)
                            candidates.remove(chosen)
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
        if (motif == "doorway-frame" and doorway_frames_placed < 3
                and pairs_placed < pair_budget and anchors.door_entries):
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
        if (motif == "divider" and room.w >= 10 and room.h >= 10
                and theme in ("grand", "barracks", "guardpost")):
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
        if (motif == "corner-stash" and blocking and pairs_placed < pair_budget
                and theme in ("storage", "barracks", "jail")):
            item = rng.choice(blocking)
            corners = list(anchors.corners)
            rng.shuffle(corners)
            for cornx, corny in corners:
                # Same rule as the zoned cluster: barrels may stack, a stash of
                # anything else leaves a gap so it reads as a pair of objects
                # rather than a pile.
                step = 1 if item in (24, 58) else 2
                nx = cornx + (step if cornx < cx else -step)
                ny = corny + (step if corny < cy else -step)
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
                        # Attached on purpose -- spill belongs against its stash --
                        # so it is exempt from spacing rather than silently refused.
                        _place_open(rng.choice(spill), spill_item, spaced=False)
                    break

        # --- Pattern: pillar colonnade (grand / anchor rooms) ---
        if (motif == "colonnade" and pairs_placed < pair_budget
                and theme == "grand" and room.w >= 8 and room.h >= 8):
            depth = max(2, min(room.w // 3, room.h // 3))
            # Rows two apart, not one. Offsets of -1/0/+1 put three pillars in
            # contact down each side, which is a block rather than a colonnade --
            # a colonnade reads by the gaps between its columns.
            for offset in (0, -2, 2):
                if pairs_placed >= pair_budget:
                    break
                a = (room.x + depth, cy + offset)
                b = (room.x + room.w - 1 - depth, cy + offset)
                if _try_place([a, b], 30):   # WhitePillar
                    pairs_placed += 1

        # --- Vignette: banquet row along the center axis ---
        # Tables march down the room's long axis in mirrored pairs, the way
        # authored mess halls are dressed, instead of landing on random cells.
        if (motif == "banquet-row" and pairs_placed < pair_budget
                and concept in ("barracks", "dining-hall")
                and max(room.w, room.h) >= 8):
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
        if (motif == "courtyard-centerpiece" and pairs_placed < pair_budget
                and concept in ("courtyard", "storage")
                and room.w >= 9 and room.h >= 9):
            if _try_place([(cx, cy)], 59 if concept == "storage" else 30):
                pairs_placed += 1

        zones = _DECOR_ZONES.get(concept) if concept == theme else None
        if zones:
            zones = tuple((tuple(dict.fromkeys(
                                 barrel_item if item in (24, 58) else item
                                 for item in solid
                                 if item != 35 and (item not in LIGHTING_ITEMS
                                                    or item in allowed_lights))),
                           # Ceiling fixtures are excluded outright, not merely
                           # filtered to the room's family: the Stage B lattice
                           # has already lit this room on a 3-4 tile rhythm, and
                           # a zone dropping its own lamp landed one adjacent to
                           # a lattice lamp in 13% of zoned rooms. Solid floor
                           # lamps still come through the blocking set above.
                           tuple(item for item in open_
                                 if item not in (27, 37)))
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
            # Furniture reads as "set against the walls," so both members of a
            # pair belong flush on the wall-backed outer ring.
            #
            # The old center-mirror `interior_pairs` fallback is deliberately
            # gone. It mirrored cells across the room centre on the *inner*
            # floor band -- one cell short of the wall -- which is precisely
            # what makes a prop read as floating, and it was the dominant source
            # of the 37.5% of solid props that stood free in open floor against
            # the corpus's 11.9%. It existed to hold density up when the wall
            # band was spoken for; Stage C now supplies that density from
            # wall-, corner- and nook-backed cells instead, so the fallback has
            # nothing left to do.
            if pairs_placed < pair_budget and blocking:
                backed = {cell for cell in edge_free
                          if cell not in keep_clear and _wall_backed(cell)}
                lr_pairs = [((room.x, y), (room.x + room.w - 1, y))
                            for y in range(room.y + 1, room.y + room.h - 1)
                            if (room.x, y) in backed
                            and (room.x + room.w - 1, y) in backed]
                tb_pairs = [((x, room.y), (x, room.y + room.h - 1))
                            for x in range(room.x + 1, room.x + room.w - 1)
                            if (x, room.y) in backed
                            and (x, room.y + room.h - 1) in backed]
                flush_pairs = lr_pairs + tb_pairs
                rng.shuffle(flush_pairs)
                # Travel-aware pairs stay ahead of wall-flush symmetry.
                ordered = travel_pairs + flush_pairs
                seen: set[tuple[tuple[int, int], tuple[int, int]]] = set()
                all_pairs = []
                for pair in ordered:
                    if pair in seen or pair[::-1] in seen:
                        continue
                    seen.add(pair)
                    all_pairs.append(pair)
                # One item family per room, chosen once. Re-rolling
                # rng.choice(blocking) per candidate pair -- including for every
                # rejected pair -- is what produced rooms holding a lamp pair
                # *and* a plant pair *and* a barrel pair: a grab bag rather than
                # a composition.
                pair_item = rng.choice(blocking)
                for (ax, ay), (bx, by) in all_pairs:
                    if pairs_placed >= pair_budget:
                        break
                    if _try_place([(ax, ay), (bx, by)], pair_item):
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
            # Floor clutter sits beside furniture or hugs a wall midpoint.
            # Nothing floats on a random mid-room cell.
            #
            # Ceiling fixtures are deliberately absent here. The Stage B lattice
            # above owns lighting for the whole room; this pass used to add its
            # own fixtures on the centre axis as well, which put a second lamp
            # within two tiles of a lattice lamp and undid the 3-4 tile rhythm
            # the corpus shows. One pass, one rhythm.
            area = room.w * room.h
            open_budget = max(1, round((3 if area >= 80 else 2 if area >= 45 else 1) * density))
            count = rng.randrange(0, open_budget + 1)
            floor_clutter = [item for item in open_items if item not in (27, 37)]
            # Two kinds of spot, and they get different rules. Spill deliberately
            # attached to furniture may touch it -- that is the authored look, a
            # crate with a basket beside it -- but it is capped at one per room,
            # because the corpus clusters Pots at 21% and Basket at 24%, not the
            # 74% and 53% an uncapped attachment pass produced. Wall-midcell
            # clutter is ordinary furnishing and keeps its distance.
            attached: list[tuple[tuple[int, int], int]] = []
            spaced_spots: list[tuple[tuple[int, int], int]] = []
            if floor_clutter:
                beside = [(x + dx, y + dy) for x, y in room_blocked
                          for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
                          if (x + dx, y + dy) in free]
                rng.shuffle(beside)
                # Occasional, not standard. Clustering counts both members of an
                # adjacent pair, so one attachment in every room would put roughly
                # 18% of a floor's decoration in contact by itself -- the whole of
                # the corpus's clustering budget spent on spill. The corpus puts
                # Pots in contact 21% of the time and Basket 24%, meaning most are
                # free-standing, so a third of rooms get spill and the rest do not.
                # Prestige concepts (war-room, gallery, officers' quarters …) have
                # floor_clutter only because fill needs these items eligible there.
                # Actual spill beside a desk or suit of armor would read as
                # combat debris rather than the tidy-but-furnished look those
                # rooms carry in the corpus, so suppress the attachment roll there.
                _NO_SPILL = frozenset({"war-room", "gallery", "trophy-hall",
                                       "officers-quarters", "grand"})
                attached = ([(cell, rng.choice(floor_clutter))
                             for cell in beside[:1]]
                            if beside and rng.random() < 0.33
                            and concept not in _NO_SPILL else [])
                mids = [cell for cell in anchors.wall_midcells if cell in free]
                rng.shuffle(mids)
                spaced_spots = [(cell, rng.choice(floor_clutter)) for cell in mids]
            placed_open = 0
            for cell, item in attached:
                if placed_open >= count:
                    break
                if _place_open(cell, item, spaced=False):
                    placed_open += 1
            # Keep trying spots rather than consuming the budget on refusals: a
            # rejected cell used to lose its slot outright, which cost vocabulary
            # because the rarer clutter items simply stopped appearing.
            for cell, item in spaced_spots:
                if placed_open >= count:
                    break
                if _place_open(cell, item):
                    placed_open += 1

        # --- Floor lamps: the sole owner, in corners, at most two ---
        # Gated by `placing_lamps` so no other pass can add item 26. Runs before
        # Stage C fill because fill also wants corner cells (plants and pillars
        # live there); when fill went first it took every corner and the lamp had
        # nowhere to go.
        #
        # Corpus shape: 76% of the 1,283 authored floor lamps sit in a true
        # corner, 17% along a wall, 1% free. They form 3 runs in all 43,122
        # decoration instances and pair 0-3% of the time, so one or two per room
        # is the authored idiom and a row never is. The old signature positions
        # like (room.x + 1, room.y + 1) sit one step diagonally in from the
        # corner with floor on all four sides, which is why they read as a lamp
        # standing in open floor.
        #
        # `_prepare_boss_arena` lays a deliberate symmetric four-lamp
        # composition before decoration runs at all. Those cells are not in
        # `room_blocked`, are not counted here, and stay untouched.
        # Tops up to the same `lamp_cap` the composed passes above draw against,
        # so a room that already earned a matched pair is left alone rather than
        # gaining a third lamp in a corner.
        if lighting == "floor-lamp":
            placing_lamps = True
            # Corners first, always. The corpus puts 76% of floor lamps in a
            # corner and only 17% along a plain wall, and a terminus cell is a
            # plain wall cell. Offering termini alongside corners diluted that
            # share below the corpus rate. A terminus is a fallback for a room
            # whose corners are all taken, not a peer of them.
            corner_cells = [cell for cell in edge_free | free
                            if _cell_geometry(tiles, cell) in ("corner", "nook")]
            rng.shuffle(corner_cells)
            terminus_cells = [cell for cell in edge_free | free
                              if _cell_geometry(tiles, cell) not in ("corner", "nook")
                              and _wall_orientation(tiles, cell) == "terminus"]
            rng.shuffle(terminus_cells)
            lamp_cells = corner_cells + terminus_cells
            for cell in lamp_cells:
                if len(room_lamps) >= lamp_cap or static_headroom <= 0:
                    break
                if (_cell_geometry(tiles, cell) not in ("corner", "nook")
                        and _wall_orientation(tiles, cell) != "terminus"):
                    continue
                _try_place([cell], 26)
            placing_lamps = False

        # --- Wall displays get the wall the space points at ---
        # Flags, armour and spear racks are the items a corridor frames for its
        # whole length, and authored maps hang them on the terminus 28% of the
        # time against our 8.5%. They never got there by themselves: the
        # signature table needs a room at least 6x6, and a square room has no
        # terminus at all -- the run along the wall always beats the run away
        # from it -- so the only rooms with a terminus were the corridors the
        # signatures skip. Give them a dedicated candidate list, exactly as
        # floor lamps have, which is what took lamps from 5.6% to 22.9%.
        for display in (62, 39, 69):
            if display not in blocking or static_headroom <= 0:
                continue
            if any(_at(things, x, y) == display
                   for y in range(room.y, room.y + room.h)
                   for x in range(room.x, room.x + room.w)):
                continue
            display_cells = [
                cell for cell in edge_free | free
                if cell not in keep_clear and _wall_backed(cell)
                and _wall_orientation(tiles, cell) == "terminus"
                and not any(
                    _at(things, cell[0] + dx, cell[1] + dy) in _FLOOR_DECOR
                    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
                )
            ]
            if not display_cells:
                continue
            # No probability gate here. A terminus is already scarce -- most
            # rooms are squarish and have none at all -- so gating on top of
            # that scarcity compounded down to 12.6% against 28.4% authored.
            # Taking the terminus whenever one exists lands on the authored
            # rate; the randomness that matters is which rooms have one.
            rng.shuffle(display_cells)
            for cell in display_cells:
                if _try_place([cell], display):
                    break
            break

        # --- Stage B guarantee: nothing leaves this loop dark ---
        # Runs here, before fill, rather than at the end of the room: fill
        # consumes the very cells a fallback fixture would need, and a crowded
        # small room could finish with no fixture at all. The Stage B lattice
        # already covers ceiling-lamp and chandelier rooms; this catches the
        # floor-lamp room whose corners were all spoken for. Reassign the
        # reported family too, so the room's fixtures stay coherent with what
        # validate_map reads back.
        if not any(_at(things, x, y) in LIGHTING_ITEMS
                   for y in range(room.y, room.y + room.h)
                   for x in range(room.x, room.x + room.w)):
            rhythm, spare = _fixture_lattice(room, tiles, _FIXTURE_STRIDES[1])
            for cell in rhythm + spare:
                if cell in reserved or _at(things, *cell) != 0 or static_headroom <= 0:
                    continue
                _set(things, *cell, 37)
                ledger_reserve(reserved, [cell], "decorations",
                               "density-fill")
                free.discard(cell)
                edge_free.discard(cell)
                static_headroom -= 1
                lighting = "ceiling-lamp"
                lighting_families[ridx] = lighting
                allowed_lights = LIGHTING_FAMILY_ITEMS[lighting]
                break

        # --- Stage C: geometry-led fill up to the authored density ---
        # The motif above gives the room its idea; this brings it up to the
        # density authored maps actually carry (0.134 decorations per floor
        # cell against the 0.063 this generator used to produce, a 2.9x gap).
        #
        # Fill is not scatter. Every candidate is classified by its own
        # geometry and visited corner-first, because corner cells are the
        # single largest destination for authored solid props (22.1% of all
        # decoration), then nook, slot, wall, and only lastly free floor. The
        # item is drawn from the corpus distribution for that exact bucket,
        # intersected with what this room's concept permits, so a crypt still
        # fills with crypt furniture.
        room_floor = sum(1 for y in range(room.y, room.y + room.h)
                         for x in range(room.x, room.x + room.w)
                         if _is_floor(_at(tiles, x, y)))
        already = sum(1 for y in range(room.y, room.y + room.h)
                      for x in range(room.x, room.x + room.w)
                      if _at(things, x, y) in _ALL_DECOR)
        fill_target = round(room_floor * _TARGET_DECOR_DENSITY * density) - already
        if fill_target > 0:
            eligible = frozenset(blocking) | frozenset(open_items)
            candidates = [cell for cell in (free | edge_free)
                          if cell not in keep_clear and _at(things, *cell) == 0]
            # Deterministic base order, then shuffled inside each geometry
            # class so the rhythm is not biased toward one corner of the room.
            classified: dict[str, list[tuple[int, int]]] = {}
            for cell in sorted(candidates):
                classified.setdefault(_cell_geometry(tiles, cell), []).append(cell)
            for cells in classified.values():
                rng.shuffle(cells)

            placed_fill = 0
            for geo in _FILL_ORDER:
                if placed_fill >= fill_target:
                    break
                for cell in classified.get(geo, ()):
                    if placed_fill >= fill_target or static_headroom <= 0:
                        break
                    if _at(things, *cell) != 0:
                        continue
                    bucket = _FILL_BUCKETS.get((geo, _cell_openness(tiles, cell)))
                    if not bucket:
                        continue
                    choices = [(item, weight) for item, weight in bucket
                               if item in eligible]
                    if not choices:
                        continue
                    # Barrels are the one family the corpus stacks shoulder to
                    # shoulder (stride 1 dominates their runs); everything else
                    # keeps its distance so fill reads as furnishing, not a pile.
                    #
                    # Orthogonal only, and tested: extending this to diagonals
                    # moved clustering 32.9% -> 33.5%, i.e. not at all. The excess
                    # clustering is not fill spreading badly, it is the authored
                    # compositions themselves -- a corner stash, a banquet row and
                    # a colonnade all place props deliberately close together.
                    neighbours = [(cell[0] + dx, cell[1] + dy)
                                  for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))]
                    touching = [_at(things, *n) for n in neighbours]
                    # _ALL_DECOR rather than _FLOOR_DECOR, and measured: relaxing
                    # this to ignore ceiling fixtures took clustering from 22.8% to
                    # 25.2% while recovering only 0.2 of a vocabulary point.
                    crowded = any(t in _ALL_DECOR for t in touching)
                    if crowded:
                        choices = [(i, w) for i, w in choices
                                   if i in (24, 58) and i in touching]
                        if not choices:
                            continue
                    material = _material_behind(tiles, cell)
                    affinity = _MATERIAL_AFFINITY.get(material or "", frozenset())
                    orientation = _wall_orientation(tiles, cell)
                    weights = [w * (_MATERIAL_MULTIPLIER if i in affinity else 1.0)
                               * (1.35 if i in (39, 62, 69) and geo == "corner"
                                  else 1.20 if i in (39, 62, 69) and orientation == "terminus"
                                  else 0.72 if i in (39, 62, 69) and orientation == "flank"
                                  else 1.0)
                               for i, w in choices]
                    item = rng.choices([i for i, _ in choices], weights=weights, k=1)[0]
                    target = cell
                    chance = _TERMINUS_PREFERENCE.get(item, 0.0)
                    if (chance and _wall_orientation(tiles, cell) != "terminus"
                            and rng.random() < chance):
                        terminus = [candidate for candidate in classified.get("wall", ())
                                    if (_at(things, *candidate) == 0
                                        and _wall_orientation(tiles, candidate) == "terminus"
                                        and any(existing == item for existing, _ in
                                                _FILL_BUCKETS.get(("wall", _cell_openness(tiles, candidate)), ())))]
                        rng.shuffle(terminus)
                        if terminus:
                            target = terminus.pop()
                    if item in STATIC_BLOCKING:
                        _try_place([target], item)
                    elif not _place_open(target, item):
                        continue
                    if _at(things, *target) == item:
                        placed_fill += 1

        # --- Pull isolated furniture flush to the wall ---
        # A blocking prop that aimed for the wall band but landed on the inner
        # floor ring sits one cell short, with a visible gap behind it that
        # reads as floating. Move only props that have no wall or furniture
        # neighbor and are already within two cells of a wall, straight toward
        # it. Centerpieces (wells, banquet tables sitting deep in the room) and
        # anything already part of a pair or cluster keep their place.
        def _solid(cell: tuple[int, int]) -> bool:
            value = _at(tiles, *cell)
            return value != -1 and not _is_floor(value) and value not in DOORS

        for cell in list(room_blocked):
            item = _at(things, *cell)
            if item in (39, 62, 69):          # wall displays are backed already
                continue
            if cell in composed_cells:
                continue                       # a group owns this position
            neighbours = [(cell[0] + dx, cell[1] + dy)
                          for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))]
            if any(_solid(n) for n in neighbours):
                continue                       # already flush to a wall
            if any(_at(things, *n) in STATIC_BLOCKING for n in neighbours):
                continue                       # reads as a deliberate cluster
            target = None
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                step1 = (cell[0] + dx, cell[1] + dy)
                step2 = (cell[0] + 2 * dx, cell[1] + 2 * dy)
                if (_solid(step2) and step1 not in keep_clear
                        and (step1 in free or step1 in edge_free)
                        and _at(things, *step1) == 0):
                    target = step1
                    break
            if target is None:
                continue
            candidate = (blocked_cells - {cell}) | {target}
            if len(_reachable(tiles, start, locked_open=True,
                              blocked=candidate)) < baseline - len(candidate):
                continue
            _set(things, *cell, 0)
            _set(things, *target, item)
            ledger_release(reserved, [cell], "decorations", "flush-to-wall-source")
            ledger_reserve(reserved, [target], "decorations",
                           "flush-to-wall-target")
            free.add(cell); free.discard(target); edge_free.discard(target)
            blocked_cells.discard(cell); blocked_cells.add(target)
            room_blocked.append(target)

        # --- Final lighting backstop ---
        # Not redundant with the guarantee above: the flush-to-wall repair pass
        # just moved props and may have freed a cell that was occupied when that
        # check ran. A no-op whenever the room is already lit, which is the
        # overwhelming majority.
        if not any(_at(things, x, y) in LIGHTING_ITEMS
                   for y in range(room.y, room.y + room.h)
                   for x in range(room.x, room.x + room.w)):
            rhythm, spare = _fixture_lattice(room, tiles, _FIXTURE_STRIDES[1])
            for cell in rhythm + spare:
                if cell in reserved or _at(things, *cell) != 0 or static_headroom <= 0:
                    continue
                _set(things, *cell, 37)
                ledger_reserve(reserved, [cell], "decorations",
                               "flush-to-wall-relocation")
                free.discard(cell)
                edge_free.discard(cell)
                static_headroom -= 1
                lighting_families[ridx] = "ceiling-lamp"
                break

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
            ledger_reserve(reserved, [cell], "decorations",
                           "corridor-rhythm")
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
                ledger_reserve(reserved, [cell], "decorations",
                               "vine-screen")
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
                    _set(things, *deep, rng.choice((31, 26, 58)))
                    ledger_reserve(reserved, [deep], "decorations",
                                   "alcove-prop")
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
            ledger_reserve(reserved, [deep], "decorations",
                           "niche-prop")
            static_headroom -= 1

    return tuple(lighting_families), tuple(vine_screens), tuple(room_motifs)


def _barrel_families(rooms, things) -> tuple[str, ...]:
    """Which barrel material each room ended up carrying.

    Read back rather than remembered: a room commits to green or blue once and
    every later pass reuses that choice, so the plane is the authority. validate_map
    rejects a room holding both, and this is what it reads.
    """
    barrel_families = tuple(
        ("green" if 24 in present else "blue" if 58 in present else "none")
        for room in rooms
        for present in ({_at(things, x, y)
                         for y in range(room.y, room.y + room.h)
                         for x in range(room.x, room.x + room.w)},))
    return barrel_families
