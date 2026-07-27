"""Focused geometry refinements for decoration placement."""
import unittest

from infiniwolf.decorations import _wall_orientation
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


if __name__ == "__main__":
    unittest.main()
