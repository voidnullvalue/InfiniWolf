import unittest

from randomwolf.config import CampaignConfig, Intensity, resolve_seed


class ConfigTests(unittest.TestCase):
    def test_numeric_seed(self):
        self.assertEqual(resolve_seed("0x2a"), 42)

    def test_text_seed_is_stable(self):
        self.assertEqual(resolve_seed("castle"), resolve_seed("castle"))

    def test_floor_subseeds_are_stable_and_distinct(self):
        config = CampaignConfig(seed=123, secrets=Intensity.HIGH)
        self.assertEqual(config.floor_seed(2), config.floor_seed(2))
        self.assertNotEqual(config.floor_seed(2), config.floor_seed(3))
        self.assertNotEqual(config.floor_seed(2), config.floor_seed(2, 1))

    def test_json_uses_numeric_intensities(self):
        self.assertIn('"guard_density": 3', CampaignConfig(seed=123).to_json())


if __name__ == "__main__":
    unittest.main()
