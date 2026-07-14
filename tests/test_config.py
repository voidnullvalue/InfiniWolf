import unittest

from infiniwolf.config import (CampaignConfig, Intensity, LittleEntropyMachine,
                               ThemeBias, resolve_seed)


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

    def test_little_entropy_machine_is_the_named_seed_source(self):
        config = CampaignConfig(seed=123)
        source = LittleEntropyMachine(config.seed)
        self.assertEqual(config.floor_seed(2), source.floor(2))
        self.assertEqual(config.variant_seed(2), source.variant(2))
        self.assertEqual(config.lock_seed(), source.locks())
        self.assertEqual(config.circulation_seed(2), source.circulation(2))

    def test_hidden_stream_is_stable_and_separate(self):
        config = CampaignConfig(seed=123, say_aardwolf=True)
        self.assertEqual(config.aardwolf_seed(2), config.aardwolf_seed(2))
        self.assertNotEqual(config.aardwolf_seed(2), config.aardwolf_seed(3))
        self.assertNotEqual(config.aardwolf_seed(2), config.floor_seed(2))
        with self.assertRaises(ValueError):
            config.aardwolf_seed(0)

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
        self.assertIn('"say_aardwolf": false', encoded)

    def test_lock_schedule_seed_is_stable_and_separate(self):
        config = CampaignConfig(seed=123)
        self.assertEqual(config.lock_seed(), CampaignConfig(seed=123).lock_seed())
        self.assertNotEqual(config.lock_seed(), config.variant_seed(1))


if __name__ == "__main__":
    unittest.main()
