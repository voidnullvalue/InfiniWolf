"""Static decoration: what furnishes a room, and where it stands.

`_place_decorations` is the terminal pass of `generate_map` -- nothing after it
draws from the main RNG stream, so decoration composition can be retuned
without perturbing layout, population, or patrols. Every blocking commit
re-runs full-map reachability with the candidate cells blocked
(`_try_place_items`), so furniture can never seal off part of a floor.

Concept tables (`_DECOR_BLOCKING` / `_DECOR_OPEN`) decide which item vocabulary
a room is eligible for; the placement geometry in `placement` decides where a
composition may key off.
"""

from __future__ import annotations

from collections import Counter, deque
import random

from .mapgrid import (Room, RoomIdentity, VineScreen, _at, _inside_room, _is_floor,
                      _reachable, _set)
from .placement import _room_anchors, _room_traversal_frame, _traversal_pair_candidates
from .tiles import (DECOR_WALLS, DOORS, ENEMY_CODES, GRID, LIGHTING_FAMILY_ITEMS,
                    LIGHTING_ITEMS, SPECIAL_WALL_TILES, STATIC_BLOCKING, STATIC_OPEN,
                    VINE_SCREEN_CONCEPTS, WALL_MATERIALS)


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

# Recessed exterior vistas retain the original wall plane as matching pillar
# supports. Name the rates so this landmark can be deliberately tuned.
SKY_VISTA_COURTYARD_CHANCE = 0.36

SKY_VISTA_INTERIOR_CHANCE = 0.18

# Items that read as a deliberate matched pair when mirrored beside a door
# or under a landmark wall: plants, lamps, pillars, vases, barrels, suits
# of armor, and flags.
_FRAMEABLE = frozenset({26, 30, 31, 34, 39, 62})

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
                       allow_sky_vista: bool = True,
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
        # Barrels are a room-level material language, not an independent prop
        # roll. Damp/crypt contexts use green barrels, formal military rooms
        # use blue, and neutral storage chooses once for the whole room.
        green_barrel_concepts = {
            "jail", "crypt", "ossuary", "burial-chamber", "holding-cell",
            "courtyard",
        }
        blue_barrel_concepts = {
            "guardpost", "checkpoint", "armory", "workshop", "war-room",
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
            barrel_item = rng.choices((58, 24), weights=(3, 2), k=1)[0]
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

        if wants_vase and static_headroom > 0:
            vase_cells = [cell for cell in edge_free
                          if cell not in keep_clear and _wall_backed(cell)]
            rng.shuffle(vase_cells)
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
                            reserved.add(wall_cell)
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
        concept_frames = {
            "war-room": (39, 62), "armory": (39, 62), "guardpost": (26,),
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
            zones = tuple((tuple(dict.fromkeys(
                                 barrel_item if item in (24, 58) else item
                                 for item in solid
                                 if item != 35 and (item not in LIGHTING_ITEMS
                                                    or item in allowed_lights))),
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
            # Furniture reads as "set against the walls," so both members of a
            # pair belong flush on the wall-backed outer ring. The interior
            # `free` ring is one floor cell short of the wall -- a barrel or
            # bunk parked there leaves a visible gap and reads as floating,
            # which is the dominant source of mid-room clutter. Prefer matched
            # cells on opposite walls; keep the old center-mirror interior
            # pairs only as a last resort so density holds where the wall band
            # is already spoken for.
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
                band = [cell for cell in free if _near_wall(*cell)]
                x_pairs = [((x, y), (2 * cx - x, y)) for x, y in band
                           if x < cx and (2 * cx - x, y) in free]
                y_pairs = [((x, y), (x, 2 * cy - y)) for x, y in band
                           if y < cy and (x, 2 * cy - y) in free]
                interior_pairs = x_pairs + y_pairs
                rng.shuffle(interior_pairs)
                # Travel-aware pairs stay ahead of room-center symmetry, then
                # wall-flush pairs, then the interior mirror as a last resort.
                ordered = travel_pairs + flush_pairs + interior_pairs
                seen: set[tuple[tuple[int, int], tuple[int, int]]] = set()
                all_pairs = []
                for pair in ordered:
                    if pair in seen or pair[::-1] in seen:
                        continue
                    seen.add(pair)
                    all_pairs.append(pair)
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
            reserved.discard(cell); reserved.add(target)
            free.add(cell); free.discard(target); edge_free.discard(target)
            blocked_cells.discard(cell); blocked_cells.add(target)
            room_blocked.append(target)

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
                    _set(things, *deep, rng.choice((31, 26, 58)))
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
