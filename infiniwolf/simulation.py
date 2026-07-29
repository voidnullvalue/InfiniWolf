"""Deterministic tile-scale estimates of how a finished floor plays.

This is deliberately not a Wolf3D bot.  It walks the two finished map planes
with doors treated as traversable, uses wall-bounded rays for visibility, and
uses assigned floor zones as the best available evidence for sound activation.
All reported values are normalized to ``[0, 1]`` and are pure functions of the
map.

The planes do not record weapon choice, accuracy, reaction time, enemy movement
after activation, damage rolls, door timing, or secret-pushwall decisions.
Consequently this module does not claim elapsed seconds, hit points lost, or
boss ammunition cost.  ``time`` means walked tile steps, health pressure is a
relative exposure index, and ammunition uses only ordinary-enemy costs present
in :mod:`wl6`.  These limitations are exported as
``UNSUPPORTED_MEASUREMENTS`` rather than filled with invented numbers.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math

from .grid import _at, _is_floor
from .model import GeneratedMap, Room
from .wl6 import (AMMO, AMMO_COST, DOORS, ENEMY_CODES, FAMILY_BY_CODE, GRID,
                  STATIC_BLOCKING)


# Expected ordinary clips in the existing pickup economy contain eight bullets.
# Twelve clips is the measured saturation point: the earlier three-clip cap
# returned 1.0 on nearly every generated floor and therefore carried no signal.
_AMMO_STRAIN_CAP = 96.0
_ATTACKER_CAP = 8
_ENTRY_VISIBLE_CAP = 6
_RETREAT_CAP = 12
_COMBAT_INTERVAL_CAP = 24
_LONG_LANE_TILES = 12

UNSUPPORTED_MEASUREMENTS = (
    "elapsed seconds (the planes provide tile steps, not movement timing)",
    "actual health lost (accuracy, damage rolls and reaction time are absent)",
    "boss/special-enemy ammunition cost (wl6 records no comparable cost)",
    "weapon choice and dropped-ammunition collection",
    "dynamic enemy movement after sound activation",
    "whether a player discovers or opens a secret pushwall",
)


@dataclass(frozen=True, slots=True)
class ProfileMetrics:
    """Normalized experience estimates for one deterministic walk.

    Higher values mean "more" of the named quantity, not always "better".
    Counts and distances saturate at the documented module caps.  Fractions
    (lane time, lateral movement, congestion, activation and backtracking) are
    already naturally bounded.
    """

    profile: str
    enemies_visible_on_entry: float
    maximum_simultaneous_attackers: float
    angular_threat_spread: float
    retreat_distance: float
    long_exposed_lane_time: float
    lateral_movement: float
    choke_congestion: float
    sound_zone_activation: float
    ammunition_spend_before_resupply: float
    health_pressure: float
    backtracking_distance: float
    time_between_combat_beats: float


@dataclass(frozen=True, slots=True)
class SimulationSummary:
    """Profile detail plus the two bounded views consumed by quality scoring."""

    profiles: tuple[ProfileMetrics, ...]
    encounter_affordance: float
    pacing_sustainability: float


@dataclass(frozen=True, slots=True)
class _FloorView:
    level: GeneratedMap
    blocked: frozenset[tuple[int, int]]
    enemies: tuple[tuple[int, int, int], ...]

    def passable(self, cell: tuple[int, int]) -> bool:
        tile = _at(self.level.tiles, *cell)
        return cell not in self.blocked and (_is_floor(tile) or tile in DOORS)

    def zone(self, cell: tuple[int, int]) -> int:
        tile = _at(self.level.tiles, *cell)
        return tile if _is_floor(tile) else -1


def _bounded(value: float) -> float:
    return min(1.0, max(0.0, value))


def _mean(values) -> float:
    values = tuple(values)
    return sum(values) / len(values) if values else 0.0


def _probe(view: _FloorView, room: Room) -> tuple[int, int] | None:
    """Nearest unblocked floor cell to a room centre, stable in map order."""
    cx, cy = room.center
    candidates = (
        (abs(x - cx) + abs(y - cy), y, x, (x, y))
        for y in range(room.y, room.y + room.h)
        for x in range(room.x, room.x + room.w)
        if view.passable((x, y))
    )
    return min(candidates, default=(0, 0, 0, None))[3]


def _shortest_path(view: _FloorView, start: tuple[int, int],
                   target: tuple[int, int]) -> list[tuple[int, int]]:
    """Stable shortest walk with static solid decorations respected."""
    if start == target:
        return [start]
    parent: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
    queue = deque([start])
    while queue:
        x, y = queue.popleft()
        for dx, dy in ((1, 0), (0, 1), (-1, 0), (0, -1)):
            nxt = x + dx, y + dy
            if nxt in parent or not view.passable(nxt):
                continue
            parent[nxt] = (x, y)
            if nxt == target:
                queue.clear()
                break
            queue.append(nxt)
    if target not in parent:
        return []
    result = []
    cursor: tuple[int, int] | None = target
    while cursor is not None:
        result.append(cursor)
        cursor = parent[cursor]
    return list(reversed(result))


def _join_waypoints(view: _FloorView,
                    waypoints: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not waypoints:
        return []
    path = [waypoints[0]]
    cache: dict[tuple[tuple[int, int], tuple[int, int]],
                list[tuple[int, int]]] = {}
    for target in waypoints[1:]:
        if target == path[-1]:
            continue
        key = path[-1], target
        segment = cache.get(key)
        if segment is None:
            reverse = cache.get((target, path[-1]))
            segment = list(reversed(reverse)) if reverse else _shortest_path(
                view, path[-1], target)
            cache[key] = segment
        if segment:
            path.extend(segment[1:])
    return path


def _direct_waypoints(view: _FloorView) -> list[tuple[int, int]]:
    level = view.level
    route = [index for index in level.critical_route
             if 0 <= index < len(level.rooms)]
    probes = [_probe(view, level.rooms[index]) for index in route]
    waypoints = [level.start, *(cell for cell in probes if cell is not None)]
    target = level.exit_stand
    if target is None and 0 <= level.boss_arena_room < len(level.rooms):
        target = _probe(view, level.rooms[level.boss_arena_room])
    if target is None:
        bosses = [(x, y) for x, y, code in view.enemies
                  if code not in FAMILY_BY_CODE]
        target = bosses[0] if bosses else None
    if target is not None and view.passable(target):
        waypoints.append(target)
    return list(dict.fromkeys(waypoints))


def _explorer_waypoints(view: _FloorView) -> list[tuple[int, int]]:
    """Critical route with a stable depth-first visit to each optional branch."""
    level = view.level
    route = list(dict.fromkeys(
        index for index in level.critical_route
        if 0 <= index < len(level.rooms)))
    if not route and level.rooms:
        route = [0]
    route_set = set(route)
    links = {index: [] for index in range(len(level.rooms))}
    for first, second in level.edges:
        if first in links and second in links:
            links[first].append(second)
            links[second].append(first)
    for neighbors in links.values():
        neighbors.sort()

    room_walk: list[int] = []
    visited = set(route_set)

    def branches(root: int) -> None:
        for neighbor in links[root]:
            if neighbor in visited:
                continue
            visited.add(neighbor)
            room_walk.append(neighbor)
            branches(neighbor)
            room_walk.append(root)

    for position, room_index in enumerate(route):
        if not room_walk or room_walk[-1] != room_index:
            room_walk.append(room_index)
        branches(room_index)
        if position + 1 < len(route):
            room_walk.append(route[position + 1])

    waypoints = [level.start]
    waypoints.extend(
        cell for index in room_walk
        for cell in (_probe(view, level.rooms[index]),)
        if cell is not None)
    target = level.exit_stand
    if target is None and 0 <= level.boss_arena_room < len(level.rooms):
        target = _probe(view, level.rooms[level.boss_arena_room])
    if target is not None and view.passable(target):
        waypoints.append(target)
    return waypoints


def _cautious_walk(view: _FloorView,
                   direct: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Add one-tile deterministic peeks at turns and immediately after doors."""
    if len(direct) < 3:
        return direct
    result = [direct[0]]
    for index in range(1, len(direct) - 1):
        previous, current, following = direct[index - 1:index + 2]
        result.append(current)
        before = current[0] - previous[0], current[1] - previous[1]
        after = following[0] - current[0], following[1] - current[1]
        crossed_door = _at(view.level.tiles, *previous) in DOORS
        if before == after and not crossed_door:
            continue
        candidates = [
            (current[0] - after[1], current[1] + after[0]),
            (current[0] + after[1], current[1] - after[0]),
        ]
        peek = next((cell for cell in candidates
                     if cell not in (previous, following)
                     and view.passable(cell)), None)
        if peek is not None:
            result.extend((peek, current))
    result.append(direct[-1])
    return result


def _line_visible(view: _FloorView, origin: tuple[int, int],
                  target: tuple[int, int]) -> bool:
    """Bresenham ray through floor and doors; sprites do not occlude sprites."""
    x0, y0 = origin
    x1, y1 = target
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    error = dx - dy
    x, y = x0, y0
    while True:
        if (x, y) not in (origin, target):
            tile = _at(view.level.tiles, x, y)
            if not _is_floor(tile) and tile not in DOORS:
                return False
        if (x, y) == target:
            return True
        twice = 2 * error
        if twice > -dy:
            error -= dy
            x += sx
        if twice < dx:
            error += dx
            y += sy


def _threat_spread(origin: tuple[int, int],
                   threats: list[tuple[int, int]]) -> float:
    if len(threats) < 2:
        return 0.0
    angles = sorted(math.atan2(y - origin[1], x - origin[0])
                    % math.tau for x, y in threats)
    gaps = [second - first for first, second in zip(angles, angles[1:])]
    gaps.append(math.tau - angles[-1] + angles[0])
    # A half-circle already requires checking opposite flanks, so pressure
    # saturates there rather than reserving 1.0 for an exact full surround.
    return _bounded((math.tau - max(gaps)) / math.pi)


def _lateral_room(view: _FloorView, path: list[tuple[int, int]],
                  index: int) -> float:
    if len(path) < 2:
        return 0.0
    if index:
        dx = path[index][0] - path[index - 1][0]
        dy = path[index][1] - path[index - 1][1]
    else:
        dx = path[1][0] - path[0][0]
        dy = path[1][1] - path[0][1]
    if not dx and not dy:
        return 0.0
    total = 0
    for side in ((-dy, dx), (dy, -dx)):
        for distance in range(1, 4):
            cell = (path[index][0] + side[0] * distance,
                    path[index][1] + side[1] * distance)
            if not view.passable(cell):
                break
            total += 1
    return total / 6.0


def _lane_length(view: _FloorView, cell: tuple[int, int]) -> int:
    spans = []
    for axis in (((1, 0), (-1, 0)), ((0, 1), (0, -1))):
        span = 1
        for dx, dy in axis:
            for distance in range(1, GRID):
                if not view.passable(
                        (cell[0] + dx * distance, cell[1] + dy * distance)):
                    break
                span += 1
        spans.append(span)
    return max(spans)


def _backtracking(path: list[tuple[int, int]]) -> float:
    if len(path) < 2:
        return 0.0
    traversed: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    retraced = 0
    for first, second in zip(path, path[1:]):
        retraced += (second, first) in traversed
        traversed.add((first, second))
    return retraced / (len(path) - 1)


def _resource_steps(view: _FloorView, path: list[tuple[int, int]],
                    profile: str) -> frozenset[int]:
    """Steps at which a plausible nearby clip becomes available."""
    ammo_cells = [(index % GRID, index // GRID)
                  for index, code in enumerate(view.level.things)
                  if code == AMMO]
    if not ammo_cells:
        return frozenset()
    first_zone_step: dict[int, int] = {}
    for index, cell in enumerate(path):
        zone = view.zone(cell)
        if zone >= 0:
            first_zone_step.setdefault(zone, index)
    steps = set()
    for ammo in ammo_cells:
        if profile == "explorer":
            step = first_zone_step.get(view.zone(ammo))
            if step is not None:
                steps.add(step)
            continue
        radius = 2 if profile == "cautious-corner-checker" else 1
        step = next((
            index for index, cell in enumerate(path)
            if abs(cell[0] - ammo[0]) + abs(cell[1] - ammo[1]) <= radius
            and _line_visible(view, cell, ammo)
        ), None)
        if step is not None:
            steps.add(step)
    return frozenset(steps)


def _profile(view: _FloorView, name: str,
             path: list[tuple[int, int]],
             lane_lengths: dict[tuple[int, int], int],
             visibility_cache: dict[tuple[tuple[int, int], int], bool]
             ) -> ProfileMetrics:
    if not path:
        return ProfileMetrics(name, *(0.0 for _ in range(12)))

    enemies_by_zone: dict[int, set[int]] = {}
    for enemy_index, (x, y, _) in enumerate(view.enemies):
        enemies_by_zone.setdefault(view.zone((x, y)), set()).add(enemy_index)

    visible_at: list[list[int]] = []
    for cell in path:
        visible = []
        for enemy_index, (x, y, _) in enumerate(view.enemies):
            key = cell, enemy_index
            if key not in visibility_cache:
                visibility_cache[key] = _line_visible(view, cell, (x, y))
            if visibility_cache[key]:
                visible.append(enemy_index)
        visible_at.append(visible)

    lateral = [_lateral_room(view, path, index)
               for index in range(len(path))]
    for cell in set(path):
        if cell not in lane_lengths:
            lane_lengths[cell] = _lane_length(view, cell)

    entry_counts = []
    sound_batches = []
    engaged: set[int] = set()
    beat_steps = []
    beat_spreads = []
    retreat_distances = []
    beat_pressures = []
    active_zone = view.zone(path[0])
    activated_zones = {active_zone} if active_zone >= 0 else set()
    initial_zone_threats = set(enemies_by_zone.get(active_zone, ()))
    if active_zone >= 0:
        sound_batches.append(len(initial_zone_threats))

    ammo_steps = _resource_steps(view, path, name)
    ammo_segment = 0.0
    maximum_ammo_segment = 0.0
    exposed_lane_steps = 0
    congested_steps = 0
    combat_exposed_steps = 0

    for step, (cell, visible) in enumerate(zip(path, visible_at)):
        zone = view.zone(cell)
        entered = zone >= 0 and zone != active_zone
        zone_threats: set[int] = (initial_zone_threats if step == 0
                                  else set())
        if entered:
            active_zone = zone
            if zone not in activated_zones:
                activated_zones.add(zone)
                zone_threats = enemies_by_zone.get(zone, set())
                sound_batches.append(len(zone_threats))
            entry_counts.append(len(visible))

        if step in ammo_steps:
            maximum_ammo_segment = max(maximum_ammo_segment, ammo_segment)
            ammo_segment = 0.0

        newly_engaged = (set(visible) | zone_threats) - engaged
        if newly_engaged:
            engaged.update(newly_engaged)
            beat_steps.append(step)
            threat_cells = [(view.enemies[index][0], view.enemies[index][1])
                            for index in visible]
            spread = _threat_spread(cell, threat_cells)
            beat_spreads.append(spread)

            retreat = 1.0
            for distance in range(1, min(_RETREAT_CAP, step) + 1):
                prior = path[step - distance]
                safe = not any(
                    _line_visible(view, prior,
                                  (view.enemies[index][0],
                                   view.enemies[index][1]))
                    for index in visible)
                if safe and lateral[step - distance] > 0.0:
                    retreat = distance / _RETREAT_CAP
                    break
            retreat_distances.append(retreat)

            load = _bounded(len(visible) / _ENTRY_VISIBLE_CAP)
            pressure = load * (
                0.45 + 0.20 * spread + 0.20 * retreat
                + 0.15 * (1.0 - lateral[step]))
            beat_pressures.append(_bounded(pressure))
            ammo_segment += sum(
                AMMO_COST.get(FAMILY_BY_CODE.get(view.enemies[index][2]), 0.0)
                for index in newly_engaged)

        if visible:
            combat_exposed_steps += 1
            if lane_lengths[cell] >= _LONG_LANE_TILES:
                exposed_lane_steps += 1
            if lateral[step] <= 1.0 / 6.0 and len(visible) >= 2:
                congested_steps += 1

    maximum_ammo_segment = max(maximum_ammo_segment, ammo_segment)
    intervals = [second - first
                 for first, second in zip(beat_steps, beat_steps[1:])]
    maximum_attackers = max(map(len, visible_at), default=0)
    return ProfileMetrics(
        profile=name,
        enemies_visible_on_entry=_bounded(
            _mean(entry_counts) / _ENTRY_VISIBLE_CAP),
        maximum_simultaneous_attackers=_bounded(
            maximum_attackers / _ATTACKER_CAP),
        angular_threat_spread=_bounded(_mean(beat_spreads)),
        retreat_distance=_bounded(_mean(retreat_distances)),
        long_exposed_lane_time=(
            exposed_lane_steps / combat_exposed_steps
            if combat_exposed_steps else 0.0),
        lateral_movement=_bounded(_mean(lateral)),
        choke_congestion=(
            congested_steps / combat_exposed_steps
            if combat_exposed_steps else 0.0),
        # Average first-entry activation batch; unlike map coverage, this stays
        # informative when the explorer deliberately visits every branch.
        sound_zone_activation=_bounded(
            _mean(sound_batches) / _ENTRY_VISIBLE_CAP),
        ammunition_spend_before_resupply=_bounded(
            maximum_ammo_segment / _AMMO_STRAIN_CAP),
        health_pressure=_bounded(_mean(beat_pressures)),
        backtracking_distance=_bounded(_backtracking(path)),
        time_between_combat_beats=_bounded(
            _mean(intervals) / _COMBAT_INTERVAL_CAP),
    )


def _summary(profiles: tuple[ProfileMetrics, ...]) -> SimulationSummary:
    combat_profiles = [profile for profile in profiles
                       if profile.maximum_simultaneous_attackers > 0.0
                       or profile.sound_zone_activation > 0.0]
    if not combat_profiles:
        return SimulationSummary(profiles, 0.0, 0.0)

    encounter = _mean(
        _mean((
            _bounded(profile.enemies_visible_on_entry * 3.0),
            profile.lateral_movement,
            1.0 - profile.retreat_distance,
            1.0 - profile.choke_congestion,
            1.0 - _mean((profile.maximum_simultaneous_attackers,
                         profile.angular_threat_spread)),
            1.0 - profile.sound_zone_activation,
        ))
        for profile in combat_profiles
    )
    pacing = _mean(
        _mean((
            1.0 - profile.long_exposed_lane_time,
            1.0 - profile.ammunition_spend_before_resupply,
            1.0 - profile.health_pressure,
            1.0 - profile.choke_congestion,
            1.0 - profile.backtracking_distance,
            profile.time_between_combat_beats,
        ))
        for profile in combat_profiles
    )
    return SimulationSummary(
        profiles, _bounded(encounter), _bounded(pacing))


def simulate_player_experience(level: GeneratedMap) -> SimulationSummary:
    """Walk ``level`` as direct, cautious and exploratory player profiles."""
    if (len(level.tiles) != GRID * GRID
            or len(level.things) != GRID * GRID
            or not (0 <= level.start[0] < GRID and 0 <= level.start[1] < GRID)):
        empty = tuple(
            ProfileMetrics(name, *(0.0 for _ in range(12)))
            for name in ("direct-route", "cautious-corner-checker", "explorer"))
        return SimulationSummary(empty, 0.0, 0.0)

    blocked = frozenset(
        (index % GRID, index // GRID)
        for index, code in enumerate(level.things)
        if code in STATIC_BLOCKING)
    enemies = tuple(
        (index % GRID, index // GRID, code)
        for index, code in enumerate(level.things)
        if code in ENEMY_CODES)
    view = _FloorView(level, blocked, enemies)
    direct = _join_waypoints(view, _direct_waypoints(view))
    paths = (
        ("direct-route", direct),
        ("cautious-corner-checker", _cautious_walk(view, direct)),
        ("explorer", _join_waypoints(view, _explorer_waypoints(view))),
    )
    lane_lengths: dict[tuple[int, int], int] = {}
    visibility_cache: dict[tuple[tuple[int, int], int], bool] = {}
    profiles = tuple(
        _profile(view, name, path, lane_lengths, visibility_cache)
        for name, path in paths)
    return _summary(profiles)
