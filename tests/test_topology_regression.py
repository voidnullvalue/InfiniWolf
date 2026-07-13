"""Regression guards for varied, enjoyable composition, not just validity.

validate_map already guarantees a map is playable; these tests guard
properties that make a *valid* map still look procedural or broken in a way
validate_map doesn't check -- e.g. two rooms silently fused into one long
open sightline by a corridor-routing fallback. Each test names the concrete
failure mode it exists to catch.
"""
from collections import deque
import random
import unittest

from infiniwolf.config import CampaignConfig
from infiniwolf.generator import (DOGS, DOOR_ELEVATOR, DOOR_EW, DOOR_GOLD_EW, DOOR_NS, DOORS,
                                   ELEVATOR_TILE, FLOOR, GRID, GUARDS, OFFICERS, SS,
                                   SECRET_EXIT_ZONE, STATIC_BLOCKING, ZONE_MAX, _at,
                                   _floor_components, _is_floor, FLOOR_TEN_STONE_THEME,
                                   TREASURE, WALL_THEMES, _plan_floor, generate_map)


def _generate_with_retries(config: CampaignConfig, floor: int, attempts: int = 50):
    """Mirror generate_campaign's own retry loop: a single (seed, floor,
    attempt) combination may legitimately fail validate_map and needs a
    fresh sub-seed, exactly as production usage already tolerates."""
    last_error = None
    for attempt in range(attempts):
        try:
            return generate_map(config, floor, attempt)
        except ValueError as error:
            last_error = error
    raise AssertionError(f"floor {floor} never validated in {attempts} attempts: {last_error}")

# Deterministic seeds known (as of the 2026-07-12 macro-layout work) to
# exercise specific structural edges: crowded hubs, dense districts, small
# and large layout-complexity extremes. Keep adding pathological seeds here
# as they're found rather than only relying on broad fuzzing.
REGRESSION_SEEDS = ("e", "alpha", "bravo", "charlie", "delta", 42, 1783823867320418919)


def _longest_straight_run(tiles: list[int]) -> int:
    """Longest unobstructed floor run on any row or column; doors and walls
    both break a run. A corridor silently fused into a room with no wall or
    door at the seam (a real bug once caused by a routing fallback) shows up
    here as a long run with zero doors anywhere on that line."""
    best = 0
    for horizontal in (True, False):
        for fixed in range(GRID):
            run = 0
            for moving in range(GRID):
                x, y = (moving, fixed) if horizontal else (fixed, moving)
                run = run + 1 if _is_floor(_at(tiles, x, y)) else 0
                best = max(best, run)
    return best


def _test_floor_components(tiles: list[int]) -> list[set[tuple[int, int]]]:
    unassigned = {(x, y) for y in range(GRID) for x in range(GRID) if _is_floor(_at(tiles, x, y))}
    components = []
    while unassigned:
        start = next(iter(unassigned))
        component = {start}
        queue = deque([start])
        unassigned.remove(start)
        while queue:
            x, y = queue.popleft()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxt = (x + dx, y + dy)
                if nxt in unassigned:
                    unassigned.remove(nxt)
                    component.add(nxt)
                    queue.append(nxt)
        components.append(component)
    return components


def _visible_wall_material_shares(tiles: list[int]) -> list[float]:
    """Base-material shares for wall columns that actually face plain floor."""
    base_by_tile = {tile: base for base, accents in WALL_THEMES
                    for tile in (base, *accents)}
    base_by_tile[FLOOR_TEN_STONE_THEME[0]] = FLOOR_TEN_STONE_THEME[0]
    counts = {}
    for index, tile in enumerate(tiles):
        if tile not in base_by_tile:
            continue
        x, y = index % GRID, index // GRID
        if not any(_is_floor(_at(tiles, x + dx, y + dy))
                   for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
            continue
        base = base_by_tile[tile]
        counts[base] = counts.get(base, 0) + 1
    total = sum(counts.values()) or 1
    return sorted((count / total for count in counts.values()), reverse=True)


# ECWolf's old-format loader computes each thing's facing angle as
# (oldnum - base) * 90 and casts it straight to MapTile::Side, whose enum
# order is {East, North, West, South} (gamemap.h) -- deliberately hardcoded
# here from the raw base thing-codes (108/116/126/134) and that enum order,
# independent of however infiniwolf.generator's GUARDS/OFFICERS/SS/DOGS
# tuples happen to be arranged. A test that instead re-derived this from
# those tuples would just re-check the generator's own assumption about
# itself -- exactly how the previous version of this test passed for weeks
# while actors were still visibly facing walls in-game, because the
# generator's facings tuple and this test agreed with each other while both
# were wrong relative to the engine.
_ENGINE_SIDE_DELTA = ((1, 0), (0, -1), (-1, 0), (0, 1))  # East, North, West, South
_ENGINE_BASE = {108: GUARDS, 116: OFFICERS, 126: SS, 134: DOGS}


def _engine_facing_delta(code: int) -> tuple[int, int] | None:
    """Decode a things-plane code back to the direction ECWolf will actually
    render it facing, across the tier offsets (+36/+72) skill 2/3 copies use.
    36 is a multiple of 4 so it never perturbs the angle offset mod 4."""
    for base, family in _ENGINE_BASE.items():
        for tier in (0, 1, 2):
            candidate = code - 36 * tier
            if candidate in family:
                return _ENGINE_SIDE_DELTA[(candidate - base) % 4]
    return None


class ActorFacingRegressionTests(unittest.TestCase):
    def test_actors_do_not_face_a_wall_or_blocking_decoration(self):
        """Stationary facing is picked against the map before decorations are
        placed; decoration placement only checked that a cell was empty, not
        who was facing it, so a pillar/barrel/table could land directly in a
        guard's face -- indistinguishable in-game from facing a wall. Covers
        both the literal-wall case and the blocking-decoration case that
        earlier tests never exercised. Checks against ECWolf's actual
        East/North/West/South thing-angle convention, not the generator's own
        (formerly mismatched) facings-tuple assumption -- see
        _engine_facing_delta."""
        blocking = set(STATIC_BLOCKING)
        for seed in REGRESSION_SEEDS:
            config = CampaignConfig(seed=seed)
            for floor in (2, 5, 8):
                level = _generate_with_retries(config, floor)
                tiles, things = level.tiles, level.things
                for index, code in enumerate(things):
                    delta = _engine_facing_delta(code)
                    if delta is None:
                        continue
                    x, y = index % GRID, index // GRID
                    dx, dy = delta
                    fx, fy = x + dx, y + dy
                    ahead_tile = _at(tiles, fx, fy)
                    ahead_thing = _at(things, fx, fy)
                    self.assertTrue(
                        _is_floor(ahead_tile) or ahead_tile in DOORS,
                        f"seed={seed!r} floor={floor}: actor at ({x},{y}) faces "
                        f"non-floor tile {ahead_tile}")
                    self.assertNotIn(
                        ahead_thing, blocking,
                        f"seed={seed!r} floor={floor}: actor at ({x},{y}) faces "
                        f"blocking decoration {ahead_thing}")


class ElevatorContainmentRegressionTests(unittest.TestCase):
    def test_elevator_switch_tiles_are_never_adjacent_to_foreign_floor(self):
        """Every ELEVATOR_TILE(21) cell is elevator paneling or the exit
        switch's back wall; the only floor-plane cells allowed to touch one
        are the two interior cells of that same shaft (exit_stand and the
        cell between it and the door) and a SECRET_EXIT_ZONE(107) approach.
        Any other adjacent floor cell means a room or corridor was carved
        flush against the shaft, exposing the switch's paneling or the back
        wall from outside -- the exact failure mode earlier commits
        (89b04f6, e20a530, b9af24a) fixed piecemeal without a regression
        test locking it in."""
        for seed in REGRESSION_SEEDS:
            config = CampaignConfig(seed=seed)
            for floor in (1, 5, 9):
                level = _generate_with_retries(config, floor)
                tiles = level.tiles
                ex, ey = level.exit_stand
                legit = {(ex, ey)}
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    if _at(tiles, ex + 2 * dx, ey + 2 * dy) in (DOOR_ELEVATOR, DOOR_GOLD_EW):
                        legit.add((ex + dx, ey + dy))
                for y in range(GRID):
                    for x in range(GRID):
                        if _at(tiles, x, y) != ELEVATOR_TILE:
                            continue
                        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                            nx, ny = x + dx, y + dy
                            nv = _at(tiles, nx, ny)
                            if nv == SECRET_EXIT_ZONE or (nx, ny) in legit:
                                continue
                            self.assertFalse(
                                FLOOR <= nv <= ZONE_MAX,
                                f"seed={seed!r} floor={floor}: elevator tile at "
                                f"({x},{y}) exposes foreign floor cell ({nx},{ny})")


class TopologyRegressionTests(unittest.TestCase):
    def test_floor_ten_rooms_are_larger_than_floor_seven(self):
        for seed in REGRESSION_SEEDS:
            config = CampaignConfig(seed=seed)
            level7 = _generate_with_retries(config, 7)
            level10 = _generate_with_retries(config, 10)
            self.assertGreater(sum(room.w * room.h for room in level10.rooms),
                               sum(room.w * room.h for room in level7.rooms),
                               f"seed={seed!r}: floor 10 rooms are not larger than floor 7")

    def test_floor_ten_has_more_treasure_than_floor_seven(self):
        for seed in REGRESSION_SEEDS:
            config = CampaignConfig(seed=seed)
            level7 = _generate_with_retries(config, 7)
            level10 = _generate_with_retries(config, 10)
            self.assertGreater(sum(thing in TREASURE for thing in level10.things),
                               sum(thing in TREASURE for thing in level7.things),
                               f"seed={seed!r}: floor 10 has no extra treasure")

    def test_floor_ten_secret_budget_is_bumped(self):
        for seed in REGRESSION_SEEDS:
            config = CampaignConfig(seed=seed)
            level7 = _generate_with_retries(config, 7)
            level10 = _generate_with_retries(config, 10)
            self.assertGreaterEqual(len(level10.secret_rewards), len(level7.secret_rewards),
                                    f"seed={seed!r}: floor 10 secret budget regressed")

    def test_no_unbounded_sightlines(self):
        """Report ceiling: no straight unobstructed run should exceed 21
        tiles. A run this long with no door on it means two spaces were
        fused with no separating architecture at all, not just a lane the
        sightline-breaker declined to touch."""
        for seed in REGRESSION_SEEDS:
            config = CampaignConfig(seed=seed)
            for floor in (2, 5, 8):
                level = _generate_with_retries(config, floor)
                longest = _longest_straight_run(level.tiles)
                self.assertLessEqual(
                    longest, 21,
                    f"seed={seed!r} floor={floor}: longest straight run {longest} > 21")

    def test_door_bounded_rooms_are_not_gigantic_blobs(self):
        """A silently-fused corridor also shows up as one door-bounded floor
        component absorbing most of the map instead of many rooms -- a
        second, independent signal for the same failure class."""
        for seed in REGRESSION_SEEDS:
            config = CampaignConfig(seed=seed)
            for floor in (2, 5, 8, 10):
                level = _generate_with_retries(config, floor)
                components = _test_floor_components(level.tiles)
                total = sum(len(c) for c in components) or 1
                biggest = max(len(c) for c in components)
                self.assertLess(
                    biggest / total, 0.5,
                    f"seed={seed!r} floor={floor}: one component is "
                    f"{biggest}/{total} of all floor tiles")

    def test_plain_doors_have_no_floor_only_walkaround(self):
        """A notch or alcove may meet its neighbour beside a valid corridor
        door.  Such a door no longer gates anything: with every door held
        closed, its two flanking floor cells must remain separate."""
        for seed in REGRESSION_SEEDS:
            config = CampaignConfig(seed=seed)
            for floor in (2, 5, 8, 10):
                level = _generate_with_retries(config, floor)
                components = _floor_components(level.tiles)
                owner = {cell: index for index, component in enumerate(components)
                         for cell in component}
                for index, tile in enumerate(level.tiles):
                    if tile not in (DOOR_EW, DOOR_NS):
                        continue
                    x, y = index % GRID, index // GRID
                    dx, dy = (1, 0) if tile % 2 == 0 else (0, 1)
                    before = owner.get((x - dx, y - dy))
                    after = owner.get((x + dx, y + dy))
                    self.assertFalse(
                        before is not None and before == after,
                        f"seed={seed!r} floor={floor}: plain door at ({x},{y}) "
                        "has a floor-only walkaround")


class AreaThemeRegressionTests(unittest.TestCase):
    def test_wall_material_balance_across_seeds(self):
        """Enough floors show a meaningful second visible wall material.

        This is deliberately aggregate rather than per-floor: theme regions
        legitimately vary with the layout, but a return to one huge merged
        group should not make almost every deterministic sample monochrome.
        """
        second_shares = []
        for seed in range(20):
            config = CampaignConfig(seed=seed)
            for floor in (2, 5, 8):
                level = _generate_with_retries(config, floor)
                plan = _plan_floor(random.Random(level.seed),
                                   int(config.layout_complexity), floor)
                if len({spec.district for spec in plan.specs}) < 2:
                    continue
                shares = _visible_wall_material_shares(level.tiles)
                second_shares.append(shares[1] if len(shares) > 1 else 0.0)
        required = (65 * len(second_shares) + 99) // 100
        balanced = sum(share >= 0.10 for share in second_shares)
        self.assertGreaterEqual(
            balanced, required,
            f"only {balanced}/{len(second_shares)} samples have a second base material "
            f"at or above 10%; second-largest shares="
            f"{[round(share, 3) for share in second_shares]}")

    def test_wall_material_never_leaks_across_a_non_door_boundary(self):
        """A wall-plane tile may touch only one final material region.

        This is intentionally geometric: it proves the containment guarantee
        independently of the texture values selected for those regions.
        """
        theme_tiles = ({tile for base, accents in WALL_THEMES
                        for tile in (base, *accents)}
                       | {FLOOR_TEN_STONE_THEME[0]})
        for seed in REGRESSION_SEEDS:
            config = CampaignConfig(seed=seed)
            for floor in (2, 5, 8):
                level = _generate_with_retries(config, floor)
                components = _floor_components(level.tiles)
                owner = {cell: index for index, component in enumerate(components)
                         for cell in component}
                parents = list(range(len(components)))

                def find(component):
                    while parents[component] != component:
                        parents[component] = parents[parents[component]]
                        component = parents[component]
                    return component

                def union(first, second):
                    first, second = find(first), find(second)
                    if first != second:
                        parents[second] = first

                for index, tile in enumerate(level.tiles):
                    if tile not in theme_tiles:
                        continue
                    x, y = index % GRID, index // GRID
                    neighbors = {owner[cell] for cell in ((x + 1, y), (x - 1, y),
                                                          (x, y + 1), (x, y - 1))
                                 if cell in owner}
                    if neighbors:
                        first = min(neighbors)
                        for other in neighbors - {first}:
                            union(first, other)
                for index, tile in enumerate(level.tiles):
                    if tile not in theme_tiles:
                        continue
                    x, y = index % GRID, index // GRID
                    neighbors = {find(owner[cell]) for cell in ((x + 1, y), (x - 1, y),
                                                                (x, y + 1), (x, y - 1))
                                 if cell in owner}
                    self.assertLessEqual(
                        len(neighbors), 1,
                        f"seed={seed!r} floor={floor}: wall tile at ({x},{y}) "
                        f"touches material groups {sorted(neighbors)}")

    def test_theme_regions_do_not_mix_material_families(self):
        """Every wall material touching one floor component shares a family."""
        families = [set(accents) | {base}
                    for base, accents in WALL_THEMES + (FLOOR_TEN_STONE_THEME,)]
        theme_tiles = set().union(*families)
        bases = {base for base, _ in WALL_THEMES} | {FLOOR_TEN_STONE_THEME[0]}
        seen_multiple = False
        for seed in REGRESSION_SEEDS:
            config = CampaignConfig(seed=seed)
            for floor in (2, 5, 8):
                level = _generate_with_retries(config, floor)
                components = _floor_components(level.tiles)
                owner = {cell: index for index, component in enumerate(components)
                         for cell in component}
                materials = {index: set() for index in range(len(components))}
                for index, tile in enumerate(level.tiles):
                    if tile not in theme_tiles:
                        continue
                    x, y = index % GRID, index // GRID
                    for cell in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                        if cell in owner:
                            materials[owner[cell]].add(tile)
                seen_multiple |= len({tile for tiles in materials.values()
                                      for tile in tiles if tile in bases}) > 1
                for component, tiles in materials.items():
                    self.assertTrue(
                        any(tiles <= family for family in families),
                        f"seed={seed!r} floor={floor}: component {component} "
                        f"mixes wall materials {sorted(tiles)}")
        self.assertTrue(seen_multiple, "no regression level used multiple base materials")


if __name__ == "__main__":
    unittest.main()
