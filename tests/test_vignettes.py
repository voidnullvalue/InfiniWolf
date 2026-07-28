"""Cross-system vignette contracts."""
import unittest

from infiniwolf.config import CampaignConfig
from infiniwolf.generator import generate_map
from infiniwolf.model import Room, RoomIdentity
from infiniwolf.vignettes import plan_vignettes


def identity(concept):
    return RoomIdentity("branch", "standard", "spine", 0, "standard", concept, concept)


class VignetteTests(unittest.TestCase):
    def test_planner_is_deterministic_and_respects_special_floor_contracts(self):
        rooms = [Room(2, 2, 7, 7), Room(12, 2, 7, 7)]
        identities = [identity("guardpost"), identity("storage")]
        first = plan_vignettes("vignette-contract", 2, rooms, identities, [(0, 1)], ())
        second = plan_vignettes("vignette-contract", 2, rooms, identities, [(0, 1)], ())
        self.assertEqual(first, second)
        self.assertEqual((), plan_vignettes("vignette-contract", 9, rooms, identities, [(0, 1)], ()))

    def test_realized_vignette_records_all_three_subsystems(self):
        # This is an intentionally fixed integration seed, not a probabilistic
        # assertion: campaign intent itself is a deterministic schedule.
        level = generate_map(CampaignConfig(seed="0"), 4)
        self.assertTrue(level.vignette_plans)
        self.assertTrue(level.realized_vignettes)
        realized = level.realized_vignettes[0]
        self.assertTrue(realized.encounter_rooms)
        self.assertTrue(realized.pickup_rooms)
        self.assertTrue(realized.decoration_rooms)
        self.assertLessEqual(len(level.vignette_plans), 1)


if __name__ == "__main__":
    unittest.main()
