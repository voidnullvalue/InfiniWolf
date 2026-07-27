"""Landmark hierarchy: one dominant space, a few supporting ones, never adjacent.

Selection runs before decoration and consumes no randomness, which is what these
tests mostly exist to protect. If it ever reads props, a landmark could appear or
vanish between two candidates that are geometrically identical; if it ever draws
from the RNG, adding a decoration roll upstream could silently move the hierarchy.
"""
import unittest

from infiniwolf.model import Room, RoomSpec
from infiniwolf.semantics import _MAX_SECONDARY, plan_landmarks


def spec(role="beat", tier="standard", district=0):
    return RoomSpec(role, tier, district)


def scenario(count=10, anchor_at=4):
    """A chain floor: rooms 0..count-1 in a line, one anchor-tier room."""
    rooms = [Room(index * 8, 0, 6, 6) for index in range(count)]
    rooms[anchor_at] = Room(anchor_at * 8, 0, 12, 12)
    specs = [spec(tier="anchor" if index == anchor_at else "standard")
             for index in range(count)]
    roles = ["start"] + ["beat"] * (count - 2) + ["exit"]
    edges = [(index, index + 1) for index in range(count - 1)]
    districts = [0 if index < count // 2 else 1 for index in range(count)]
    critical = list(range(count))
    return rooms, specs, roles, edges, districts, critical


class LandmarkPlanningTests(unittest.TestCase):
    def test_exactly_one_primary(self):
        plans = plan_landmarks(*scenario())
        self.assertEqual(sum(p.rank == "primary" for p in plans), 1)

    def test_the_anchor_room_wins(self):
        """Anchor tier plus the largest area should dominate the score."""
        plans = plan_landmarks(*scenario(anchor_at=4))
        primary = next(p for p in plans if p.rank == "primary")
        self.assertEqual(primary.room_index, 4)
        secondaries = [p.score for p in plans if p.rank == "secondary"]
        self.assertTrue(all(primary.score > s for s in secondaries),
                        "the primary must actually dominate, not merely sort first")

    def test_landmarks_are_never_graph_adjacent(self):
        """Adjacent landmarks compete instead of composing.

        Two emphatic rooms either side of one door read as a single confusing
        space, so spacing is a hard constraint rather than a scoring term.
        """
        rooms, specs, roles, edges, districts, critical = scenario(12, anchor_at=5)
        plans = plan_landmarks(rooms, specs, roles, edges, districts, critical)
        neighbours = {index: set() for index in range(len(rooms))}
        for first, second in edges:
            neighbours[first].add(second)
            neighbours[second].add(first)
        chosen = [p.room_index for p in plans]
        for index in chosen:
            for other in chosen:
                if index != other:
                    self.assertNotIn(other, neighbours[index],
                                     f"rooms {index} and {other} are adjacent")

    def test_secondary_count_is_capped(self):
        plans = plan_landmarks(*scenario(24, anchor_at=10))
        self.assertLessEqual(sum(p.rank == "secondary" for p in plans),
                             _MAX_SECONDARY)

    def test_utility_and_terminal_rooms_are_never_landmarks(self):
        """A closet is not a landmark however it measures, and the start and exit
        are navigated *to*, not *by*."""
        rooms, specs, roles, edges, districts, critical = scenario(10, anchor_at=4)
        specs[6] = spec(tier="closet")
        specs[7] = spec(tier="corridor")
        rooms[6] = Room(48, 0, 14, 14)          # deliberately the largest room
        rooms[7] = Room(64, 0, 13, 13)
        plans = plan_landmarks(rooms, specs, roles, edges, districts, critical)
        chosen = {p.room_index for p in plans}
        self.assertNotIn(6, chosen)
        self.assertNotIn(7, chosen)
        self.assertNotIn(0, chosen, "start room")
        self.assertNotIn(len(rooms) - 1, chosen, "exit room")

    def test_selection_is_deterministic_and_rng_free(self):
        args = scenario()
        first = plan_landmarks(*args)
        second = plan_landmarks(*args)
        self.assertEqual([(p.room_index, p.rank, p.score) for p in first],
                         [(p.room_index, p.rank, p.score) for p in second])

    def test_ties_resolve_by_index_not_by_iteration_order(self):
        """Identical rooms must still yield a stable hierarchy."""
        count = 9
        rooms = [Room(index * 8, 0, 7, 7) for index in range(count)]
        specs = [spec() for _ in range(count)]
        roles = ["start"] + ["beat"] * (count - 2) + ["exit"]
        edges = [(index, index + 1) for index in range(count - 1)]
        districts = [0] * count
        plans = plan_landmarks(rooms, specs, roles, edges, districts,
                              list(range(count)))
        primary = next(p for p in plans if p.rank == "primary")
        self.assertEqual(primary.room_index, min(
            p.room_index for p in plans if p.score == primary.score))

    def test_empty_floor_yields_no_landmarks(self):
        self.assertEqual(plan_landmarks([], [], [], [], [], []), ())

    def test_approach_room_is_a_real_neighbour(self):
        rooms, specs, roles, edges, districts, critical = scenario()
        plans = plan_landmarks(rooms, specs, roles, edges, districts, critical)
        neighbours = {index: set() for index in range(len(rooms))}
        for first, second in edges:
            neighbours[first].add(second)
            neighbours[second].add(first)
        for plan in plans:
            if plan.approach_room >= 0:
                self.assertIn(plan.approach_room, neighbours[plan.room_index])


if __name__ == "__main__":
    unittest.main()


class BossArenaFamilyTests(unittest.TestCase):
    """Every authored boss-arena family must be reachable.

    Two of the five were dead: their wall displays sat at offsets measured from
    the arena centre, an arena is 14-17 tiles across, so those cells were always
    interior floor. validate_map requires a flag on the perimeter with rock
    behind, so the decoration was placed and then the whole floor was rejected --
    every time, for command-bunker and columned-fortress. Planning still chose
    them about 40% of the time between them, and the retry loop silently re-rolled
    until it landed on a family that worked, which also skewed the boss pick.
    """

    def test_wall_displays_snap_to_a_backed_perimeter_cell(self):
        from infiniwolf.model import Room
        from infiniwolf.special_floors import _perimeter_anchor, _wall_backed
        from infiniwolf.wl6 import FLOOR, GRID, WALL

        room = Room(20, 20, 15, 15)
        tiles = [WALL] * (GRID * GRID)
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR
        cx, cy = room.center

        # The offsets the arena families actually use.
        for dx, dy in ((-5, 4), (5, -4), (0, -5), (0, 5), (-5, -5), (5, -5)):
            with self.subTest(offset=(dx, dy)):
                anchored = _perimeter_anchor(room, cx + dx, cy + dy)
                on_edge = (anchored[0] in (room.x, room.x + room.w - 1)
                           or anchored[1] in (room.y, room.y + room.h - 1))
                self.assertTrue(on_edge, f"{anchored} is not on the perimeter")
                self.assertTrue(_wall_backed(tiles, room, anchored),
                                f"{anchored} has no rock behind it")

    def test_an_interior_cell_is_not_considered_wall_backed(self):
        from infiniwolf.model import Room
        from infiniwolf.special_floors import _wall_backed
        from infiniwolf.wl6 import FLOOR, GRID, WALL
        room = Room(20, 20, 15, 15)
        tiles = [WALL] * (GRID * GRID)
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR
        self.assertFalse(_wall_backed(tiles, room, room.center))


class BossGatingTests(unittest.TestCase):
    """Floor 9's exit is boss-gated by one of two mechanisms, never neither.

    Hans and Gretel drop a gold key natively, so their elevator stays locked and
    the kill is mandatory. The other four native bosses drop nothing, so locking
    the elevator would strand the player -- for them the arena is a cut vertex and
    the gate is positional. Accepting either is what lets all six bosses appear;
    requiring the gold objective restricted floor 9 to two actors.
    """

    def test_the_roster_is_the_curated_native_list(self):
        from infiniwolf.campaign import BOSS_ROSTER
        from infiniwolf.wl6 import BOSSES, FAKE_HITLER
        self.assertEqual(tuple(BOSS_ROSTER), tuple(BOSSES))
        self.assertEqual(len(BOSS_ROSTER), 6)
        # FakeHitler neither drops a key nor calls A_BossDeath, so it is a
        # novelty actor rather than a boss; Spear of Destiny bosses are absent
        # because their sprites live in SOD's VSWAP, not wl6's.
        self.assertNotIn(FAKE_HITLER, BOSS_ROSTER)

    def test_boss_choice_is_attempt_independent(self):
        """A rejected floor must not re-roll the boss.

        It used to: the boss was drawn from the floor rng, and because two arena
        families could never validate, the retries skewed the result 2:1 toward
        one boss. Deriving it from a campaign-scale stream removes the coupling.
        """
        from infiniwolf.campaign import choose_boss, resolve_schedule
        from infiniwolf.config import CampaignConfig
        for seed in (1, 7, 99):
            config = CampaignConfig(seed=seed)
            self.assertEqual(choose_boss(config), choose_boss(config))
            self.assertEqual(resolve_schedule(config).boss, choose_boss(config))

    def test_every_boss_is_reachable_across_seeds(self):
        from collections import Counter
        from infiniwolf.campaign import BOSS_ROSTER, choose_boss
        from infiniwolf.config import CampaignConfig
        seen = Counter(choose_boss(CampaignConfig(seed=seed))
                       for seed in range(300))
        for boss in BOSS_ROSTER:
            with self.subTest(boss=boss):
                self.assertGreater(
                    seen[boss], 0,
                    f"boss {boss} never selected in 300 seeds")

    def test_the_lock_follows_the_drop(self):
        """Gold is scheduled only when a boss can actually provide it.

        Locking the elevator for a boss that drops nothing strands the player,
        since floor 9 has no other gold source.
        """
        from infiniwolf.campaign import _lock_schedule, choose_boss
        from infiniwolf.config import CampaignConfig, Intensity
        from infiniwolf.wl6 import KEY_DROP_BOSSES
        for seed in range(60):
            config = CampaignConfig(seed=seed, locked_doors=Intensity.VERY_HIGH)
            gate = _lock_schedule(config)[8]
            drops = choose_boss(config) in KEY_DROP_BOSSES
            with self.subTest(seed=seed):
                self.assertEqual("gold" in gate.colors, drops,
                                 f"gate {gate.colors} vs drop={drops}")
