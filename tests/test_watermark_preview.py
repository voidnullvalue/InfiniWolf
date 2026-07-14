from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import zipfile

from infiniwolf.generator import _wad_bytes
from infiniwolf.preview import load_previews
from infiniwolf.watermark import (
    apply_campaign_watermark,
    floor_target,
    plane_residue,
    plane_residue_secondary,
    secondary_target,
    verify_campaign,
    verify_single_map,
)


def _levels():
    result = []
    for number in range(1, 11):
        tiles = [108 + index % 12 for index in range(64 * 64)]
        things = [0] * (64 * 64)
        tiles[1], tiles[2], tiles[3] = 21, 85, 90
        things[4] = 98
        result.append(SimpleNamespace(number=number, seed=1000 + number,
                                      tiles=tiles, things=things))
    return result


class WatermarkAndPreviewTests(unittest.TestCase):
    def test_every_floor_has_two_signatures_and_campaign_totals_42(self):
        levels = _levels()
        apply_campaign_watermark(levels, 12345)
        primary = []
        for level in levels:
            first = plane_residue(level.tiles, level.things, level.number)
            second = plane_residue_secondary(level.tiles, level.things, level.number)
            self.assertEqual(first, floor_target(level.number))
            self.assertEqual(second, secondary_target(level.number))
            primary.append(first)
        self.assertEqual(sum(primary) % 43, 42)

    def test_standalone_wad_is_independently_verifiable(self):
        level = _levels()[4]
        apply_campaign_watermark([level], 987)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "renamed.wad"
            path.write_bytes(_wad_bytes("IW05", level.tiles, level.things))
            result = verify_single_map(path)
        self.assertEqual(result.verdict, "verified")
        self.assertEqual(result.floor_numbers, (5,))
        self.assertEqual(result.watermark_floors, 1)
        self.assertEqual(result.secondary_floors, 1)

    def test_campaign_checker_and_preview_do_not_depend_on_manifest(self):
        levels = _levels()
        apply_campaign_watermark(levels, 456)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaign.pk3"
            with zipfile.ZipFile(path, "w") as package:
                for level in levels:
                    package.writestr(
                        f"maps/iw{level.number:02d}.wad",
                        _wad_bytes(f"IW{level.number:02d}", level.tiles, level.things))
            result = verify_campaign(path)
            previews = load_previews(path)
        self.assertEqual(result.verdict, "verified")
        self.assertTrue(result.global_42)
        self.assertFalse(result.manifest_present)
        self.assertEqual(len(previews), 10)
        self.assertEqual(previews[0].name, "Floor 1")


if __name__ == "__main__":
    unittest.main()
