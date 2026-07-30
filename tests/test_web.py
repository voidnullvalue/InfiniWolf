from pathlib import Path
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
import zipfile

from infiniwolf.config import GenerationQuality, Intensity, ThemeBias
from infiniwolf.web import campaign_config, check_for_web, generate_for_web


class CampaignConfigTests(unittest.TestCase):
    def test_browser_settings_use_cli_types(self):
        config = campaign_config({
            "seed": "browser-seed",
            "guard_density": 5,
            "theme_bias": "catacombs",
            "generation_quality": "fast",
        })
        self.assertEqual(config.guard_density, Intensity.VERY_HIGH)
        self.assertEqual(config.enemy_toughness, Intensity.NORMAL)
        self.assertEqual(config.theme_bias, ThemeBias.CATACOMBS)
        self.assertEqual(config.generation_quality, GenerationQuality.FAST)

    def test_unknown_setting_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown setting"):
            campaign_config('{"not_a_real_setting": 3}')


class BrowserAdapterTests(unittest.TestCase):
    @patch("infiniwolf.web.generate_campaign")
    def test_generation_returns_download_metadata(self, generate_campaign):
        generate_campaign.return_value = Path("/tmp/infiniwolf.pk3")
        progress = Mock()

        result = json.loads(generate_for_web(
            '{"seed": "42", "generation_quality": "fast"}',
            "/tmp/infiniwolf.pk3",
            progress,
        ))

        config, output = generate_campaign.call_args.args
        self.assertEqual(config.seed, 42)
        self.assertEqual(output, Path("/tmp/infiniwolf.pk3"))
        self.assertIs(generate_campaign.call_args.kwargs["progress"], progress)
        self.assertEqual(result["seed"], 42)
        self.assertEqual(result["output"], "/tmp/infiniwolf.pk3")
        self.assertIn("build", result)
        self.assertIn("commit", result)

    @patch("infiniwolf.web.zipfile.is_zipfile", return_value=False)
    @patch("infiniwolf.web.verify_path")
    def test_checker_keeps_floor_for_standalone_wad(self, verify_path, _is_zipfile):
        verification = Mock()
        verification.to_json.return_value = '{"verdict": "verified"}'
        verify_path.return_value = verification

        result = check_for_web("/tmp/map.wad", 7)

        verify_path.assert_called_once_with(Path("/tmp/map.wad"), 7)
        self.assertEqual(json.loads(result)["verdict"], "verified")

    @patch("infiniwolf.web.zipfile.is_zipfile", return_value=True)
    @patch("infiniwolf.web.verify_path")
    def test_checker_ignores_floor_for_pk3(self, verify_path, _is_zipfile):
        verification = Mock()
        verification.to_json.return_value = '{"verdict": "verified"}'
        verify_path.return_value = verification

        result = check_for_web("/tmp/campaign.pk3", 7)

        verify_path.assert_called_once_with(Path("/tmp/campaign.pk3"), None)
        self.assertEqual(json.loads(result)["verdict"], "verified")

    def test_checker_classifies_unrelated_pk3(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "other.pk3"
            with zipfile.ZipFile(path, "w") as package:
                package.writestr("maps/map01.wad", b"not an InfiniWolf map")

            result = json.loads(check_for_web(path, 7))

        self.assertEqual(result["verdict"], "not-infiniwolf")
        self.assertEqual(result["maps_checked"], 0)
        self.assertEqual(result["floor_numbers"], [])

    def test_checker_rejects_partial_infiniwolf_campaign_cleanly(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "partial.pk3"
            with zipfile.ZipFile(path, "w") as package:
                package.writestr("maps/iw01.wad", b"not enough maps")

            with self.assertRaisesRegex(
                ValueError,
                r"incomplete InfiniWolf campaign; missing maps/iw02\.wad",
            ):
                check_for_web(path)


if __name__ == "__main__":
    unittest.main()
