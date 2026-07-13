"""Regression tests for ECWolf's marker-driven patrol movement."""
import unittest

from infiniwolf.config import CampaignConfig
from infiniwolf.generator import (DOORS, PATROL_GUARDS, PATROL_OFFICERS,
                                   PATROL_POINT_DIRECTIONS, PATROL_SS, PATROL_DOGS,
                                   _at, _is_floor, _patrol_actor_direction,
                                   generate_map, validate_patrols)


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
        for seed in ("alpha", "bravo"):
            for floor in (2, 8):
                level = generate_map(CampaignConfig(seed=seed), floor)
                validate_patrols(level)
                for index, thing in enumerate(level.things):
                    x, y = index % 64, index // 64
                    if _patrol_actor_direction(thing) is not None:
                        patrols += 1
                    if thing in PATROL_POINT_DIRECTIONS:
                        markers += 1
                        self.assertTrue(_is_floor(_at(level.tiles, x, y)))
                        self.assertNotIn(_at(level.tiles, x, y), DOORS)
        self.assertGreater(patrols, 0)
        # Each current route is a rectangle with its four corners as markers.
        self.assertEqual(markers, patrols * 4)


if __name__ == "__main__":
    unittest.main()
