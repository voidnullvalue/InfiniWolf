"""Regression tests for ECWolf's marker-driven patrol movement."""
import unittest

from infiniwolf.config import CampaignConfig
from infiniwolf.generator import (DOORS, PATROL_GUARDS, PATROL_OFFICERS,
                                   PATROL_POINT_DIRECTIONS, PATROL_SS, PATROL_DOGS,
                                   _at, _is_floor, generate_map, validate_patrols)


def _generate_with_retries(config: CampaignConfig, floor: int, attempts: int = 50):
    """Mirror campaign generation: an individual candidate may be rejected
    before patrol validation for an unrelated topology or progression rule."""
    last_error = None
    for attempt in range(attempts):
        try:
            return generate_map(config, floor, attempt)
        except ValueError as error:
            last_error = error
    raise AssertionError(
        f"floor {floor} never validated in {attempts} attempts: {last_error}")


class PatrolTests(unittest.TestCase):
    def test_patrol_tuples_keep_ecwolfs_cardinal_old_num_order(self):
        """The family tuples are N/E/S/W, while old-number offsets are E/N/W/S."""
        for patrol, base in ((PATROL_GUARDS, 112), (PATROL_OFFICERS, 120),
                             (PATROL_SS, 130), (PATROL_DOGS, 138)):
            self.assertEqual(patrol, (base + 1, base, base + 3, base + 2))
        self.assertEqual(PATROL_POINT_DIRECTIONS, {92: 0, 90: 1, 96: 2, 94: 3})

    def test_patrols_have_safe_marker_chains_on_regression_seeds(self):
        """Exercise the exact TryWalk-only-current-direction oracle end to end."""
        patrols = 0
        markers = 0
        expected_markers = 0
        for seed in ("alpha", "bravo"):
            for floor in (2, 8):
                level = _generate_with_retries(CampaignConfig(seed=seed), floor)
                validate_patrols(level)
                routes = [encounter for encounter in level.encounters
                          if encounter.patrol_kind]
                patrols += len(routes)
                for route in routes:
                    self.assertEqual(len(route.cells), 1)
                    turn_count = (2 if route.patrol_kind in
                                  ("hall-shuttle", "doorway-shuttle") else 4)
                    route_markers = [cell for cell in route.patrol_path
                                     if _at(level.things, *cell)
                                     in PATROL_POINT_DIRECTIONS]
                    self.assertEqual(len(route_markers), turn_count)
                    expected_markers += turn_count
                for index, thing in enumerate(level.things):
                    x, y = index % 64, index // 64
                    if thing in PATROL_POINT_DIRECTIONS:
                        markers += 1
                        self.assertTrue(_is_floor(_at(level.tiles, x, y)))
                        self.assertNotIn(_at(level.tiles, x, y), DOORS)
        self.assertGreater(patrols, 0)
        self.assertEqual(markers, expected_markers)


if __name__ == "__main__":
    unittest.main()
