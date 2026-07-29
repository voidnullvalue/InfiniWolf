"""Room-owned combat compositions coordinated into cross-room sequences.

Every ordinary actor on a floor belongs to a recorded `EncounterPlacement`: a
named template, the room that owns it, the squad family, the cells, any reveal
slot, and a patrol route where the actor moves. Nothing places a bare actor at a
bare coordinate -- provenance is what lets validate_map prove an actor was
composed rather than scattered, and what lets the manifest explain a fight.

Guard recesses and galleries are architecture that exists for a fight, so the
geometry request is made elsewhere but the actors inside them are owned here. That
split is deliberate: a mirrored hallway recess is a shape geometry can refuse,
while who stands in it and which way they face is an encounter decision.

Reads the finished `RoomIdentity` rather than re-deciding what a room is, so a
barracks is populated as sleeping quarters and a checkpoint as a manned post.

Its guard-recess tile writes are category 2 feature-owned bounded geometry: the
recess exists solely to realize its ambush encounter, not as general room shaping.
"""

from __future__ import annotations

from collections import Counter, deque
import random

from .config import CampaignConfig
from .grid import _at, _floor_distances, _is_floor, _reachable, _set
from .model import (EncounterPlacement, GuardGallery, GuardRecess, KeyObjective,
                    PatrolRoute, RoomSpec, SetPiecePlan,
                    Room, RoomIdentity, SpritePlacement)
from .planning import _SET_PIECE_CONTRACTS
from .wl6 import (ACTOR_BUDGET_SCALE, ACTOR_SPACING, DOG_FOOD, DOOR_EW, DOOR_NS,
                  DOORS, ENEMY_FAMILIES, FAKE_HITLER, FLOOR, GHOSTS, GRID,
                  GUARDS, NOVELTY_SPAWN_CHANCE, PATROL_POINT_CODES,
                  PATROLS_BY_FAMILY, WALL)
from .ledger import reserve as ledger_reserve


_ENCOUNTER_CONTRACT_INTENTS = frozenset({
    "guarded", "ambush", "patrolled", "overlook", "light", "objective",
})


def _encounter_contracts_by_room(
        identities: list[RoomIdentity],
        set_pieces: tuple[SetPiecePlan, ...] = (),
        realized_plan_indices: tuple[int, ...] = (),
) -> dict[int, str]:
    """Resolve advisory role contracts onto realized room indices."""
    resolved: dict[int, str] = {}
    if set_pieces:
        realized = ({plan_index: room_index
                     for room_index, plan_index
                     in enumerate(realized_plan_indices)}
                    if realized_plan_indices else
                    {index: index for index in range(len(identities))})
        for set_piece in set_pieces:
            for role, intent in set_piece.roles_for(
                    "encounter_contract").items():
                if intent not in _ENCOUNTER_CONTRACT_INTENTS:
                    continue
                for plan_index in set_piece.rooms_for_role(role):
                    room_index = realized.get(plan_index)
                    if room_index is not None and room_index < len(identities):
                        resolved.setdefault(room_index, intent)
        return resolved

    # RoomIdentity is the current encounter boundary. The motif reverse-maps a
    # surviving named role without treating an ordinary concept as contracted.
    for room_index, identity in enumerate(identities):
        parts = identity.motif.split(":", 2)
        if len(parts) != 3 or parts[0] != "setpiece":
            continue
        _, family, role = parts
        pairs = _SET_PIECE_CONTRACTS.get(family, {}).get("encounter", ())
        intent = dict(pairs).get(role)
        if intent in _ENCOUNTER_CONTRACT_INTENTS:
            resolved.setdefault(room_index, intent)
    return resolved


def _carve_guard_recesses(tiles: list[int], things: list[int], rooms: list[Room],
                          specs: list[RoomSpec], roles: list[str],
                          reserved: set[tuple[int, int]], rng: random.Random,
                          start: tuple[int, int], exit_room: Room | None,
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
            ledger_reserve(reserved, cells, "encounters",
                           "guard-recess")
            return (GuardRecess(room_index, cells, actor_cell),)
    return ()


def _place_guard_gallery(tiles: list[int], things: list[int], rooms: list[Room],
                         identities: list[RoomIdentity], room_shapes: list[str],
                         reserved: set[tuple[int, int]], rng: random.Random,
                         start: tuple[int, int], eligible_rooms: frozenset[int],
                         set_pieces: tuple[SetPiecePlan, ...] = (),
                         realized_plan_indices: tuple[int, ...] = (),
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
    contract_intents = _encounter_contracts_by_room(
        identities, set_pieces, realized_plan_indices)
    overlook_rooms = {index for index, intent in contract_intents.items()
                      if intent == "overlook"}
    candidates = [index for index in eligible_rooms
                  if index and room_shapes[index] == "rectangle"
                  and (identities[index].concept in suitable_concepts
                       or index in overlook_rooms)
                  and min(rooms[index].w, rooms[index].h) >= 7
                  and max(rooms[index].w, rooms[index].h) >= 9]
    rng.shuffle(candidates)
    candidates.sort(key=lambda index: (
        index not in overlook_rooms,
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
            ledger_reserve(reserved, screen, "encounters",
                           "gallery-screen")
            ledger_reserve(reserved, rear_cells, "encounters",
                           "gallery-rear-chamber")
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


_PACING_PATTERN = (
    ("orientation", 0.40),
    ("modest-resistance", 0.80),
    ("exploration-choice", 0.55),
    ("memorable-encounter", 1.35),
    ("recovery", 0.40),
    ("objective-pressure", 1.20),
    ("shortcut-recontextualization", 0.70),
    ("climax", 1.50),
    ("decompression", 0.40),
)


def _schedule_pacing_beats(
        critical_route: tuple[int, ...],
        key_hosts: frozenset[int] = frozenset(),
        boss_room_index: int = -1,
) -> dict[int, tuple[str, float]]:
    """Map the mandatory route onto an explicit sequence of contrasting beats.

    The route can have fewer or more rooms than the nine-beat reference pattern,
    so positions are sampled across the whole pattern. Duplicate samples on a
    long route are changed to a lower-intensity connective beat; this is the
    concrete guard against a run of identical medium rooms.
    """
    route = tuple(dict.fromkeys(critical_route))
    if not route:
        return {}
    if len(route) == 1:
        schedule = {route[0]: _PACING_PATTERN[0]}
    else:
        last = len(_PACING_PATTERN) - 1
        schedule = {}
        previous = ""
        for position, room_index in enumerate(route):
            pattern_index = round(position * last / (len(route) - 1))
            beat = _PACING_PATTERN[pattern_index]
            if beat[0] == previous:
                beat = (_PACING_PATTERN[2] if position % 2
                        else _PACING_PATTERN[4])
            schedule[room_index] = beat
            previous = beat[0]
    for room_index in key_hosts & schedule.keys():
        schedule[room_index] = _PACING_PATTERN[5]
    if boss_room_index in schedule:
        schedule[boss_room_index] = _PACING_PATTERN[7]
    return schedule


def _place_population(config: CampaignConfig, number: int, rooms: list[Room],
                      tiles: list[int], things: list[int], reserved: set[tuple[int, int]],
                      rng: random.Random, start: tuple[int, int],
                      exit_room: Room | None, *, patrol_chance: float = 0.15,
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
                      encounter_out: list[EncounterPlacement] | None = None,
                      vignette_treatments: dict[int, str] | None = None,
                      set_pieces: tuple[SetPiecePlan, ...] = (),
                      realized_plan_indices: tuple[int, ...] = (),
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
        ledger_reserve(reserved, [facing_cell], "encounters",
                       "stationary-facing")
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
    boss_room_index = (rooms.index(boss_room)
                       if boss_room is not None and boss_room in rooms else -1)
    pacing_beats = _schedule_pacing_beats(
        critical_route, frozenset(key_hosts), boss_room_index)
    recess_by_room = {recess.room_index: recess for recess in guard_recesses}
    vignette_treatments = vignette_treatments or {}
    contract_intents = _encounter_contracts_by_room(
        identities, set_pieces, realized_plan_indices)

    budgets: dict[int, int] = {}
    for ridx, room in enumerate(rooms[1:], 1):
        depth = depth_of(room)
        beat_scale = pacing_beats.get(ridx, ("depth-ramp", pacing(depth)))[1]
        budget = max(0, round(per_room * ACTOR_BUDGET_SCALE
                              * (0.4 if room == exit_room else beat_scale)))
        if ridx in calm_rooms:
            budget = 0
        elif room == boss_room:
            budget = 0 if rng.random() < 0.55 else min(2, budget)
        elif (contract_intents.get(ridx) == "light"
              and ridx not in recess_by_room and ridx not in key_hosts):
            budget = 0
        elif contract_intents.get(ridx) in {
                "guarded", "ambush", "patrolled", "objective"}:
            budget = max(1, budget)
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
    contracted_patrol_rooms = {index for index, intent in contract_intents.items()
                               if intent == "patrolled"}
    patrol_rooms = [index for index, budget in budgets.items()
                    if budget and index not in calm_rooms and rooms[index] != boss_room
                    and index not in recess_by_room and index not in key_hosts]
    room_zones = {}
    for index, room in enumerate(rooms):
        zones = Counter(
            _at(tiles, x, y)
            for y in range(room.y, room.y + room.h)
            for x in range(room.x, room.x + room.w)
            if _is_floor(_at(tiles, x, y)))
        room_zones[index] = (min(zones, key=lambda zone: (-zones[zone], zone))
                             if zones else -1)
    checkpoint_fronts = [
        index for index in critical_route
        if index < len(identities)
        and identities[index].concept in ("checkpoint", "guardpost")
    ]
    response_candidates = {
        index for front in checkpoint_fronts
        for index in optional_rooms
        if index in budgets and budgets[index]
        and room_zones.get(index) == room_zones.get(front)
        and (abs(rooms[index].center[0] - rooms[front].center[0])
             + abs(rooms[index].center[1] - rooms[front].center[1]) <= 24)
    }
    rng.shuffle(patrol_rooms)
    patrol_rooms.sort(key=lambda index: (
        index not in contracted_patrol_rooms,
        index not in response_candidates,
        identities[index].tier not in ("corridor", "hall"),
        -depth_of(rooms[index])))
    planned_patrols: dict[int, list[PatrolRoute]] = {}
    for ridx in patrol_rooms:
        if (ridx not in contracted_patrol_rooms
                and sum(len(routes) for routes in planned_patrols.values()) >= patrol_target):
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
            ledger_reserve(reserved, route.cells, "encounters",
                           "patrol-route")
            if (ridx not in contracted_patrol_rooms
                    and sum(len(routes) for routes in planned_patrols.values()) >= patrol_target):
                break

    # Compose the room-local templates into sequences. Each grammar is only
    # claimed when its physical evidence exists; unsupported ideas such as
    # dynamic counterflow spawning are deliberately absent.
    sequence_templates: dict[int, str] = {}

    def claim(grammar: str, instance: int,
              participants: tuple[tuple[int, str], ...]) -> bool:
        del grammar, instance
        if (len({room_index for room_index, _ in participants}) < 2
                or any(not budgets.get(room_index)
                       or room_index in sequence_templates
                       or room_index in recess_by_room
                       or room_index in vignette_treatments
                       for room_index, _ in participants)):
            return False
        for room_index, template in participants:
            sequence_templates[room_index] = template
        return True

    route = tuple(index for index in critical_route if index in budgets)
    # A key on the mandatory route is defended as one approach/guard/retreat
    # composition. Optional keys lack a known mandatory retreat path here, so
    # their already-good local objective guard remains unlabelled.
    for host in sorted(key_hosts, key=lambda index: critical_positions.get(index, 10 ** 9)):
        position = critical_positions.get(host)
        if position is None or position <= 0:
            continue
        approach = critical_route[position - 1]
        claim("objective-defense", host,
              ((approach, "staggered-flank"), (host, "objective-guard")))

    # A posted checkpoint and a nearby off-route patrol in the same acoustic
    # pocket form an actual alert response rather than two unrelated rooms.
    for front in checkpoint_fronts:
        candidates = [
            index for index in response_candidates
            if index in planned_patrols
            and index not in sequence_templates and front not in sequence_templates
        ]
        if not candidates:
            continue
        response = min(candidates, key=lambda index: (
            abs(rooms[index].center[0] - rooms[front].center[0])
            + abs(rooms[index].center[1] - rooms[front].center[1]), index))
        claim("checkpoint-response", front,
              ((front, "visible-sentry"), (response, "staggered-flank")))

    # An optional, two-entry room in the direct room's sound zone is evidence
    # of a real flank loop. Requiring a patrol on the optional leg makes the
    # crossfire active rather than merely naming graph topology.
    for direct in route:
        flankers = [
            index for index in optional_rooms
            if index in planned_patrols and len(room_entries.get(rooms[index], ())) >= 2
            and room_zones.get(index) == room_zones.get(direct)
            and index not in sequence_templates and direct not in sequence_templates
        ]
        if flankers:
            flanker = min(flankers, key=lambda index: (
                abs(rooms[index].center[0] - rooms[direct].center[0])
                + abs(rooms[index].center[1] - rooms[direct].center[1]), index))
            if claim("crossfire-loop", direct,
                     ((direct, "visible-sentry"), (flanker, "staggered-flank"))):
                break

    # A quiet route room followed through a door into a high beat is a layered
    # breach. Distinct zones prove the doorway really gates the alert chain.
    for front, rear in zip(route, route[1:]):
        front_scale = pacing_beats.get(front, ("", pacing(depth_of(rooms[front]))))[1]
        rear_scale = pacing_beats.get(rear, ("", pacing(depth_of(rooms[rear]))))[1]
        if (front_scale <= 0.70 and rear_scale >= 1.15
                and room_zones.get(front) != room_zones.get(rear)
                and claim("layered-breach", front,
                          ((front, "visible-sentry"), (rear, "strongpoint")))):
            break

    # Doorway shuttles are the engine's expressible version of a timed patrol
    # intersection: the moving actor crosses the expected arrival boundary.
    for room_index in route:
        routes_here = planned_patrols.get(room_index, ())
        if (room_index in sequence_templates
                or not any(route.kind == "doorway-shuttle"
                           for route in routes_here)):
            continue
        position = critical_positions.get(room_index, 0)
        if position <= 0:
            continue
        approach = critical_route[position - 1]
        if claim("patrol-intersection", room_index,
                 ((approach, "visible-sentry"), (room_index, "staggered-flank"))):
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
        visible_candidates = [cell for cell in candidates
                              if any(_line_visible(entry, cell) for entry in entries)]
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
        elif ridx in vignette_treatments:
            template = vignette_treatments[ridx]
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
        if ridx in sequence_templates:
            template = sequence_templates[ridx]
        contract_intent = contract_intents.get(ridx)
        if ridx not in recess_by_room:
            if contract_intent == "guarded" and budget and visible_candidates:
                template = "visible-sentry"
            elif contract_intent == "ambush" and budget and hidden_candidates:
                template = "blind-corner-ambush"
            elif contract_intent == "patrolled" and ridx in planned_patrols:
                template = "patrol"
            elif contract_intent == "objective" and budget and candidates:
                template = "objective-guard"
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
                if not objectives and contract_intent == "objective":
                    objectives = [room.center]
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
                      if contract_intents.get(rooms.index(room)) != "light"
                      for y in range(room.y + 1, room.y + room.h - 1)
                      for x in range(room.x + 1, room.x + room.w - 1)
                      if (x, y) not in reserved and _at(things, x, y) == 0
                      and _is_floor(_at(tiles, x, y))
                      and abs(x - start[0]) + abs(y - start[1]) >= 6]
        if candidates:
            cell = rng.choice(candidates)
            _set(things, *cell, novelty)
            ledger_reserve(reserved, [cell], "encounters",
                           "actor-cell")
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
            ledger_reserve(reserved, [candidates[0]], "encounters",
                           "dog-food")
            if placements is not None:
                room_index = rooms.index(room)
                placements.append(SpritePlacement(
                    "kennel-support", "kennel-wall", room_index,
                    ((candidates[0][0], candidates[0][1], DOG_FOOD),)))
    return tuple(tier_counts)
