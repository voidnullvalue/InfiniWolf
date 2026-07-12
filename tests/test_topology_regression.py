"""Regression guards for authored-feeling composition, not just validity.

validate_map already guarantees a map is playable; these tests guard
properties that make a *valid* map still look procedural or broken in a way
validate_map doesn't check -- e.g. two rooms silently fused into one long
open sightline by a corridor-routing fallback. Each test names the concrete
failure mode it exists to catch.
"""
from collections import deque
import unittest

from randomwolf.config import CampaignConfig
from randomwolf.generator import DOORS, GRID, _at, _is_floor, generate_map


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
