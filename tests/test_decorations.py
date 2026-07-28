"""Focused geometry refinements for decoration placement."""
import unittest

from infiniwolf.decorations import _phase_item_weight, _phase_motif_overrides, _wall_orientation
from infiniwolf.model import AestheticPhase
from infiniwolf.grid import _set
from infiniwolf.wl6 import FLOOR, GRID, WALL


class WallOrientationTests(unittest.TestCase):
    def test_skinny_hall_end_wall_is_terminus_not_long_side(self):
        tiles = [WALL] * (GRID * GRID)
        for y in range(10, 25):
            for x in range(30, 33):
                _set(tiles, x, y, FLOOR)
        self.assertEqual(_wall_orientation(tiles, (31, 10)), "terminus")
        self.assertEqual(_wall_orientation(tiles, (30, 17)), "flank")


class AestheticPhasePolicyTests(unittest.TestCase):
    def test_all_non_damage_fields_have_a_narrow_named_direction(self):
        low = AestheticPhase(orderliness=0.75, damage=1.0, occupation=0.75,
                             monumentality=0.75, abandonment=0.75)
        high = AestheticPhase(orderliness=1.30, damage=1.0, occupation=1.30,
                              monumentality=1.30, abandonment=1.30)
        low_motifs = _phase_motif_overrides(low)
        high_motifs = _phase_motif_overrides(high)
        self.assertGreater(high_motifs["travel-pair"], low_motifs["travel-pair"])
        self.assertGreater(high_motifs["landmark-frame"], low_motifs["landmark-frame"])
        self.assertGreater(high_motifs["colonnade"], low_motifs["colonnade"])
        occupied = AestheticPhase(1.0, 1.0, 1.30, 1.0, 0.75)
        abandoned = AestheticPhase(1.0, 1.0, 0.75, 1.0, 1.30)
        self.assertGreater(_phase_item_weight(46, occupied),
                           _phase_item_weight(46, abandoned))
        self.assertGreater(_phase_item_weight(61, abandoned),
                           _phase_item_weight(61, occupied))


if __name__ == "__main__":
    unittest.main()
