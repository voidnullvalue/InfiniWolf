import unittest

from infiniwolf.config import CampaignConfig, Intensity, ThemeBias, resolve_seed


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

    def test_variant_seeds_are_stable_distinct_and_separate_from_floor_seeds(self):
        config = CampaignConfig(seed=123)
        self.assertEqual(config.variant_seed(2), config.variant_seed(2))
        self.assertNotEqual(config.variant_seed(2), config.variant_seed(3))
        self.assertNotEqual(config.variant_seed(2), config.floor_seed(2))
        with self.assertRaises(ValueError):
            config.variant_seed(0)

    def test_json_uses_numeric_intensities(self):
        encoded = CampaignConfig(seed=123, theme_bias=ThemeBias.CATACOMBS).to_json()
        self.assertIn('"guard_density": 3', encoded)
        self.assertIn('"decoration_amount": 3', encoded)
        self.assertIn('"theme_bias": "catacombs"', encoded)

    def test_lock_schedule_seed_is_stable_and_separate(self):
        config = CampaignConfig(seed=123)
        self.assertEqual(config.lock_seed(), CampaignConfig(seed=123).lock_seed())
        self.assertNotEqual(config.lock_seed(), config.variant_seed(1))


if __name__ == "__main__":
    unittest.main()
