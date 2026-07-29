"""Pure preview data and self-contained SVG campaign rendering.

Plane-only package data can derive shortest start-to-exit routes, enemy sprite
locations, pickups, secrets, and sound-zone membership. Authored sightlines are
also loaded when an InfiniWolf manifest is present. The exact authored critical
room route, landmark room coordinates, and encounter provenance exist only on a
freshly generated ``GeneratedMap``; ``preview_generated()`` preserves them.
Package previews therefore use the shortest traversable route for
``critical-route``, all enemy sprites for ``encounters``, and cannot render
``landmarks`` because the manifest intentionally does not serialize room origins.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import json
from pathlib import Path
import struct
from xml.sax.saxutils import escape
import zipfile

from . import generator as G


OVERLAY_KINDS = (
    "critical-route",
    "landmarks",
    "encounters",
    "sound-zones",
    "authored-sightlines",
)


@dataclass(frozen=True, slots=True)
class MapPreview:
    number: int
    name: str
    tiles: tuple[int, ...]
    things: tuple[int, ...]
    exit_cell: tuple[int, int] | None = None
    critical_route_cells: tuple[tuple[int, int], ...] = ()
    landmark_cells: tuple[tuple[int, int], ...] = ()
    encounter_cells: tuple[tuple[int, int], ...] = ()
    authored_sightline_cells: tuple[tuple[int, int], ...] = ()

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
        # A floor 9 whose boss ends the campaign has no lift: he is what the
        # route leads to, so draw it to him rather than showing no route at all.
        for index, thing in enumerate(self.things):
            if thing in G.VICTORY_BOSSES:
                return index % G.GRID, index // G.GRID
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


def _manifest_cells(items) -> tuple[tuple[int, int], ...]:
    cells = set()
    for item in items if isinstance(items, list) else ():
        for raw in item.get("cells", ()) if isinstance(item, dict) else ():
            if (isinstance(raw, (list, tuple)) and len(raw) == 2
                    and all(isinstance(value, int) for value in raw)):
                x, y = raw
                if 0 <= x < G.GRID and 0 <= y < G.GRID:
                    cells.add((x, y))
    return tuple(sorted(cells, key=lambda cell: (cell[1], cell[0])))


def load_previews(path: Path) -> tuple[MapPreview, ...]:
    """Load ten package maps and any overlay metadata the package retains.

    See the module docstring for the generation-only landmark and provenance
    limitations. Packages without a manifest still support all plane-derived
    overlays.
    """
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
            sightlines = _manifest_cells(detail.get("authored_sightlines", ()))
            result.append(MapPreview(
                number, str(detail.get("name") or fallback), tiles, things,
                exit_cell, authored_sightline_cells=sightlines,
            ))
    return tuple(result)


def _room_cells(level, room_indices) -> tuple[tuple[int, int], ...]:
    cells = set()
    for index in room_indices:
        if not 0 <= index < len(level.rooms):
            continue
        room = level.rooms[index]
        cells.update(
            (x, y)
            for y in range(room.y, room.y + room.h)
            for x in range(room.x, room.x + room.w)
            if G._is_floor(level.tiles[y * G.GRID + x])
        )
    return tuple(sorted(cells, key=lambda cell: (cell[1], cell[0])))


def _room_focus(level, room_index: int) -> tuple[int, int] | None:
    if not 0 <= room_index < len(level.rooms):
        return None
    room = level.rooms[room_index]
    center = room.center
    cells = _room_cells(level, (room_index,))
    return min(cells, key=lambda cell: (abs(cell[0] - center[0])
                                        + abs(cell[1] - center[1]), cell)) if cells else None


def preview_generated(level, name: str | None = None) -> MapPreview:
    """Preserve all generation-only overlay evidence in a ``MapPreview``."""
    landmarks = tuple(
        cell for cell in (_room_focus(level, plan.room_index)
                          for plan in level.landmarks)
        if cell is not None
    )
    encounters = tuple(sorted(
        {(x, y) for encounter in level.encounters for x, y, _ in encounter.cells},
        key=lambda cell: (cell[1], cell[0]),
    ))
    sightlines = tuple(sorted(
        {cell for line in level.authored_sightlines for cell in line.cells},
        key=lambda cell: (cell[1], cell[0]),
    ))
    fallback = "Secret Floor" if level.number == 10 else f"Floor {level.number}"
    label = name or (f"{fallback}: {level.variant}" if level.variant else fallback)
    return MapPreview(
        level.number, label, tuple(level.tiles), tuple(level.things),
        level.exit_stand,
        critical_route_cells=_room_cells(level, level.critical_route),
        landmark_cells=landmarks,
        encounter_cells=encounters,
        authored_sightline_cells=sightlines,
    )


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


def sound_zone_color(tile: int) -> str:
    """Stable high-contrast color for a sound-zone label."""
    palette = ("#5cc8ff", "#ff9f68", "#a8e063", "#c792ea", "#ffd166",
               "#76e6c5", "#ff7aa2", "#9aa7ff", "#e0c568", "#71c4a8",
               "#ed8cff", "#8fd3ff")
    return palette[(tile - G.FLOOR) % len(palette)]


def overlay_cells(preview: MapPreview, kind: str) -> tuple[tuple[int, int], ...]:
    """Return cells for plane-derived and generation-aware overlay kinds.

    ``landmarks`` requires ``preview_generated``. ``authored-sightlines`` needs
    generation or an InfiniWolf manifest. A package's ``critical-route`` falls
    back to a shortest traversable start/exit path, while package ``encounters``
    shows all enemy sprites because authored encounter grouping is not in WAD
    planes.
    """
    if kind == "route":
        return preview.route()
    if kind == "critical-route":
        return preview.critical_route_cells or preview.route()
    if kind == "landmarks":
        return preview.landmark_cells
    if kind == "encounters":
        if preview.encounter_cells:
            return preview.encounter_cells
        accepted = G.ENEMY_CODES
    elif kind == "sound-zones":
        return tuple((index % G.GRID, index // G.GRID)
                     for index, tile in enumerate(preview.tiles)
                     if G.FLOOR <= tile <= G.ZONE_MAX)
    elif kind == "authored-sightlines":
        return preview.authored_sightline_cells
    elif kind == "start-exit":
        return tuple(cell for cell in (preview.start(), preview.exit()) if cell is not None)
    elif kind == "enemies":
        accepted = G.ENEMY_CODES
    elif kind == "pickups":
        accepted = G.PICKUP_CODES
    elif kind == "secrets":
        accepted = {G.PUSHWALL}
    else:
        return ()
    return tuple((index % G.GRID, index // G.GRID)
                 for index, thing in enumerate(preview.things) if thing in accepted)


def _runs_by_color(preview: MapPreview) -> dict[str, list[tuple[int, int, int]]]:
    runs: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
    for y in range(G.GRID):
        x = 0
        while x < G.GRID:
            color = tile_color(preview.tiles[y * G.GRID + x])
            end = x + 1
            while (end < G.GRID
                   and tile_color(preview.tiles[y * G.GRID + end]) == color):
                end += 1
            runs[color].append((x, y, end - x))
            x = end
    return runs


def _path_data(runs) -> str:
    return "".join(f"M{x} {y}h{width}v1h-{width}z" for x, y, width in runs)


def render_contact_sheet(previews: tuple[MapPreview, ...], overlay: str | None = None,
                         columns: int = 5) -> str:
    """Render campaign previews into one self-contained, dependency-free SVG."""
    if not previews:
        raise ValueError("contact sheet needs at least one floor")
    if overlay is not None and overlay not in OVERLAY_KINDS:
        raise ValueError(f"unknown contact-sheet overlay: {overlay}")
    columns = max(1, min(columns, len(previews)))
    rows = (len(previews) + columns - 1) // columns
    tile_scale = 2.5
    map_size = G.GRID * tile_scale
    panel_width, panel_height = 184, 198
    margin = 16
    width = margin * 2 + columns * panel_width
    height = margin * 2 + rows * panel_height + 22
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}">',
        '<rect width="100%" height="100%" fill="#111418"/>',
        '<g font-family="sans-serif" fill="#edf1f5">',
    ]
    for index, preview in enumerate(previews):
        column, row = index % columns, index // columns
        panel_x = margin + column * panel_width
        panel_y = margin + row * panel_height
        map_x, map_y = panel_x + 12, panel_y + 24
        parts.append(
            f'<g class="floor-panel" id="floor-{preview.number}" '
            f'data-floor="{preview.number}">'
        )
        parts.append(
            f'<text x="{panel_x + 12}" y="{panel_y + 16}" font-size="12" '
            f'font-weight="600">{escape(preview.name)}</text>'
        )
        parts.append(
            f'<rect x="{map_x - 1}" y="{map_y - 1}" width="{map_size + 2}" '
            f'height="{map_size + 2}" rx="2" fill="#090b0d" stroke="#3a4149"/>'
        )
        parts.append(
            f'<g transform="translate({map_x} {map_y}) scale({tile_scale})" '
            'shape-rendering="crispEdges">'
        )
        for color, runs in _runs_by_color(preview).items():
            parts.append(f'<path fill="{color}" d="{_path_data(runs)}"/>')

        cells = overlay_cells(preview, overlay) if overlay else ()
        if overlay == "sound-zones":
            by_zone: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
            for x, y in cells:
                by_zone[preview.tiles[y * G.GRID + x]].append((x, y, 1))
            for zone, runs in by_zone.items():
                parts.append(
                    f'<path fill="{sound_zone_color(zone)}" fill-opacity="0.72" '
                    f'd="{_path_data(runs)}"/>'
                )
        elif overlay == "landmarks":
            for x, y in cells:
                parts.append(
                    f'<circle cx="{x + 0.5}" cy="{y + 0.5}" r="1.35" '
                    'fill="#ffd166" stroke="#16191d" stroke-width="0.45"/>'
                )
        elif overlay == "encounters":
            for x, y in cells:
                parts.append(
                    f'<circle cx="{x + 0.5}" cy="{y + 0.5}" r="0.72" '
                    'fill="#ff4d5f" stroke="#2a080c" stroke-width="0.25"/>'
                )
        elif overlay == "authored-sightlines":
            parts.append(
                f'<path fill="#41e6ff" fill-opacity="0.82" d="'
                f'{_path_data((x, y, 1) for x, y in cells)}"/>'
            )
        elif overlay == "critical-route":
            parts.append(
                f'<path fill="#3b82f6" fill-opacity="0.42" d="'
                f'{_path_data((x, y, 1) for x, y in cells)}"/>'
            )

        start, goal = preview.start(), preview.exit()
        if start is not None:
            parts.append(
                f'<circle cx="{start[0] + 0.5}" cy="{start[1] + 0.5}" r="1.05" '
                'fill="#35e879" stroke="#072b16" stroke-width="0.35"/>'
            )
        if goal is not None:
            parts.append(
                f'<rect x="{goal[0] - 0.35}" y="{goal[1] - 0.35}" '
                'width="1.7" height="1.7" fill="#f15cff" '
                'stroke="#351038" stroke-width="0.35"/>'
            )
        parts.append('</g></g>')
    label = overlay or "none"
    parts.append(
        f'<text x="{margin + 12}" y="{height - 12}" font-size="11" '
        f'fill="#aeb7c2">Overlay: {escape(label)} · green=start · magenta=exit</text>'
    )
    parts.append('</g></svg>')
    return "\n".join(parts) + "\n"


def write_contact_sheet(path: Path, previews: tuple[MapPreview, ...],
                        overlay: str | None = None) -> Path:
    """Write ``render_contact_sheet`` atomically enough for a diagnostic file."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_contact_sheet(previews, overlay), encoding="utf-8")
    return path
