"""Regression guards for authored-feeling composition, not just validity.

validate_map already guarantees a map is playable; these tests guard
properties that make a *valid* map still look procedural or broken in a way
validate_map doesn't check -- e.g. two rooms silently fused into one long
open sightline by a corridor-routing fallback. Each test names the concrete
failure mode it exists to catch.
"""
from collections import deque
import unittest

from infiniwolf.config import CampaignConfig
from infiniwolf.generator import (DOGS, DOORS, GRID, GUARDS, OFFICERS, SS,
                                   STATIC_BLOCKING, _at, _is_floor, generate_map)


def _generate_with_retries(config: CampaignConfig, floor: int, attempts: int = 50):
    """Mirror generate_campaign's own retry loop: a single (seed, floor,
    attempt) combination may legitimately fail validate_map and needs a
    fresh sub-seed, exactly as production usage already tolerates."""
    last_error = None
    for attempt in range(attempts):
        try:
            return generate_map(config, floor, attempt)
        except ValueError as error:
            last_error = error
    raise AssertionError(f"floor {floor} never validated in {attempts} attempts: {last_error}")

# Deterministic seeds known (as of the 2026-07-12 macro-layout work) to
# exercise specific structural edges: crowded hubs, dense districts, small
# and large layout-complexity extremes. Keep adding pathological seeds here
# as they're found rather than only relying on broad fuzzing.
REGRESSION_SEEDS = ("e", "alpha", "bravo", "charlie", "delta", 42, 1783823867320418919)


def _longest_straight_run(tiles: list[int]) -> int:
    """Longest unobstructed floor run on any row or column; doors and walls
    both break a run. A corridor silently fused into a room with no wall or
    door at the seam (a real bug once caused by a routing fallback) shows up
    here as a long run with zero doors anywhere on that line."""
    best = 0
    for horizontal in (True, False):
        for fixed in range(GRID):
            run = 0
            for moving in range(GRID):
                x, y = (moving, fixed) if horizontal else (fixed, moving)
                run = run + 1 if _is_floor(_at(tiles, x, y)) else 0
                best = max(best, run)
    return best


def _floor_components(tiles: list[int]) -> list[set[tuple[int, int]]]:
    unassigned = {(x, y) for y in range(GRID) for x in range(GRID) if _is_floor(_at(tiles, x, y))}
    components = []
    while unassigned:
        start = next(iter(unassigned))
        component = {start}
        queue = deque([start])
        unassigned.remove(start)
        while queue:
            x, y = queue.popleft()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxt = (x + dx, y + dy)
                if nxt in unassigned:
                    unassigned.remove(nxt)
                    component.add(nxt)
                    queue.append(nxt)
        components.append(component)
    return components


_ACTOR_FACINGS = ((0, -1), (1, 0), (0, 1), (-1, 0))


def _actor_facing_index(code: int) -> int | None:
    """Decode a things-plane code back to its N/E/S/W facing, across the
    tier offsets (+36/+72) that skill 2/3 copies use."""
    for family in (GUARDS, OFFICERS, SS, DOGS):
        for tier in (0, 1, 2):
            base = code - 36 * tier
            if base in family:
                return family.index(base)
    return None


class ActorFacingRegressionTests(unittest.TestCase):
    def test_actors_do_not_face_a_wall_or_blocking_decoration(self):
        """Stationary facing is picked against the map before decorations are
        placed; decoration placement only checked that a cell was empty, not
        who was facing it, so a pillar/barrel/table could land directly in a
        guard's face -- indistinguishable in-game from facing a wall. Covers
        both the literal-wall case and the blocking-decoration case that
        earlier tests never exercised."""
        blocking = set(STATIC_BLOCKING)
        for seed in REGRESSION_SEEDS:
            config = CampaignConfig(seed=seed)
            for floor in (2, 5, 8):
                level = _generate_with_retries(config, floor)
                tiles, things = level.tiles, level.things
                for index, code in enumerate(things):
                    facing = _actor_facing_index(code)
                    if facing is None:
                        continue
                    x, y = index % GRID, index // GRID
                    dx, dy = _ACTOR_FACINGS[facing]
                    fx, fy = x + dx, y + dy
                    ahead_tile = _at(tiles, fx, fy)
                    ahead_thing = _at(things, fx, fy)
                    self.assertTrue(
                        _is_floor(ahead_tile) or ahead_tile in DOORS,
                        f"seed={seed!r} floor={floor}: actor at ({x},{y}) faces "
                        f"non-floor tile {ahead_tile}")
                    self.assertNotIn(
                        ahead_thing, blocking,
                        f"seed={seed!r} floor={floor}: actor at ({x},{y}) faces "
                        f"blocking decoration {ahead_thing}")


class TopologyRegressionTests(unittest.TestCase):
    def test_no_unbounded_sightlines(self):
        """Report ceiling: no straight unobstructed run should exceed 21
        tiles. A run this long with no door on it means two spaces were
        fused with no separating architecture at all, not just a lane the
        sightline-breaker declined to touch."""
        for seed in REGRESSION_SEEDS:
            config = CampaignConfig(seed=seed)
            for floor in (2, 5, 8):
                level = _generate_with_retries(config, floor)
                longest = _longest_straight_run(level.tiles)
                self.assertLessEqual(
                    longest, 21,
                    f"seed={seed!r} floor={floor}: longest straight run {longest} > 21")

    def test_door_bounded_rooms_are_not_gigantic_blobs(self):
        """A silently-fused corridor also shows up as one door-bounded floor
        component absorbing most of the map instead of many rooms -- a
        second, independent signal for the same failure class."""
        for seed in REGRESSION_SEEDS:
            config = CampaignConfig(seed=seed)
            for floor in (2, 5, 8):
                level = _generate_with_retries(config, floor)
                components = _floor_components(level.tiles)
                total = sum(len(c) for c in components) or 1
                biggest = max(len(c) for c in components)
                self.assertLess(
                    biggest / total, 0.5,
                    f"seed={seed!r} floor={floor}: one component is "
                    f"{biggest}/{total} of all floor tiles")


if __name__ == "__main__":
    unittest.main()
