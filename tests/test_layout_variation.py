import random
import unittest

import infiniwolf.generator as generator
from infiniwolf.config import CampaignConfig


class LayoutVariationTests(unittest.TestCase):
    def test_progression_grammars_are_seeded_and_do_not_repeat_adjacent(self):
        sequence = generator._progression_sequence(CampaignConfig(seed=42))
        self.assertEqual(sequence, generator._progression_sequence(CampaignConfig(seed=42)))
        self.assertEqual(len(sequence), 10)
        self.assertGreaterEqual(len(set(sequence)), 4)
        self.assertFalse(any(a == b for a, b in zip(sequence, sequence[1:])))

    def test_circulation_skeletons_change_spine_geometry_not_only_metadata(self):
        signatures = set()
        for skeleton in generator.CIRCULATION_SKELETONS:
            rng = random.Random(100)
            plan = generator._plan_floor(
                rng, 3, 5, skeleton=skeleton,
                progression_grammar="hub-relay")
            placed = generator._place_planned_rooms(rng, plan, 5)
            spine_count = next(
                index for index, spec in enumerate(plan.specs)
                if spec.role == "exit") + 1
            signatures.add(tuple(
                (room.x, room.y, room.w, room.h)
                for room in placed.rooms[:spine_count]))

        self.assertGreaterEqual(len(signatures), 4)

    def test_rare_motif_schedule_is_three_percent_and_late_only(self):
        schedules = [generator._rare_motif_schedule(CampaignConfig(seed=seed))
                     for seed in range(5000)]
        selected = [floor for floor in schedules if floor]
        self.assertTrue(all(floor in (6, 7, 8, 9) for floor in selected))
        self.assertGreater(len(selected) / len(schedules), 0.02)
        self.assertLess(len(selected) / len(schedules), 0.04)

    def test_swastika_profile_is_connected_bounded_and_four_armed(self):
        room = generator.Room(20, 20, 15, 15)
        tiles = [generator.WALL] * (generator.GRID * generator.GRID)
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                generator._set(tiles, x, y, generator.FLOOR)
        result = generator._carve_swastika_profile(tiles, room, random.Random(5))
        self.assertIsNotNone(result)
        _, endpoints = result
        open_cells = {(x, y) for y in range(room.y, room.y + room.h)
                      for x in range(room.x, room.x + room.w)
                      if generator._is_floor(generator._at(tiles, x, y))}
        reachable = generator._reachable(tiles, room.center, locked_open=True)
        self.assertEqual(len(endpoints), 4)
        self.assertTrue(set(endpoints) <= open_cells)
        self.assertTrue(open_cells <= reachable)

    def test_boss_families_own_distinct_geometry_and_decoration(self):
        families = ("throne-stronghold", "command-bunker",
                    "laboratory-gauntlet", "columned-fortress", "central-duel")
        profiles = set()
        for family in families:
            tiles = [generator.WALL] * (generator.GRID * generator.GRID)
            things = [0] * (generator.GRID * generator.GRID)
            room = generator.Room(20, 20, 17, 17)
            for y in range(room.y, room.y + room.h):
                for x in range(room.x, room.x + room.w):
                    generator._set(tiles, x, y, generator.FLOOR)
            detail = generator._prepare_boss_arena(
                tiles, things, room, set(), random.Random(9), family)
            profiles.add(detail.profile)
            self.assertGreaterEqual(len(detail.geometry), 2)
            self.assertGreaterEqual(len(detail.decorations), 3)
        self.assertEqual(len(profiles), len(families))

    def test_scheduled_rare_motif_remains_optional_and_keeps_keys_out(self):
        last_error = None
        for attempt in range(50):
            try:
                level = generator.generate_map(
                    CampaignConfig(seed=42), 7, attempt,
                    rare_motif_enabled=True)
                break
            except ValueError as error:
                last_error = error
        else:
            self.fail(f"scheduled rare motif never validated: {last_error}")
        self.assertIsNotNone(level.rare_motif)
        room_index = level.rare_motif.room_index
        self.assertNotIn(room_index, level.critical_route)
        self.assertNotIn(room_index,
                         {objective.host_room for objective in level.key_objectives})


if __name__ == "__main__":
    unittest.main()
