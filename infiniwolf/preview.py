"""Pure PK3/WAD parsing and render data for the optional campaign viewer."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
from pathlib import Path
import struct
import zipfile

from . import generator as G


@dataclass(frozen=True, slots=True)
class MapPreview:
    number: int
    name: str
    tiles: tuple[int, ...]
    things: tuple[int, ...]
    exit_cell: tuple[int, int] | None = None

    def start(self) -> tuple[int, int] | None:
        for index, thing in enumerate(self.things):
            if thing in G.PLAYER_START_CODES:
                return index % G.GRID, index // G.GRID
        return None

    def exit(self) -> tuple[int, int] | None:
        if self.exit_cell is not None:
            return self.exit_cell
        for y in range(G.GRID):
            for x in range(G.GRID):
                if not G._is_floor(self.tiles[y * G.GRID + x]):
                    continue
                if any(0 <= x + dx < G.GRID
                       and self.tiles[y * G.GRID + x + dx] == G.ELEVATOR_TILE
                       for dx in (-1, 1)):
                    return x, y
        return None

    def route(self) -> tuple[tuple[int, int], ...]:
        start, goal = self.start(), self.exit()
        if start is None or goal is None:
            return ()
        previous: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
        queue = deque([start])
        while queue and goal not in previous:
            x, y = queue.popleft()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxt = x + dx, y + dy
                if nxt in previous or not (0 <= nxt[0] < G.GRID and 0 <= nxt[1] < G.GRID):
                    continue
                tile = self.tiles[nxt[1] * G.GRID + nxt[0]]
                if not (G._is_floor(tile) or tile in G.DOORS):
                    continue
                previous[nxt] = (x, y)
                queue.append(nxt)
        if goal not in previous:
            return ()
        result = []
        cursor: tuple[int, int] | None = goal
        while cursor is not None:
            result.append(cursor)
            cursor = previous[cursor]
        return tuple(reversed(result))


def _parse_wad(data: bytes) -> tuple[tuple[int, ...], tuple[int, ...]]:
    if len(data) < 32 or data[:4] != b"PWAD":
        raise ValueError("map is not a WDC PWAD")
    size = struct.unpack_from("<I", data, 8)[0]
    body = data[12:12 + size]
    if body[:6] != b"WDC3.1":
        raise ValueError("map uses unsupported planes")
    _, planes, name_length = struct.unpack_from("<IHH", body, 6)
    offset = 14 + name_length
    width, height = struct.unpack_from("<HH", body, offset)
    offset += 4
    if width != G.GRID or height != G.GRID or planes < 2:
        raise ValueError("map has unsupported dimensions")
    if offset + 16384 > len(body):
        raise ValueError("map planes are truncated")
    tiles = struct.unpack_from("<4096H", body, offset)
    things = struct.unpack_from("<4096H", body, offset + 8192)
    return tiles, things


def load_previews(path: Path) -> tuple[MapPreview, ...]:
    with zipfile.ZipFile(path) as package:
        try:
            manifest = json.loads(package.read("infiniwolf-manifest.json"))
        except KeyError:
            manifest = {"floors": []}
        described = {int(item.get("number", 0)): item
                     for item in manifest.get("floors", [])}
        result = []
        for number in range(1, 11):
            tiles, things = _parse_wad(package.read(f"maps/iw{number:02d}.wad"))
            fallback = "Secret Floor" if number == 10 else f"Floor {number}"
            detail = described.get(number, {})
            raw_exit = detail.get("exit_stand")
            exit_cell = (tuple(raw_exit) if isinstance(raw_exit, list)
                         and len(raw_exit) == 2 else None)
            result.append(MapPreview(number, str(detail.get("name") or fallback),
                                     tiles, things, exit_cell))
    return tuple(result)


def tile_color(tile: int) -> str:
    if G._is_floor(tile):
        return "#d8d3c6"
    if tile in G.GOLD_DOORS:
        return "#d2a72c"
    if tile in G.SILVER_DOORS:
        return "#aeb8bf"
    if tile in (G.DOOR_ELEVATOR, G.DOOR_ELEVATOR_NS):
        return "#3d87a8"
    if tile in G.DOORS:
        return "#8a5a3b"
    if tile in (G.ELEVATOR_TILE, G.DUMMY_ELEVATOR_TILE):
        return "#4c7180"
    palettes = ("#25272a", "#343945", "#4a4038", "#42384c",
                "#38463e", "#4b4640", "#303a46")
    return palettes[tile % len(palettes)]


def overlay_cells(preview: MapPreview, kind: str) -> tuple[tuple[int, int], ...]:
    if kind == "route":
        return preview.route()
    if kind == "start-exit":
        return tuple(cell for cell in (preview.start(), preview.exit()) if cell is not None)
    if kind == "enemies":
        accepted = G.ENEMY_CODES
    elif kind == "pickups":
        accepted = G.PICKUP_CODES
    elif kind == "secrets":
        accepted = {G.PUSHWALL}
    else:
        return ()
    return tuple((index % G.GRID, index // G.GRID)
                 for index, thing in enumerate(preview.things) if thing in accepted)
