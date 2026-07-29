"""Authored pickups and the ammo economy.

Every in-room gameplay pickup is placed by a named, geometry-aware composition --
wall cache, entry staging, recovery station, treasure display, corner cache,
centre dais, kennel support, boss-arena support, secret cache -- and each records a
`SpritePlacement` naming the intent it satisfies and the room that owns it. There
is no free scatter path: a required economy intent either lands as a composition
or rejects the candidate floor.

This module also owns `_PlacementGrammar`, which previously shared placement.py
with the decoration anchors. That file was mixing two owners -- the geometry-aware
composition engine the pickup passes use, and the room-anchor helpers decoration
uses -- so the grammar moves here and the anchors stay behind.

Economy rather than aesthetics is the boundary with decorations.py: a health pack
is a pacing decision, a wall lamp is a visual one, and only the former may reject
a floor for being unplaceable.
"""

from __future__ import annotations

from collections import Counter, deque
from itertools import combinations
import math
import random

from .config import CampaignConfig
from .grid import _at, _floor_distances, _is_floor, _set
from .model import (SET_PIECE_CONTRACTS, Room, RoomIdentity, SetPiecePlan,
                    SpritePlacement)
from .placement import _room_anchors
from .wl6 import (AMMO, AMMO_COST, AMMO_SUPPLY_EXEMPT_FLOORS, AMMO_SUPPLY_SCALE,
                  CHAINGUN, DOG_FOOD, FAMILY_BY_CODE, FIRST_AID, FOOD, GRID,
                  MACHINE_GUN, ONE_UP, TREASURE, DOORS)
from .ledger import reserve as ledger_reserve


AUTHORED_PICKUP_TEMPLATES = frozenset({
    "wall-cache", "entry-staging", "recovery-station",
    "treasure-display", "corner-cache", "center-dais",
    "kennel-wall", "boss-arena-cross", "secret-cache",
})

# Set-piece rewards deliberately reuse the ordinary pickup grammar.  The item
# groups are surplus rather than progression necessities: in particular, none
# contains a key, so a contract can never move a required objective behind a
# pushwall.
_SET_PIECE_REWARD_TREATMENTS = {
    "cache": ((AMMO, AMMO, FIRST_AID), ("wall-cache", "corner-cache")),
    "objective": ((MACHINE_GUN,), ("center-dais", "treasure-display")),
    "resupply": ((FIRST_AID, AMMO), ("recovery-station", "wall-cache")),
    "treasure": ((TREASURE[1], TREASURE[2]),
                 ("treasure-display", "center-dais")),
}


def _set_pieces_from_motifs(
        identities: list[RoomIdentity]) -> tuple[SetPiecePlan, ...]:
    """Recover realized program records from their room motif tags.

    ``_place_authored_pickups`` predates ``FloorPlan.set_pieces`` and its
    production caller still supplies the realized room identities rather than
    the abstract plan.  The tags are the documented reverse lookup: rebuilding
    the small reward-facing view here preserves the existing call boundary.
    The optional explicit ``set_pieces`` argument remains authoritative for
    direct callers and future plumbing.
    """
    by_family: dict[str, list[tuple[int, str]]] = {}
    for room_index, identity in enumerate(identities):
        if not identity.motif.startswith("setpiece:"):
            continue
        parts = identity.motif.split(":", 2)
        if len(parts) != 3 or not parts[1] or not parts[2]:
            continue
        by_family.setdefault(parts[1], []).append((room_index, parts[2]))

    # The shared model owns declarations; pickups only interprets rewards.
    recovered = []
    for family, realized in by_family.items():
        reward_contract = tuple(
            SET_PIECE_CONTRACTS.get(family, {}).get("reward", ()))
        if not reward_contract:
            continue
        rooms = tuple(room for room, _ in realized)
        roles = tuple(role for _, role in realized)
        recovered.append(SetPiecePlan(
            family, "realized", rooms, roles, (), roles[0], roles[-1],
            reward_contract=reward_contract))
    return tuple(recovered)


def _place_set_piece_rewards(
        set_pieces: tuple[SetPiecePlan, ...],
        identities: list[RoomIdentity],
        place_group) -> int:
    """Best-effort realization of named reward contracts.

    Returns the number honoured for audits and focused tests.  A missing room,
    unknown intent, or geometry failure simply leaves ordinary room treatment
    in place; contracts never reject a floor.
    """
    honoured = 0
    for set_piece in set_pieces:
        intents = set_piece.roles_for("reward_contract")
        for role, kind in intents.items():
            if (kind not in _SET_PIECE_REWARD_TREATMENTS
                    or not set_piece.rooms_for_role(role)):
                continue
            tag = f"setpiece:{set_piece.family}:{role}"
            candidates = [
                room_index for room_index, identity in enumerate(identities)
                if identity.motif == tag
            ]
            if not candidates:
                continue
            items, templates = _SET_PIECE_REWARD_TREATMENTS[kind]
            # Existing provenance checks classify all valuables as exploration
            # treasure.  The target room and template still prove which
            # contract entry this placement honours.
            reason = ("exploration-treasure" if kind == "treasure" else
                      f"setpiece-reward:{set_piece.family}:{role}:{kind}")
            if place_group(items, reason, candidates, templates):
                honoured += 1
    return honoured


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
                ledger_reserve(self.reserved, [(x, y)], "pickups",
                               "authored-composition")
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
                            expedition_rooms_out: list[int] | None = None,
                            vignettes: tuple = (),
                            set_pieces: tuple[SetPiecePlan, ...] = ()) -> None:
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

    # Program rewards are surplus claims made before generic economy fills the
    # same wall and corner anchors.  Failure is intentionally ignored: an
    # advisory contract degrades to an ordinary room, never a rejected floor.
    reward_plans = set_pieces or _set_pieces_from_motifs(identities)
    _place_set_piece_rewards(reward_plans, identities, place_group)

    # Cross-system plans claim their economy beat before generic needs.  This
    # still uses the pickup grammar, so it can reject an unplaceable candidate.
    vignette_items = {"supply-cache": (AMMO, AMMO), "recovery": (FOOD, FIRST_AID),
                      "medical": (FIRST_AID,), "treasure": (TREASURE[1],)}
    vignette_templates = {"supply-cache": ("wall-cache", "corner-cache"),
                          "recovery": ("recovery-station", "wall-cache"),
                          "medical": ("recovery-station",),
                          "treasure": ("treasure-display", "center-dais")}
    for vignette in vignettes:
        target = vignette.rooms[-1]
        treatment = vignette.pickup_treatment
        if treatment in vignette_items and not place_group(
                vignette_items[treatment], f"vignette-{vignette.family}", [target],
                vignette_templates[treatment]):
            raise ValueError(f"vignette {vignette.family} cannot realize its reward")

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
    # Set-piece reward ammo counts here like any other. Excluding it kept the
    # contract "surplus" in the sense of never reducing the mandatory route
    # stage, but a clip is a clip: the route pass then placed its full quota on
    # top and floor 5 came out at 0.50 supply against a 0.45 ceiling. Counting
    # it lowers the route quota by the same amount, so the player ends up with
    # the intended total and the contract still decides WHERE some of it lives.
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
