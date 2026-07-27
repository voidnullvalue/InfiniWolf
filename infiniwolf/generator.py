"""Deterministic WL6 campaign generation and ECWolf package writing."""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, replace
import heapq
from itertools import combinations
import json
import math
from pathlib import Path
import random
import struct
import tempfile
from typing import Callable
import zipfile

from . import __version__
from .build_info import COMMIT as BUILD_COMMIT
from .config import CampaignConfig

# The WL6 code vocabulary lives in the tiles leaf. Imported by name (rather
# than star) so it is re-exported from `infiniwolf.generator` exactly as
# before: generator_validation, the test suite, and tools/ all import these
# from here.
from .wl6 import (  # noqa: F401
    GRID, WALL, FLOOR, ZONE_MAX, DOOR_EW, DOOR_NS, DOOR_ELEVATOR, DOOR_ELEVATOR_NS,
    DOOR_GOLD_EW, DOOR_GOLD_NS, DOOR_SILVER_EW, DOOR_SILVER_NS, GOLD_DOORS, SILVER_DOORS,
    LOCKED_DOORS, DOORS, PLAYER_START_CODES, PLAYER_START, PUSHWALL, ELEVATOR_TILE,
    DUMMY_ELEVATOR_TILE, SECRET_EXIT_ZONE, GOLD_KEY, SILVER_KEY, HANS_GROSSE, SCHABBS, GRETEL,
    GIFT, FAT_FACE, MECHA_HITLER, FAKE_HITLER, GHOSTS, BOSSES, KEY_DROP_BOSSES, GUARDS,
    OFFICERS, SS, DOGS, PATROL_GUARDS, PATROL_OFFICERS, PATROL_SS, PATROL_DOGS,
    PATROL_POINT_CODES, PATROL_POINT_DIRECTIONS, DOG_FOOD, AMMO, FOOD, FIRST_AID, MACHINE_GUN,
    CHAINGUN, ONE_UP, TREASURE, PICKUP_CODES, ENEMY_CODES, ENEMY_FAMILIES,
    NOVELTY_SPAWN_CHANCE, PATROLS_BY_FAMILY, FAMILY_BY_CODE, AMMO_COST, AMMO_SUPPLY_SCALE,
    AMMO_SUPPLY_EXEMPT_FLOORS, ACTOR_BUDGET_SCALE, ACTOR_SPACING, WallMaterialFamily,
    WALL_MATERIALS, MATERIAL_BY_BASE, WALL_THEMES, JAIL_CANDIDATE_PROBABILITY,
    _codes_for_colors,
    FLOOR_TEN_STONE_THEME, PURPLE_MIN_FLOOR, DECOR_WALLS, SPECIAL_WALL_TILES,
    SECRET_HINT_BY_BASE, SECRET_HINT_WALLS, WALL_LANDMARK_CONCEPTS, DAMAGED_WALL_CONCEPTS,
    STATIC_BLOCKING, STATIC_OPEN, LIGHTING_ITEMS, LIGHTING_FAMILY_ITEMS, SPEAR_CONCEPTS,
    VINE_SCREEN_CONCEPTS,
)
from .model import (  # noqa: F401
    ArrivalDetail, BossArenaDetail, EncounterPlacement, FloorPlan, FloorVariant,
    GatePlan, GeneratedMap, GuardGallery, GuardRecess, KeyObjective, PatrolRoute,
    PlacedPlan, RareMotifDetail, Room, RoomIdentity, RoomSpec, SecretDetail,
    SpritePlacement, VineScreen,
)
from .grid import (  # noqa: F401
    _at, _set, _is_floor, _inside_room, _door_zone, _reachable,
    _floor_components, _floor_distances, _shortest_floor_path, _path_bends,
    _overlaps, _rooms_by_distance, _room_graph_path, _room_predecessor,
    _FLOOR_OR_DOOR,
)
from .placement import (  # noqa: F401
    RoomAnchors, TraversalFrame, _room_anchors, _room_traversal_frame, _traversal_pair_candidates,
)
from .generator_validation import (  # noqa: F401
    validate_map, validate_objects, validate_patrols, validate_door_axes,
    _patrol_actor_direction,
)
from .progression import (  # noqa: F401
    _carve_secret_pocket, _hint_secrets, _key_spot, _key_spot_in_region,
    _lock_code, _minimum_critical_route_rooms, _pick_secret_variant,
    _place_arrival_elevator, _place_doors, _place_elevator, _place_secret,
    _secret_reward,
)
from .planning import _plan_floor  # noqa: F401
from .quality import _critique  # noqa: F401
from .special_floors import _place_boss, _prepare_boss_arena  # noqa: F401
from .pickups import (  # noqa: F401
    AUTHORED_PICKUP_TEMPLATES, _PlacementGrammar, _place_authored_pickups,
)
from .encounters import (  # noqa: F401
    _carve_guard_recesses, _place_guard_gallery, _place_population,
    _populate_guard_galleries, _spread_actor_cells,
)
from .semantics import (  # noqa: F401
    _apply_wall_theme, _assign_area_themes, _room_identities,
    _select_jail_rooms,
)
from .geometry import (  # noqa: F401
    DOOR_SPACING, _add_pillars, _adjacent_to_room, _carve_connection,
    _carve_notches, _carve_swastika_profile, _carve_symmetric_profiles,
    _door_axis, _door_candidate, _far_from_doors, _place_planned_rooms,
    _room_size, _snap_offsets, _widen_corridors,
    _assign_sound_zones, _break_long_sightlines, _heal_pinched_room_door_pairs,
    _limit_theme_merge_size, _remove_redundant_plain_doors,
    _spatial_districts, _split_oversized_zones, _harvest_sky_vistas,
    _primary_hall_geometry,
)
from .campaign import (  # noqa: F401
    CIRCULATION_MODES, CIRCULATION_SKELETONS, FLOOR_VARIANT_ROTATION,
    HALLWAY_FIRST_SKELETONS, PROGRESSION_GRAMMARS, RARE_MOTIF_CHANCE,
    VARIANT_STRONGHOLD, VARIANT_VAULT, _aardwolf_variant, _candidate_score,
    _circulation_sequence, _lock_schedule, _progression_sequence,
    _rare_motif_schedule, _set_distance, _variant_sequence,
    CampaignSchedule, resolve_schedule, _layout_signature,
)
from .generator_artifacts import (  # noqa: F401
    _manifest, _wad_bytes, _mapinfo, _display_name,
    _reproducibility_text, read_manifest, validate_package,
)
from .decorations import (  # noqa: F401
    SKY_VISTA_COURTYARD_CHANCE, SKY_VISTA_INTERIOR_CHANCE, _DECOR_BLOCKING, _DECOR_OPEN,
    _DECOR_ZONES, _FRAMEABLE, _LIGHTING_OPTIONS, _decor_theme, _lighting_family,
    _place_decorations, _place_zoned, _barrel_families,
)


DECORATION_MULTIPLIERS = (0.0, 0.70, 0.85, 1.00, 1.15, 1.30)
SHAPE_MULTIPLIERS = (0.0, 0.65, 0.82, 1.00, 1.10, 1.20)
# Target share of ordinary actors that should visibly patrol. The old values
# were per-room attempt chances and produced only ~3% moving actors at the
# normal setting because most full-room loops failed geometry reservations.
PATROL_TARGETS = (0.0, 0.04, 0.09, 0.16, 0.23, 0.30)

SHAPE_TARGETS = (0.0, 0.15, 0.25, 0.40, 0.48, 0.55)


class GenerationCancelled(RuntimeError):
    """Raised when a caller cancels before atomic package installation."""


def generate_map(config: CampaignConfig, number: int, attempt: int = 0,
                 secret_exit: bool = False, secret_source: int | None = None,
                 hallway_vine_budget: int = 0,
                 guard_gallery_enabled: bool = False,
                 rare_motif_enabled: bool = False,
                 sky_vista_enabled: bool = True,
                 ) -> GeneratedMap:
    seed = config.floor_seed(number, attempt)
    if config.say_aardwolf:
        seed ^= config.aardwolf_seed(number)
    rng = random.Random(seed)
    tiles = [WALL] * (GRID * GRID)
    things = [0] * (GRID * GRID)
    complexity = int(config.layout_complexity)
    floor_variant = _aardwolf_variant(
        config, number, _variant_sequence(config)[number - 1])
    circulation_skeleton = _circulation_sequence(config)[number - 1]
    progression_grammar = _progression_sequence(config)[number - 1]
    scheduled_gate = _lock_schedule(config)[number - 1]
    plan = _plan_floor(rng, complexity, number, variant=floor_variant,
                       skeleton=circulation_skeleton,
                       progression_grammar=progression_grammar,
                       rare_motif=rare_motif_enabled)
    placed = _place_planned_rooms(rng, plan, number)
    rooms = placed.rooms
    edges = placed.edges
    specs = [plan.specs[index] for index in placed.spec_indices]
    roles = [spec.role for spec in specs]
    districts = _spatial_districts(rooms, len({spec.district for spec in specs}))
    for room in rooms:
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                _set(tiles, x, y, FLOOR)
    shape_scale = SHAPE_MULTIPLIERS[int(config.room_shape_variation)]
    shape_target = SHAPE_TARGETS[int(config.room_shape_variation)]
    shape_budget = max(1, round(len(rooms) * shape_target))
    utility_shapes = frozenset(
        index for index, spec in enumerate(specs)
        if spec.role in {"start", "arrival", "exit", "victory", "recovery"}
        or spec.tier in {"closet", "corridor", "motif"}
        or spec.role == "boss-arena")
    rare_profile: tuple[int, str, tuple[tuple[int, int], ...]] | None = None
    for room_index, (room, spec) in enumerate(zip(rooms, specs)):
        if spec.motif != "swastika":
            continue
        carved = _carve_swastika_profile(tiles, room, rng)
        if carved is not None:
            rare_profile = (room_index, carved[0], carved[1])
        break
    if rare_motif_enabled and rare_profile is None:
        raise ValueError("scheduled rare motif could not be realized")
    notch_budget = max(1, round(shape_budget * 0.30))
    notch_anchors = _carve_notches(
        tiles, rooms, rng,
        chance=min(1.0, floor_variant.notch_chance * shape_scale),
        max_rooms=notch_budget, excluded=utility_shapes)
    authored_shape_count = (1 if rare_profile is not None else 0) + (1 if number == 9 else 0)
    profile_anchors, profile_shapes = _carve_symmetric_profiles(
        tiles, rooms, rng,
        chance=min(1.0, shape_scale),
        max_rooms=max(0, shape_budget - len(notch_anchors) - authored_shape_count),
        excluded=frozenset(notch_anchors) | utility_shapes)
    notch_anchors.update(profile_anchors)
    realized_shapes = ["rectangle"] * len(rooms)
    for room_index in notch_anchors:
        realized_shapes[room_index] = profile_shapes.get(room_index, "mirrored-notch")
    if rare_profile is not None:
        realized_shapes[rare_profile[0]] = "swastika-profile"
    overrides = dict(floor_variant.decor_overrides)
    for room, spec in zip(rooms, specs):
        predicted = overrides.get(_decor_theme(spec.role, spec.tier),
                                  _decor_theme(spec.role, spec.tier))
        structural = (predicted == "grand"
                      or (floor_variant.name == "catacombs" and spec.tier == "anchor"))
        if structural:
            _add_pillars(tiles, room, rng, chance=floor_variant.pillar_chance)
    is_boss = number == 9
    planned_exit_index = roles.index("exit")
    anchor_index = next(index for index, spec in enumerate(specs)
                        if spec.tier == "anchor")
    minimum_route_rooms = _minimum_critical_route_rooms(roles)
    required_post_anchor = next((index for index, role in enumerate(roles)
                                 if role in ("victory", "recovery")), None)

    # Floor one begins inside the castle rather than beside an inert lift.
    # Later floors retain their bounded arrival cars and reserve that geometry
    # before routing, so their rock backing cannot be consumed by corridors.
    first_neighbor = next((second if first == 0 else first
                           for first, second in edges if 0 in (first, second)), 1)
    arrival = None
    if number != 1:
        arrival = _place_arrival_elevator(
            tiles, rooms[0], rooms[first_neighbor].center, rng,
            floor_variant.name)
    # Reserve the terminal car at the same architectural stage. Prefer the
    # planned final spine room, then another sufficiently deep post-anchor
    # route when its horizontal exterior wall is the one that remains clean.
    exit_geometry_candidates: list[tuple[int, list[int]]] = []
    for room_index in range(1, len(rooms)):
        route = _room_graph_path(len(rooms), edges, room_index)
        if (anchor_index not in route[:-1] or room_index == anchor_index
                or len(route) < minimum_route_rooms
                or (required_post_anchor is not None
                    and required_post_anchor not in route[:-1])):
            continue
        exit_geometry_candidates.append((room_index, route))
    exit_geometry_candidates.sort(
        key=lambda item: (item[0] == planned_exit_index, len(item[1])),
        reverse=True)
    preplaced_exit_index = -1
    preplaced_exit_route: list[int] = []
    exit_stand = None
    for room_index, route in exit_geometry_candidates:
        trial_tiles = tiles.copy()
        try:
            trial_stand = _place_elevator(
                trial_tiles, rooms[room_index], locked=is_boss)
        except ValueError:
            continue
        tiles[:] = trial_tiles
        preplaced_exit_index = room_index
        preplaced_exit_route = route
        exit_stand = trial_stand
        break
    if exit_stand is None:
        raise ValueError("no post-climax room has a rock-backed horizontal elevator wall")

    switch_dx = next(dx for dx in (-1, 1)
                     if _at(tiles, exit_stand[0] + dx, exit_stand[1])
                     == ELEVATOR_TILE)
    exit_portal = (exit_stand[0] - 2 * switch_dx, exit_stand[1])
    terminal_footprint = {
        (exit_portal[0] + switch_dx * depth, exit_portal[1] + side)
        for depth in range(5) for side in (-2, -1, 0, 1, 2)}
    protected_elevators = ((set(arrival.footprint) if arrival else set())
                            | terminal_footprint)
    door_zones: set[tuple[int, int]] = ({arrival.portal} if arrival else set())
    paths = [_carve_connection(tiles, rooms[a], rooms[b], rng, complexity,
                               door_zones, protected_elevators)
             for a, b in edges]
    _widen_corridors(tiles, rooms, paths, rng,
                     widen_chance=floor_variant.widen_chance,
                     protected=protected_elevators)
    if arrival is not None:
        start = arrival.player
        facing = arrival.facing
    else:
        start = rooms[0].center
        tx, ty = rooms[first_neighbor].center
        dx, dy = tx - start[0], ty - start[1]
        facing = (1 if abs(dx) >= abs(dy) and dx >= 0 else
                  3 if abs(dx) >= abs(dy) else
                  2 if dy >= 0 else 0)
    _set(things, *start, PLAYER_START_CODES[facing])
    if arrival is not None and arrival.item is not None:
        _set(things, *arrival.item)
    exit_room = rooms[preplaced_exit_index]
    # The elevator belongs near the deepest authored frontier, after the
    # anchor/climax room and at the end of a route containing most of the
    # mandatory spine. The old behavior always tried the nominal exit first,
    # even when a much deeper wing made it a trivial early solution.
    preliminary_distances = _floor_distances(tiles, start)
    center_distances = {index: preliminary_distances.get(room.center, 0)
                        for index, room in enumerate(rooms)}
    room_routes = {index: _room_graph_path(len(rooms), edges, index)
                   for index in range(1, len(rooms))}
    post_anchor_frontier = [
        index for index, route in room_routes.items()
        if anchor_index in route[:-1] and len(route) >= minimum_route_rooms
        and (required_post_anchor is None
             or required_post_anchor in route[:-1])]
    # Side destinations on a strong central hall may be physically farther
    # from the start while branching before the climax. They should enrich
    # exploration, not make every legitimate post-climax elevator look
    # artificially shallow. Compare exit depth only with the eligible
    # post-anchor frontier that an exit is actually allowed to occupy.
    deepest_center = max((center_distances[index]
                          for index in post_anchor_frontier), default=1) or 1
    exit_index = preplaced_exit_index
    critical_route = preplaced_exit_route
    if preliminary_distances.get(exit_stand, 0) / deepest_center < 0.75:
        raise ValueError("no post-climax room satisfies the deep-exit route")
    if exit_index != planned_exit_index:
        roles[planned_exit_index] = "relief"
        roles[exit_index] = "exit"
    notch_cells = {cell for cells in notch_anchors.values() for cell in cells}
    reserved = ({start, exit_stand, *notch_cells}
                | (set(arrival.clearance) | set(arrival.car_cells)
                   if arrival else set()))
    rewards: list[tuple[int, int]] = []
    secret_variants: list[str] = []
    secret_details: list[SecretDetail] = []
    shortcut_pushwalls: list[tuple[int, int]] = []
    secret_protected: set[tuple[int, int]] = (set(arrival.footprint)
                                              if arrival else set())
    floor_distances = _floor_distances(tiles, start)
    room_distances = {room: floor_distances.get(room.center, 0) for room in rooms}
    max_room_distance = max(room_distances.values(), default=1) or 1
    # The secret elevator is planned in addition to the ordinary secret
    # budget. Discovering the route to floor 10 must not silently consume one
    # of the floor's normal reward pockets.
    ordinary_secret_target = max(2, int(config.secrets)
                                 + (1 if number == 10 else 0))
    target_secrets = ordinary_secret_target + (1 if secret_exit else 0)
    # A secret pocket must never reuse or seal the terminal room's elevator
    # wall after the elevator has been carved.
    rare_room_index = rare_profile[0] if rare_profile is not None else -1
    candidates = [room for index, room in enumerate(rooms[1:], 1)
                  if room != exit_room and index != rare_room_index]
    room_index_by_room = {room: index for index, room in enumerate(rooms)}
    if number == 9:
        arena_depth = room_distances[rooms[anchor_index]]
        candidates = [room for room in candidates
                      if roles[room_index_by_room[room]] not in
                      ("boss-arena", "victory", "exit")
                      and room_distances[room] <= arena_depth]

    if secret_exit:
        # Build and rank the entire host roster before carving. Deep optional
        # rooms, distance from the normal lift, and generous room proportions
        # win; a small square is intentionally not a fallback for this route.
        # Measure that depth within the eligible host roster. The terminal
        # elevator room cannot host this pocket, and using its often-extreme
        # distance as the denominator can incorrectly disqualify every
        # optional room even when one is deep within the explorable floor.
        host_depth_scale = (max((room_distances[room] for room in candidates),
                                default=max_room_distance) or 1)
        ranked_hosts = sorted(candidates, key=lambda room: (
            room_distances[room] / host_depth_scale >= 0.45,
            room_index_by_room[room] not in critical_route,
            roles[room_index_by_room[room]] in ("branch", "ring", "relief", "closet"),
            room_distances[room] / host_depth_scale,
            abs(room.center[0] - exit_room.center[0])
            + abs(room.center[1] - exit_room.center[1]),
            room.w * room.h), reverse=True)
        ranked_hosts = [room for room in ranked_hosts
                        if room_distances[room] / host_depth_scale >= 0.45]
        variant_order = list(("vault", "reliquary", "gallery", "nested"))
        rng.shuffle(variant_order)
        placed_exit = None
        exit_host = None
        exit_direction = 1
        for variant in variant_order:
            for room in ranked_hosts:
                for direction in (1, -1):
                    placed_exit = _place_secret(
                        tiles, things, room, rng, variant,
                        room_distances[room] / host_depth_scale, True,
                        reward_quality=int(config.secret_reward_quality),
                        number=number, protected=secret_protected,
                        direction=direction)
                    if placed_exit:
                        exit_host = room
                        exit_direction = direction
                        break
                if placed_exit:
                    break
            if placed_exit:
                break
        if placed_exit is None or exit_host is None:
            raise ValueError("no substantial deep host fits the secret elevator")
        reward, realized_variant, push_cell = placed_exit
        depth_ratio = room_distances[exit_host] / host_depth_scale
        rewards.append(reward)
        secret_variants.append(realized_variant)
        secret_details.append(SecretDetail(
            realized_variant, 3, room_index_by_room[exit_host], depth_ratio,
            push_cell, True, "symmetric-landmark", number + 1,
            exit_direction))
        reserved.add(reward)
        candidates.remove(exit_host)

    rng.shuffle(candidates)
    while len(rewards) < target_secrets and candidates:
        variant = _pick_secret_variant(rng, secret_variants)
        placed_secret = None
        host = None
        for room in candidates:
            placed_secret = _place_secret(tiles, things, room, rng, variant,
                                          room_distances[room] / max_room_distance,
                                          False,
                                          reward_quality=int(config.secret_reward_quality),
                                          number=number,
                                          protected=secret_protected)
            if placed_secret:
                host = room
                break
        # A slot whose larger footprint fits nowhere still gets the proven
        # baseline experience rather than silently shrinking the budget.
        if placed_secret is None and variant != "square":
            for room in candidates:
                placed_secret = _place_secret(tiles, things, room, rng, "square",
                                              room_distances[room] / max_room_distance,
                                              False,
                                              reward_quality=int(config.secret_reward_quality),
                                              number=number,
                                              protected=secret_protected)
                if placed_secret:
                    host = room
                    break
        if placed_secret:
            reward, realized_variant, push_cell = placed_secret
            rewards.append(reward); secret_variants.append(realized_variant)
            secret_details.append(SecretDetail(
                realized_variant, 7 if number == 9 else 3,
                room_index_by_room[host],
                room_distances[host] / max_room_distance, push_cell, False))
            reserved.add(reward)
            candidates.remove(host)
        else:
            break
    # Dense motifs can consume every nominal east wall; a rock-backed hall
    # threshold is a safe last host with the same push direction and margin.
    reachable_walls = _reachable(tiles, start, locked_open=True)
    fallback_walls = [(x, y) for y in range(3, GRID - 3) for x in range(3, GRID - 4)
                      if _at(tiles, x, y) == WALL and (x - 1, y) in reachable_walls
                      and (number != 9
                           or floor_distances.get((x - 1, y), max_room_distance + 1)
                           <= room_distances[rooms[anchor_index]])]
    rng.shuffle(fallback_walls)
    while len(rewards) < target_secrets:
        variant = _pick_secret_variant(rng, secret_variants)
        reward = None
        fallback_push: tuple[int, int] | None = None
        fallback_depth = 0.0
        for px, py in fallback_walls:
            approach_distance = floor_distances.get((px - 1, py), 0)
            reward = _carve_secret_pocket(
                tiles, things, px, py, rng, False, variant,
                min(1.0, approach_distance / max_room_distance),
                reward_quality=int(config.secret_reward_quality),
                number=number,
                protected=secret_protected)
            if reward:
                fallback_push = (px, py)
                fallback_depth = min(1.0, approach_distance / max_room_distance)
                break
        if reward is None and variant != "square":
            variant = "square"
            for px, py in fallback_walls:
                approach_distance = floor_distances.get((px - 1, py), 0)
                reward = _carve_secret_pocket(
                    tiles, things, px, py, rng, False, variant,
                    min(1.0, approach_distance / max_room_distance),
                    reward_quality=int(config.secret_reward_quality),
                    number=number,
                    protected=secret_protected)
                if reward:
                    fallback_push = (px, py)
                    fallback_depth = min(1.0, approach_distance / max_room_distance)
                    break
        if reward:
            rewards.append(reward); secret_variants.append(variant); reserved.add(reward)
            if fallback_push is None:
                raise ValueError("fallback secret lost its pushwall metadata")
            secret_details.append(SecretDetail(
                variant, 7 if number == 9 else 3, -1, fallback_depth,
                fallback_push, False))
        else:
            break
    reserved.update(secret_protected)
    known_push_directions = {detail.pushwall: detail.push_direction
                             for detail in secret_details}
    reserved.update((index % GRID
                     - known_push_directions.get((index % GRID, index // GRID), 1),
                     index // GRID)
                    for index, thing in enumerate(things) if thing == PUSHWALL)
    if is_boss and scheduled_gate.colors[:1] == ("silver",):
        anchor_route_end = critical_route.index(anchor_index) + 1
        door_gate_plan = GatePlan(("silver",))
        door_route = critical_route[:anchor_route_end]
        door_target = rooms[anchor_index].center
    elif is_boss:
        door_gate_plan = GatePlan()
        door_route = critical_route
        door_target = rooms[anchor_index].center
    else:
        door_gate_plan = scheduled_gate
        door_route = critical_route
        door_target = exit_stand
    rare_key_reservations: set[tuple[int, int]] = set()
    if rare_room_index >= 0:
        rare_room = rooms[rare_room_index]
        rare_key_reservations = {
            (x, y) for y in range(rare_room.y, rare_room.y + rare_room.h)
            for x in range(rare_room.x, rare_room.x + rare_room.w)
            if _is_floor(_at(tiles, x, y))}
        reserved.update(rare_key_reservations)
    locks, key_order, key_objectives = _place_doors(
        tiles, things, rooms, edges, paths, rng, start, door_target, roles,
        reserved, door_gate_plan, door_route)
    reserved.difference_update(rare_key_reservations)
    for objective in key_objectives:
        room = rooms[objective.host_room]
        inward = sorted(
            ((objective.cell[0] + dx, objective.cell[1] + dy)
             for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
             if room.x <= objective.cell[0] + dx < room.x + room.w
             and room.y <= objective.cell[1] + dy < room.y + room.h
             and _is_floor(_at(tiles, objective.cell[0] + dx,
                               objective.cell[1] + dy))),
            key=lambda cell: (abs(cell[0] - room.center[0])
                              + abs(cell[1] - room.center[1]), cell))
        if inward:
            reserved.add(inward[0])
    _break_long_sightlines(tiles, things, rooms, reserved, rng, start)
    _split_oversized_zones(tiles, rooms, rng, reserved)
    if _remove_redundant_plain_doors(tiles):
        # A removed door can extend a floor-only sightline which the earlier
        # pass correctly treated as interrupted; repair only that new case.
        _break_long_sightlines(tiles, things, rooms, reserved, rng, start,
                               allow_doors=False, walls_for_redundant_doors=True)
    # Collapse tight double-doorways where a corridor clips a pinched room
    # corner into a single clean threshold (rare; leaves wide double entrances).
    door_pushwalls = {(index % GRID, index // GRID)
                      for index, thing in enumerate(things) if thing == PUSHWALL}
    if _heal_pinched_room_door_pairs(tiles, rooms, start, door_pushwalls):
        _break_long_sightlines(tiles, things, rooms, reserved, rng, start,
                               allow_doors=False, walls_for_redundant_doors=True)
    # With the gate probe below, seeds 0--19 on floors 2/5/8 placed at most
    # two doors (40 total doors at most), retried no maps, and left 41/60
    # samples with a second visible base material share of at least 10%.
    _limit_theme_merge_size(tiles, rooms, rng, reserved)
    if sum(tile in DOORS for tile in tiles) > 56:
        raise ValueError("door budget exceeded")
    guard_recesses = _carve_guard_recesses(
        tiles, things, rooms, specs, roles, reserved, rng, start, exit_room)
    boss_room = None
    boss_arena_detail = None
    preboss_index = None
    # Secret pockets are authored pickup compositions too. Record every
    # reward sprite, not only the focal cell exposed in secret metadata, so a
    # pocket that happens to overlap another room's rectangular bookkeeping
    # boundary cannot be mistaken for loose, untracked room loot.
    secret_pickups = tuple(
        (x, y, _at(things, x, y))
        for x, y in sorted(secret_protected)
        if _at(things, x, y) in PICKUP_CODES)
    pickup_placements: list[SpritePlacement] = (
        [SpritePlacement("secret-reward", "secret-cache", -1,
                         secret_pickups)] if secret_pickups else [])
    if is_boss:
        boss_index = anchor_index
        boss_room = rooms[boss_index]
        boss_choice = rng.choice(tuple(sorted(KEY_DROP_BOSSES)))
        boss_arena_detail = _prepare_boss_arena(
            tiles, things, boss_room, reserved, rng, plan.special_family)
        realized_shapes[boss_index] = f"boss-{boss_arena_detail.profile}"
        boss = _place_boss(tiles, things, boss_room, reserved, rng,
                           room_index=boss_index, placements=pickup_placements,
                           boss=boss_choice, family=plan.special_family)
        preboss_index = next((index for index, role in enumerate(roles)
                              if role == "staging"),
                             _room_predecessor(len(rooms), edges, boss_index))
        if preboss_index is not None and rooms[preboss_index] == exit_room:
            preboss_index = None
        boss_cell = next((index % GRID, index // GRID)
                         for index, thing in enumerate(things) if thing == boss)
        key_objectives = key_objectives + (
            KeyObjective("gold", boss_cell, boss_index, len(key_order) + 1,
                         0, "boss-drop"),)
        locks += 1
        key_order = key_order + ("gold",)
    # Resolve architecture and room identity before population. Encounters
    # consume the same role/theme/concept decision as decoration rather than
    # independently guessing what kind of room they occupy.
    _assign_sound_zones(tiles)
    component_of, group_theme = _assign_area_themes(tiles, rooms, districts, rng, number,
                                                    theme_pool=floor_variant.theme_pool)
    jail_rooms = _select_jail_rooms(rooms, districts, component_of, group_theme, tiles, rng,
                                    jail_probability=floor_variant.jail_probability)
    identities = _room_identities(rooms, specs, districts, edges, floor_variant, jail_rooms,
                                  component_of, group_theme, exit_room, boss_room,
                                  plan.special_family, key_objectives)
    landmarks = _apply_wall_theme(tiles, things, rooms, districts, component_of, group_theme,
                                  rng, jail_rooms, identities=identities,
                                  atmosphere=int(config.atmosphere))
    exit_pushwall = next((detail.pushwall for detail in secret_details
                          if detail.secret_exit), None)
    # Only the inner wall of a nested double secret is deliberately forced
    # plain, so the second chamber is not given away. Ordinary secrets are
    # already not uniformly telegraphed: plaster pushwalls stay plain by design
    # and a few materials have no landmark accent, so hint_secrets never marks
    # every secret to begin with. The nested outer wall and the mandatory
    # secret-exit wall are always marked.
    nested_inner = {(detail.pushwall[0] + 4 * detail.push_direction,
                     detail.pushwall[1])
                    for detail in secret_details if detail.shape == "nested"}
    plain_walls = frozenset(nested_inner)
    secret_hints = _hint_secrets(tiles, things, component_of, group_theme, rng,
                                 special_pushwall=exit_pushwall,
                                 plain_walls=plain_walls)
    if exit_pushwall is not None and secret_hints.get(exit_pushwall) != "symmetric-landmark":
        raise ValueError("secret elevator host cannot support its landmark hint")
    # Keep each recorded treatment truthful to what was actually painted.
    secret_details = [
        replace(detail, hint_treatment=secret_hints.get(detail.pushwall,
                                                        detail.hint_treatment))
        for detail in secret_details]
    rare_motif_detail = None
    if rare_profile is not None:
        room_index, realization, endpoints = rare_profile
        for cell in endpoints:
            item = 62
            if (_is_floor(_at(tiles, *cell)) and _at(things, *cell) == 0
                    and cell not in reserved):
                _set(things, *cell, item)
                reserved.add(cell)
        rare_motif_detail = RareMotifDetail(
            "swastika", room_index, realization, endpoints)
    gallery_eligible = frozenset(
        index for index in range(1, len(rooms))
        if index not in critical_route and rooms[index] != exit_room
        and index not in {objective.host_room for objective in key_objectives}
        and roles[index] not in {"arrival", "victory", "recovery", "boss-arena",
                                 "staging", "premium-vault"})
    guard_galleries = (_place_guard_gallery(
        tiles, things, rooms, identities, realized_shapes, reserved, rng, start,
        gallery_eligible) if guard_gallery_enabled else ())
    actor_clearance: set[tuple[int, int]] = set()
    calm_rooms = frozenset(index for index, role in enumerate(roles)
                           if role in ("arrival", "victory"))
    optional_rooms = frozenset(index for index in range(len(rooms))
                               if index not in critical_route
                               and rooms[index] != exit_room)
    encounters: list[EncounterPlacement] = []
    enemy_tiers = _place_population(
        config, number, rooms, tiles, things, reserved, rng, start, exit_room,
        patrol_chance=PATROL_TARGETS[int(config.patrol_activity)],
        placements=pickup_placements, actor_clearance=actor_clearance,
        progression_number=(secret_source if number == 10 and secret_source else number),
        calm_rooms=calm_rooms, boss_room=boss_room,
        optional_rooms=optional_rooms, identities=identities,
        critical_route=tuple(critical_route), guard_recesses=guard_recesses,
        key_objectives=key_objectives, encounter_out=encounters)
    gallery_tiers = _populate_guard_galleries(
        guard_galleries, things, number, rng, encounters)
    enemy_tiers = tuple(ordinary + gallery
                        for ordinary, gallery in zip(enemy_tiers, gallery_tiers))
    premium_index = (next((index for index, role in enumerate(roles)
                           if role == "premium-vault"), None)
                     if number == 10 else None)
    expedition_rooms: list[int] = []
    _place_authored_pickups(
        config, number, rooms, tiles, things, reserved, rng, start, identities,
        critical_route, edges, pickup_placements, preboss_index=preboss_index,
        premium_index=premium_index,
        expedition_candidates=tuple(optional_rooms),
        expedition_rooms_out=expedition_rooms)
    reserved.difference_update(notch_cells)
    # A notch anchor can also be the open tile directly in front of an actor.
    # Releasing the architectural reservation must not erase that later,
    # independent reason to keep the cell clear.
    reserved.update(actor_clearance)
    lighting_families, vine_screens = _place_decorations(
        rooms, tiles, things, reserved, start, rng, roles=roles, specs=specs,
        jail_rooms=jail_rooms,
        density=(floor_variant.decor_density
                 * DECORATION_MULTIPLIERS[int(config.decoration_amount)]),
        theme_overrides=floor_variant.decor_overrides, landmarks=landmarks,
        paths=paths, identities=identities, atmosphere=int(config.atmosphere),
        notch_anchors=notch_anchors, hallway_vine_budget=hallway_vine_budget,
        allow_sky_vista=sky_vista_enabled)
    primary_hall_geometry = _primary_hall_geometry(plan, rooms, specs)
    barrel_families = _barrel_families(rooms, things)
    sky_vistas, sky_vista_recesses, sky_vista_supports = _harvest_sky_vistas(
        tiles, things)
    final_distances = _floor_distances(tiles, start)
    deepest_room_distance = max((final_distances.get(room.center, 0) for room in rooms),
                                default=1) or 1
    exit_depth_ratio = final_distances.get(exit_stand, 0) / deepest_room_distance
    layout_signature = _layout_signature(
        plan, specs, realized_shapes, guard_recesses, encounters, edges)
    result = GeneratedMap(number=number, tiles=tiles, things=things, start=start,
                          exit_stand=exit_stand, secret_rewards=rewards, seed=seed,
                          has_secret_exit=secret_exit, locked_doors=locks, boss=is_boss,
                          enemy_tiers=enemy_tiers, motifs=plan.motifs,
                          motif_rooms=tuple(spec.motif for spec in specs),
                          secret_variants=tuple(secret_variants),
                          shortcut_pushwalls=tuple(shortcut_pushwalls), rooms=tuple(rooms),
                          edges=tuple(edges), jail_rooms=jail_rooms,
                          variant=floor_variant.name,
                          room_concepts=tuple(identity.concept for identity in identities),
                          key_order=key_order,
                          critical_route=tuple(critical_route),
                          room_districts=tuple(districts),
                          exit_depth_ratio=exit_depth_ratio,
                          room_roles=tuple(roles),
                          room_tiers=tuple(spec.tier for spec in specs),
                          circulation_skeleton=plan.skeleton,
                          district_circulation=plan.district_circulation,
                          layout_signature=layout_signature,
                          pickup_placements=tuple(pickup_placements),
                          room_shapes=tuple(realized_shapes),
                          lighting_families=lighting_families,
                          vine_screens=vine_screens,
                          key_objectives=key_objectives,
                          secret_details=tuple(secret_details),
                          special_family=plan.special_family,
                          boss_arena_room=anchor_index if is_boss else -1,
                          preboss_room=preboss_index if preboss_index is not None else -1,
                          premium_room=premium_index if premium_index is not None else -1,
                          expedition_rooms=tuple(dict.fromkeys(expedition_rooms)),
                          secret_source=secret_source or 0,
                          arrival=arrival, guard_recesses=guard_recesses,
                          guard_galleries=guard_galleries,
                          encounters=tuple(encounters),
                          patrol_target=PATROL_TARGETS[int(config.patrol_activity)],
                          progression_grammar=plan.progression_grammar,
                          motif_realizations=plan.motif_realizations,
                          rare_motif=rare_motif_detail,
                          boss_arena=boss_arena_detail,
                          shape_target=shape_target,
                          primary_hall_geometry=primary_hall_geometry,
                          barrel_families=barrel_families,
                          sky_vistas=sky_vistas,
                          sky_vista_recesses=sky_vista_recesses,
                          sky_vista_supports=sky_vista_supports)
    validate_map(result)
    result.critique = _critique(result)
    return result




def generate_campaign(config: CampaignConfig, output: Path,
                      progress: Callable[[int, int], None] | None = None,
                      cancelled: Callable[[], bool] | None = None) -> Path:
    schedule = resolve_schedule(config)
    levels = []
    secret_from = schedule.secret_from
    vine_floor, vine_budget = schedule.vine_floor, schedule.vine_budget
    gallery_floor = schedule.gallery_floor
    rare_motif_floor = schedule.rare_motif_floor
    vista_parity = schedule.vista_parity
    for number in range(1, 11):
        if cancelled and cancelled():
            raise GenerationCancelled("campaign generation cancelled")
        last_error = None
        candidates: list[GeneratedMap] = []
        clean: list[GeneratedMap] = []
        for attempt in range(50):
            try:
                candidate = generate_map(config, number, attempt,
                                         **schedule.floor_options(number))
            except ValueError as error:
                last_error = error
                continue
            candidates.append(candidate)
            if not candidate.critique:
                if not config.say_aardwolf:
                    levels.append(candidate)
                    break
                clean.append(candidate)
                if len(clean) == 2:
                    levels.append(max(
                        clean, key=lambda level: _candidate_score(
                            level, levels, config)))
                    break
            if config.say_aardwolf and len(candidates) == 8:
                pool = clean or candidates
                levels.append(max(
                    pool, key=lambda level: (
                        -len(level.critique),
                        _candidate_score(level, levels, config))))
                break
            if not config.say_aardwolf and len(candidates) == 3:
                levels.append(min(candidates, key=lambda level: len(level.critique)))
                break
        else:
            if candidates:
                if config.say_aardwolf:
                    pool = clean or candidates
                    levels.append(max(
                        pool, key=lambda level: (
                            -len(level.critique),
                            _candidate_score(level, levels, config))))
                else:
                    levels.append(min(candidates,
                                      key=lambda level: len(level.critique)))
            else:
                raise RuntimeError(f"floor {number} failed generation: {last_error}")
        if progress:
            progress(number, 10)
    realized_vine_floors = {
        level.number for level in levels
        if any(screen.kind == "hallway-run" for screen in level.vine_screens)}
    realized_vine_runs = sum(
        screen.kind == "hallway-run" for level in levels for screen in level.vine_screens)
    if (realized_vine_floors - {vine_floor}
            or len(realized_vine_floors) > 1
            or realized_vine_runs > vine_budget):
        raise RuntimeError("campaign hallway-vine budget was violated")
    realized_gallery_floors = {
        level.number for level in levels if level.guard_galleries}
    if realized_gallery_floors - {gallery_floor} or len(realized_gallery_floors) > 1:
        raise RuntimeError("campaign guard-gallery budget was violated")
    if any(first.variant == second.variant
           for first, second in zip(levels, levels[1:])):
        raise RuntimeError("campaign repeated the same floor type consecutively")
    if any(first.circulation_skeleton == second.circulation_skeleton
           for first, second in zip(levels, levels[1:])):
        raise RuntimeError("campaign repeated the same circulation skeleton consecutively")
    if sum(level.circulation_skeleton in HALLWAY_FIRST_SKELETONS
           for level in levels) != 3:
        raise RuntimeError("campaign violated its three-floor hallway-first schedule")
    if any(first.progression_grammar == second.progression_grammar
           for first, second in zip(levels, levels[1:])):
        raise RuntimeError("campaign repeated the same progression grammar consecutively")
    if any(first.sky_vistas and second.sky_vistas
           for first, second in zip(levels, levels[1:])):
        raise RuntimeError("campaign repeated the exterior-vista motif consecutively")
    realized_rare = [level.number for level in levels if level.rare_motif is not None]
    expected_rare = [rare_motif_floor] if rare_motif_floor else []
    if realized_rare != expected_rare:
        raise RuntimeError("campaign rare-motif schedule was violated")
    # Encode metadata-independent provenance only after every gameplay choice
    # is final. Zone-label permutations preserve all acoustic grouping.
    from .watermark import apply_campaign_watermark
    apply_campaign_watermark(levels, config.seed)
    for level in levels:
        validate_map(level)
    manifest = _manifest(
        config, levels, secret_from, vine_floor, vine_budget, realized_vine_runs,
        gallery_floor, realized_gallery_floors, rare_motif_floor, realized_rare,
        vista_parity, _lock_schedule(config))
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".tmp", delete=False) as temporary:
        temp_path = Path(temporary.name)
    try:
        if cancelled and cancelled():
            raise GenerationCancelled("campaign generation cancelled")
        with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as package:
            def write(name: str, data: str | bytes) -> None:
                info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                package.writestr(info, data)
            write("mapinfo.txt", _mapinfo(secret_from, tuple(level.variant for level in levels)))
            write("infiniwolf-manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
            write("infiniwolf-settings.txt", _reproducibility_text(config, secret_from))
            for level in levels:
                write(f"maps/iw{level.number:02d}.wad",
                      _wad_bytes(f"IW{level.number:02d}", level.tiles, level.things))
        validate_package(temp_path)
        if cancelled and cancelled():
            raise GenerationCancelled("campaign generation cancelled")
        temp_path.replace(output)
    finally:
        temp_path.unlink(missing_ok=True)
    return output
