"""Shared inaccessible voids: visible from several rooms, entered from none.

The mechanism is the one the guard gallery and the exterior vista already use --
floor cells fronted by a complete line of blocking pillars. What makes a void
*shared* is the requirement that at least two distinct rooms overlook it: a space
one room can see is an alcove, and the point of a void is spatial recognition,
telling the player two rooms have a relationship in space and not just on a graph.
"""
import random
import unittest

from infiniwolf.geometry import _VOID_DRESSING, _VOID_FAMILIES, carve_shared_void
from infiniwolf.grid import _at, _is_floor, _reachable
from infiniwolf.ledger import Ledger
from infiniwolf.model import Room
from infiniwolf.wl6 import FLOOR, GRID, WALL


def two_rooms_around_a_pocket():
    """Two rooms either side of a 3x3 rock pocket, connected by a corridor.

    The corridor matters: without it the rooms are separate components and the
    containment check would pass for the wrong reason.
    """
    tiles = [WALL] * (GRID * GRID)
    rooms = [Room(10, 20, 6, 9), Room(22, 20, 6, 9)]
    for room in rooms:
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR
    # Pocket occupies x 17..19, y 23..25; rooms sit one wall away either side.
    for x in range(16, 22):
        tiles[32 * GRID + x] = FLOOR          # corridor below, linking the rooms
    for y in range(28, 33):
        tiles[y * GRID + 16] = FLOOR
        tiles[y * GRID + 21] = FLOOR
    return tiles, rooms


class SharedVoidTests(unittest.TestCase):
    def test_families_all_have_dressing(self):
        for family in _VOID_FAMILIES:
            with self.subTest(family=family):
                self.assertIn(family, _VOID_DRESSING)
                self.assertTrue(_VOID_DRESSING[family])

    def test_a_void_is_sealed_by_its_screens(self):
        tiles, rooms = two_rooms_around_a_pocket()
        things = [0] * (GRID * GRID)
        void = carve_shared_void(tiles, things, rooms, Ledger(),
                                 random.Random(4), rooms[0].center)
        self.assertIsNotNone(void, "no void found in a map built to contain one")
        self.assertGreaterEqual(len(void.viewing_rooms), 2)
        sealed = _reachable(tiles, rooms[0].center, locked_open=True,
                            blocked=set(void.screens))
        for cell in void.interior:
            with self.subTest(cell=cell):
                self.assertTrue(_is_floor(_at(tiles, *cell)))
                self.assertNotIn(cell, sealed)

    def test_every_screen_cell_carries_a_pillar(self):
        """A gap in the screen is a way in, so the line must be complete."""
        tiles, rooms = two_rooms_around_a_pocket()
        things = [0] * (GRID * GRID)
        void = carve_shared_void(tiles, things, rooms, Ledger(),
                                 random.Random(4), rooms[0].center)
        self.assertIsNotNone(void)
        for cell in void.screens:
            self.assertEqual(_at(things, *cell), 30, f"{cell} has no pillar")

    def test_a_failed_carve_leaves_the_plane_untouched(self):
        """Rollback matters: a half-carved pocket is a hole in the map.

        Given a map with no room at all, no pocket can qualify, and nothing may
        have been written on the way to finding that out.
        """
        tiles = [WALL] * (GRID * GRID)
        things = [0] * (GRID * GRID)
        before = list(tiles)
        void = carve_shared_void(tiles, things, [], Ledger(), random.Random(1),
                                 (5, 5))
        self.assertIsNone(void)
        self.assertEqual(tiles, before)
        self.assertEqual(things, [0] * (GRID * GRID))

    def test_one_room_alone_does_not_earn_a_void(self):
        tiles = [WALL] * (GRID * GRID)
        room = Room(10, 10, 8, 8)
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR
        things = [0] * (GRID * GRID)
        before = list(tiles)
        void = carve_shared_void(tiles, things, [room], Ledger(),
                                 random.Random(2), room.center)
        self.assertIsNone(void, "a void needs two overlooking rooms, not one")
        self.assertEqual(tiles, before)

    def test_the_campaign_schedules_at_most_one_void(self):
        from infiniwolf.campaign import resolve_schedule
        from infiniwolf.config import CampaignConfig
        for seed in range(30):
            schedule = resolve_schedule(CampaignConfig(seed=seed))
            with self.subTest(seed=seed):
                self.assertIn(schedule.void_floor, (0, *range(2, 9)))
                enabled = [n for n in range(1, 11)
                           if schedule.floor_options(n)["shared_void_enabled"]]
                self.assertLessEqual(len(enabled), 1)


if __name__ == "__main__":
    unittest.main()
