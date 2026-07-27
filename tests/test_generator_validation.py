"""Hard-rule validation coverage."""
import unittest

from infiniwolf.generator_validation import validate_map
from infiniwolf.grid import _set
from infiniwolf.model import GeneratedMap
from infiniwolf.wl6 import DOOR_EW, DOOR_NS, FLOOR, GRID, WALL


class DoorJunctionValidationTests(unittest.TestCase):
    def test_rejects_perpendicular_door_junction(self):
        tiles = [WALL] * (GRID * GRID)
        _set(tiles, 20, 20, FLOOR)
        _set(tiles, 19, 20, DOOR_EW)
        _set(tiles, 20, 19, DOOR_NS)
        level = GeneratedMap(3, tiles, [0] * (GRID * GRID), (20, 20),
                             (20, 20), [], 1)
        with self.assertRaisesRegex(ValueError, "perpendicular door junction"):
            validate_map(level)


if __name__ == "__main__":
    unittest.main()
