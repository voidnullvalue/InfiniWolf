"""Geometry-aware composition engine shared by the pickup and decoration passes.

`_room_anchors` reduces a room to the handful of cells a composition can key
off -- doorway approaches, corners, wall midpoints -- and
`_room_traversal_frame` derives the dominant walking route through it, so
nothing has to reason about raw coordinates. `_PlacementGrammar` is the commit
API built on top: it only ever places a *named* template, and a template that
does not fit relocates to another compatible room rather than degrading into
scatter.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from itertools import combinations
import random

from .grid import _at, _is_floor, _set
from .model import Room, RoomIdentity, SpritePlacement
from .wl6 import DOORS


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

def _doorway_keep_clear(tiles: list[int]) -> frozenset[tuple[int, int]]:
    """Reserve two walkable cells on each side of every door.

    Room-local anchors cannot see connector repairs and corridor mouths that
    sit just beyond a room's recorded rectangle, so this map-wide reservation
    is the authoritative collision buffer for blocking decoration.
    """
    clear: set[tuple[int, int]] = set()
    for y in range(len(tiles) // 64):
        for x in range(64):
            if _at(tiles, x, y) not in DOORS:
                continue
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                first = x + dx, y + dy
                if not _is_floor(_at(tiles, *first)):
                    continue
                clear.add(first)
                second = first[0] + dx, first[1] + dy
                if _is_floor(_at(tiles, *second)):
                    clear.add(second)
    return frozenset(clear)

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
