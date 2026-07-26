"""Grid primitives: reachability semantics and the router lookup table.

Two things worth pinning here. `_reachable`'s treatment of a blocked start cell
is surprising and easy to "fix" wrongly. And `_FLOOR_OR_DOOR` is a precomputed
copy of `_is_floor(code) or code in DOORS` that the corridor router indexes in
its inner loop; duplicated data is only safe when divergence is caught
mechanically, so the table is compared against the predicates it replaces for
every possible code.
"""
import unittest

from infiniwolf.grid import _FLOOR_OR_DOOR, _is_floor, _reachable, _set
from infiniwolf.wl6 import DOORS, FLOOR, GRID, WALL


class GridPrimitiveTests(unittest.TestCase):
    def test_router_lookup_table_matches_the_predicates_it_replaces(self):
        """_FLOOR_OR_DOOR must agree with _is_floor/DOORS for every code.

        The corridor router indexes this table instead of calling both
        predicates on all four neighbours of every cell it examines. A new door
        code, or a widened floor-zone range, that landed in one and not the
        other would silently let routes fuse rooms together with no wall or door
        at the seam -- a failure mode this project has already had once.
        """
        self.assertEqual(len(_FLOOR_OR_DOOR), 256)
        for code in range(256):
            with self.subTest(code=code):
                self.assertEqual(bool(_FLOOR_OR_DOOR[code]),
                                 _is_floor(code) or code in DOORS)

    def test_router_neighbour_indexing_stays_in_bounds(self):
        """The router drops _at's bounds check, so the clamp must guarantee it.

        find_route only examines cells with 2 <= x, y < GRID - 2 and then reads
        their four neighbours by direct index. That is safe precisely because the
        clamp leaves a cell of margin. If it were ever loosened to 1, neighbour
        reads would wrap to the far edge of the plane instead of raising.
        """
        for coordinate in (2, GRID - 3):
            for offset in (-1, 1):
                neighbour = coordinate + offset
                self.assertGreaterEqual(neighbour, 0)
                self.assertLess(neighbour, GRID)
        for x, y in ((2, 2), (GRID - 3, GRID - 3)):
            base = y * GRID + x
            for delta in (-GRID, GRID, -1, 1):
                self.assertGreaterEqual(base + delta, 0)
                self.assertLess(base + delta, GRID * GRID)

    def test_blocking_the_start_cell_does_not_stop_the_flood(self):
        """`blocked` filters neighbours, not the seed.

        _reachable puts `start` into `seen` and then expands from it regardless,
        so naming start in `blocked` excludes nothing and the whole room is still
        reached. Worth an explicit test: the natural reading is that a blocked
        start yields a single cell, and anyone optimizing this function will
        reach for that assumption.
        """
        tiles = [WALL] * (GRID * GRID)
        for y in range(10, 14):
            for x in range(10, 14):
                _set(tiles, x, y, FLOOR)
        start = (11, 11)
        self.assertEqual(
            len(_reachable(tiles, start, locked_open=True, blocked={start})), 16,
            "blocking the seed should not shrink the flood")

    def test_a_wall_of_props_across_a_corridor_is_rejected(self):
        """The property the decoration reachability gate exists for.

        Blocking cells may cost at most themselves. A prop sealing a corridor
        strands everything past it and the count falls below baseline minus the
        candidate size; a prop at a dead end costs only its own cell. This is
        also why the gate cannot be answered early: on the success path the count
        only reaches its threshold once the flood is finished.
        """
        tiles = [WALL] * (GRID * GRID)
        for x in range(10, 30):
            _set(tiles, x, 12, FLOOR)
        start = (10, 12)
        baseline = len(_reachable(tiles, start, locked_open=True))
        self.assertEqual(baseline, 20)

        plug = {(20, 12)}
        self.assertLess(
            len(_reachable(tiles, start, locked_open=True, blocked=plug)),
            baseline - len(plug),
            "sealing the corridor should strand the far half")

        tip = {(29, 12)}
        self.assertEqual(
            len(_reachable(tiles, start, locked_open=True, blocked=tip)),
            baseline - len(tip),
            "a prop at the dead end should cost only its own cell")


if __name__ == "__main__":
    unittest.main()
