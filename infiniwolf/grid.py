"""Tile-plane primitives.

Both map planes are flat GRID*GRID lists of native WL6 codes. `_at`/`_set`
bounds-check against that grid so callers can probe freely off-map (`-1`), and
`_reachable` is the flood fill every blocking placement re-runs before it
commits.

Purely spatial: these answer "what is at this cell" and "what can be walked to",
never "what should go here". Progression, encounter and semantic policy belong to
the modules that own those decisions -- a helper that consults a room's role or
concept is in the wrong file.
"""

from __future__ import annotations

from collections import deque

from .model import Room
from .wl6 import (DOOR_ELEVATOR, DOOR_ELEVATOR_NS, DOOR_EW, DOOR_NS, FLOOR, GRID,
                  LOCKED_DOORS, SECRET_EXIT_ZONE, ZONE_MAX)


def _at(plane: list[int], x: int, y: int) -> int:
    return plane[y * GRID + x] if 0 <= x < GRID and 0 <= y < GRID else -1

def _set(plane: list[int], x: int, y: int, value: int) -> None:
    if 0 <= x < GRID and 0 <= y < GRID:
        plane[y * GRID + x] = value

def _is_floor(value: int) -> bool:
    return FLOOR <= value <= ZONE_MAX or value == SECRET_EXIT_ZONE

def _inside_room(rooms: list[Room], x: int, y: int) -> bool:
    return any(room.x <= x < room.x + room.w and room.y <= y < room.y + room.h
               for room in rooms)

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
            passable = _is_floor(tile) or tile in (DOOR_EW, DOOR_NS, DOOR_ELEVATOR,
                                                   DOOR_ELEVATOR_NS)
            if ((locked_open and tile in LOCKED_DOORS)
                    or (open_lock_codes is not None and tile in open_lock_codes)):
                passable = True
            if passable or nxt in extra_passable:
                seen.add(nxt); queue.append(nxt)
    return seen
