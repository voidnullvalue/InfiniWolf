from collections import Counter
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest import mock
import zipfile

import infiniwolf.generator as generator
from infiniwolf.config import CampaignConfig, Intensity
from infiniwolf.generator import GenerationCancelled, generate_campaign, generate_map, validate_map, validate_package
from infiniwolf.generator import (BOSSES, DECOR_WALLS, DOGS, ELEVATOR_TILE, FAKE_HITLER,
                                   GHOSTS, GOLD_KEY, GRID, GUARDS, KEY_DROP_BOSSES, OFFICERS,
                                   PUSHWALL, Room, RoomSpec, SECRET_EXIT_ZONE, SS, WALL, WALL_THEMES,
                                   AMMO, CHAINGUN, FIRST_AID, MACHINE_GUN, ONE_UP,
                                   _DECOR_ZONES, _apply_wall_theme, _at, _decor_theme, _hint_secrets,
                                   _is_floor, _place_decorations, _place_zoned, _reachable,
                                   _room_predecessor)
from infiniwolf.generator import FLOOR, FLOOR_TEN_STONE_THEME, ZONE_MAX


def _generate_with_retries(config: CampaignConfig, floor: int, attempts: int = 50):
    """Mirror generate_campaign's own retry loop: a single (seed, floor,
    attempt) combination may legitimately fail validate_map (same helper as
    the topology suite), so seed sweeps must not assume attempt 0 succeeds."""
    last_error = None
    for attempt in range(attempts):
        try:
            return generate_map(config, floor, attempt)
        except ValueError as error:
            last_error = error
    raise AssertionError(f"floor {floor} never validated in {attempts} attempts: {last_error}")


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
                validate_map(_generate_with_retries(config, 1))
                validate_map(_generate_with_retries(config, 9))

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
        # Seed 122, not 121: lock placement can legitimately come up empty on
        # a rare layout, and 121 rerolled into that case when floor variants
        # shifted the rng stream (a 1-in-20 outcome in the surrounding seeds).
        low = generate_map(CampaignConfig(seed=122, locked_doors=Intensity.VERY_LOW), 4)
        high = generate_map(CampaignConfig(seed=122, locked_doors=Intensity.VERY_HIGH), 4)
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

    def test_boss_room_is_a_grand_purpose_built_arena(self):
        for seed in range(8):
            level = generate_map(CampaignConfig(seed=seed), 9)
            boss_cell = next(index for index, thing in enumerate(level.things) if thing in BOSSES)
            x, y = boss_cell % GRID, boss_cell // GRID
            room = next(room for room in level.rooms
                        if room.x <= x < room.x + room.w and room.y <= y < room.y + room.h)
            self.assertGreaterEqual(room.w, 14)
            self.assertGreaterEqual(room.h, 14)
            self.assertEqual(room, max(level.rooms, key=lambda candidate: candidate.w * candidate.h))

    def test_anchor_tier_room_always_gets_grand_decor_theme(self):
        self.assertEqual(_decor_theme("climax", "anchor"), "grand")
        self.assertEqual(_decor_theme("hub", "anchor"), "grand")

    def test_room_predecessor_uses_stable_bfs_loop_tie_break(self):
        edges = [(0, 1), (0, 2), (1, 3), (2, 3), (3, 4)]
        self.assertEqual(_room_predecessor(5, edges, 3), 1)
        self.assertEqual(_room_predecessor(5, edges, 4), 3)
        self.assertIsNone(_room_predecessor(5, edges, 0))

    def test_room_before_boss_has_a_stock_up_cache(self):
        stock_up_items = {FIRST_AID, AMMO, MACHINE_GUN, CHAINGUN, ONE_UP}
        for seed in range(8):
            level = generate_map(CampaignConfig(seed=seed), 9)
            boss_cell = next(index for index, thing in enumerate(level.things) if thing in BOSSES)
            x, y = boss_cell % GRID, boss_cell // GRID
            boss_index = next(index for index, room in enumerate(level.rooms)
                              if room.x <= x < room.x + room.w and room.y <= y < room.y + room.h)
            preboss_index = _room_predecessor(len(level.rooms), list(level.edges), boss_index)
            self.assertIsNotNone(preboss_index)
            room = level.rooms[preboss_index]
            count = sum(thing in stock_up_items
                        for index, thing in enumerate(level.things)
                        if room.x <= index % GRID < room.x + room.w
                        and room.y <= index // GRID < room.y + room.h)
            self.assertGreaterEqual(count, 2)

    def test_non_key_drop_bosses_get_a_reachable_physical_key(self):
        seen_non_droppers = set()
        for seed in range(12):
            level = generate_map(CampaignConfig(seed=seed), 9)
            boss = next(thing for thing in level.things if thing in BOSSES)
            if boss in KEY_DROP_BOSSES:
                continue
            seen_non_droppers.add(boss)
            self.assertIn(GOLD_KEY, level.things)
            key_index = level.things.index(GOLD_KEY)
            self.assertIn((key_index % 64, key_index // 64),
                          _reachable(level.tiles, level.start, locked_open=False))
        self.assertTrue(seen_non_droppers)

    def test_fake_hitler_is_not_a_boss_and_only_spawns_on_floor_nine(self):
        self.assertNotIn(FAKE_HITLER, BOSSES)
        for number in range(1, 11):
            for seed in range(1):
                level = generate_map(CampaignConfig(seed=seed), number)
                if number != 9:
                    self.assertNotIn(FAKE_HITLER, level.things)
                self.assertLessEqual(level.things.count(FAKE_HITLER), 1)

    def test_ghosts_only_spawn_on_secret_floor(self):
        for number in range(1, 10):
            for seed in range(1):
                level = generate_map(CampaignConfig(seed=seed), number)
                self.assertFalse(set(level.things) & set(GHOSTS))
        for seed in range(3):
            level = generate_map(CampaignConfig(seed=seed), 10)
            self.assertLessEqual(sum(thing in GHOSTS for thing in level.things), 1)

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
            {8, 7, 41},          # blue stone: BSTONEA1, BSTCELB1, BSTSIGN1
            {40, 34, 36},        # blue wall: BLUWALL1, BLUSKUL1, BLUSWAS1
            {9},                 # rare floor-10 masonry: BSTONEB1
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

    def test_skeleton_cage_is_a_single_landmark_not_room_material(self):
        self.assertIn(7, DECOR_WALLS)
        for seed in range(6):
            level = generate_map(CampaignConfig(seed=seed), 2)
            for ridx, room in enumerate(level.rooms):
                if ridx in level.jail_rooms:
                    continue
                cells = ({(x, room.y - 1) for x in range(room.x - 1, room.x + room.w + 1)}
                         | {(x, room.y + room.h) for x in range(room.x - 1, room.x + room.w + 1)}
                         | {(room.x - 1, y) for y in range(room.y, room.y + room.h)}
                         | {(room.x + room.w, y) for y in range(room.y, room.y + room.h)})
                wall_ring = [_at(level.tiles, *cell) for cell in cells]
                self.assertLessEqual(wall_ring.count(7), 1)

    def test_jail_cells_never_exceed_a_ratio_of_occupied_to_unoccupied(self):
        seen_jail = False
        for seed in range(8):
            for number in (2, 5, 8):
                level = generate_map(CampaignConfig(seed=seed), number)
                for ridx in level.jail_rooms:
                    seen_jail = True
                    room = level.rooms[ridx]
                    ring = [_at(level.tiles, x, room.y - 1)
                            for x in range(room.x - 1, room.x + room.w + 1)]
                    ring += [_at(level.tiles, x, room.y + room.h)
                             for x in range(room.x - 1, room.x + room.w + 1)]
                    ring += [_at(level.tiles, room.x - 1, y)
                             for y in range(room.y, room.y + room.h)]
                    ring += [_at(level.tiles, room.x + room.w, y)
                             for y in range(room.y, room.y + room.h)]
                    self.assertLessEqual(ring.count(7), ring.count(5))
                    self.assertGreater(ring.count(5) + ring.count(7), 0)
        self.assertTrue(seen_jail)

    def test_jail_pattern_only_appears_in_base_eight_groups(self):
        captured = []
        original = generator._apply_wall_theme

        def capture(*args, **kwargs):
            captured.append((args[4], args[5]))
            return original(*args, **kwargs)

        with mock.patch.object(generator, "_apply_wall_theme", side_effect=capture):
            levels = [generate_map(CampaignConfig(seed=seed), 2) for seed in range(6)]
        for level, (component_of, group_theme) in zip(levels, captured):
            for room in level.rooms:
                ring = [_at(level.tiles, x, room.y - 1)
                        for x in range(room.x - 1, room.x + room.w + 1)]
                ring += [_at(level.tiles, x, room.y + room.h)
                         for x in range(room.x - 1, room.x + room.w + 1)]
                ring += [_at(level.tiles, room.x - 1, y)
                         for y in range(room.y, room.y + room.h)]
                ring += [_at(level.tiles, room.x + room.w, y)
                         for y in range(room.y, room.y + room.h)]
                if set(ring) & {5, 7}:
                    self.assertEqual(group_theme[component_of[room.center]][0], 8)

    def test_jail_rooms_are_a_minority_of_blue_stone_rooms(self):
        captured = []
        original = generator._apply_wall_theme

        def capture(*args, **kwargs):
            captured.append((args[4], args[5]))
            return original(*args, **kwargs)

        with mock.patch.object(generator, "_apply_wall_theme", side_effect=capture):
            levels = [generate_map(CampaignConfig(seed=seed), 2) for seed in range(12)]
        for level, (component_of, group_theme) in zip(levels, captured):
            blue_rooms = sum(group_theme[component_of[room.center]][0] == 8
                             for room in level.rooms)
            if blue_rooms > 1:
                self.assertLess(len(level.jail_rooms), blue_rooms)

    def test_jail_mortar_pillar_spacing(self):
        room = Room(10, 10, 6, 4)
        tiles = [WALL] * (GRID * GRID)
        component_of = {}
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR
                component_of[x, y] = 0
        _apply_wall_theme(tiles, [0] * len(tiles), [room], [0], component_of,
                          {0: (8, (7, 41))}, random.Random(0), jail_rooms=frozenset({0}))
        run = [(x, room.y - 1) for x in range(room.x, room.x + room.w)]
        for index, cell in enumerate(run):
            if index % 3 == 0:
                self.assertEqual(_at(tiles, *cell), 8)
            else:
                self.assertIn(_at(tiles, *cell), (5, 7))

    def test_jail_decor_theme_biases_bones_and_blood(self):
        room = Room(10, 10, 8, 8)
        for seed in range(6):
            tiles = [WALL] * (GRID * GRID)
            for y in range(room.y, room.y + room.h):
                for x in range(room.x, room.x + room.w):
                    tiles[y * GRID + x] = FLOOR
            things = [0] * len(tiles)
            _place_decorations([room], tiles, things, set(), room.center, random.Random(seed),
                               jail_rooms=frozenset({0}))
            placed = [_at(things, x, y)
                      for y in range(room.y, room.y + room.h)
                      for x in range(room.x, room.x + room.w) if _at(things, x, y)]
            # 32 is the flat skeleton the jail-remains corner vignette lays
            # down; everything else stays barrels, blood, and bone variants.
            self.assertTrue(set(placed) <= {58, 61, 42, 64, 65, 66, 32})

    def test_decoration_zoning_splits_across_room_halves(self):
        class ThemedRandom(random.Random):
            def random(self):
                return 0.0

        room = Room(10, 10, 8, 8)
        tiles = [WALL] * (GRID * GRID)
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR
        things = [0] * len(tiles)
        _place_decorations([room], tiles, things, set(), room.center, ThemedRandom(0),
                           roles=["start"])
        cx, _ = room.center
        zone_a = {26, 35, 37}
        zone_b = {31, 27}
        placed = [(index % GRID, index // GRID, thing)
                  for index, thing in enumerate(things) if thing]
        self.assertTrue(placed)
        for x, _, thing in placed:
            if thing in zone_a:
                self.assertLess(x, cx)
            if thing in zone_b:
                self.assertGreaterEqual(x, cx)

    def test_decoration_scattered_path_is_unchanged_for_ineligible_rooms(self):
        cases = ((Room(10, 10, 8, 8), [RoomSpec("beat", "closet", 0)]),
                 (Room(30, 10, 5, 5), [RoomSpec("start", "standard", 0)]))
        for room, specs in cases:
            tiles = [WALL] * (GRID * GRID)
            for y in range(room.y, room.y + room.h):
                for x in range(room.x, room.x + room.w):
                    tiles[y * GRID + x] = FLOOR
            with mock.patch.object(generator, "_place_zoned") as zoned:
                _place_decorations([room], tiles, [0] * len(tiles), set(), room.center,
                                   random.Random(0), roles=[specs[0].role], specs=specs)
            zoned.assert_not_called()

    def test_zoned_open_items_place_even_when_blocking_budget_is_exhausted(self):
        """A room whose colonnade/divider already spent pair_budget must
        still get its themed open items -- the roll that picks zoning must
        not silently zero out decoration just because no blocking budget is
        left for a cluster."""
        room = Room(10, 10, 8, 8)
        things = [0] * (GRID * GRID)
        free = {(x, y) for y in range(room.y + 1, room.y + room.h - 1)
                for x in range(room.x + 1, room.x + room.w - 1)}
        _place_zoned(room, _DECOR_ZONES["grand"], free, set(), set(), things,
                    random.Random(0), lambda cells, item: False, 0)
        self.assertTrue(any(things))

    def test_landmark_walls_hang_in_symmetric_arrangements(self):
        room = Room(10, 10, 16, 6)
        tiles = [WALL] * (GRID * GRID)
        component_of = {}
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR
                component_of[x, y] = 0
        landmarks = _apply_wall_theme(tiles, [0] * len(tiles), [room], [0], component_of,
                                      {0: (1, (3,))}, random.Random(0))
        cells = landmarks[0]
        # An 18-tile clean run earns the center-plus-mirrored-pair triplet.
        self.assertEqual(len(cells), 3)
        self.assertEqual(len({y for _, y in cells}), 1, "all hang on one wall")
        xs = sorted(x for x, _ in cells)
        self.assertEqual(xs[2] - xs[1], xs[1] - xs[0])
        for cell in cells:
            self.assertEqual(_at(tiles, *cell), 3)

    def test_landmark_frame_places_matched_pair_flanking_the_wall(self):
        room = Room(10, 10, 8, 8)
        tiles = [WALL] * (GRID * GRID)
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR
        tiles[9 * GRID + 14] = 3   # portrait landmark on the north wall
        things = [0] * len(tiles)
        _place_decorations([room], tiles, things, set(), room.center, random.Random(0),
                           landmarks={0: [(14, 9)]})
        self.assertNotEqual(_at(things, 13, 10), 0)
        self.assertEqual(_at(things, 13, 10), _at(things, 15, 10),
                         "frame must be a matched mirrored pair")
        self.assertEqual(_at(things, 14, 10), 0, "the picture itself stays visible")

    def test_doorway_approach_cells_never_get_blocking_decor(self):
        room = Room(10, 10, 8, 8)
        base_tiles = [WALL] * (GRID * GRID)
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                base_tiles[y * GRID + x] = FLOOR
        base_tiles[9 * GRID + 14] = 91   # door through the north wall
        base_tiles[8 * GRID + 14] = FLOOR
        for seed in range(12):
            for role in ("relief", "beat", "start"):
                tiles = list(base_tiles)
                things = [0] * len(tiles)
                _place_decorations([room], tiles, things, set(), room.center,
                                   random.Random(seed), roles=[role])
                for cell in ((14, 10), (14, 11)):
                    self.assertNotIn(_at(things, *cell), generator.STATIC_BLOCKING,
                                     "doorway approach is furniture-jammed")

    def test_decoration_respects_the_statics_soft_cap(self):
        room = Room(10, 10, 8, 8)
        tiles = [WALL] * (GRID * GRID)
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR
        things = [0] * len(tiles)
        for index in range(319):   # pre-existing treasure consumes the budget
            things[index] = 52
        _place_decorations([room], tiles, things, set(), room.center, random.Random(0))
        self.assertLessEqual(sum(23 <= thing <= 74 for thing in things), 320)

    def test_corridor_lights_pace_straight_halls_evenly(self):
        tiles = [WALL] * (GRID * GRID)
        path = [(x, 20) for x in range(10, 30)]
        for x, y in path:
            tiles[y * GRID + x] = FLOOR
        things = [0] * len(tiles)
        _place_decorations([], tiles, things, set(), path[0], random.Random(0),
                           paths=[path])
        lights = sorted(x for x in range(10, 30) if _at(things, x, 20) == 37)
        self.assertGreater(len(lights), 1)
        self.assertEqual({b - a for a, b in zip(lights, lights[1:])}, {4},
                         "lights must march at a fixed rhythm")

    def test_alcove_pocket_gets_a_niche_piece_at_its_deepest_cell(self):
        room = Room(10, 10, 8, 8)
        tiles = [WALL] * (GRID * GRID)
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR
        for y in (13, 14):   # 2x2 alcove bump off the east wall
            for x in (18, 19):
                tiles[y * GRID + x] = FLOOR
        things = [0] * len(tiles)
        _place_decorations([room], tiles, things, set(), room.center, random.Random(0),
                           paths=[[]])
        niche = [(x, y) for y in (13, 14) for x in (18, 19) if _at(things, x, y)]
        self.assertEqual(len(niche), 1)
        self.assertEqual(niche[0][0], 19, "piece sits at the pocket's deepest cell")
        self.assertIn(_at(things, *niche[0]), (35, 31, 26, 58))

    def test_corridor_pocket_open_decor_is_a_light_not_pots(self):
        """Playtest bug: hanging pots/pans showed up in a hallway. Pockets
        whose only mouth is a corridor must fall back to a ceiling light,
        never kitchenware or remains."""
        tiles = [WALL] * (GRID * GRID)
        path = [(x, 20) for x in range(10, 26)]
        for x, y in path:
            tiles[y * GRID + x] = FLOOR
        tiles[19 * GRID + 16] = FLOOR   # 1-cell dead-end stub off the hall
        for seed in range(6):
            things = [0] * len(tiles)
            _place_decorations([], tiles, things, set(), path[0], random.Random(seed),
                               paths=[path])
            self.assertIn(_at(things, 16, 19), (0, 37))

    def test_open_decor_is_anchored_not_scattered(self):
        open_codes = {27, 37, 61, 67}
        for seed in (0, 3, 7):
            for floor in (2, 5):
                level = _generate_with_retries(CampaignConfig(seed=seed), floor)
                self.assertLessEqual(sum(23 <= t <= 74 for t in level.things), 320)
                for index, thing in enumerate(level.things):
                    if thing not in open_codes:
                        continue
                    x, y = index % GRID, index // GRID
                    room = next((r for r in level.rooms
                                 if r.x <= x < r.x + r.w and r.y <= y < r.y + r.h),
                                None)
                    if room is None:
                        continue   # corridor fixtures are rhythm-placed
                    cx, cy = room.center
                    near_wall = (x <= room.x + 2 or x >= room.x + room.w - 3
                                 or y <= room.y + 2 or y >= room.y + room.h - 3)
                    on_axis = x == cx or y == cy
                    beside_static = any(
                        _at(level.things, x + dx, y + dy) in generator.STATIC_BLOCKING
                        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
                    self.assertTrue(near_wall or on_axis or beside_static,
                                    f"floating open decor at {(x, y)} "
                                    f"seed {seed} floor {floor}")

    def test_reachability_still_holds_with_jail_and_zoned_decor(self):
        saw_jail = False
        for seed in range(6):
            for number in (2, 5, 8):
                level = generate_map(CampaignConfig(seed=seed), number)
                validate_map(level)
                saw_jail |= bool(level.jail_rooms)
        self.assertTrue(saw_jail)

    def test_plain_blue_wall_theme_has_no_accent_leakage(self):
        room = Room(10, 10, 4, 3)
        tiles = [WALL] * (GRID * GRID)
        component_of = {}
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR
                component_of[x, y] = 0
        _apply_wall_theme(tiles, [0] * len(tiles), [room], [0], component_of,
                          {0: (40, ())}, random.Random(0))
        cells = ({(x, room.y - 1) for x in range(room.x - 1, room.x + room.w + 1)}
                 | {(x, room.y + room.h) for x in range(room.x - 1, room.x + room.w + 1)}
                 | {(room.x - 1, y) for y in range(room.y, room.y + room.h)}
                 | {(room.x + room.w, y) for y in range(room.y, room.y + room.h)})
        # The four outer corners (diagonal to the room, touching no floor cell
        # on any of their 4 faces) are never painted -- they have no face a
        # player standing in an open cell could ever see, matching the same
        # "never rendered, left as literal WALL" treatment as deep interior
        # rock in _apply_wall_theme's phase 1.
        corners = {(room.x - 1, room.y - 1), (room.x + room.w, room.y - 1),
                   (room.x - 1, room.y + room.h), (room.x + room.w, room.y + room.h)}
        self.assertTrue(all(_at(tiles, *cell) == 40 for cell in cells - corners))

    def test_secret_hint_never_mixes_material_families(self):
        """Empty-accent themes borrow only same-base decor, if it exists."""
        tiles = [WALL] * (GRID * GRID)
        things = [0] * (GRID * GRID)
        component_of = {}
        group_theme = {
            0: (40, ()),
            1: FLOOR_TEN_STONE_THEME,
            2: (1, (2, 3, 4)),
            3: (8, (7, 41)),
        }
        pushwalls = ((10, 10), (20, 10), (30, 10), (40, 10))
        for group, (x, y) in enumerate(pushwalls):
            tiles[y * GRID + x] = group_theme[group][0]
            things[y * GRID + x] = PUSHWALL
            component_of[x + 1, y] = group

        _hint_secrets(tiles, things, component_of, group_theme, random.Random(0))

        families = {base: set(accents) | {base} for base, accents in WALL_THEMES}
        families[FLOOR_TEN_STONE_THEME[0]] = (set(FLOOR_TEN_STONE_THEME[1])
                                              | {FLOOR_TEN_STONE_THEME[0]})
        for group, (x, y) in enumerate(pushwalls):
            base, accents = group_theme[group]
            tile = _at(tiles, x, y)
            self.assertIn(tile, families[base])
            if accents:
                self.assertIn(tile, set(accents) & DECOR_WALLS)
        self.assertIn(_at(tiles, 10, 10), (34, 36))
        self.assertEqual(_at(tiles, 20, 10), 41)
        self.assertNotIn(_at(tiles, 10, 10), (3, 4))
        self.assertNotIn(_at(tiles, 20, 10), (3, 4))

    def test_blue_insignia_panels_are_single_landmarks_not_room_material(self):
        self.assertTrue({34, 36} <= DECOR_WALLS)
        rooms = [Room(10, 10, 4, 3), Room(30, 30, 4, 3)]
        tiles = [WALL] * (GRID * GRID)
        component_of = {}
        for group, room in enumerate(rooms):
            for y in range(room.y, room.y + room.h):
                for x in range(room.x, room.x + room.w):
                    tiles[y * GRID + x] = FLOOR
                    component_of[x, y] = group
        _apply_wall_theme(tiles, [0] * len(tiles), rooms, [0, 1], component_of,
                          {0: (40, (34, 36)), 1: (40, (34, 36))}, random.Random(0))
        for room, tile in zip(rooms, (34, 36)):
            cells = ({(x, room.y - 1) for x in range(room.x - 1, room.x + room.w + 1)}
                     | {(x, room.y + room.h) for x in range(room.x - 1, room.x + room.w + 1)}
                     | {(room.x - 1, y) for y in range(room.y, room.y + room.h)}
                     | {(room.x + room.w, y) for y in range(room.y, room.y + room.h)})
            wall_ring = [_at(tiles, *cell) for cell in cells]
            self.assertEqual(wall_ring.count(tile), 1)

    def test_blue_stone_masonry_is_floor_ten_only(self):
        for number in range(1, 10):
            for seed in range(2):
                self.assertNotIn(9, _generate_with_retries(CampaignConfig(seed=seed), number).tiles)

    def test_blue_stone_masonry_can_appear_on_floor_ten(self):
        # The roll is ~25% per generation; 16 seeds left under 1% odds of an
        # all-miss sample (0.75**16), which is exactly what an RNG-stream
        # shift from an unrelated upstream change once hit. Widen the sample
        # so this stays robust to that class of shift instead of re-picking
        # a new lucky range every time upstream rng consumption changes.
        seen_masonry = any(9 in _generate_with_retries(CampaignConfig(seed=seed), 10).tiles
                           for seed in range(48))
        self.assertTrue(seen_masonry)

    def test_wall_theme_varies_by_seed_not_just_floor_number(self):
        # Counting every wall-plane tile (including deep interior rock that
        # never borders floor and is therefore never painted or rendered)
        # always makes literal WALL(1) the "dominant" material regardless of
        # how varied the actual painted theme is -- only tiles with a floor
        # neighbor are ever player-visible, so only those should count.
        dominant = set()
        for seed in range(40):
            level = _generate_with_retries(CampaignConfig(seed=seed), 2)
            counts = Counter(
                tile for index, tile in enumerate(level.tiles)
                if 1 <= tile < 90
                and any(_is_floor(_at(level.tiles, index % GRID + dx, index // GRID + dy))
                       for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))))
            if counts:
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
            level = _generate_with_retries(CampaignConfig(seed=seed), 2)
            sx, sy = level.exit_stand
            switch_east = _at(level.tiles, sx + 1, sy) == ELEVATOR_TILE
            switch_west = _at(level.tiles, sx - 1, sy) == ELEVATOR_TILE
            self.assertTrue(switch_east or switch_west,
                            "switch must be on the east/west axis")
            door_x = sx - 2 if switch_east else sx + 2
            self.assertEqual(_at(level.tiles, door_x, sy), 100,
                             "elevator entrance must be a real elevator door")

    def _floor_with_variant(self, name, floor=2, max_seed=200):
        for seed in range(max_seed):
            config = CampaignConfig(seed=seed)
            if generator._variant_sequence(config)[floor - 1].name == name:
                return config, floor
        self.fail(f"no seed under {max_seed} yields variant {name} on floor {floor}")

    def test_variant_sequence_is_deterministic_with_no_consecutive_repeats(self):
        rotation_names = {variant.name for variant in generator.FLOOR_VARIANT_ROTATION}
        for seed in range(50):
            config = CampaignConfig(seed=seed)
            sequence = generator._variant_sequence(config)
            self.assertEqual(sequence, generator._variant_sequence(config))
            self.assertEqual(len(sequence), 10)
            names = [variant.name for variant in sequence[:8]]
            self.assertTrue(set(names) <= rotation_names)
            for previous, current in zip(names, names[1:]):
                self.assertNotEqual(previous, current)
            self.assertEqual(sequence[8].name, "stronghold")
            self.assertEqual(sequence[9].name, "vault")

    def test_variant_sequence_fixed_seed_regression(self):
        # Locks the infiniwolf:variant:v1 derivation: if these sequences
        # change, every existing campaign's floor identities silently reroll,
        # which must be a deliberate versioned decision, not a side effect.
        expected = {
            0: ("catacombs", "storehouse", "quarters", "catacombs", "quarters",
                "garrison", "catacombs", "garrison", "stronghold", "vault"),
            42: ("catacombs", "garrison", "quarters", "storehouse", "quarters",
                 "catacombs", "garrison", "grand-halls", "stronghold", "vault"),
        }
        for seed, names in expected.items():
            sequence = generator._variant_sequence(CampaignConfig(seed=seed))
            self.assertEqual(tuple(variant.name for variant in sequence), names)

    def test_variant_sequence_varies_across_seeds(self):
        sequences = {tuple(variant.name for variant in
                           generator._variant_sequence(CampaignConfig(seed=seed)))
                     for seed in range(20)}
        self.assertGreater(len(sequences), 1)

    def test_generated_floor_records_its_variant_stably_across_attempts(self):
        config = CampaignConfig(seed=77)
        expected = generator._variant_sequence(config)[2].name
        self.assertEqual(generate_map(config, 3).variant, expected)
        self.assertEqual(generate_map(config, 3, attempt=1).variant, expected)

    def test_manifest_names_each_floors_variant(self):
        config = CampaignConfig(seed=4242)
        expected = [variant.name for variant in generator._variant_sequence(config)]
        with tempfile.TemporaryDirectory() as directory:
            package = generate_campaign(config, Path(directory) / "campaign.pk3")
            manifest = generator.read_manifest(package)
        self.assertEqual([floor["variant"] for floor in manifest["floors"]], expected)

    def test_catacombs_variant_only_uses_pooled_wall_families(self):
        # catacombs pools blue stone/grey/brick; wood, metal, and BLUWALL
        # family tiles (bases and their accents) must never be painted.
        config, floor = self._floor_with_variant("catacombs")
        level = _generate_with_retries(config, floor)
        foreign = {12, 10, 11, 23, 15, 14, 40, 34, 36}
        self.assertFalse(foreign & set(level.tiles))

    def test_storehouse_variant_has_no_jail_cellblocks(self):
        config, floor = self._floor_with_variant("storehouse")
        self.assertEqual(_generate_with_retries(config, floor).jail_rooms, frozenset())

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
