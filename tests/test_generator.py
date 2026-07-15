from collections import Counter
from itertools import combinations
import json
import math
from pathlib import Path
import random
import tempfile
import unittest
from unittest import mock
import zipfile

import infiniwolf.generator as generator
from infiniwolf.config import CampaignConfig, Intensity, ThemeBias
from infiniwolf.generator import GenerationCancelled, generate_campaign, generate_map, validate_map, validate_package
from infiniwolf.generator import (BOSSES, DECOR_WALLS, DOGS, ELEVATOR_TILE, FAKE_HITLER,
                                   GHOSTS, GOLD_KEY, GRID, GUARDS, KEY_DROP_BOSSES, OFFICERS,
                                   PUSHWALL, Room, RoomSpec, SECRET_EXIT_ZONE, SS, WALL, WALL_THEMES,
                                   AMMO, CHAINGUN, FIRST_AID, MACHINE_GUN, ONE_UP, SILVER_KEY,
                                   _DECOR_ZONES, _apply_wall_theme, _at, _decor_theme, _hint_secrets,
                                   _carve_connection, _carve_notches, _is_floor, _path_bends,
                                   _place_decorations, _place_zoned, _reachable,
                                   _place_planned_rooms, _plan_floor, _snap_offsets,
                                   _room_predecessor)
from infiniwolf.generator import FLOOR, FLOOR_TEN_STONE_THEME, ZONE_MAX


def _generate_with_retries(config: CampaignConfig, floor: int, attempts: int = 50,
                           **kwargs):
    """Mirror generate_campaign's own retry loop: a single (seed, floor,
    attempt) combination may legitimately fail validate_map (same helper as
    the topology suite), so seed sweeps must not assume attempt 0 succeeds."""
    last_error = None
    for attempt in range(attempts):
        try:
            return generate_map(config, floor, attempt, **kwargs)
        except ValueError as error:
            last_error = error
    raise AssertionError(f"floor {floor} never validated in {attempts} attempts: {last_error}")


class GeneratorTests(unittest.TestCase):
    def test_arrival_elevator_is_inert_rock_bounded_and_behind_player(self):
        room = Room(20, 20, 8, 8)
        # ECWolf expands old-format player starts from thing 19 in its native
        # East/North/West/South angle order. This table is intentionally
        # independent of PLAYER_START_CODES so the test catches a rotated
        # generator convention instead of agreeing with it.
        engine_facing = {19: (1, 0), 20: (0, -1),
                         21: (-1, 0), 22: (0, 1)}
        kinds = ("outside-empty", "outside-supply", "inside-closed")
        for kind in kinds:
            with self.subTest(kind=kind):
                tiles = [WALL] * (GRID * GRID)
                for y in range(room.y, room.y + room.h):
                    for x in range(room.x, room.x + room.w):
                        tiles[y * GRID + x] = FLOOR
                arrival = generator._place_arrival_elevator(
                    tiles, room, (40, 24), random.Random(1),
                    forced_kind=kind)
                self.assertNotIn(ELEVATOR_TILE,
                                 [_at(tiles, *cell)
                                  for cell in arrival.footprint])
                dx, dy = ((0, -1), (1, 0), (0, 1), (-1, 0))[arrival.facing]
                self.assertEqual(
                    engine_facing[generator.PLAYER_START_CODES[arrival.facing]],
                    (dx, dy))
                if kind.startswith("inside-"):
                    self.assertEqual(arrival.player,
                                     (arrival.portal[0] - 2 * dx,
                                      arrival.portal[1] - 2 * dy))
                else:
                    self.assertEqual(arrival.portal,
                                     (arrival.player[0] - 2 * dx,
                                      arrival.player[1] - 2 * dy))
                self.assertTrue(all(_is_floor(_at(tiles, *cell))
                                    for cell in arrival.car_cells))
                self.assertEqual(
                    _at(tiles, *arrival.portal),
                    (generator.DOOR_ELEVATOR if dx
                     else generator.DOOR_ELEVATOR_NS),
                    "every arrival car must retain an elevator door")
                outward = (-dx, -dy)
                px, py = -outward[1], outward[0]
                for depth in (1, 2, 3):
                    for side in (-1, 1):
                        self.assertEqual(
                            _at(tiles,
                                arrival.portal[0] + depth * outward[0] + side * px,
                                arrival.portal[1] + depth * outward[1] + side * py),
                            generator.DUMMY_ELEVATOR_TILE,
                            "arrival car exposes a non-elevator side wall")
                self.assertEqual(bool(arrival.item), kind == "outside-supply")

        tiles = [WALL] * (GRID * GRID)
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR
        with self.assertRaisesRegex(ValueError, "unknown arrival elevator kind"):
            generator._place_arrival_elevator(
                tiles, room, (40, 24), random.Random(1),
                forced_kind="flush-facade")

    def test_arrival_elevators_only_use_horizontal_door_axes(self):
        """Vertical elevator doors use a different native tile and are easy
        to misread as exposed track.  Arrival cars deliberately use only the
        engine's horizontal elevator-door composition, regardless of where
        the next room lies relative to the start room."""
        room = Room(20, 20, 8, 8)
        toward_points = ((24, 4), (24, 55), (4, 24), (55, 24))
        for kind in ("outside-empty", "outside-supply", "inside-closed"):
            for toward in toward_points:
                with self.subTest(kind=kind, toward=toward):
                    tiles = [WALL] * (GRID * GRID)
                    for y in range(room.y, room.y + room.h):
                        for x in range(room.x, room.x + room.w):
                            tiles[y * GRID + x] = FLOOR
                    arrival = generator._place_arrival_elevator(
                        tiles, room, toward, random.Random(7),
                        forced_kind=kind)
                    self.assertIn(arrival.facing, (1, 3))
                    self.assertEqual(_at(tiles, *arrival.portal),
                                     generator.DOOR_ELEVATOR)

    def test_terminal_elevator_has_no_plain_wall_strip_inside_car(self):
        room = Room(20, 20, 8, 8)
        tiles = [WALL] * (GRID * GRID)
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR
        stand = generator._place_elevator(tiles, room)
        switch_dx = (1 if _at(tiles, stand[0] + 1, stand[1]) == ELEVATOR_TILE
                     else -1)
        door = stand[0] - 2 * switch_dx, stand[1]
        self.assertEqual(_at(tiles, *door), generator.DOOR_ELEVATOR)
        for depth in (1, 2, 3):
            x = door[0] + depth * switch_dx
            for side in (-1, 1):
                self.assertEqual(_at(tiles, x, door[1] + side), ELEVATOR_TILE,
                                 "terminal car exposes a non-elevator side wall")

    def test_generated_floors_record_purposeful_encounters_and_real_patrols(self):
        level = _generate_with_retries(CampaignConfig(seed=42), 4)
        self.assertIsNotNone(level.arrival)
        self.assertGreaterEqual(len({encounter.template
                                     for encounter in level.encounters}), 3)
        tracked = {(x, y): item for encounter in level.encounters
                   for x, y, item in encounter.cells}
        actual = {(index % GRID, index // GRID): item
                  for index, item in enumerate(level.things)
                  if item in generator.ENEMY_CODES and item not in BOSSES}
        self.assertEqual(tracked, actual)
        moving = sum(generator._patrol_actor_direction(item) is not None
                     for item in actual.values())
        self.assertGreaterEqual(moving / len(actual), 0.06)
        self.assertTrue(all(encounter.patrol_path
                            for encounter in level.encounters
                            if encounter.patrol_kind))

    def test_patrol_planner_uses_varied_engine_valid_route_families(self):
        kinds = set()
        for seed in range(6):
            level = _generate_with_retries(CampaignConfig(seed=seed), 4,
                                           attempts=10)
            kinds.update(encounter.patrol_kind for encounter in level.encounters
                         if encounter.patrol_kind)
            generator.validate_patrols(level)
        self.assertGreaterEqual(len(kinds), 3)
        self.assertTrue(kinds <= {"room-loop", "compact-loop",
                                  "hall-shuttle", "doorway-shuttle"})

    def test_guard_recesses_are_rare_mirrored_and_own_their_ambusher(self):
        corridor = Room(20, 20, 12, 4)
        tiles = [WALL] * (GRID * GRID)
        things = [0] * len(tiles)
        for y in range(corridor.y, corridor.y + corridor.h):
            for x in range(corridor.x, corridor.x + corridor.w):
                tiles[y * GRID + x] = FLOOR
        recesses = generator._carve_guard_recesses(
            tiles, things, [Room(3, 3, 7, 7), corridor],
            [RoomSpec("start", "standard", 0),
             RoomSpec("circulation", "corridor", 0)],
            ["start", "circulation"], set(), random.Random(2), (5, 5),
            Room(45, 45, 7, 7), chance=1.0)
        self.assertEqual(len(recesses), 1)
        first, second = recesses[0].cells
        self.assertEqual(first[0], second[0])
        self.assertEqual({first[1], second[1]},
                         {corridor.y - 1, corridor.y + corridor.h})

        realized = None
        for seed in range(12):
            level = _generate_with_retries(CampaignConfig(seed=seed), 1,
                                           attempts=10)
            if level.guard_recesses:
                realized = level
                break
        self.assertIsNotNone(realized)
        recess = realized.guard_recesses[0]
        self.assertIn(_at(realized.things, *recess.actor_cell),
                      generator.ENEMY_CODES)
        self.assertTrue(any(encounter.template == "blind-corner-ambush"
                            and recess.actor_cell in {
                                (x, y) for x, y, _ in encounter.cells}
                            for encounter in realized.encounters))

    def test_flags_are_always_purposefully_wall_backed(self):
        room = Room(10, 10, 10, 8)
        identity = generator.RoomIdentity("beat", "standard", "spine", 0,
                                          "garrison", "checkpoint", "guardpost")
        found = False
        for seed in range(12):
            tiles = [WALL] * (GRID * GRID)
            for y in range(room.y, room.y + room.h):
                for x in range(room.x, room.x + room.w):
                    tiles[y * GRID + x] = FLOOR
            things = [0] * len(tiles)
            _place_decorations([room], tiles, things, set(), room.center,
                               random.Random(seed), identities=[identity])
            flags = [(index % GRID, index // GRID)
                     for index, item in enumerate(things) if item == 62]
            found |= bool(flags)
            for x, y in flags:
                outside = []
                if x == room.x:
                    outside.append((x - 1, y))
                if x == room.x + room.w - 1:
                    outside.append((x + 1, y))
                if y == room.y:
                    outside.append((x, y - 1))
                if y == room.y + room.h - 1:
                    outside.append((x, y + 1))
                self.assertTrue(any(not _is_floor(_at(tiles, *cell))
                                    and _at(tiles, *cell) not in generator.DOORS
                                    for cell in outside))
        self.assertTrue(found)

    def test_rooms_never_mix_green_and_standard_barrels(self):
        """The two native barrel actors are alternate prop languages, not a
        matched set.  A storage room may repeat either family but must not
        scatter both old-num 24 and 58 into the same composition."""
        observed = set()
        for concept in ("storage", "supply-cache"):
            for seed in range(48):
                room = Room(20, 20, 12, 10)
                tiles = [WALL] * (GRID * GRID)
                things = [0] * len(tiles)
                for y in range(room.y, room.y + room.h):
                    for x in range(room.x, room.x + room.w):
                        generator._set(tiles, x, y, FLOOR)
                identity = generator.RoomIdentity(
                    "beat", "standard", "spine", 0,
                    "storehouse", concept, "storage")
                _place_decorations(
                    [room], tiles, things, set(), room.center,
                    random.Random(seed), identities=[identity], density=1.3,
                    traversal_pair_chance=1.0)
                room_items = {
                    _at(things, x, y)
                    for y in range(room.y, room.y + room.h)
                    for x in range(room.x, room.x + room.w)
                }
                observed.update(room_items & {24, 58})
                self.assertFalse(
                    {24, 58} <= room_items,
                    f"{concept} seed={seed} mixed both barrel families")
        self.assertEqual(observed, {24, 58})

    def test_vases_are_singular_wall_backed_accents(self):
        """Vase 35 is a single wall-side accent, never a freestanding pair
        or room-filling furniture family."""
        found = False
        for concept in ("guardpost", "grand", "lounge",
                        "burial-chamber", "dining-hall"):
            for seed in range(40):
                room = Room(20, 20, 12, 10)
                tiles = [WALL] * (GRID * GRID)
                things = [0] * len(tiles)
                for y in range(room.y, room.y + room.h):
                    for x in range(room.x, room.x + room.w):
                        generator._set(tiles, x, y, FLOOR)
                identity = generator.RoomIdentity(
                    "beat", "standard", "spine", 0,
                    "garrison", concept, concept)
                _place_decorations(
                    [room], tiles, things, set(), room.center,
                    random.Random(seed), identities=[identity], density=1.3,
                    traversal_pair_chance=1.0)
                vases = [
                    (x, y)
                    for y in range(room.y, room.y + room.h)
                    for x in range(room.x, room.x + room.w)
                    if _at(things, x, y) == 35
                ]
                found |= bool(vases)
                self.assertLessEqual(
                    len(vases), 1,
                    f"{concept} seed={seed} placed {len(vases)} vases")
                for x, y in vases:
                    outward = []
                    if x == room.x:
                        outward.append((x - 1, y))
                    if x == room.x + room.w - 1:
                        outward.append((x + 1, y))
                    if y == room.y:
                        outward.append((x, y - 1))
                    if y == room.y + room.h - 1:
                        outward.append((x, y + 1))
                    self.assertTrue(
                        any(not _is_floor(_at(tiles, *cell))
                            and _at(tiles, *cell) not in generator.DOORS
                            for cell in outward),
                        f"{concept} seed={seed} vase at {(x, y)} is not wall-backed")
        self.assertTrue(found, "test never exercised vase decoration")

    def test_sky_vistas_are_visible_in_one_tile_recesses(self):
        """A vista occupies a shallow exterior bay: the old wall plane is
        walkable recess floor, the next plane is one contiguous SKY run, and
        a balanced pillar pair frames rather than completely hides the view."""
        room = Room(20, 20, 12, 10)
        realized = None
        for seed in range(160):
            tiles = [WALL] * (GRID * GRID)
            things = [0] * len(tiles)
            for y in range(room.y, room.y + room.h):
                for x in range(room.x, room.x + room.w):
                    generator._set(tiles, x, y, FLOOR)
            identity = generator.RoomIdentity(
                "hub", "anchor", "courtyard", 0,
                "grand-halls", "courtyard", "grand")
            _place_decorations(
                [room], tiles, things, set(), room.center,
                random.Random(seed), identities=[identity], density=1.0,
                landmark_frame_chance=0.0, traversal_pair_chance=0.0)
            sky = {(index % GRID, index // GRID)
                   for index, tile in enumerate(tiles) if tile == 16}
            if sky:
                realized = tiles, things, sky, seed
                break
        self.assertIsNotNone(realized, "test never exercised a sky vista")
        tiles, things, sky, seed = realized

        side_options = (
            ((0, -1), {(x, room.y - 2)
                       for x in range(room.x, room.x + room.w)}),
            ((0, 1), {(x, room.y + room.h + 1)
                      for x in range(room.x, room.x + room.w)}),
            ((-1, 0), {(room.x - 2, y)
                       for y in range(room.y, room.y + room.h)}),
            ((1, 0), {(room.x + room.w + 1, y)
                      for y in range(room.y, room.y + room.h)}),
        )
        matches = [(outward, plane) for outward, plane in side_options
                   if sky <= plane]
        self.assertEqual(len(matches), 1,
                         f"seed={seed} SKY is not on the recessed outer plane")
        outward, _ = matches[0]
        dx, dy = outward
        varying = sorted(x if dy else y for x, y in sky)
        self.assertGreaterEqual(len(varying), 3)
        self.assertEqual(varying, list(range(varying[0], varying[-1] + 1)))

        recess = {(x - dx, y - dy) for x, y in sky}
        room_edge = {(x - 2 * dx, y - 2 * dy) for x, y in sky}
        self.assertTrue(all(_is_floor(_at(tiles, *cell)) for cell in recess))
        self.assertTrue(all(_is_floor(_at(tiles, *cell)) for cell in room_edge))
        supports = [cell for cell in recess if _at(things, *cell) == 30]
        support_axis = sorted(x if dy else y for x, y in supports)
        expected_support_axis = ([varying[0], varying[-1]]
                                 if len(varying) < 9
                                 else [varying[0], varying[len(varying) // 2],
                                       varying[-1]])
        self.assertEqual(support_axis, expected_support_axis)
        self.assertTrue(any(_at(things, *cell) != 30 for cell in recess),
                        "pillar supports completely hide the sky vista")
        self.assertTrue(all(_at(things, *cell) in (0, 30) for cell in recess),
                        "later alcove decoration polluted the vista recess")

    def test_theme_bias_is_strong_but_preserves_campaign_contrast(self):
        mixed = sum(variant.name == "catacombs" for seed in range(64)
                    for variant in generator._variant_sequence(
                        CampaignConfig(seed=seed))[:8])
        biased_sequences = [generator._variant_sequence(CampaignConfig(
            seed=seed, theme_bias=ThemeBias.CATACOMBS)) for seed in range(64)]
        biased = sum(variant.name == "catacombs" for variants in biased_sequences
                     for variant in variants[:8])
        self.assertGreater(biased, mixed)
        self.assertTrue(all(all(first.name != second.name
                                for first, second in zip(variants, variants[1:8]))
                            for variants in biased_sequences))
        self.assertTrue(all(variants[-2].name == "stronghold"
                            and variants[-1].name == "vault"
                            for variants in biased_sequences))

    def test_structural_room_columns_are_never_singletons(self):
        room = Room(10, 10, 12, 10)
        tiles = [WALL] * (GRID * GRID)
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR
        generator._add_pillars(tiles, room, random.Random(0), chance=1.0)
        columns = [(x, y) for y in range(room.y + 1, room.y + room.h - 1)
                   for x in range(room.x + 1, room.x + room.w - 1)
                   if _at(tiles, x, y) == WALL]
        self.assertEqual(len(columns), 2)
        cx, cy = room.center
        self.assertTrue(columns[0][0] + columns[1][0] == 2 * cx
                        or columns[0][1] + columns[1][1] == 2 * cy)

    def test_opposing_landmark_frames_match_and_use_only_centers(self):
        room = Room(10, 10, 12, 8)
        tiles = [WALL] * (GRID * GRID)
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR
        landmarks = {0: [(13, 9), (16, 9), (19, 9),
                         (13, 18), (16, 18), (19, 18)]}
        things = [0] * len(tiles)
        identity = generator.RoomIdentity("climax", "anchor", "spine", 0,
                                          "grand-halls", "war-room", "grand")
        _place_decorations([room], tiles, things, set(), room.center, random.Random(2),
                           landmarks=landmarks, identities=[identity],
                           landmark_frame_chance=1.0)
        framed = [(15, 10), (17, 10), (15, 17), (17, 17)]
        self.assertEqual(len({_at(things, *cell) for cell in framed}), 1)
        self.assertNotEqual(_at(things, *framed[0]), 0)
        for cell in ((12, 10), (14, 10), (18, 10), (20, 10),
                     (12, 17), (14, 17), (18, 17), (20, 17)):
            self.assertEqual(_at(things, *cell), 0)

    def test_kitchen_appliances_are_wall_backed_spaced_and_sink_is_optional(self):
        room = Room(10, 10, 10, 8)
        identity = generator.RoomIdentity("relief", "standard", "spine", 0,
                                          "quarters", "mess-kitchen", "lounge")
        sink_presence = set()
        # Exercise several RNG streams: the contract is that a sink is an
        # optional signature, not that two particular seeds consume random
        # draws in a permanently fixed order.
        for seed in range(4):
            tiles = [WALL] * (GRID * GRID)
            for y in range(room.y, room.y + room.h):
                for x in range(room.x, room.x + room.w):
                    tiles[y * GRID + x] = FLOOR
            things = [0] * len(tiles)
            _place_decorations([room], tiles, things, set(), room.center,
                               random.Random(seed), identities=[identity])
            self.assertEqual(things.count(68), 1)
            sink_presence.add(things.count(33) == 1)
            kitchen = [(index % GRID, index // GRID)
                       for index, item in enumerate(things)
                       if item in (33, 38, 67, 68)]
            for x, y in kitchen:
                outside = []
                if x == room.x:
                    outside.append((x - 1, y))
                if x == room.x + room.w - 1:
                    outside.append((x + 1, y))
                if y == room.y:
                    outside.append((x, y - 1))
                if y == room.y + room.h - 1:
                    outside.append((x, y + 1))
                self.assertTrue(outside)
                self.assertTrue(any(not _is_floor(_at(tiles, *cell))
                                    for cell in outside))
            for first, second in combinations(kitchen, 2):
                self.assertGreaterEqual(abs(first[0] - second[0])
                                        + abs(first[1] - second[1]), 4)
        self.assertEqual(sink_presence, {False, True})

    def test_suits_of_armor_are_wall_backed(self):
        room = Room(10, 10, 10, 8)
        tiles = [WALL] * (GRID * GRID)
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR
        things = [0] * len(tiles)
        identity = generator.RoomIdentity("climax", "anchor", "spine", 0,
                                          "grand-halls", "war-room", "grand")
        _place_decorations([room], tiles, things, set(), room.center,
                           random.Random(2), identities=[identity])

        armor = [(index % GRID, index // GRID)
                 for index, item in enumerate(things) if item == 39]
        self.assertTrue(armor)
        for x, y in armor:
            outside = []
            if x == room.x:
                outside.append((x - 1, y))
            if x == room.x + room.w - 1:
                outside.append((x + 1, y))
            if y == room.y:
                outside.append((x, y - 1))
            if y == room.y + room.h - 1:
                outside.append((x, y + 1))
            self.assertTrue(outside)
            self.assertTrue(any(not _is_floor(_at(tiles, *cell))
                                and _at(tiles, *cell) not in generator.DOORS
                                for cell in outside))

    def test_room_profiles_have_authored_symmetry_but_never_dominate(self):
        rooms = [Room(3 + (index % 4) * 14, 3 + (index // 4) * 16, 10, 10)
                 for index in range(8)]
        tiles = [WALL] * (GRID * GRID)
        for room in rooms:
            for y in range(room.y, room.y + room.h):
                for x in range(room.x, room.x + room.w):
                    tiles[y * GRID + x] = FLOOR
        anchors, shapes = generator._carve_symmetric_profiles(
            tiles, rooms, random.Random(4), chance=1.0, max_rooms=2)
        self.assertEqual(len(shapes), 2)
        self.assertEqual(set(anchors), set(shapes))
        self.assertLessEqual(len(shapes), len(rooms) // 4)
        horizontal_families = {
            "stepped-cross", "shallow-t", "paired-side-bays",
            "paired-end-bays",
        }
        vertical_families = {
            "stepped-cross", "paired-side-bays", "offset-side-bay",
            "paired-end-bays",
        }
        for room_index in shapes:
            room = rooms[room_index]
            carved = {(x, y) for y in range(room.y, room.y + room.h)
                      for x in range(room.x, room.x + room.w)
                      if _at(tiles, x, y) == WALL}
            self.assertTrue(carved)
            for x, y in carved:
                if shapes[room_index] in horizontal_families:
                    self.assertIn((2 * room.x + room.w - 1 - x, y), carved)
                if shapes[room_index] in vertical_families:
                    self.assertIn((x, 2 * room.y + room.h - 1 - y), carved)

    def test_spears_are_wall_backed_armory_displays(self):
        room = Room(10, 10, 12, 10)
        tiles = [WALL] * (GRID * GRID)
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR
        things = [0] * len(tiles)
        identity = generator.RoomIdentity("beat", "standard", "spine", 0,
                                          "garrison", "armory", "guardpost")
        _place_decorations([room], tiles, things, set(), room.center,
                           random.Random(2), identities=[identity])
        spears = [(index % GRID, index // GRID)
                  for index, item in enumerate(things) if item == 69]
        self.assertTrue(spears)
        for x, y in spears:
            self.assertTrue(x in (room.x, room.x + room.w - 1)
                            or y in (room.y, room.y + room.h - 1))

    def test_each_room_uses_at_most_one_lighting_family(self):
        room = Room(10, 10, 12, 10)
        identity = generator.RoomIdentity("climax", "anchor", "spine", 0,
                                          "grand-halls", "trophy-hall", "grand")
        realized = set()
        for seed in range(12):
            tiles = [WALL] * (GRID * GRID)
            for y in range(room.y, room.y + room.h):
                for x in range(room.x, room.x + room.w):
                    tiles[y * GRID + x] = FLOOR
            things = [0] * len(tiles)
            families, _ = _place_decorations(
                [room], tiles, things, set(), room.center, random.Random(seed),
                identities=[identity])
            family = families[0]
            fixtures = {item for item in things if item in generator.LIGHTING_ITEMS}
            self.assertLessEqual(fixtures, generator.LIGHTING_FAMILY_ITEMS[family])
            realized.add(family)
        self.assertGreaterEqual(len(realized), 2)

    def test_vines_only_form_complete_room_screens(self):
        room = Room(10, 10, 12, 10)
        identity = generator.RoomIdentity("climax", "anchor", "spine", 0,
                                          "grand-halls", "courtyard", "grand")
        found = None
        for seed in range(40):
            tiles = [WALL] * (GRID * GRID)
            for y in range(room.y, room.y + room.h):
                for x in range(room.x, room.x + room.w):
                    tiles[y * GRID + x] = FLOOR
            things = [0] * len(tiles)
            _, screens = _place_decorations(
                [room], tiles, things, set(), room.center, random.Random(seed),
                identities=[identity])
            if screens:
                found = screens[0]
                break
        self.assertIsNotNone(found)
        self.assertEqual(found.kind, "room-divider")
        xs = {x for x, _ in found.cells}
        ys = {y for _, y in found.cells}
        self.assertTrue((len(xs) == 1 and ys == set(range(room.y, room.y + room.h)))
                        or (len(ys) == 1
                            and xs == set(range(room.x, room.x + room.w))))

    def test_hallway_vines_fill_a_longitudinal_run_not_one_cell(self):
        tiles = [WALL] * (GRID * GRID)
        things = [0] * len(tiles)
        path = [(x, 20) for x in range(10, 25)]
        for cell in path:
            tiles[cell[1] * GRID + cell[0]] = FLOOR
        _, screens = _place_decorations(
            [], tiles, things, set(), path[0], random.Random(1), paths=[path],
            hallway_vine_budget=1)
        self.assertEqual(len(screens), 1)
        self.assertEqual(screens[0].kind, "hallway-run")
        self.assertEqual(screens[0].cells, tuple(path[1:-1]))
        self.assertGreaterEqual(len(screens[0].cells), 3)

    def test_guard_gallery_is_symmetric_inaccessible_and_pickup_free(self):
        room = Room(10, 10, 8, 10)
        tiles = [WALL] * (GRID * GRID)
        things = [0] * len(tiles)
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR
        for y in range(7, room.y + 1):
            tiles[y * GRID + room.center[0]] = FLOOR
        identity = generator.RoomIdentity(
            "branch", "standard", "spine", 0, "garrison", "war-room", "grand")
        start_identity = generator.RoomIdentity(
            "start", "standard", "spine", 0, "garrison", "guardpost", "guardpost")
        reserved = set()
        galleries = generator._place_guard_gallery(
            tiles, things, [Room(2, 2, 3, 3), room], [start_identity, identity],
            ["rectangle", "rectangle"], reserved, random.Random(0),
            (room.center[0], 7), frozenset({1}))
        self.assertEqual(len(galleries), 1)
        gallery = galleries[0]
        self.assertEqual(len(gallery.screen), room.w)
        self.assertTrue(all(_at(things, *cell) == 30 for cell in gallery.screen))
        encounters = []
        generator._populate_guard_galleries(
            galleries, things, 5, random.Random(0), encounters)
        self.assertEqual(encounters[0].template, "guard-gallery")
        self.assertEqual({(x, y) for x, y, _ in encounters[0].cells},
                         set(gallery.actor_cells))
        reachable = generator._reachable(
            tiles, (room.center[0], 7), locked_open=True,
            blocked=set(gallery.screen))
        self.assertFalse(set(gallery.rear_cells) & reachable)
        self.assertFalse(any(_at(things, *cell) in generator.PICKUP_CODES
                             for cell in gallery.rear_cells))

    def test_seed_3332_floor_one_balances_room_concepts_and_caps_kitchens(self):
        level = _generate_with_retries(CampaignConfig(seed=3332), 1, attempts=3)
        self.assertEqual(level.variant, "grand-halls")
        self.assertEqual(level.room_concepts.count("mess-kitchen"), 1)
        self.assertLessEqual(level.things.count(68), 1)
        self.assertLessEqual(level.things.count(33), 1)
        self.assertFalse(any(level.room_concepts[first] == level.room_concepts[second]
                             for first, second in level.edges))

    def test_every_in_room_pickup_has_authored_provenance(self):
        level = _generate_with_retries(CampaignConfig(seed=3332), 1, attempts=3)
        tracked = {(x, y): item for placement in level.pickup_placements
                   for x, y, item in placement.cells
                   if any(room.x <= x < room.x + room.w
                          and room.y <= y < room.y + room.h
                          for room in level.rooms)}
        expected = {(index % GRID, index // GRID): item
                    for index, item in enumerate(level.things)
                    if item in generator.PICKUP_CODES
                    and any(room.x <= index % GRID < room.x + room.w
                            and room.y <= index // GRID < room.y + room.h
                            for room in level.rooms)}
        self.assertEqual(tracked, expected)
        self.assertTrue(all(placement.template in
                            generator.AUTHORED_PICKUP_TEMPLATES
                            for placement in level.pickup_placements))
        route = set(level.critical_route)
        self.assertTrue(all(placement.room_index in route
                            for placement in level.pickup_placements
                            if placement.reason == "route-ammo"))
        self.assertTrue(all(placement.reason in
                            {"exploration-treasure", "secret-reward"}
                            for placement in level.pickup_placements
                            if any(item in generator.TREASURE
                                   for _, _, item in placement.cells)))

    def test_secret_pickups_have_explicit_cache_provenance(self):
        level = generate_map(CampaignConfig(seed=0), 4)
        caches = [placement for placement in level.pickup_placements
                  if placement.template == "secret-cache"]
        self.assertEqual(len(caches), 1)
        self.assertEqual(caches[0].room_index, -1)
        self.assertEqual(sum(detail.reward_count for detail in level.secret_details),
                         len(caches[0].cells))

    def test_wall_pickup_templates_are_actually_wall_backed(self):
        level = _generate_with_retries(CampaignConfig(seed=3332), 1, attempts=3)
        wall_templates = {"wall-cache", "entry-staging", "recovery-station",
                          "treasure-display", "corner-cache"}
        for placement in level.pickup_placements:
            if placement.template not in wall_templates:
                continue
            room = level.rooms[placement.room_index]
            for x, y, _ in placement.cells:
                outside = []
                if x == room.x:
                    outside.append((x - 1, y))
                if x == room.x + room.w - 1:
                    outside.append((x + 1, y))
                if y == room.y:
                    outside.append((x, y - 1))
                if y == room.y + room.h - 1:
                    outside.append((x, y + 1))
                self.assertTrue(any(not _is_floor(_at(level.tiles, *cell))
                                    and _at(level.tiles, *cell) not in generator.DOORS
                                    for cell in outside))

    def test_dog_food_is_contextual_and_capped(self):
        rooms = [Room(3, 3, 8, 8), Room(18, 3, 10, 10), Room(36, 3, 10, 10),
                 Room(50, 3, 9, 9)]
        tiles = [FLOOR] * (GRID * GRID)
        things = [0] * len(tiles)
        things[rooms[0].center[1] * GRID + rooms[0].center[0]] = generator.PLAYER_START
        with mock.patch.object(generator, "ENEMY_FAMILIES", (("dog", DOGS, 1, 0.5),)):
            generator._place_population(
                CampaignConfig(seed=1, guard_density=Intensity.HIGH), 4, rooms,
                tiles, things, {rooms[0].center}, random.Random(4), rooms[0].center,
                rooms[-1], patrol_chance=0.0)
        food_cells = [(index % GRID, index // GRID) for index, item in enumerate(things)
                      if item == generator.DOG_FOOD]
        self.assertLessEqual(len(food_cells), 3)
        self.assertTrue(food_cells)
        dog_codes = {code for code, family in generator.FAMILY_BY_CODE.items()
                     if family == DOGS}
        for food in food_cells:
            owner = next(room for room in rooms
                         if room.x <= food[0] < room.x + room.w
                         and room.y <= food[1] < room.y + room.h)
            dogs = [(index % GRID, index // GRID) for index, item in enumerate(things)
                    if item in dog_codes and owner.x <= index % GRID < owner.x + owner.w
                    and owner.y <= index // GRID < owner.y + owner.h]
            self.assertTrue(dogs)
            self.assertLessEqual(min(abs(food[0] - x) + abs(food[1] - y)
                                     for x, y in dogs), 4)

    def test_bespoke_secret_shapes_are_sealed_three_item_caches(self):
        reward_codes = {AMMO, generator.FOOD, FIRST_AID, MACHINE_GUN, CHAINGUN,
                        ONE_UP, *generator.TREASURE}
        for variant in ("square", "vault", "reliquary", "gallery", "nested"):
            with self.subTest(variant=variant):
                px, py = 20, 30
                tiles = [WALL] * (GRID * GRID)
                things = [0] * len(tiles)
                tiles[py * GRID + px - 1] = FLOOR
                protected = set()
                reward = generator._carve_secret_pocket(
                    tiles, things, px, py, random.Random(3), False, variant, 0.7,
                    reward_quality=3, protected=protected)
                self.assertIsNotNone(reward)
                self.assertEqual(sum(item in reward_codes for item in things), 3)
                self.assertEqual(things.count(PUSHWALL), 2 if variant == "nested" else 1)
                closed = _reachable(tiles, (px - 1, py), locked_open=True)
                self.assertNotIn((px + 1, py), closed)
                pushwalls = {(index % GRID, index // GRID) for index, item in enumerate(things)
                             if item == PUSHWALL}
                opened = _reachable(tiles, (px - 1, py), locked_open=True,
                                    extra_passable=pushwalls,
                                    blocked={(x + 2, y) for x, y in pushwalls})
                self.assertIn(reward, opened)

    def test_floor_nine_secrets_are_boss_preparation_caches(self):
        px, py = 20, 30
        tiles = [WALL] * (GRID * GRID)
        things = [0] * len(tiles)
        tiles[py * GRID + px - 1] = FLOOR
        reward = generator._carve_secret_pocket(
            tiles, things, px, py, random.Random(9), False, "square", 0.7,
            reward_quality=3, number=9)
        self.assertIsNotNone(reward)
        rewards = [item for item in things
                   if item in {AMMO, generator.FOOD, FIRST_AID, MACHINE_GUN,
                               CHAINGUN, ONE_UP, *generator.TREASURE}]
        self.assertEqual(len(rewards), 7)
        self.assertEqual(rewards.count(AMMO), 4)
        self.assertIn(FIRST_AID, rewards)
        self.assertTrue(any(item in (MACHINE_GUN, CHAINGUN) for item in rewards))

    def test_generated_floor_nine_carries_each_boss_secret_budget(self):
        level = _generate_with_retries(CampaignConfig(seed=909), 9, attempts=8)
        secret_count = len(level.secret_rewards)
        self.assertGreaterEqual(level.things.count(AMMO), 4 * secret_count)
        self.assertGreaterEqual(
            sum(item in (MACHINE_GUN, CHAINGUN) for item in level.things),
            secret_count)

    def test_call_apogee_is_absent_from_all_decoration_registries(self):
        self.assertNotIn(63, generator.STATIC_BLOCKING)
        self.assertNotIn(63, generator.STATIC_OPEN)
        self.assertFalse(any(63 in pool for pool in generator._DECOR_BLOCKING.values()))
        self.assertFalse(any(63 in pool for pool in generator._DECOR_OPEN.values()))

    def test_snap_offsets_prefers_center_then_flush_edges(self):
        parent = Room(10, 10, 12, 10)
        offsets = _snap_offsets(parent, rw=6, rh=4, side=(1, 0),
                                rng=random.Random(7))
        # Horizontal attachment aligns the child on the parent's y-axis:
        # centre first, then its two possible edge-flush positions.  The
        # random fallback offsets must not displace those architectural picks.
        self.assertEqual(offsets[0], 0)
        self.assertEqual(set(offsets[1:3]), {-3, 3})
        self.assertEqual(len(offsets), len(set(offsets)))

    def test_adjacent_rooms_usually_align_or_flush(self):
        aligned = candidates = 0
        for seed in range(4):
            config = CampaignConfig(seed=seed)
            rng = random.Random(config.floor_seed(5, 0))
            plan = _plan_floor(rng, int(config.layout_complexity), 5)
            placed = _place_planned_rooms(rng, plan, 5)
            for first, second in combinations(placed.rooms, 2):
                horizontal_gap = max(first.x, second.x) - min(first.x + first.w,
                                                               second.x + second.w)
                vertical_gap = max(first.y, second.y) - min(first.y + first.h,
                                                             second.y + second.h)
                if 1 <= horizontal_gap <= 3 and vertical_gap < 0:
                    candidates += 1
                    aligned += (first.center[1] == second.center[1]
                                or first.y == second.y
                                or first.y + first.h == second.y + second.h)
                elif 1 <= vertical_gap <= 3 and horizontal_gap < 0:
                    candidates += 1
                    aligned += (first.center[0] == second.center[0]
                                or first.x == second.x
                                or first.x + first.w == second.x + second.w)
        self.assertGreater(candidates, 0)
        self.assertGreaterEqual(aligned / candidates, 0.5)

    def test_circulation_skeletons_vary_without_campaign_repeats(self):
        observed = set()
        for seed in range(12):
            skeletons = generator._circulation_sequence(CampaignConfig(seed=seed))
            observed.update(skeletons)
            self.assertFalse(any(first == second
                                 for first, second in zip(skeletons, skeletons[1:])))
        self.assertEqual(observed, set(generator.CIRCULATION_SKELETONS))

    def test_floor_plans_use_real_corridor_nodes_and_varied_district_modes(self):
        observed_modes = set()
        for skeleton in generator.CIRCULATION_SKELETONS:
            rng = random.Random(100 + len(observed_modes))
            plan = _plan_floor(rng, 3, 5, skeleton=skeleton)
            placed = _place_planned_rooms(rng, plan, 5)
            specs = [plan.specs[index] for index in placed.spec_indices]
            corridors = [index for index, spec in enumerate(specs)
                         if spec.tier == "corridor"]
            self.assertGreaterEqual(len(corridors), 2)
            self.assertTrue(all(
                (min(placed.rooms[index].w, placed.rooms[index].h) == 3
                 and max(placed.rooms[index].w, placed.rooms[index].h) >= 5)
                if specs[index].motif == "hallway-arm" else
                max(placed.rooms[index].w, placed.rooms[index].h)
                >= 2 * min(placed.rooms[index].w, placed.rooms[index].h)
                for index in corridors))
            mediated = sum(first in corridors or second in corridors
                           for first, second in placed.edges) / len(placed.edges)
            self.assertGreaterEqual(mediated, 0.20)
            observed_modes.update(plan.district_circulation)
        self.assertGreaterEqual(len(observed_modes), 4)

    def test_layout_complexity_controls_planned_room_count_without_larger_rooms(self):
        expected_targets = {
            Intensity.VERY_LOW: 16,
            Intensity.LOW: 18,
            Intensity.NORMAL: 20,
            Intensity.HIGH: 22,
            Intensity.VERY_HIGH: 24,
        }
        for intensity, target in expected_targets.items():
            rng = random.Random(700 + int(intensity))
            plan = _plan_floor(rng, int(intensity), 5)
            self.assertEqual(len(plan.specs), target)
        floor_ten = _plan_floor(random.Random(710), int(Intensity.NORMAL), 10)
        self.assertEqual(len(floor_ten.specs), 24)

        for seed in range(64):
            width, height = generator._room_size(random.Random(seed), "standard", 5)
            self.assertIn(width, range(6, 10))
            self.assertIn(height, range(6, 10))

    def test_authored_corridor_rooms_are_three_tiles_wide(self):
        """Authored hallway nodes use a three-tile minor axis so their
        centered doorways and fixtures have a real centerline."""
        for seed in range(128):
            width, height = generator._room_size(
                random.Random(seed), "corridor", 5)
            major, minor = max(width, height), min(width, height)
            self.assertGreaterEqual(major, 8)
            self.assertEqual(minor, 3)

    def test_carved_connectors_are_either_one_or_three_tiles_wide(self):
        """Optional widening adds both shoulders atomically.  This preserves
        a deliberate 1/3-tile corridor vocabulary instead of producing the
        generator-looking two-tile strips used by older releases."""
        path = [(x, 20) for x in range(8, 24)]
        for widen_chance, expected_width in ((0.0, 1), (1.0, 3)):
            with self.subTest(widen_chance=widen_chance):
                tiles = [WALL] * (GRID * GRID)
                for cell in path:
                    generator._set(tiles, *cell, FLOOR)
                generator._widen_corridors(
                    tiles, [], [path], random.Random(4),
                    widen_chance=widen_chance)
                for x, y in path[2:-2]:
                    width = sum(_is_floor(_at(tiles, x, y + offset))
                                for offset in (-1, 0, 1))
                    self.assertEqual(width, expected_width)

    def test_door_facing_room_dimensions_prefer_odd_sizes(self):
        """The dimension parallel to a room's connecting wall should usually
        be odd, giving the doorway a centered architectural axis without
        forcing every room into the same rectangular proportions."""
        odd = total = 0
        for seed in range(48):
            rng = random.Random(1700 + seed)
            plan = _plan_floor(rng, int(Intensity.NORMAL), 5,
                               skeleton="central-axis")
            placed = _place_planned_rooms(rng, plan, 5)
            for first, second in placed.edges:
                # The later authored spec is the child whose size was oriented
                # against this attachment during room realization.
                child = max((first, second),
                            key=lambda index: placed.spec_indices[index])
                parent = second if child == first else first
                child_spec = plan.specs[placed.spec_indices[child]]
                if child_spec.tier in ("corridor", "motif"):
                    continue
                parent_room = placed.rooms[parent]
                child_room = placed.rooms[child]
                dx = child_room.center[0] - parent_room.center[0]
                dy = child_room.center[1] - parent_room.center[1]
                door_facing_dimension = (child_room.h if abs(dx) >= abs(dy)
                                         else child_room.w)
                odd += door_facing_dimension % 2
                total += 1
        self.assertGreater(total, 300)
        self.assertGreater(odd / total, 0.62)

    def test_normal_layouts_recover_local_fillers_instead_of_staying_sparse(self):
        realized_counts = []
        realization_ratios = []
        for seed in range(12):
            config = CampaignConfig(seed=seed)
            for floor in (1, 3, 5, 7):
                rng = random.Random(config.floor_seed(floor, 0))
                plan = _plan_floor(rng, int(config.layout_complexity), floor)
                placed = _place_planned_rooms(rng, plan, floor)
                realized_counts.append(len(placed.rooms))
                realization_ratios.append(len(placed.rooms) / len(plan.specs))
                self.assertTrue(all(
                    0 <= first < len(placed.rooms) and 0 <= second < len(placed.rooms)
                    for first, second in placed.edges))

        self.assertGreaterEqual(sum(realized_counts) / len(realized_counts), 17.5)
        self.assertGreaterEqual(
            sum(realization_ratios) / len(realization_ratios), 0.875)

    def test_mirrored_notches_produce_symmetric_bites(self):
        room = Room(20, 20, 10, 10)
        tiles = [WALL] * (GRID * GRID)
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR
        anchors = _carve_notches(tiles, [room], random.Random(3), chance=1.0)
        self.assertIn(len(anchors[0]), (2, 4))
        mirror_x = all(_at(tiles, x, y) == _at(tiles, 2 * room.x + room.w - 1 - x, y)
                       for y in range(room.y, room.y + room.h)
                       for x in range(room.x, room.x + room.w))
        mirror_y = all(_at(tiles, x, y) == _at(tiles, x, 2 * room.y + room.h - 1 - y)
                       for y in range(room.y, room.y + room.h)
                       for x in range(room.x, room.x + room.w))
        self.assertTrue(mirror_x or mirror_y)
        cx, cy = room.center
        self.assertTrue(all(_at(tiles, x, cy) == FLOOR
                            for x in range(room.x, room.x + room.w)))
        self.assertTrue(all(_at(tiles, cx, y) == FLOOR
                            for y in range(room.y, room.y + room.h)))

    def test_mirrored_notches_receive_matching_decorations(self):
        room = Room(20, 20, 10, 10)
        tiles = [WALL] * (GRID * GRID)
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR
        anchors = _carve_notches(tiles, [room], random.Random(5), chance=1.0)
        things = [0] * len(tiles)
        identity = generator.RoomIdentity("beat", "standard", "spine", 0,
                                          "storehouse", "storage", "storage")
        _place_decorations([room], tiles, things, set(), room.center,
                           random.Random(5), identities=[identity],
                           notch_anchors=anchors)
        accents = [_at(things, *cell) for cell in anchors[0]]
        self.assertTrue(all(accents))
        self.assertEqual(len(set(accents)), 1)

    def test_aligned_rooms_get_a_straight_corridor(self):
        a, b = Room(10, 20, 6, 6), Room(30, 20, 6, 6)
        for seed in range(4):
            tiles = [WALL] * (GRID * GRID)
            for room in (a, b):
                for y in range(room.y, room.y + room.h):
                    for x in range(room.x, room.x + room.w):
                        tiles[y * GRID + x] = FLOOR
            path = _carve_connection(tiles, a, b, random.Random(seed), complexity=3,
                                     avoid=set())
            self.assertEqual(_path_bends(path), 0)

    def test_offset_rooms_get_a_single_elbow(self):
        a, b = Room(10, 10, 6, 6), Room(30, 30, 6, 6)
        for seed in range(4):
            tiles = [WALL] * (GRID * GRID)
            for room in (a, b):
                for y in range(room.y, room.y + room.h):
                    for x in range(room.x, room.x + room.w):
                        tiles[y * GRID + x] = FLOOR
            path = _carve_connection(tiles, a, b, random.Random(seed), complexity=3,
                                     avoid=set())
            self.assertLessEqual(_path_bends(path), 1)

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
                self.assertEqual(manifest["seed_source"], "LittleEntropyMachine")
                self.assertIn("commit", manifest)
                self.assertTrue(all(floor["validation"]["passed"] for floor in manifest["floors"]))
                settings = package.read("infiniwolf-settings.txt").decode("utf-8")
                self.assertIn(f"version = {manifest['version']}", settings)
                self.assertIn(f"commit = {manifest['commit']}", settings)
                self.assertIn(f"seed = {config.seed}", settings)
                for name, value in manifest["settings"].items():
                    if name != "seed":
                        self.assertIn(f"{name} = {value}", settings)
                self.assertIn("python3 -m infiniwolf --seed", settings)
                mapinfo = package.read("mapinfo.txt").decode("utf-8")
                self.assertIn("par =", mapinfo)
                # Without an explicit floornumber ECWolf shows "Floor 1" on
                # every status bar and score tally (its default is "1").
                for number in range(1, 11):
                    self.assertIn(f"floornumber = {number} ", mapinfo)
            self.assertEqual(validate_package(first)["seed"], config.seed)

    def test_hidden_switch_is_isolated_deterministic_and_reproducible(self):
        ordinary = _generate_with_retries(CampaignConfig(seed=404), 4)
        explicit = _generate_with_retries(
            CampaignConfig(seed=404, say_aardwolf=False), 4)
        first = _generate_with_retries(
            CampaignConfig(seed=404, say_aardwolf=True), 4)
        second = _generate_with_retries(
            CampaignConfig(seed=404, say_aardwolf=True), 4)
        self.assertEqual((ordinary.tiles, ordinary.things,
                          ordinary.layout_signature),
                         (explicit.tiles, explicit.things,
                          explicit.layout_signature))
        self.assertEqual((first.tiles, first.things, first.layout_signature),
                         (second.tiles, second.things,
                          second.layout_signature))
        self.assertNotEqual((ordinary.tiles, ordinary.things),
                            (first.tiles, first.things))
        variants = generator._variant_sequence(
            CampaignConfig(seed=404, say_aardwolf=True))
        skeletons = generator._circulation_sequence(
            CampaignConfig(seed=404, say_aardwolf=True))
        self.assertTrue(all(left.name != right.name
                            for left, right in zip(variants, variants[1:])))
        self.assertTrue(all(left != right
                            for left, right in zip(skeletons, skeletons[1:])))
        with tempfile.TemporaryDirectory() as directory:
            package_path = generate_campaign(
                CampaignConfig(seed=404, say_aardwolf=True),
                Path(directory) / "campaign.pk3")
            with zipfile.ZipFile(package_path) as package:
                manifest = json.loads(package.read(
                    "infiniwolf-manifest.json"))
                settings = package.read(
                    "infiniwolf-settings.txt").decode("utf-8")
            self.assertIs(manifest["settings"]["say_aardwolf"], True)
            self.assertIn("say_aardwolf = True", settings)
            self.assertIn("--say-aardwolf", settings)
            self.assertNotIn("--say-aardwolf True", settings)

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
        level = _generate_with_retries(
            CampaignConfig(seed=44), 3, secret_exit=True)
        self.assertEqual(level.tiles.count(SECRET_EXIT_ZONE), 1)
        index = level.tiles.index(SECRET_EXIT_ZONE)
        detail = next(detail for detail in level.secret_details if detail.secret_exit)
        direction = detail.push_direction
        # The modzone floor cell must face a real elevator switch: that pair
        # is what ECWolf rewrites into an Exit_Secret trigger.
        self.assertEqual(_at(level.tiles, index % 64 + direction, index // 64),
                         ELEVATOR_TILE)
        self.assertTrue(level.secret_rewards)
        zx, zy = index % GRID, index // GRID
        self.assertEqual(_at(level.tiles, zx - 2 * direction, zy),
                         generator.DOOR_ELEVATOR)
        self.assertTrue(all(_at(level.tiles, x, zy + side) == ELEVATOR_TILE
                            for x in range(zx - 1, zx + 2)
                            for side in (-1, 1)))
        self.assertTrue(all(not _is_floor(_at(level.tiles, *cell))
                            and _at(level.tiles, *cell) not in generator.DOORS
                            for cell in ({(zx + 2 * direction, zy + side)
                                         for side in range(-2, 3)}
                                         | {(x, zy + side)
                                            for x in range(zx - 2, zx + 3)
                                            for side in (-2, 2)})))
        closed = _reachable(level.tiles, level.start, locked_open=True)
        self.assertNotIn((zx, zy), closed)
        self.assertNotEqual(detail.shape, "square")
        self.assertGreaterEqual(detail.depth_ratio, 0.45)

    def test_lock_setting_changes_progression_content(self):
        # Seed 122, not 121: lock placement can legitimately come up empty on
        # a rare layout, and 121 rerolled into that case when floor variants
        # shifted the rng stream (a 1-in-20 outcome in the surrounding seeds).
        low = _generate_with_retries(
            CampaignConfig(seed=122, locked_doors=Intensity.VERY_LOW), 4)
        high = _generate_with_retries(
            CampaignConfig(seed=122, locked_doors=Intensity.VERY_HIGH), 4)
        self.assertEqual(low.locked_doors, 0)
        self.assertGreater(high.locked_doors, 0)
        self.assertIn(GOLD_KEY, high.things)
        self.assertIn(SILVER_KEY, high.things)
        self.assertEqual(high.key_order, ("gold", "silver"))
        key_index = high.things.index(GOLD_KEY)
        self.assertIn((key_index % 64, key_index // 64),
                      _reachable(high.tiles, high.start, locked_open=False))

    def test_seeded_lock_schedule_preserves_unlocked_floors_and_late_weighting(self):
        later = early = 0
        single_colors = set()
        for seed in range(32):
            schedule = generator._lock_schedule(CampaignConfig(seed=seed))
            self.assertFalse(schedule[9].colors)
            gated = [index + 1 for index, plan in enumerate(schedule[:8])
                     if plan.colors]
            self.assertGreaterEqual(8 - len(gated), 2)
            self.assertFalse(any(all(floor in gated for floor in range(start, start + 3))
                                 for start in range(1, 7)))
            early += sum(bool(schedule[index].colors) for index in range(4))
            later += sum(bool(schedule[index].colors) for index in range(4, 8))
            single_colors.update(plan.colors[0] for plan in schedule[:8]
                                 if len(plan.colors) == 1)
        self.assertGreater(later, early)
        self.assertEqual(single_colors, {"gold", "silver"})

    def test_dual_key_floor_requires_both_colors_in_order(self):
        level = generate_map(CampaignConfig(seed=0), 4)
        self.assertEqual(level.key_order, ("gold", "silver"))
        self.assertEqual(level.locked_doors, 2)
        self.assertIn(GOLD_KEY, level.things)
        self.assertIn(SILVER_KEY, level.things)
        closed = _reachable(level.tiles, level.start, locked_open=False)
        gold_open = _reachable(level.tiles, level.start, locked_open=False,
                               open_lock_codes=generator.GOLD_DOORS)
        both_open = _reachable(level.tiles, level.start, locked_open=False,
                               open_lock_codes=generator.LOCKED_DOORS)
        gold_index = level.things.index(GOLD_KEY)
        silver_index = level.things.index(SILVER_KEY)
        self.assertIn((gold_index % GRID, gold_index // GRID), closed)
        self.assertNotIn((silver_index % GRID, silver_index // GRID), closed)
        self.assertIn((silver_index % GRID, silver_index // GRID), gold_open)
        self.assertNotIn(level.exit_stand, gold_open)
        self.assertIn(level.exit_stand, both_open)
        self.assertEqual(tuple(objective.color for objective in level.key_objectives),
                         level.key_order)
        self.assertTrue(all(objective.detour >= 2
                            and objective.cell
                            != level.rooms[objective.host_room].center
                            for objective in level.key_objectives))
        self.assertEqual(len({objective.host_room
                              for objective in level.key_objectives}), 2)

    def test_boss_floor_can_add_silver_before_gold_and_floor_ten_stays_open(self):
        boss_floor = _generate_with_retries(CampaignConfig(seed=0), 9, attempts=5)
        self.assertEqual(boss_floor.key_order, ("silver", "gold"))
        self.assertEqual(boss_floor.locked_doors, 2)
        self.assertIn(SILVER_KEY, boss_floor.things)
        final_floor = _generate_with_retries(CampaignConfig(seed=0), 10, attempts=10)
        self.assertEqual(final_floor.locked_doors, 0)
        self.assertFalse(final_floor.key_order)
        self.assertNotIn(GOLD_KEY, final_floor.things)
        self.assertNotIn(SILVER_KEY, final_floor.things)

    def test_exit_uses_long_post_climax_route(self):
        level = _generate_with_retries(CampaignConfig(seed=3332), 1, attempts=3)
        self.assertGreaterEqual(level.exit_depth_ratio, 0.75)
        self.assertGreaterEqual(len(level.critical_route),
                                generator._minimum_critical_route_rooms(
                                    level.room_roles))

    def test_optional_density_does_not_make_exit_depth_impossible(self):
        spine = ("start", *("beat" for _ in range(7)),
                 "climax", "relief", "exit")
        roles = (*spine, *("branch" for _ in range(13)))
        self.assertEqual(generator._minimum_critical_route_rooms(roles), 10)

    def test_floor_nine_has_native_boss(self):
        level = _generate_with_retries(CampaignConfig(seed=909), 9, attempts=5)
        self.assertTrue(level.boss)
        self.assertEqual(sum(thing in BOSSES for thing in level.things), 1)
        self.assertNotIn(level.exit_stand, _reachable(level.tiles, level.start, locked_open=False))

    def test_boss_room_is_a_grand_purpose_built_arena(self):
        for seed in range(4):
            level = _generate_with_retries(CampaignConfig(seed=seed), 9, attempts=10)
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
        for seed in range(4):
            level = _generate_with_retries(CampaignConfig(seed=seed), 9, attempts=10)
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

    def test_boss_elevator_key_is_a_verified_native_boss_drop(self):
        seen_bosses = set()
        for seed in range(6):
            level = _generate_with_retries(CampaignConfig(seed=seed), 9, attempts=10)
            boss = next(thing for thing in level.things if thing in BOSSES)
            seen_bosses.add(boss)
            self.assertIn(boss, KEY_DROP_BOSSES)
            self.assertNotIn(GOLD_KEY, level.things)
            objective = next(objective for objective in level.key_objectives
                             if objective.color == "gold")
            self.assertEqual(objective.treatment, "boss-drop")
            self.assertEqual(_at(level.things, *objective.cell), boss)
        self.assertGreater(len(seen_bosses), 1)

    def test_fake_hitler_is_not_a_boss_and_only_spawns_on_floor_nine(self):
        self.assertNotIn(FAKE_HITLER, BOSSES)
        for number in range(1, 11):
            for seed in range(1):
                level = _generate_with_retries(CampaignConfig(seed=seed), number)
                if number != 9:
                    self.assertNotIn(FAKE_HITLER, level.things)
                self.assertLessEqual(level.things.count(FAKE_HITLER), 1)

    def test_ghosts_only_spawn_on_secret_floor(self):
        for number in range(1, 10):
            for seed in range(1):
                level = _generate_with_retries(CampaignConfig(seed=seed), number)
                self.assertFalse(set(level.things) & set(GHOSTS))
        for seed in range(3):
            level = _generate_with_retries(CampaignConfig(seed=seed), 10)
            self.assertLessEqual(sum(thing in GHOSTS for thing in level.things), 1)

    def test_boss_choice_varies_across_seeds(self):
        bosses = {next(thing for thing in _generate_with_retries(
                           CampaignConfig(seed=seed), 9).things
                       if thing in BOSSES)
                  for seed in range(20)}
        self.assertGreater(len(bosses), 1, "boss floor always picked the same boss across seeds")

    def test_special_floor_grammars_keep_plenty_of_cross_seed_variation(self):
        boss_plans = [_plan_floor(random.Random(seed), 3, 9,
                                  variant=generator.VARIANT_STRONGHOLD)
                      for seed in range(32)]
        vault_plans = [_plan_floor(random.Random(seed), 3, 10,
                                   variant=generator.VARIANT_VAULT)
                       for seed in range(32)]
        for plans in (boss_plans, vault_plans):
            self.assertGreaterEqual(len({plan.special_family for plan in plans}), 4)
            self.assertGreaterEqual(len({plan.skeleton for plan in plans}), 5)
            self.assertGreaterEqual(
                len({(plan.special_family, plan.skeleton,
                      plan.district_circulation, plan.motifs)
                     for plan in plans}), 16)

    def test_floor_ten_is_a_source_aware_reward_expedition(self):
        level = None
        for attempt in range(10):
            try:
                level = generate_map(CampaignConfig(seed=71), 10, attempt,
                                     secret_source=3)
                break
            except ValueError:
                continue
        self.assertIsNotNone(level)
        self.assertEqual(level.secret_source, 3)
        self.assertIn(level.premium_room, level.critical_route)
        self.assertGreaterEqual(len(level.expedition_rooms), 3)
        self.assertEqual(sum(placement.reason == "floor-ten-premium"
                             for placement in level.pickup_placements), 1)
        self.assertGreaterEqual(sum(placement.reason == "floor-ten-expedition"
                                    for placement in level.pickup_placements), 2)
        arrival = level.room_roles.index("arrival")
        room = level.rooms[arrival]
        self.assertFalse(any(_at(level.things, x, y) in generator.ENEMY_CODES
                             for y in range(room.y, room.y + room.h)
                             for x in range(room.x, room.x + room.w)))

    def test_wall_theme_materials_are_internally_consistent(self):
        """Every WALL_THEMES entry must draw its base and accents from a
        single WL6 texture family (verified by tile name prefix in
        wolf3d.txt's xlat table). Theme selection is randomized per floor
        rather than pinned to floor number, so any entry can land on any
        floor -- a cross-family mix (e.g. blue stone corridors with a metal
        accent room) is now visible immediately instead of hiding behind
        whichever one floor number used to select that entry."""
        families = (
            {1, 2, 3, 4, 6, 27, 28},  # grey stone and its signs/landmarks
            {24, 26},            # damp/slimy grey stone (no clean-grey sign)
            {8, 7, 41},          # blue stone: BSTONEA1, BSTCELB1, BSTSIGN1
            {40, 34, 36},        # blue wall: BLUWALL1, BLUSKUL1, BLUSWAS1
            {9},                 # rare floor-10 masonry: BSTONEB1
            {12, 10, 11, 23},    # wood: WOOD1, WODEAGL1, WODHTLR1, WODCROS1
            {15, 14},            # metal: METAL1, METLSGN1
            {17, 18, 20, 38},    # brick: BRICK1, BRIKWRT1, BRIKEGL1, BRIKODD1
            {19, 25},            # purple, clean/blooded
            {29, 30, 31, 32},    # chipped cave stone, clean/blooded
            {35, 37, 39, 43, 49},  # grey brick, vent/crack/map/portrait
            {42, 46, 47},        # marble variants and banner
            {44, 45},            # brown stone variants
            {48},                # plaster
        )
        for base, accents in WALL_THEMES:
            materials = {base, *accents}
            self.assertTrue(
                any(materials <= family for family in families),
                f"theme base={base} accents={accents} mixes incompatible wall materials")

    def test_purple_wall_material_is_reserved_for_late_floors(self):
        room = Room(20, 20, 8, 8)
        tiles = [WALL] * (GRID * GRID)
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR
        _, early = generator._assign_area_themes(
            tiles, [room], [0], random.Random(1), 5, theme_pool=(19,))
        _, late = generator._assign_area_themes(
            tiles, [room], [0], random.Random(1), 6, theme_pool=(19,))
        self.assertNotIn(19, {base for base, _ in early.values()})
        self.assertEqual({base for base, _ in late.values()}, {19})

    def test_cell_wall_tile_is_never_the_base_theme(self):
        """BSTCELA1(5)/BSTCELB1(7), the barred prison-cell wall, reads as a
        specific set piece; if either is a theme's base it fills every wall
        on the whole floor instead of being confined to a themed room."""
        for base, accents in WALL_THEMES:
            self.assertNotIn(base, (5, 7))

    def test_skeleton_cage_is_a_single_landmark_not_room_material(self):
        self.assertIn(7, DECOR_WALLS)
        for seed in range(6):
            level = _generate_with_retries(CampaignConfig(seed=seed), 2)
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
                level = _generate_with_retries(CampaignConfig(seed=seed), number)
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
            # Retry candidates legitimately reach wall theming before a later
            # validator rejects them. Preserve the tile-plane identity so the
            # final level is paired with its own component/theme mapping.
            captured.append((args[0], args[4], args[5]))
            return original(*args, **kwargs)

        with mock.patch.object(generator, "_apply_wall_theme", side_effect=capture):
            levels = [_generate_with_retries(CampaignConfig(seed=seed), 2)
                      for seed in range(6)]
        for level in levels:
            component_of, group_theme = next(
                (component_of, group_theme)
                for tiles, component_of, group_theme in captured
                if tiles is level.tiles)
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
            # Keep the tile plane identity so captures from legitimately
            # rejected retry candidates cannot be paired with the eventual
            # successful level for this seed.
            captured.append((args[0], args[4], args[5]))
            return original(*args, **kwargs)

        with mock.patch.object(generator, "_apply_wall_theme", side_effect=capture):
            levels = [_generate_with_retries(CampaignConfig(seed=seed), 2)
                      for seed in range(12)]
        for level in levels:
            component_of, group_theme = next(
                (component_of, group_theme)
                for tiles, component_of, group_theme in captured
                if tiles is level.tiles)
            blue_rooms = sum(group_theme[component_of[room.center]][0] == 8
                             for room in level.rooms)
            if blue_rooms > 1:
                self.assertLess(len(level.jail_rooms) * 2, blue_rooms)

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
            # down; 40/41 are the hanging/skeleton cages -- everything else
            # stays barrels, blood, and bone variants.
            self.assertTrue(set(placed) <= {24, 61, 42, 64, 65, 66, 32, 40, 41})

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
                           roles=["start"], traversal_pair_chance=0.0)
        cx, _ = room.center
        # Vases use their own single, wall-backed accent grammar rather than
        # the two-half zoning grammar exercised by this test.
        zone_a = {26, 37}
        zone_b = {31, 27}
        placed = [(index % GRID, index // GRID, thing)
                  for index, thing in enumerate(things) if thing]
        self.assertTrue(placed)
        traversal = generator._room_traversal_frame(room, tiles)
        signature_cells = set(generator._traversal_pair_candidates(
            room, tiles, traversal)[0])
        off_zone_a = 0
        for x, y, thing in placed:
            if (x, y) in signature_cells:
                continue
            if thing in zone_a:
                off_zone_a += x >= cx
            if thing in zone_b:
                self.assertGreaterEqual(x, cx)
        # The independently authored room-light pair may share the zone-A
        # item type; the furniture cluster itself must remain on its half.
        self.assertLessEqual(off_zone_a, 2)
        self.assertTrue(any(thing in zone_a and x < cx
                            for x, _, thing in placed))

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

    def test_zoned_potted_plants_are_not_doubled_in_one_corner(self):
        room = Room(10, 10, 8, 8)
        things = [0] * (GRID * GRID)
        free = {(x, y) for y in range(room.y + 1, room.y + room.h - 1)
                for x in range(room.x + 1, room.x + room.w - 1)}

        def place(cells, item):
            for cell in cells:
                things[cell[1] * GRID + cell[0]] = item
                free.discard(cell)
            return True

        plant_zones = (((31,), ()), ((34,), ()))
        _place_zoned(room, plant_zones, free, set(), set(), things,
                     random.Random(0), place, 2)
        plants = [(index % GRID, index // GRID, item)
                  for index, item in enumerate(things) if item in (31, 34)]
        self.assertEqual(len(plants), 2)
        self.assertEqual({item for _, _, item in plants}, {31, 34})
        self.assertLess(plants[0][0], room.center[0])
        self.assertGreaterEqual(plants[1][0], room.center[0])

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
                           landmarks={0: [(14, 9)]}, landmark_frame_chance=1.0)
        self.assertNotEqual(_at(things, 13, 10), 0)
        self.assertEqual(_at(things, 13, 10), _at(things, 15, 10),
                         "frame must be a matched mirrored pair")
        self.assertEqual(_at(things, 14, 10), 0, "the picture itself stays visible")

    def test_hallway_pairs_are_bisected_by_door_to_door_travel(self):
        room = Room(10, 10, 12, 8)
        tiles = [WALL] * (GRID * GRID)
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR
        travel_y = 14
        tiles[travel_y * GRID + room.x - 1] = generator.DOOR_EW
        tiles[travel_y * GRID + room.x + room.w] = generator.DOOR_EW
        tiles[travel_y * GRID + room.x - 2] = FLOOR
        tiles[travel_y * GRID + room.x + room.w + 1] = FLOOR
        things = [0] * len(tiles)
        identity = generator.RoomIdentity("circulation", "hall", "spine", 0,
                                          "grand-halls", "corridor", "corridor")
        _place_decorations(
            [room], tiles, things, set(), (room.x - 2, travel_y), random.Random(6),
            identities=[identity], traversal_pair_chance=1.0)
        lamps = {(index % GRID, index // GRID)
                 for index, item in enumerate(things) if item == 26}
        self.assertGreaterEqual(len(lamps), 2)
        self.assertEqual(len(lamps) % 2, 0)
        self.assertTrue(all((x, 2 * travel_y - y) in lamps for x, y in lamps),
                        "matched lamps must occupy opposite sides of the travel line")

    def test_blocking_decor_stays_off_door_to_door_traversal(self):
        """Tables and barrels may frame a hall, but never occupy its path."""
        room = Room(10, 10, 12, 8)
        base_tiles = [WALL] * (GRID * GRID)
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                base_tiles[y * GRID + x] = FLOOR
        travel_y = 14
        base_tiles[travel_y * GRID + room.x - 1] = generator.DOOR_EW
        base_tiles[travel_y * GRID + room.x + room.w] = generator.DOOR_EW
        base_tiles[travel_y * GRID + room.x - 2] = FLOOR
        base_tiles[travel_y * GRID + room.x + room.w + 1] = FLOOR
        identity = generator.RoomIdentity(
            "beat", "standard", "spine", 0, "garrison", "barracks", "barracks")
        saw_table_or_barrel = False
        for seed in range(30):
            tiles = list(base_tiles)
            things = [0] * len(tiles)
            _place_decorations(
                [room], tiles, things, set(), (room.x - 2, travel_y),
                random.Random(seed), identities=[identity])
            frame = generator._room_traversal_frame(room, tiles)
            self.assertTrue(frame.path)
            self.assertTrue(all(_at(things, *cell) not in generator.STATIC_BLOCKING
                                for cell in frame.path),
                            f"seed {seed} blocks the authored door-to-door lane")
            saw_table_or_barrel |= any(item in (24, 25, 36, 58)
                                       for item in things)
        self.assertTrue(saw_table_or_barrel,
                        "test never exercised table/barrel decoration")

    def test_bent_traversal_frame_uses_local_path_directions(self):
        room = Room(10, 10, 8, 8)
        tiles = [WALL] * (GRID * GRID)
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR
        tiles[9 * GRID + 14] = generator.DOOR_NS
        tiles[8 * GRID + 14] = FLOOR
        tiles[14 * GRID + 18] = generator.DOOR_EW
        tiles[14 * GRID + 19] = FLOOR
        frame = generator._room_traversal_frame(room, tiles)
        self.assertEqual(len(frame.entries), 2)
        self.assertIn((1, 0), frame.station_axes)
        self.assertIn((0, 1), frame.station_axes)

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
                level = _generate_with_retries(CampaignConfig(seed=seed), number)
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

    def test_damp_stone_and_plaster_keep_foreign_door_signs_out(self):
        damp = generator.MATERIAL_BY_BASE[24]
        self.assertEqual(damp.landmarks, ())
        self.assertNotIn(28, damp.accents)
        self.assertEqual(generator.SECRET_HINT_BY_BASE[24], (26,))
        self.assertEqual(generator.SECRET_HINT_BY_BASE[48], (48,))

        tiles = [WALL] * (GRID * GRID)
        things = [0] * (GRID * GRID)
        component_of = {(11, 10): 0, (21, 10): 1}
        group_theme = {0: (24, (26,)), 1: (48, ())}
        for x in (10, 20):
            things[10 * GRID + x] = PUSHWALL
        treatments = _hint_secrets(
            tiles, things, component_of, group_theme, random.Random(0))
        self.assertEqual(_at(tiles, 10, 10), 26)
        self.assertEqual(_at(tiles, 20, 10), 48)
        self.assertNotIn(28, (_at(tiles, 10, 10), _at(tiles, 20, 10)))
        self.assertNotIn(13, (_at(tiles, 10, 10), _at(tiles, 20, 10)))
        self.assertEqual(treatments[(20, 10)], "plain-wall")

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
        level = _generate_with_retries(
            CampaignConfig(seed=771, layout_complexity=Intensity.HIGH), 5)
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
            switch_dx = 1 if switch_east else -1
            threshold_x = sx - switch_dx
            self.assertEqual(_at(level.tiles, threshold_x, sy - 1), ELEVATOR_TILE,
                             "upper rail must begin immediately behind the door")
            self.assertEqual(_at(level.tiles, threshold_x, sy + 1), ELEVATOR_TILE,
                             "lower rail must begin immediately behind the door")
            self.assertEqual(_at(level.tiles, sx, sy - 1), ELEVATOR_TILE)
            self.assertEqual(_at(level.tiles, sx, sy + 1), ELEVATOR_TILE)

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

    def test_generated_floor_records_variant_and_circulation_stably_across_attempts(self):
        config = CampaignConfig(seed=77)
        expected = (generator._variant_sequence(config)[2].name,
                    generator._circulation_sequence(config)[2])
        realized = []
        for attempt in range(10):
            try:
                level = generate_map(config, 3, attempt=attempt)
                realized.append((level.variant, level.circulation_skeleton))
            except ValueError:
                continue
            if len(realized) == 2:
                break
        self.assertEqual(realized, [expected, expected])

    def test_manifest_names_each_floors_variant(self):
        config = CampaignConfig(seed=4242)
        expected = [variant.name for variant in generator._variant_sequence(config)]
        expected_skeletons = list(generator._circulation_sequence(config))
        with tempfile.TemporaryDirectory() as directory:
            package = generate_campaign(config, Path(directory) / "campaign.pk3")
            manifest = generator.read_manifest(package)
        self.assertEqual(manifest["seed_source"], "LittleEntropyMachine")
        self.assertEqual([floor["variant"] for floor in manifest["floors"]], expected)
        self.assertEqual([floor["circulation_skeleton"]
                          for floor in manifest["floors"]], expected_skeletons)
        self.assertTrue(all(floor["district_circulation"]
                            and floor["layout_signature"]
                            and floor["pickup_compositions"]
                            for floor in manifest["floors"]))

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
        level = _generate_with_retries(
            CampaignConfig(seed=600, guard_density=Intensity.HIGH), 8)
        easy, medium_extra, hard_extra = level.enemy_tiers
        self.assertGreater(easy, 0)
        self.assertGreater(medium_extra, 0)
        self.assertGreater(hard_extra, 0)
        self.assertGreaterEqual(level.things.count(49), 1)
        self.assertGreaterEqual(level.things.count(47) + level.things.count(48), 1)


if __name__ == "__main__":
    unittest.main()
