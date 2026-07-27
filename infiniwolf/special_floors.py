"""Authored special-floor contracts: floor 9's stronghold and floor 10's vault.

Deliberate exceptions live here rather than as `number == 9` branches scattered
through the ordinary modules. Boss arena geometry, the boss placement and supply
contracts, and the symmetric four-lamp composition the arena lays before
decoration runs at all.

These are contracts, not tendencies. A floor 9 that cannot realize its arena
rejects the attempt rather than shipping a boss standing in a plain room, which is
why these functions raise. The arena's own decorations are placed here and are
exempt from the ordinary per-room decoration rules -- decoration.py knows to leave
them alone because they are already a composition.

This module may coordinate the other subsystems through explicit requests. It must
not grow into a second generator: if a rule applies to ordinary floors too, it
belongs in the module that owns that rule.
"""

from __future__ import annotations

import random

from .grid import _at, _is_floor, _set
from .model import BossArenaDetail, Room, SpritePlacement
from .wl6 import AMMO, DOORS, FIRST_AID, KEY_DROP_BOSSES, WALL
from .ledger import reserve as ledger_reserve


# Items validate_map requires to hang on a wall rather than stand in the open.
_WALL_DISPLAYS = (39, 62, 69)


def _perimeter_anchor(room: Room, x: int, y: int) -> tuple[int, int]:
    """Snap a cell to the nearest room-perimeter cell, keeping its side.

    A wall display's authored offset says which part of the arena it decorates;
    this preserves that intent while moving it to a cell that can actually have
    rock behind it.
    """
    left, right = x - room.x, room.x + room.w - 1 - x
    top, bottom = y - room.y, room.y + room.h - 1 - y
    nearest = min(left, right, top, bottom)
    if nearest == left:
        return room.x, y
    if nearest == right:
        return room.x + room.w - 1, y
    if nearest == top:
        return x, room.y
    return x, room.y + room.h - 1


def _wall_backed(tiles: list[int], room: Room, cell: tuple[int, int]) -> bool:
    """True when `cell` is on the perimeter with non-floor, non-door outside.

    Mirrors generator_validation's own test rather than approximating it, so a
    display this function accepts cannot be one validation rejects.
    """
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
    return any(not _is_floor(_at(tiles, *neighbor))
               and _at(tiles, *neighbor) not in DOORS
               for neighbor in outside)


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
            ledger_reserve(reserved, [cell], "special_floors",
                           "boss-arena-geometry")
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
        # Flags, armour and spear racks are wall displays: validate_map requires
        # them on the room perimeter with rock behind. The authored offsets are
        # measured from the arena centre, and an arena is 14-17 tiles across, so
        # every one of them landed on interior floor -- placed, then rejected.
        # That silently killed two of the five families outright: command-bunker
        # and columned-fortress were planned about 40% of the time between them
        # and realized never, because their flag cells are always plain floor.
        # Snap to the nearest wall on the same side so the composition survives.
        if item in _WALL_DISPLAYS:
            x, y = _perimeter_anchor(room, x, y)
            if not _wall_backed(tiles, room, (x, y)):
                continue
        if (_is_floor(_at(tiles, x, y)) and _at(things, x, y) == 0
                and (x, y) not in reserved):
            _set(things, x, y, item)
            ledger_reserve(reserved, [(x, y)], "special_floors",
                           "boss-arena-decoration")
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
    ledger_reserve(reserved, [(bx, by)], "special_floors",
                   "boss-stand")
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
            ledger_reserve(reserved, [(x, y)], "special_floors",
                           "boss-support-supply")
            placed_supplies.append((x, y, thing))
    if placements is not None and placed_supplies:
        placements.append(SpritePlacement(
            "boss-arena-support", "boss-arena-cross", room_index,
            tuple(placed_supplies)))
    return boss
