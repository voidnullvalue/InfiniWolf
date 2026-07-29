"""Focused geometry refinements for decoration placement."""
import random
import unittest

from infiniwolf.decorations import (
    _PillarPolicy,
    _blocking_budget,
    _open_budget,
    _phase_item_weight,
    _phase_motif_overrides,
    _place_zoned,
    _wall_orientation,
)
from infiniwolf.model import AestheticPhase, Room
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


class AreaScaledBudgetTests(unittest.TestCase):
    def test_budgets_keep_old_small_room_steps_and_continue_with_area(self):
        self.assertEqual([_blocking_budget(area) for area in (63, 64, 159, 160, 400)],
                         [1, 2, 2, 3, 5])
        self.assertEqual([_open_budget(area) for area in (44, 45, 79, 80, 144, 400)],
                         [1, 2, 2, 3, 4, 8])

    def test_large_zoned_room_can_use_more_than_two_corner_compositions(self):
        room = Room(10, 10, 20, 20)
        things = [0] * (GRID * GRID)
        free = {(x, y)
                for y in range(room.y + 1, room.y + room.h - 1)
                for x in range(room.x + 1, room.x + room.w - 1)}

        def place(cells, item):
            for cell in cells:
                things[cell[1] * GRID + cell[0]] = item
                free.discard(cell)
            return True

        zones = (((31,), ()), ((34,), ()))
        _place_zoned(room, zones, free, set(), set(), things,
                     random.Random(0), place,
                     _blocking_budget(room.w * room.h))
        self.assertEqual(sum(thing in (31, 34) for thing in things), 4)

    def test_pillar_policy_scales_rooms_but_caps_map_density(self):
        rooms = [Room(2, 2, 8, 8), Room(20, 2, 10, 8)]
        tiles = [FLOOR] * 1000 + [WALL] * (GRID * GRID - 1000)
        policy = _PillarPolicy(tiles, [0] * (GRID * GRID), rooms)
        self.assertLessEqual(policy.map_cap / 1000, 0.012)
        pair = [((21, 3), 30), ((28, 3), 30)]
        second_pair = [((21, 8), 30), ((28, 8), 30)]
        self.assertTrue(policy.permits(1, "guardpost", pair, "large-hall-pair"))
        policy.record(1, pair, "large-hall-pair")
        self.assertTrue(policy.permits(1, "guardpost", second_pair,
                                       "large-hall-pair"))


if __name__ == "__main__":
    unittest.main()
