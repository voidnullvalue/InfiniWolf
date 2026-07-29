"""Finished-plane player-experience simulation."""

from dataclasses import fields
import unittest

from infiniwolf.model import GeneratedMap, Room
from infiniwolf.quality import quality_report
from infiniwolf.simulation import (ProfileMetrics, UNSUPPORTED_MEASUREMENTS,
                                   simulate_player_experience)
from infiniwolf.wl6 import AMMO, DOOR_EW, DOOR_NS, FLOOR, GRID, GUARDS, WALL


def _level(*, branch_enemy: bool = True, corridor_enemies: int = 1,
           ammo_in_branch: bool = True) -> GeneratedMap:
    """An L-shaped route with one optional room and real sound boundaries."""
    tiles = [WALL] * (GRID * GRID)
    things = [0] * (GRID * GRID)
    rooms = (
        Room(2, 2, 5, 5),
        Room(10, 2, 5, 5),
        Room(10, 10, 5, 5),
        Room(18, 2, 5, 5),
    )
    for zone, room in enumerate(rooms):
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR + zone

    # Mandatory 0 -> 1 -> 2 turns south; room 3 branches east from room 1.
    tiles[4 * GRID + 7] = DOOR_EW
    for x in (8, 9):
        tiles[4 * GRID + x] = FLOOR + 1
    tiles[7 * GRID + 12] = DOOR_NS
    for y in (8, 9):
        tiles[y * GRID + 12] = FLOOR + 2
    tiles[4 * GRID + 15] = DOOR_EW
    for x in (16, 17):
        tiles[4 * GRID + x] = FLOOR + 3

    for offset in range(corridor_enemies):
        things[(3 + offset) * GRID + 12] = GUARDS[0]
    if branch_enemy:
        things[4 * GRID + 20] = GUARDS[0]
    if ammo_in_branch:
        things[5 * GRID + 20] = AMMO
    return GeneratedMap(
        number=3, tiles=tiles, things=things, start=(4, 4),
        exit_stand=(12, 12), secret_rewards=[], seed=99,
        rooms=rooms, edges=((0, 1), (1, 2), (1, 3)),
        critical_route=(0, 1, 2))


class SimulationTests(unittest.TestCase):
    SCALARS = tuple(field.name for field in fields(ProfileMetrics)
                    if field.name != "profile")

    def test_all_profile_metrics_are_bounded_and_deterministic(self):
        level = _level(corridor_enemies=2)
        first = simulate_player_experience(level)
        second = simulate_player_experience(level)
        self.assertEqual(first, second)
        self.assertEqual(
            tuple(profile.profile for profile in first.profiles),
            ("direct-route", "cautious-corner-checker", "explorer"))
        for profile in first.profiles:
            for metric in self.SCALARS:
                with self.subTest(profile=profile.profile, metric=metric):
                    self.assertGreaterEqual(getattr(profile, metric), 0.0)
                    self.assertLessEqual(getattr(profile, metric), 1.0)
        self.assertGreaterEqual(first.encounter_affordance, 0.0)
        self.assertLessEqual(first.encounter_affordance, 1.0)
        self.assertGreaterEqual(first.pacing_sustainability, 0.0)
        self.assertLessEqual(first.pacing_sustainability, 1.0)

    def test_profiles_walk_differently_and_explorer_activates_branch(self):
        profiles = {
            profile.profile: profile
            for profile in simulate_player_experience(_level()).profiles
        }
        direct = profiles["direct-route"]
        cautious = profiles["cautious-corner-checker"]
        explorer = profiles["explorer"]
        self.assertGreater(cautious.backtracking_distance,
                           direct.backtracking_distance)
        self.assertGreater(explorer.backtracking_distance,
                           direct.backtracking_distance)
        self.assertGreater(explorer.sound_zone_activation,
                           direct.sound_zone_activation)

    def test_finished_plane_threat_count_changes_exposure(self):
        sparse_level = _level(branch_enemy=False, corridor_enemies=1)
        crowded_level = _level(branch_enemy=False, corridor_enemies=3)
        sparse = simulate_player_experience(sparse_level)
        crowded = simulate_player_experience(crowded_level)
        sparse_direct = sparse.profiles[0]
        crowded_direct = crowded.profiles[0]
        self.assertGreater(crowded_direct.maximum_simultaneous_attackers,
                           sparse_direct.maximum_simultaneous_attackers)
        self.assertGreater(crowded_direct.health_pressure,
                           sparse_direct.health_pressure)
        self.assertNotEqual(crowded.encounter_affordance,
                            sparse.encounter_affordance)
        # With no encounter records, this reaches quality through simulation.
        self.assertNotEqual(
            quality_report(crowded_level, [], object()).encounter_quality,
            quality_report(sparse_level, [], object()).encounter_quality)

    def test_unavailable_engine_outcomes_are_named_not_fabricated(self):
        joined = " ".join(UNSUPPORTED_MEASUREMENTS)
        self.assertIn("actual health lost", joined)
        self.assertIn("boss", joined)
        self.assertIn("elapsed seconds", joined)


if __name__ == "__main__":
    unittest.main()
