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
from .config import CampaignConfig, GenerationQuality

# The WL6 code vocabulary lives in the tiles leaf. Imported by name (rather
# than star) so it is re-exported from `infiniwolf.generator` exactly as
# before: generator_validation, the test suite, and tools/ all import these
# from here.
from .wl6 import (  # noqa: F401
    GRID, WALL, FLOOR, ZONE_MAX, DOOR_EW, DOOR_NS, DOOR_ELEVATOR, DOOR_ELEVATOR_NS,
    DOOR_GOLD_EW, DOOR_GOLD_NS, DOOR_SILVER_EW, DOOR_SILVER_NS, GOLD_DOORS, SILVER_DOORS,
    LOCKED_DOORS, DOORS, PLAYER_START_CODES, PLAYER_START, PUSHWALL, ELEVATOR_TILE,
    DUMMY_ELEVATOR_TILE, SECRET_EXIT_ZONE, GOLD_KEY, SILVER_KEY, HANS_GROSSE, SCHABBS, GRETEL,
    GIFT, FAT_FACE, MECHA_HITLER, FAKE_HITLER, GHOSTS, BOSSES, KEY_DROP_BOSSES,
    VICTORY_BOSSES, GUARDS,
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
from .ledger import Ledger
from .model import (  # noqa: F401
    AestheticPhase, ArrivalDetail, BossArenaDetail, EncounterPlacement, FloorPlan, FloorVariant,
    GatePlan, GeneratedMap, GuardGallery, GuardRecess, KeyObjective, PatrolRoute,
    PlacedPlan, RareMotifDetail, Room, RoomIdentity, RoomSpec, SecretDetail,
    SpritePlacement, VineScreen, PillarPlacement, RealizedVignette, FloorCanvas,
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
    _secret_reward, verify_exit_depth, verify_arena_terminus, install_secrets,
    select_exit_host, gate_plan_for_floor, add_boss_gate_objective,
)
from .planning import _plan_floor  # noqa: F401
from .vignettes import plan_vignettes
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
    _select_jail_rooms, plan_landmarks,
)
from .geometry import (  # noqa: F401
    DOOR_SPACING, _add_pillars, _adjacent_to_room, _carve_connection,
    _carve_notches, _carve_swastika_profile, _carve_symmetric_profiles,
    _door_axis, _door_candidate, _far_from_doors, _place_planned_rooms,
    _room_size, _snap_offsets, _widen_corridors, ShapeBudget, shape_budget,
    realize_room_shapes,
    SHAPE_MULTIPLIERS, SHAPE_TARGETS,
    _assign_sound_zones, _break_long_sightlines, _heal_pinched_room_door_pairs,
    _limit_theme_merge_size, _remove_redundant_plain_doors,
    _spatial_districts, _split_oversized_zones, _harvest_sky_vistas,
    _primary_hall_geometry, paint_room_floors, plan_authored_sightlines, carve_shared_void,
)
from .campaign import (  # noqa: F401
    CIRCULATION_MODES, CIRCULATION_SKELETONS, FLOOR_VARIANT_ROTATION,
    HALLWAY_FIRST_SKELETONS, PROGRESSION_GRAMMARS, RARE_MOTIF_CHANCE,
    VARIANT_STRONGHOLD, VARIANT_VAULT, _aardwolf_variant, _candidate_score,
    _circulation_sequence, _lock_schedule, _progression_sequence,
    _rare_motif_schedule, _set_distance, _variant_sequence,
    CampaignSchedule, resolve_schedule, _layout_signature, validate_campaign_budgets,
)
from .generator_artifacts import (  # noqa: F401
    _manifest, _wad_bytes, _mapinfo, _display_name,
    _reproducibility_text, read_manifest, validate_package,
)
from .watermark import apply_campaign_watermark
from .decorations import (  # noqa: F401
    SKY_VISTA_COURTYARD_CHANCE, SKY_VISTA_INTERIOR_CHANCE, _DECOR_BLOCKING, _DECOR_OPEN,
    _DECOR_ZONES, _FRAMEABLE, _LIGHTING_OPTIONS, _decor_theme, _lighting_family,
    _place_decorations, _place_zoned, _barrel_families, occupy_dead_end_alcoves,
)


DECORATION_MULTIPLIERS = (0.0, 0.70, 0.85, 1.00, 1.15, 1.30)
# Target share of ordinary actors that should visibly patrol. The old values
# were per-room attempt chances and produced only ~3% moving actors at the
# normal setting because most full-room loops failed geometry reservations.
PATROL_TARGETS = (0.0, 0.04, 0.09, 0.16, 0.23, 0.30)


class GenerationCancelled(RuntimeError):
    """Raised when a caller cancels before atomic package installation."""


def generate_map(config: CampaignConfig, number: int, attempt: int = 0,
                 secret_exit: bool = False, secret_source: int | None = None,
                 hallway_vine_budget: int = 0,
                 guard_gallery_enabled: bool = False,
                 rare_motif_enabled: bool = False,
                 sky_vista_enabled: bool = True,
                 boss: int | None = None,
                 phase: AestheticPhase | None = None,
                 shared_void_enabled: bool = False,
                 stream_advance: dict[str, int] | None = None,
                 ) -> GeneratedMap:
    streams = {name: random.Random(config.subsystem_seed(number, attempt, name))
               for name in ("planning", "geometry", "progression", "semantics",
                            "encounters", "pickups", "decorations", "special_floors")}
    planning_rng = streams["planning"]
    geometry_rng = streams["geometry"]
    progression_rng = streams["progression"]
    semantics_rng = streams["semantics"]
    encounters_rng = streams["encounters"]
    pickups_rng = streams["pickups"]
    decorations_rng = streams["decorations"]
    special_floors_rng = streams["special_floors"]
    # Test hook: deliberately consume one named stream without perturbing any
    # other subsystem. It makes the isolation contract directly testable.
    for name, draws in (stream_advance or {}).items():
        if name not in streams:
            raise ValueError(f"unknown floor stream: {name}")
        for _ in range(draws):
            streams[name].random()
    seed = config.subsystem_seed(number, attempt, "planning")
    tiles = [WALL] * (GRID * GRID)
    things = [0] * (GRID * GRID)
    complexity = int(config.layout_complexity)
    floor_variant = _aardwolf_variant(
        config, number, _variant_sequence(config)[number - 1])
    circulation_skeleton = _circulation_sequence(config)[number - 1]
    progression_grammar = _progression_sequence(config)[number - 1]
    scheduled_gate = _lock_schedule(config)[number - 1]
    is_boss = number == 9
    # Floor 9's boss is settled before the building program exists, because it
    # decides whether the program has a lift room at all. The campaign schedule
    # normally supplies it so a rejected attempt cannot re-roll him; a standalone
    # floor-9 call keeps the historic key-drop pair, which is also the first draw
    # off this stream either way.
    boss_choice = (boss if boss is not None
                   else special_floors_rng.choice(tuple(sorted(KEY_DROP_BOSSES)))
                   ) if is_boss else None
    # Two gates, and the boss picks which one. Hans and Gretel drop a gold key
    # natively, so their elevator stays locked and the kill is mandatory. The
    # other four end the campaign themselves (see wl6.VICTORY_BOSSES), so their
    # floor gets no elevator: the arena is the last room on the spine and the
    # kill is the only way off the floor.
    boss_locks_exit = is_boss and boss_choice in KEY_DROP_BOSSES
    boss_ends_floor = is_boss and boss_choice in VICTORY_BOSSES
    plan = _plan_floor(planning_rng, complexity, number, variant=floor_variant,
                       skeleton=circulation_skeleton,
                       progression_grammar=progression_grammar,
                       rare_motif=rare_motif_enabled,
                       boss_ends_floor=boss_ends_floor)
    placed = _place_planned_rooms(geometry_rng, plan, number)
    rooms = placed.rooms
    edges = placed.edges
    specs = [plan.specs[index] for index in placed.spec_indices]
    roles = [spec.role for spec in specs]
    districts = _spatial_districts(rooms, len({spec.district for spec in specs}))
    paint_room_floors(tiles, rooms)
    shape_policy = shape_budget(config, specs)
    shape_scale = shape_policy.scale
    shape_target = shape_policy.target
    shape_budget_limit = shape_policy.budget
    utility_shapes = shape_policy.utility_shapes
    rare_profile, notch_anchors, realized_shapes = realize_room_shapes(
        tiles, rooms, specs, geometry_rng, rare_motif_enabled=rare_motif_enabled,
        number=number, floor_variant=floor_variant, shape_scale=shape_scale,
        shape_budget=shape_budget_limit, utility_shapes=utility_shapes)
    overrides = dict(floor_variant.decor_overrides)
    for room, spec in zip(rooms, specs):
        predicted = overrides.get(_decor_theme(spec.role, spec.tier),
                                  _decor_theme(spec.role, spec.tier))
        structural = (predicted == "grand"
                      or (floor_variant.name == "catacombs" and spec.tier == "anchor"))
        if structural:
            _add_pillars(tiles, room, geometry_rng, chance=floor_variant.pillar_chance)
    # Bounded visual modifiers for this floor's place in the campaign. A
    # standalone call gets the neutral phase, so single-map generation is
    # unaffected by an arc it has no position in.
    phase = phase or AestheticPhase(1.0, 1.0, 1.0, 1.0, 1.0)
    planned_exit_index = roles.index("exit") if "exit" in roles else -1
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
            tiles, rooms[0], rooms[first_neighbor].center, geometry_rng,
            floor_variant.name)
    if boss_ends_floor:
        # No lift to host, so nothing to reserve for one: the arena is the
        # terminus and the route to it is the plan's own spine.
        preplaced_exit_index, preplaced_exit_route = -1, []
        exit_stand = None
        protected_elevators = set(arrival.footprint) if arrival else set()
    else:
        (preplaced_exit_index, preplaced_exit_route, exit_stand,
         protected_elevators) = select_exit_host(
             tiles, rooms, edges, anchor_index=anchor_index,
             minimum_route_rooms=minimum_route_rooms,
             required_post_anchor=required_post_anchor,
             planned_exit_index=planned_exit_index, boss_locks_exit=boss_locks_exit,
             arrival=arrival)
    door_zones: set[tuple[int, int]] = ({arrival.portal} if arrival else set())
    paths = [_carve_connection(tiles, rooms[a], rooms[b], geometry_rng, complexity,
                               door_zones, protected_elevators)
             for a, b in edges]
    _widen_corridors(tiles, rooms, paths, geometry_rng,
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
    exit_room = rooms[preplaced_exit_index] if exit_stand is not None else None
    if boss_ends_floor:
        # The arena is the last authored room, so the mandatory route is the
        # graph path to it and there is no shallower alternative to reject.
        critical_route = verify_arena_terminus(
            tiles, rooms, edges, start, anchor_index, minimum_route_rooms)
    else:
        # The elevator belongs near the deepest authored frontier, after the
        # anchor/climax room and at the end of a route containing most of the
        # mandatory spine. The old behavior always tried the nominal exit first,
        # even when a much deeper wing made it a trivial early solution.
        exit_index, critical_route = verify_exit_depth(
            tiles, rooms, edges, roles, start, exit_stand, anchor_index,
            minimum_route_rooms, required_post_anchor, preplaced_exit_index,
            preplaced_exit_route, planned_exit_index)
    notch_cells = {cell for cells in notch_anchors.values() for cell in cells}
    # A Ledger is a set subclass, so every existing .add/.update/in/|/- keeps
    # working and adopting it cannot change generated output. Attribution is added
    # pass by pass; anything still calling .add() records as unattributed, and
    # ledger.report() shows how far that migration has reached.
    reserved = Ledger()
    reserved.reserve([start], "progression", "player-start")
    if exit_stand is not None:
        reserved.reserve([exit_stand], "progression", "exit-elevator-stand")
    reserved.reserve(notch_cells, "geometry", "shape-anchor", hard=False)
    if arrival:
        reserved.reserve(arrival.clearance, "progression", "arrival-clearance")
        reserved.reserve(arrival.car_cells, "progression", "arrival-car")
    # One record for the working state every remaining phase touches. Built here
    # rather than at the top of generate_map because `reserved` only becomes
    # meaningful once the elevators and shape anchors have claimed their cells.
    canvas = FloorCanvas(tiles=tiles, things=things, rooms=rooms, specs=specs,
                         roles=roles, edges=edges, reserved=reserved, start=start)
    installation = install_secrets(
        canvas, config, number, progression_rng, arrival=arrival, exit_room=exit_room,
        anchor_index=anchor_index, critical_route=critical_route,
        rare_profile=rare_profile, secret_exit=secret_exit)
    rewards = list(installation.rewards)
    secret_variants = list(installation.variants)
    secret_details = list(installation.details)
    shortcut_pushwalls = list(installation.shortcut_pushwalls)
    secret_protected = installation.protected
    reserved.reserve(secret_protected, "progression", "secret-footprint")
    rare_room_index = rare_profile[0] if rare_profile is not None else -1
    known_push_directions = {detail.pushwall: detail.push_direction
                             for detail in secret_details}
    reserved.reserve(((index % GRID
                       - known_push_directions.get((index % GRID, index // GRID), 1),
                       index // GRID)
                      for index, thing in enumerate(things) if thing == PUSHWALL),
                     "progression", "pushwall-travel")
    door_gate_plan, door_route, door_target = gate_plan_for_floor(
        is_boss, scheduled_gate, critical_route, anchor_index, rooms, exit_stand)
    rare_key_reservations: set[tuple[int, int]] = set()
    if rare_room_index >= 0:
        rare_room = rooms[rare_room_index]
        rare_key_reservations = {
            (x, y) for y in range(rare_room.y, rare_room.y + rare_room.h)
            for x in range(rare_room.x, rare_room.x + rare_room.w)
            if _is_floor(_at(tiles, x, y))}
        # Soft and briefly held: this keeps the rare motif's room out of the
        # door planner's reach, and is released immediately afterwards.
        reserved.reserve(rare_key_reservations, "special_floors",
                         "rare-motif-key-exclusion", hard=False)
    locks, key_order, key_objectives = _place_doors(
        tiles, things, rooms, edges, paths, progression_rng, start, door_target, roles,
        reserved, door_gate_plan, door_route)
    reserved.release(rare_key_reservations, "special_floors",
                     "rare-motif-exclusion-expired")
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
            reserved.reserve(inward[:1], "progression", "key-approach",
                             room_index=objective.host_room)
    _break_long_sightlines(tiles, things, rooms, reserved, geometry_rng, start)
    _split_oversized_zones(tiles, rooms, geometry_rng, reserved)
    if _remove_redundant_plain_doors(tiles):
        # A removed door can extend a floor-only sightline which the earlier
        # pass correctly treated as interrupted; repair only that new case.
        _break_long_sightlines(tiles, things, rooms, reserved, geometry_rng, start,
                               allow_doors=False, walls_for_redundant_doors=True)
    # Collapse tight double-doorways where a corridor clips a pinched room
    # corner into a single clean threshold (rare; leaves wide double entrances).
    door_pushwalls = {(index % GRID, index // GRID)
                      for index, thing in enumerate(things) if thing == PUSHWALL}
    if _heal_pinched_room_door_pairs(tiles, rooms, start, door_pushwalls):
        _break_long_sightlines(tiles, things, rooms, reserved, geometry_rng, start,
                               allow_doors=False, walls_for_redundant_doors=True)
    # With the gate probe below, seeds 0--19 on floors 2/5/8 placed at most
    # two doors (40 total doors at most), retried no maps, and left 41/60
    # samples with a second visible base material share of at least 10%.
    _limit_theme_merge_size(tiles, rooms, geometry_rng, reserved)
    if sum(tile in DOORS for tile in tiles) > 56:
        raise ValueError("door budget exceeded")
    # Before recesses, population and pickups: the void must own its cells outright
    # so nothing places an actor or a reward the player can see and never reach.
    shared_void = None
    if shared_void_enabled:
        shared_void = carve_shared_void(tiles, things, rooms, reserved, geometry_rng, start)
        if shared_void is not None:
            reserved.reserve(shared_void.interior, "geometry", "shared-void-interior")
            reserved.reserve(shared_void.screens, "geometry", "shared-void-screen")
    guard_recesses = _carve_guard_recesses(
        tiles, things, rooms, specs, roles, reserved, encounters_rng, start, exit_room)
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
        boss_arena_detail = _prepare_boss_arena(
            tiles, things, boss_room, reserved, special_floors_rng, plan.special_family)
        realized_shapes[boss_index] = f"boss-{boss_arena_detail.profile}"
        boss = _place_boss(tiles, things, boss_room, reserved, special_floors_rng,
                           room_index=boss_index, placements=pickup_placements,
                           boss=boss_choice, family=plan.special_family)
        preboss_index = next((index for index, role in enumerate(roles)
                              if role == "staging"),
                             _room_predecessor(len(rooms), edges, boss_index))
        if preboss_index is not None and rooms[preboss_index] == exit_room:
            preboss_index = None
        boss_cell = next((index % GRID, index // GRID)
                         for index, thing in enumerate(things) if thing == boss)
        # Only a boss with a native gold drop contributes a key objective. The
        # others end the floor by dying, so no gold exists on it and claiming
        # otherwise would fail key solvency.
        if boss_locks_exit:
            key_objectives, locks, key_order = add_boss_gate_objective(
                key_objectives, locks, key_order, boss_cell, boss_index)
    # Resolve architecture and room identity before population. Encounters
    # consume the same role/theme/concept decision as decoration rather than
    # independently guessing what kind of room they occupy.
    _assign_sound_zones(tiles)
    component_of, group_theme = _assign_area_themes(tiles, rooms, districts, semantics_rng, number,
                                                    theme_pool=floor_variant.theme_pool)
    jail_rooms = _select_jail_rooms(rooms, districts, component_of, group_theme, tiles, semantics_rng,
                                    jail_probability=floor_variant.jail_probability)
    identities = _room_identities(rooms, specs, districts, edges, floor_variant, jail_rooms,
                                  component_of, group_theme, exit_room, boss_room,
                                  plan.special_family, key_objectives)
    # Intent is selected from semantic identities and graph adjacency once.  It
    # derives from campaign seed/floor, not attempt RNG, so retries cannot reroll it.
    vignette_plans = plan_vignettes(config.seed, number, rooms, identities, edges,
                                    tuple(critical_route), special_family=plan.special_family)
    vignette_encounters = {room: item.encounter_treatment
                           for item in vignette_plans for room in item.rooms[:1]}
    vignette_motifs = {room: item.decoration_treatment
                       for item in vignette_plans for room in item.rooms}
    landmarks = _apply_wall_theme(tiles, things, rooms, districts, component_of, group_theme,
                                  semantics_rng, jail_rooms, identities=identities,
                                  atmosphere=int(config.atmosphere),
                                  damage_scale=phase.damage)
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
    secret_hints = _hint_secrets(tiles, things, component_of, group_theme, semantics_rng,
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
                reserved.reserve([cell], "special_floors", "rare-motif-accent")
        rare_motif_detail = RareMotifDetail(
            "swastika", room_index, realization, endpoints)
    gallery_eligible = frozenset(
        index for index in range(1, len(rooms))
        if index not in critical_route and rooms[index] != exit_room
        and index not in {objective.host_room for objective in key_objectives}
        and roles[index] not in {"arrival", "victory", "recovery", "boss-arena",
                                 "staging", "premium-vault"})
    guard_galleries = (_place_guard_gallery(
        tiles, things, rooms, identities, realized_shapes, reserved, special_floors_rng, start,
        gallery_eligible) if guard_gallery_enabled else ())
    actor_clearance: set[tuple[int, int]] = set()
    calm_rooms = frozenset(index for index, role in enumerate(roles)
                           if role in ("arrival", "victory"))
    optional_rooms = frozenset(index for index in range(len(rooms))
                               if index not in critical_route
                               and rooms[index] != exit_room)
    encounters: list[EncounterPlacement] = []
    enemy_tiers = _place_population(
        config, number, rooms, tiles, things, reserved, encounters_rng, start, exit_room,
        patrol_chance=PATROL_TARGETS[int(config.patrol_activity)],
        placements=pickup_placements, actor_clearance=actor_clearance,
        progression_number=(secret_source if number == 10 and secret_source else number),
        calm_rooms=calm_rooms, boss_room=boss_room,
        optional_rooms=optional_rooms, identities=identities,
        critical_route=tuple(critical_route), guard_recesses=guard_recesses,
        key_objectives=key_objectives, encounter_out=encounters,
        vignette_treatments=vignette_encounters)
    gallery_tiers = _populate_guard_galleries(
        guard_galleries, things, number, encounters_rng, encounters)
    enemy_tiers = tuple(ordinary + gallery
                        for ordinary, gallery in zip(enemy_tiers, gallery_tiers))
    premium_index = (next((index for index, role in enumerate(roles)
                           if role == "premium-vault"), None)
                     if number == 10 else None)
    expedition_rooms: list[int] = []
    _place_authored_pickups(
        config, number, rooms, tiles, things, reserved, pickups_rng, start, identities,
        critical_route, edges, pickup_placements, preboss_index=preboss_index,
        premium_index=premium_index,
        expedition_candidates=tuple(optional_rooms),
        expedition_rooms_out=expedition_rooms, vignettes=vignette_plans)
    # Shape anchors were reserved soft precisely so decoration can have them
    # back once population and pickups have finished with the room.
    reserved.release(notch_cells, "geometry", "shape-anchor-released")
    # A notch anchor can also be the open tile directly in front of an actor.
    # Releasing the architectural reservation must not erase that later,
    # independent reason to keep the cell clear.
    reserved.reserve(actor_clearance, "encounters", "actor-clearance")
    # Landmarks before decoration, not after: decoration reinforces a landmark
    # rather than inventing one, and the framed approach below has to be reserved
    # before any prop can occupy it.
    landmark_plans = plan_landmarks(
        rooms, specs, roles, edges, districts, critical_route)
    authored_sightlines = plan_authored_sightlines(tiles, things, rooms, landmark_plans)
    # Hard claims: a decoration pass may not fill in the view a player gets on
    # entering the floor's primary space. Reserving rather than carving means no
    # geometry moves, so reachability, sound zones and door axes are untouched.
    for line in authored_sightlines:
        reserved.reserve(line.cells, "semantics", "framed-landmark-view",
                         room_index=line.target_room)
    lighting_families, vine_screens, room_motifs, pillar_placements = _place_decorations(
        rooms, tiles, things, reserved, start, decorations_rng, roles=roles, specs=specs,
        jail_rooms=jail_rooms,
        density=(floor_variant.decor_density
                 * DECORATION_MULTIPLIERS[int(config.decoration_amount)]),
        theme_overrides=floor_variant.decor_overrides, landmarks=landmarks,
        paths=paths, identities=identities, atmosphere=int(config.atmosphere),
        notch_anchors=notch_anchors, hallway_vine_budget=hallway_vine_budget,
        allow_sky_vista=sky_vista_enabled, phase=phase,
        vignette_motifs=vignette_motifs)
    # Last, because it has to see everything already committed: an alcove the
    # ambush pass gave a sentry, or that decoration already filled, is not a
    # hole that needs filling. Anything still empty here is one.
    occupy_dead_end_alcoves(tiles, things, rooms, identities, reserved, decorations_rng, start)
    primary_hall_geometry = _primary_hall_geometry(plan, rooms, specs)
    barrel_families = _barrel_families(rooms, things)
    sky_vistas, sky_vista_recesses, sky_vista_supports = _harvest_sky_vistas(
        tiles, things)
    final_distances = _floor_distances(tiles, start)
    deepest_room_distance = max((final_distances.get(room.center, 0) for room in rooms),
                                default=1) or 1
    # How deep the thing that ends the floor sits: the elevator stand, or the
    # boss himself when killing him is what ends it.
    terminus = exit_stand if exit_stand is not None else rooms[anchor_index].center
    exit_depth_ratio = final_distances.get(terminus, 0) / deepest_room_distance
    layout_signature = _layout_signature(
        plan, specs, realized_shapes, guard_recesses, encounters, edges)
    realized_vignettes = tuple(
        RealizedVignette(item.family, item.rooms,
                         tuple(sorted({entry.room_index for entry in encounters
                                       if entry.room_index in item.rooms})),
                         tuple(sorted({entry.room_index for entry in pickup_placements
                                       if entry.reason == f"vignette-{item.family}"})),
                         tuple(room for room in item.rooms
                               if room_motifs[room] == item.decoration_treatment))
        for item in vignette_plans)
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
                          sky_vista_supports=sky_vista_supports,
                          landmarks=landmark_plans,
                          room_motifs=room_motifs,
                          authored_sightlines=authored_sightlines,
                          shared_void=shared_void,
                          pillar_placements=pillar_placements,
                          vignette_plans=vignette_plans,
                          realized_vignettes=realized_vignettes)
    validate_map(result)
    result.critique = _critique(result)
    return result




def _best_candidate(candidates, clean, accepted, config):
    """Pick among hard-valid candidates: fewest critique flags first, then score.

    Flag count dominates deliberately. A candidate with a concrete defect should
    never outrank a clean one because it contrasts more with the previous floor;
    contrast is a tiebreaker among equally sound maps, not a currency that buys
    off a defect.
    """
    pool = clean or candidates
    return max(pool, key=lambda level: (-len(level.critique),
                                        _candidate_score(level, accepted, config)))


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
    quality = config.generation_quality
    if config.say_aardwolf:
        quality = GenerationQuality.THOROUGH
    pool_size = quality.pool_size
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
                clean.append(candidate)
            if quality is GenerationQuality.FAST and clean:
                levels.append(clean[0])
                break
            if len(clean) >= pool_size or len(candidates) >= pool_size:
                levels.append(_best_candidate(candidates, clean, levels, config))
                break
        else:
            if not candidates:
                raise RuntimeError(f"floor {number} failed generation: {last_error}")
            levels.append(_best_candidate(candidates, clean, levels, config))
        if progress:
            progress(number, 10)
    validate_campaign_budgets(levels, schedule)
    realized_vine_runs = sum(
        screen.kind == "hallway-run" for level in levels for screen in level.vine_screens)
    realized_gallery_floors = {
        level.number for level in levels if level.guard_galleries}
    realized_rare = [level.number for level in levels if level.rare_motif is not None]
    # Encode metadata-independent provenance only after every gameplay choice
    # is final. Zone-label permutations preserve all acoustic grouping.
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
