from collections import Counter
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from infiniwolf.config import CampaignConfig, Intensity
from infiniwolf.generator import GenerationCancelled, generate_campaign, generate_map, validate_map, validate_package
from infiniwolf.generator import (BOSSES, DOGS, ELEVATOR_TILE, GOLD_KEY, GUARDS, OFFICERS, SS,
                                   SECRET_EXIT_ZONE, WALL_THEMES, _at, _reachable)
from infiniwolf.generator import FLOOR, ZONE_MAX


class GeneratorTests(unittest.TestCase):
    def test_actor_thing_codes_are_ordered_for_ecwolfs_engine_not_compass_order(self):
        """ECWolf's old-format loader computes each thing's angle as
        (oldnum - base) * 90 and casts it to MapTile::Side, an enum ordered
        {East, North, West, South} (gamemap.h) -- not the North-first order
        a human would guess. Every place in generator.py that indexes these
        arrays (facings tuple, _pick_stationary_facing, the hardcoded vault
        guard) assumes index 0/1/2/3 means N/E/S/W, so the tuples themselves
        must be pre-permuted to match: [0]=base+1 (north), [1]=base+0
        (east), [2]=base+3 (south), [3]=base+2 (west). Getting this backwards
        made every "correctly" computed facing decision place the wrong one
        of the 4 thing-codes -- actors could face a wall the generator had
        explicitly steered them away from, invisible to any test that (like
        the generator) assumed compass order instead of checking against the
        engine's actual angle formula."""
        for family, base in ((GUARDS, 108), (OFFICERS, 116), (SS, 126), (DOGS, 134)):
            self.assertEqual(family, (base + 1, base + 0, base + 3, base + 2))

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
                manifest = json.loads(package.read("infiniwolf-manifest.json"))
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
        self.assertEqual(level.tiles.count(SECRET_EXIT_ZONE), 1)
        index = level.tiles.index(SECRET_EXIT_ZONE)
        # The modzone floor cell must face a real elevator switch: that pair
        # is what ECWolf rewrites into an Exit_Secret trigger.
        self.assertEqual(_at(level.tiles, index % 64 + 1, index // 64), ELEVATOR_TILE)
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
        self.assertEqual(sum(thing in BOSSES for thing in level.things), 1)
        self.assertNotIn(level.exit_stand, _reachable(level.tiles, level.start, locked_open=False))
        self.assertNotIn(GOLD_KEY, level.things)

    def test_boss_choice_varies_across_seeds(self):
        bosses = {next(thing for thing in generate_map(CampaignConfig(seed=seed), 9).things
                       if thing in BOSSES)
                  for seed in range(20)}
        self.assertGreater(len(bosses), 1, "boss floor always picked the same boss across seeds")

    def test_wall_theme_materials_are_internally_consistent(self):
        """Every WALL_THEMES entry must draw its base and accents from a
        single WL6 texture family (verified by tile name prefix in
        wolf3d.txt's xlat table). Theme selection is randomized per floor
        rather than pinned to floor number, so any entry can land on any
        floor -- a cross-family mix (e.g. blue stone corridors with a metal
        accent room) is now visible immediately instead of hiding behind
        whichever one floor number used to select that entry."""
        families = (
            {1, 2, 3, 4},        # grey stone: GSTONEA1/B1, GSTFLAG1, GSTHTLR1
            {8, 9, 7, 41},       # blue stone: BSTONEA1/B1, BSTCELB1, BSTSIGN1
            {40, 9, 34, 36},     # blue wall: BLUWALL1, BSTONEB1, BLUSKUL1, BLUSWAS1
            {12, 10, 11, 23},    # wood: WOOD1, WODEAGL1, WODHTLR1, WODCROS1
            {15, 14},            # metal: METAL1, METLSGN1
            {17, 18, 20},        # brick: BRICK1, BRIKWRT1, BRIKEGL1
        )
        for base, accents in WALL_THEMES:
            materials = {base, *accents}
            self.assertTrue(
                any(materials <= family for family in families),
                f"theme base={base} accents={accents} mixes incompatible wall materials")

    def test_cell_wall_tile_is_never_the_base_theme(self):
        """BSTCELA1(5)/BSTCELB1(7), the barred prison-cell wall, reads as a
        specific set piece; if either is a theme's base it fills every wall
        on the whole floor instead of being confined to a themed room."""
        for base, accents in WALL_THEMES:
            self.assertNotIn(base, (5, 7))

    def test_wall_theme_varies_by_seed_not_just_floor_number(self):
        dominant = set()
        for seed in range(15):
            level = generate_map(CampaignConfig(seed=seed), 2)
            counts = Counter(tile for tile in level.tiles if 1 <= tile < 90)
            dominant.add(counts.most_common(1)[0][0])
        self.assertGreater(len(dominant), 1,
                           "floor 2 always used the same dominant wall material across seeds")

    def test_floor_codes_are_valid_sound_zones(self):
        level = generate_map(CampaignConfig(seed=771, layout_complexity=Intensity.HIGH), 5)
        zones = {tile for tile in level.tiles if FLOOR <= tile <= ZONE_MAX}
        self.assertTrue(zones)
        self.assertTrue(all(FLOOR <= zone <= ZONE_MAX for zone in zones))
        wall_materials = {tile for tile in level.tiles if 1 <= tile < 90}
        self.assertGreaterEqual(len(wall_materials), 2)
        self.assertIn(ELEVATOR_TILE, level.tiles)
        # Tile 22 is the decoy "fake elevator" switch and must never appear.
        self.assertNotIn(22, level.tiles)

    def test_elevator_is_usable_and_authentic(self):
        for seed in range(30):
            level = generate_map(CampaignConfig(seed=seed), 2)
            sx, sy = level.exit_stand
            switch_east = _at(level.tiles, sx + 1, sy) == ELEVATOR_TILE
            switch_west = _at(level.tiles, sx - 1, sy) == ELEVATOR_TILE
            self.assertTrue(switch_east or switch_west,
                            "switch must be on the east/west axis")
            door_x = sx - 2 if switch_east else sx + 2
            self.assertEqual(_at(level.tiles, door_x, sy), 100,
                             "elevator entrance must be a real elevator door")

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
