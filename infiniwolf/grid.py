"""Tile-plane primitives.

Both map planes are flat GRID*GRID lists of native WL6 codes. `_at`/`_set`
bounds-check against that grid so callers can probe freely off-map (`-1`), and
`_reachable` is the flood fill every blocking placement re-runs before it
commits.

Purely structural: these answer "what is at this cell", "what can be walked to"
and "how are the rooms connected", never "what should go here". The room
adjacency graph lives here for the same reason the tile plane does -- walking it
is a query, while deciding what the walk means is policy. Progression, encounter and semantic policy belong to
the modules that own those decisions -- a helper that consults a room's role or
concept is in the wrong file.
"""

from __future__ import annotations

from collections import deque

from .model import Room
from .wl6 import (DOOR_ELEVATOR, DOOR_ELEVATOR_NS, DOOR_EW, DOOR_NS, DOORS, ELEVATOR_TILE,
                  FLOOR, GRID, LOCKED_DOORS, PUSHWALL, SECRET_EXIT_ZONE, ZONE_MAX)


def _at(plane: list[int], x: int, y: int) -> int:
    return plane[y * GRID + x] if 0 <= x < GRID and 0 <= y < GRID else -1

def _set(plane: list[int], x: int, y: int, value: int) -> None:
    if 0 <= x < GRID and 0 <= y < GRID:
        plane[y * GRID + x] = value

def _is_floor(value: int) -> bool:
    return FLOOR <= value <= ZONE_MAX or value == SECRET_EXIT_ZONE


def _is_walkable(value: int) -> bool:
    """Whether a tile counts as an orthogonal route for topology queries."""
    return _is_floor(value) or value in DOORS


def _is_dead_end(tiles: list[int], x: int, y: int) -> bool:
    """Whether a floor cell has exactly one floor-or-door neighbour."""
    return (_is_floor(_at(tiles, x, y))
            and sum(_is_walkable(_at(tiles, x + dx, y + dy))
                    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))) == 1)


def _dead_ends_near(tiles: list[int], cells: set[tuple[int, int]]) -> set[tuple[int, int]]:
    """Return dead ends at cells touched by a mutation and their neighbours."""
    nearby = {(x + dx, y + dy) for x, y in cells
              for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1))}
    return {(x, y) for x, y in nearby if _is_dead_end(tiles, x, y)}


def _dead_end_cells(tiles: list[int]) -> set[tuple[int, int]]:
    """Return floor cells with exactly one floor-or-door neighbour."""
    return {(x, y) for y in range(GRID) for x in range(GRID)
            if _is_dead_end(tiles, x, y)}


def _qualifying_dead_end_alcove(tiles: list[int], things: list[int],
                                cell: tuple[int, int], start: tuple[int, int],
                                reachable: set[tuple[int, int]]) -> bool:
    """Whether an empty, reachable one-cell dead end needs visual occupancy."""
    x, y = cell
    if (cell == start or _at(things, x, y) != 0
            or _at(tiles, x, y) == SECRET_EXIT_ZONE
            or not _is_dead_end(tiles, x, y)):
        return False
    neighbours = ((x + dx, y + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
    if any(_at(things, nx, ny) == PUSHWALL or _at(tiles, nx, ny) == ELEVATOR_TILE
           for nx, ny in neighbours):
        return False
    return cell in reachable


def qualifying_dead_end_alcoves(tiles: list[int], things: list[int],
                                start: tuple[int, int]) -> tuple[tuple[int, int], ...]:
    """Return every empty, reachable gameplay dead-end alcove in map order."""
    reachable = _reachable(tiles, start, locked_open=True)
    return tuple((x, y) for y in range(GRID) for x in range(GRID)
                 if _qualifying_dead_end_alcove(tiles, things, (x, y), start, reachable))

# Precomputed `_is_floor(code) or code in DOORS` for every tile code. The
# corridor router evaluates that test on all four neighbours of every candidate
# cell it examines -- tens of millions of times per campaign -- and did it with
# two bounds-checked _at calls per direction inside a generator expression. Built
# from the same two predicates, so it is exact by construction. 256 entries
# covers the whole vocabulary (ZONE_MAX is 143).
_FLOOR_OR_DOOR = bytes(1 if (_is_floor(code) or code in DOORS) else 0
                       for code in range(256))


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
    # These sets are membership-only in this function. Their iteration order
    # cannot reach an RNG draw or returned collection, so integer keys are
    # safe here. `seen` remains tuple-keyed because it is returned to callers.
    extra_cells = {y * GRID + x for x, y in extra_passable}
    blocked_cells = {y * GRID + x for x, y in blocked}
    seen = {start}
    grid = GRID
    start_cell = start[1] * grid + start[0]
    visited = [False] * (grid * grid)
    visited[start_cell] = True
    queue = deque([start_cell])
    append = queue.append
    popleft = queue.popleft
    plane = tiles
    floor_or_door = _FLOOR_OR_DOOR
    locked_doors = LOCKED_DOORS
    neighbours = ((1, 0), (-1, 0), (0, 1), (0, -1))
    while queue:
        cell = popleft()
        x, y = cell % grid, cell // grid
        for dx, dy in neighbours:
            nxt = x + dx, y + dy
            nx, ny = nxt
            if not (0 <= nx < grid and 0 <= ny < grid):
                continue
            next_cell = ny * grid + nx
            if visited[next_cell] or next_cell in blocked_cells:
                continue
            tile = plane[next_cell]
            passable = floor_or_door[tile] and tile not in locked_doors
            if ((locked_open and tile in LOCKED_DOORS)
                    or (open_lock_codes is not None and tile in open_lock_codes)):
                passable = True
            if passable or next_cell in extra_cells:
                visited[next_cell] = True
                seen.add(nxt)
                append(next_cell)
    return seen


def _path_bends(path: list[tuple[int, int]]) -> int:
    """Number of direction changes along a carved corridor path."""
    headings = [(end[0] - start[0], end[1] - start[1])
                for start, end in zip(path, path[1:])]
    return sum(current != previous for previous, current in zip(headings, headings[1:]))


def _overlaps(a: Room, b: Room, pad: int = 2) -> bool:
    return not (a.x + a.w + pad <= b.x or b.x + b.w + pad <= a.x or
                a.y + a.h + pad <= b.y or b.y + b.h + pad <= a.y)


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
