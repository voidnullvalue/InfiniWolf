import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from randomwolf.config import CampaignConfig, Intensity
from randomwolf.generator import GenerationCancelled, generate_campaign, generate_map, validate_map, validate_package
from randomwolf.generator import ELEVATOR_WALL, GOLD_KEY, HANS_GROSSE, SECRET_ELEVATOR_SWITCH, _reachable
from randomwolf.generator import FLOOR, ZONE_MAX


class GeneratorTests(unittest.TestCase):
    def test_maps_validate_across_settings(self):
        for seed in range(12):
            for intensity in (Intensity.VERY_LOW, Intensity.NORMAL, Intensity.VERY_HIGH):
                config = CampaignConfig(seed=seed, guard_density=intensity,
                                        layout_complexity=intensity, secrets=intensity)
                validate_map(generate_map(config, 1))
                validate_map(generate_map(config, 9))

    def test_campaign_is_deterministic_and_asset_free(self):
        config = CampaignConfig(seed=8675309)
        with tempfile.TemporaryDirectory() as directory:
            first = generate_campaign(config, Path(directory) / "first.pk3")
            second = generate_campaign(config, Path(directory) / "second.pk3")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            different = generate_campaign(CampaignConfig(seed=8675310), Path(directory) / "different.pk3")
            self.assertNotEqual(first.read_bytes(), different.read_bytes())
            with zipfile.ZipFile(first) as package:
                names = package.namelist()
                self.assertEqual(len([name for name in names if name.endswith(".wad")]), 10)
                self.assertFalse(any(name.lower().endswith((".png", ".wav", ".wl6")) for name in names))
                manifest = json.loads(package.read("randomwolf-manifest.json"))
                self.assertEqual(manifest["seed"], config.seed)
                self.assertTrue(all(floor["validation"]["passed"] for floor in manifest["floors"]))
                self.assertIn("par =", package.read("mapinfo.txt").decode("utf-8"))
            self.assertEqual(validate_package(first)["seed"], config.seed)

    def test_cancellation_preserves_previous_output(self):
        config = CampaignConfig(seed=1001)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "campaign.pk3"
            output.write_bytes(b"previous-valid-package")
            progress = []
            with self.assertRaises(GenerationCancelled):
                generate_campaign(config, output, progress=lambda current, total: progress.append(current),
                                  cancelled=lambda: bool(progress))
            self.assertEqual(output.read_bytes(), b"previous-valid-package")
            self.assertEqual(progress, [1])

    def test_designated_floor_has_rewarded_secret_elevator(self):
        level = generate_map(CampaignConfig(seed=44), 3, secret_exit=True)
        self.assertIn(SECRET_ELEVATOR_SWITCH, level.tiles)
        self.assertTrue(level.secret_rewards)

    def test_lock_setting_changes_progression_content(self):
        low = generate_map(CampaignConfig(seed=121, locked_doors=Intensity.VERY_LOW), 4)
        high = generate_map(CampaignConfig(seed=121, locked_doors=Intensity.VERY_HIGH), 4)
        self.assertEqual(low.locked_doors, 0)
        self.assertGreater(high.locked_doors, 0)
        self.assertIn(GOLD_KEY, high.things)
        key_index = high.things.index(GOLD_KEY)
        self.assertIn((key_index % 64, key_index // 64),
                      _reachable(high.tiles, high.start, locked_open=False))

    def test_floor_nine_has_native_boss(self):
        level = generate_map(CampaignConfig(seed=909), 9)
        self.assertTrue(level.boss)
        self.assertEqual(level.things.count(HANS_GROSSE), 1)
        self.assertNotIn(level.exit_stand, _reachable(level.tiles, level.start, locked_open=False))
        self.assertNotIn(GOLD_KEY, level.things)

    def test_floor_codes_are_valid_sound_zones(self):
        level = generate_map(CampaignConfig(seed=771, layout_complexity=Intensity.HIGH), 5)
        zones = {tile for tile in level.tiles if FLOOR <= tile <= ZONE_MAX}
        self.assertTrue(zones)
        self.assertTrue(all(FLOOR <= zone <= ZONE_MAX for zone in zones))
        wall_materials = {tile for tile in level.tiles if 1 <= tile < 90}
        self.assertGreaterEqual(len(wall_materials), 2)
        self.assertIn(ELEVATOR_WALL, level.tiles)

    def test_enemy_population_has_cumulative_difficulty_layers(self):
        level = generate_map(CampaignConfig(seed=600, guard_density=Intensity.HIGH), 8)
        easy, medium_extra, hard_extra = level.enemy_tiers
        self.assertGreater(easy, 0)
        self.assertGreater(medium_extra, 0)
        self.assertGreater(hard_extra, 0)
        self.assertGreaterEqual(level.things.count(49), 1)
        self.assertGreaterEqual(level.things.count(47) + level.things.count(48), 1)


if __name__ == "__main__":
    unittest.main()
