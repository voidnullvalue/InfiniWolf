"""Focused geometry refinements for decoration placement."""
import random
import unittest

from infiniwolf.campaign import resolve_schedule
from infiniwolf.config import CampaignConfig
from infiniwolf.decorations import (
    _PillarPolicy,
    _blocking_budget,
    _bypass_preserved,
    _open_budget,
    _phase_item_weight,
    _phase_motif_overrides,
    _place_zoned,
    _wall_orientation,
)
from infiniwolf.generator import generate_map
from infiniwolf.model import AestheticPhase, Room
from infiniwolf.grid import _at, _is_floor, _set
from infiniwolf.wl6 import ENGINE_SOLID, FLOOR, GRID, WALL


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


def _plan(rows: tuple[str, ...]) -> tuple[list[int], list[int]]:
    """Build a tile/thing plane from ASCII: '#' wall, '.' floor, 'o' solid prop."""
    tiles = [WALL] * (GRID * GRID)
    things = [0] * (GRID * GRID)
    for y, row in enumerate(rows):
        for x, char in enumerate(row):
            if char != "#":
                _set(tiles, x, y, FLOOR)
            if char == "o":
                _set(things, x, y, 30)
    return tiles, things


class PassageBypassTests(unittest.TestCase):
    """A prop may cost a step around it, never a walk around the floor.

    The reachability guard on each commit only asks whether every cell is still
    reachable, which a plugged corridor with a loop back around satisfies. Seed
    1785355280054893495 floor 9 shipped a floor lamp on the junction cell where
    a one-wide north corridor met a one-wide west corridor: both mouths stayed
    reachable the long way round and the step between them became 48 tiles.
    """

    def test_room_corner_and_wall_keep_their_bypass(self):
        tiles, things = _plan(("#####",
                               "#...#",
                               "#...#",
                               "#...#",
                               "#####"))
        for cell in ((1, 1), (2, 1), (1, 2)):
            with self.subTest(cell=cell):
                self.assertTrue(_bypass_preserved(tiles, things, [cell]))

    def test_corridor_cell_and_bend_have_none(self):
        straight = _plan(("#####",
                          "#...#",
                          "#####"))
        self.assertFalse(_bypass_preserved(*straight, [(2, 1)]))
        # The floor-9 shape: two one-wide corridors meeting at a corner. The
        # cell reads as a "corner" to _cell_geometry, which is why the corpus
        # corner rule let a lamp onto it.
        bend = _plan(("##.##",
                      "##.##",
                      "...##"))
        self.assertFalse(_bypass_preserved(*bend, [(2, 2)]))

    def test_diagonal_contact_between_two_props_is_not_a_bypass(self):
        # A prop placed diagonally opposite an existing one leaves the two sides
        # touching at a corner only, and the engine will not walk a player
        # through the gap between two solid props. Committing (2, 2) here cuts
        # (3, 2) off from (2, 1) except across that corner.
        tiles, things = _plan(("######",
                               "#..o.#",
                               "#....#",
                               "######"))
        self.assertFalse(_bypass_preserved(tiles, things, [(2, 2)]))

    def test_group_leaving_one_lane_of_a_three_wide_hallway_is_allowed(self):
        # The matched pair bisecting door-to-door travel is a real composition
        # and must survive: two of the three lanes taken, one left open.
        tiles, things = _plan(("##########",
                               "#........#",
                               "#........#",
                               "#........#",
                               "##########"))
        self.assertTrue(_bypass_preserved(tiles, things, [(5, 1), (5, 2)]))


class GeneratedPassageTests(unittest.TestCase):
    SEED = "1785355280054893495"
    FLOOR, ATTEMPT = 9, 30      # the campaign's own winner for this floor

    @classmethod
    def setUpClass(cls):
        config = CampaignConfig.with_seed(cls.SEED)
        options = resolve_schedule(config).floor_options(cls.FLOOR)
        cls.level = generate_map(config, cls.FLOOR, cls.ATTEMPT, **options)

    def test_the_reported_lamp_no_longer_plugs_its_hallway(self):
        self.assertEqual(_at(self.level.things, 36, 35), 0)

    def test_no_solid_prop_plugs_a_passage(self):
        tiles, things = self.level.tiles, self.level.things
        for y in range(GRID):
            for x in range(GRID):
                if (_at(things, x, y) not in ENGINE_SOLID
                        or not _is_floor(_at(tiles, x, y))):
                    continue
                with self.subTest(cell=(x, y), item=_at(things, x, y)):
                    self.assertTrue(
                        _bypass_preserved(tiles, things, [(x, y)]),
                        f"prop {_at(things, x, y)} at ({x},{y}) leaves its "
                        f"neighbours no local route around it")

    def test_the_floor_is_still_furnished(self):
        """The guard rejects plugs, not decoration: a floor keeps its props."""
        solid = sum(1 for thing in self.level.things if thing in ENGINE_SOLID)
        self.assertGreater(solid, 40)


if __name__ == "__main__":
    unittest.main()
