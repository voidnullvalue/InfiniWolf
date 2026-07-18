"""Generated-map validation."""

from __future__ import annotations

from collections import Counter, deque
import math

from .generator import (
    AMMO, AUTHORED_PICKUP_TEMPLATES, BOSSES, CHAINGUN,
    CIRCULATION_MODES, CIRCULATION_SKELETONS, DECOR_WALLS, DOORS,
    DOOR_ELEVATOR, DOOR_ELEVATOR_NS, DUMMY_ELEVATOR_TILE, ELEVATOR_TILE,
    ENEMY_CODES, EncounterPlacement, GHOSTS, GOLD_DOORS, GOLD_KEY, GRID,
    GeneratedMap, HALLWAY_FIRST_SKELETONS, KEY_DROP_BOSSES,
    LIGHTING_FAMILY_ITEMS, LIGHTING_ITEMS, LOCKED_DOORS, MACHINE_GUN,
    ONE_UP, PATROLS_BY_FAMILY, PATROL_POINT_DIRECTIONS, PICKUP_CODES,
    PLAYER_START_CODES, PROGRESSION_GRAMMARS, PURPLE_MIN_FLOOR, PUSHWALL,
    SECRET_EXIT_ZONE, SECRET_HINT_WALLS, SILVER_DOORS, SILVER_KEY,
    SPEAR_CONCEPTS, TREASURE, _at, _codes_for_colors, _door_zone,
    _floor_distances, _inside_room, _is_floor, _minimum_critical_route_rooms,
    _path_bends, _reachable, _room_graph_path, _room_predecessor,
    _shortest_floor_path,
)

def validate_map(level: GeneratedMap) -> None:
    if len(level.tiles) != GRID * GRID or len(level.things) != GRID * GRID:
        raise ValueError("invalid plane dimensions")
    if 63 in level.things:
        raise ValueError("Call Apogee decoration is forbidden")
    if (level.number < PURPLE_MIN_FLOOR
            and any(tile in {19, 25} for tile in level.tiles)):
        raise ValueError("purple wall material appears before the late campaign")
    if level.arrival is None:
        raise ValueError("floor has no arrival elevator")
    if level.circulation_skeleton in HALLWAY_FIRST_SKELETONS:
        corridor_indices = [index for index, tier in enumerate(level.room_tiers)
                            if tier == "corridor"]
        recorded = [entry[0] for entry in level.primary_hall_geometry]
        if len(corridor_indices) < 3 or recorded != corridor_indices:
            raise ValueError("hallway-first floor lacks its recorded primary scaffold")
        degrees = Counter(index for edge in level.edges for index in edge)
        expected_arms = {"plus-concourse": 2, "t-concourse": 1,
                         "offset-boulevard": 1}.get(
                             level.circulation_skeleton, 0)
        if level.motif_rooms.count("hallway-arm") != expected_arms:
            raise ValueError("hallway-first floor lost an authored concourse arm")
        for index, motif in enumerate(level.motif_rooms):
            if motif == "hallway-arm" and degrees[index] < 2:
                raise ValueError("hallway-first floor contains an empty terminal arm")
        if any(min(level.rooms[index].w, level.rooms[index].h) != 3
               for index in corridor_indices):
            raise ValueError("hallway-first scaffold is not three tiles wide")
    arrival = level.arrival
    facings = ((0, -1), (1, 0), (0, 1), (-1, 0))
    inward = facings[arrival.facing]
    outward = (-inward[0], -inward[1])
    inside = arrival.kind.startswith("inside-")
    expected_player = (arrival.portal[0] + 2 * (outward[0] if inside else inward[0]),
                       arrival.portal[1] + 2 * (outward[1] if inside else inward[1]))
    if (arrival.kind not in {"outside-empty", "outside-supply", "inside-closed"}
            or arrival.player != level.start or level.start != expected_player
            or _at(level.things, *level.start) != PLAYER_START_CODES[arrival.facing]
            or sum(thing in PLAYER_START_CODES for thing in level.things) != 1):
        raise ValueError("player does not face away from the arrival elevator")
    if outward[1] != 0:
        raise ValueError("arrival elevator uses a directionally invalid vertical car")
    if (any(_at(level.tiles, *cell) == ELEVATOR_TILE
            for cell in arrival.footprint)
            or not all(_is_floor(_at(level.tiles, *cell))
                       and _at(level.things, *cell) == 0
                       for cell in arrival.clearance)):
        raise ValueError("arrival elevator contains a switch or blocked threshold")
    car_cells = tuple((arrival.portal[0] + depth * outward[0],
                       arrival.portal[1] + depth * outward[1])
                      for depth in (1, 2))
    if (arrival.car_cells != car_cells
            or any(not _is_floor(_at(level.tiles, *cell)) for cell in car_cells)):
        raise ValueError("arrival elevator car has invalid floor geometry")
    expected_portal = DOOR_ELEVATOR if inward[0] else DOOR_ELEVATOR_NS
    if _at(level.tiles, *arrival.portal) != expected_portal:
        raise ValueError("arrival car lacks its normal elevator door")
    px, py = -outward[1], outward[0]
    dressed = {
        (arrival.portal[0] + depth * outward[0] + side * px,
         arrival.portal[1] + depth * outward[1] + side * py)
        for depth in (1, 2, 3) for side in (-1, 1)}
    dressed.add((arrival.portal[0] + 3 * outward[0],
                 arrival.portal[1] + 3 * outward[1]))
    if any(_at(level.tiles, *cell) != DUMMY_ELEVATOR_TILE
           for cell in dressed):
        raise ValueError("arrival car does not use inert native panels")
    if arrival.item is not None:
        if (arrival.kind != "outside-supply"
                or arrival.item[:2] not in arrival.car_cells
                or arrival.item[2] not in PICKUP_CODES
                or _at(level.things, *arrival.item[:2]) != arrival.item[2]):
            raise ValueError("arrival car item lacks contextual provenance")
    elif arrival.kind == "outside-supply":
        raise ValueError("staged arrival car has no item")
    allowed_things = {level.start}
    if arrival.item is not None:
        allowed_things.add(arrival.item[:2])
    if any(_at(level.things, *cell) and cell not in allowed_things
           for cell in arrival.car_cells):
        raise ValueError("arrival car contains an unexplained object")
    for cell in arrival.footprint:
        if cell in dressed or cell in arrival.car_cells or cell == arrival.portal:
            continue
        if _is_floor(_at(level.tiles, *cell)) or _at(level.tiles, *cell) in DOORS:
            raise ValueError("arrival elevator is not rock bounded")

    realized_barrel_families: list[str] = []
    for room in level.rooms:
        room_barrels = {
            _at(level.things, x, y)
            for y in range(room.y, room.y + room.h)
            for x in range(room.x, room.x + room.w)
            if _at(level.things, x, y) in {24, 58}
        }
        if len(room_barrels) > 1:
            raise ValueError("room mixes blue and green barrel families")
        realized_barrel_families.append(
            "green" if 24 in room_barrels else
            "blue" if 58 in room_barrels else "none")
        vases = [
            (x, y)
            for y in range(room.y, room.y + room.h)
            for x in range(room.x, room.x + room.w)
            if _at(level.things, x, y) == 35
        ]
        if len(vases) > 1:
            raise ValueError("room contains a clustered blue-urn composition")
        for x, y in vases:
            if not any(not _is_floor(_at(level.tiles, nx, ny))
                       and _at(level.tiles, nx, ny) not in DOORS
                       for nx, ny in ((x + 1, y), (x - 1, y),
                                      (x, y + 1), (x, y - 1))):
                raise ValueError("blue urn is not a wall-backed accent")
    if level.barrel_families and tuple(realized_barrel_families) != level.barrel_families:
        raise ValueError("barrel-family metadata does not match room decoration")

    if len(level.guard_recesses) > 1:
        raise ValueError("guard recesses dominate the floor")
    for recess in level.guard_recesses:
        if (not 0 <= recess.room_index < len(level.rooms)
                or level.room_tiers[recess.room_index] not in ("corridor", "hall")
                or recess.actor_cell not in recess.cells
                or any(not _is_floor(_at(level.tiles, *cell)) for cell in recess.cells)
                or _at(level.things, *recess.actor_cell) not in ENEMY_CODES):
            raise ValueError("guard recess lacks its owned hallway ambush")
        room = level.rooms[recess.room_index]
        first, second = recess.cells
        mirrored = ((first[0] == second[0]
                     and {first[1], second[1]} == {room.y - 1, room.y + room.h})
                    or (first[1] == second[1]
                        and {first[0], second[0]} == {room.x - 1,
                                                      room.x + room.w}))
        if not mirrored:
            raise ValueError("hallway guard recesses are not mirrored")
        if any(sum(_is_floor(_at(level.tiles, cell[0] + dx, cell[1] + dy))
                   or _at(level.tiles, cell[0] + dx, cell[1] + dy) in DOORS
                   for dx, dy in facings) != 1 for cell in recess.cells):
            raise ValueError("guard recess is not a blind one-cell pocket")

    if len(level.guard_galleries) > 1:
        raise ValueError("guard galleries dominate the floor")
    for gallery in level.guard_galleries:
        if (not 0 <= gallery.room_index < len(level.rooms)
                or level.room_shapes[gallery.room_index] != "rectangle"
                or gallery.treatment != 30
                or len(gallery.actor_cells) != 2):
            raise ValueError("guard gallery lacks a symmetric architectural host")
        room = level.rooms[gallery.room_index]
        horizontal = len({y for _, y in gallery.screen}) == 1
        vertical = len({x for x, _ in gallery.screen}) == 1
        expected_screen = (
            {(x, gallery.screen[0][1]) for x in range(room.x, room.x + room.w)}
            if horizontal else
            {(gallery.screen[0][0], y) for y in range(room.y, room.y + room.h)})
        if (not (horizontal or vertical)
                or set(gallery.screen) != expected_screen
                or any(_at(level.things, *cell) != gallery.treatment
                       for cell in gallery.screen)):
            raise ValueError("guard gallery does not have one complete matched screen")
        first, second = gallery.actor_cells
        mirrored = ((horizontal and first[1] == second[1]
                     and first[0] + second[0] == 2 * room.center[0])
                    or (vertical and first[0] == second[0]
                        and first[1] + second[1] == 2 * room.center[1]))
        if (not mirrored or any(_at(level.things, *cell) not in ENEMY_CODES
                                for cell in gallery.actor_cells)
                or any(cell not in gallery.rear_cells for cell in gallery.actor_cells)):
            raise ValueError("guard gallery actors are not a matched firing pair")
        collision_reachable = _reachable(
            level.tiles, level.start, locked_open=True, blocked=set(gallery.screen))
        if any(cell in collision_reachable for cell in gallery.rear_cells):
            raise ValueError("guard gallery is physically accessible")
        if any(_at(level.things, *cell) in PICKUP_CODES | {GOLD_KEY, SILVER_KEY}
               for cell in gallery.rear_cells):
            raise ValueError("inaccessible guard gallery contains a pickup")
        dx, dy = facings[gallery.facing]
        for actor in gallery.actor_cells:
            x, y = actor
            crossed = None
            for _ in range(max(room.w, room.h)):
                x, y = x + dx, y + dy
                if (x, y) in gallery.screen:
                    crossed = (x, y)
                    break
                if not _is_floor(_at(level.tiles, x, y)):
                    break
            if crossed is None:
                raise ValueError("guard gallery actor cannot fire through its screen")

    allowed_encounters = {"visible-sentry", "blind-corner-ambush",
                          "staggered-flank", "strongpoint", "objective-guard",
                          "patrol", "boss-support", "novelty", "guard-gallery"}
    tracked_actors: dict[tuple[int, int], int] = {}
    for encounter in level.encounters:
        if (encounter.template not in allowed_encounters or not encounter.cells
                or encounter.family not in ("guard", "dog", "officer", "ss", "novelty")
                or not -1 <= encounter.room_index < len(level.rooms)):
            raise ValueError("actor has invalid encounter provenance")
        for x, y, actor in encounter.cells:
            if (actor not in ENEMY_CODES or _at(level.things, x, y) != actor
                    or (x, y) in tracked_actors):
                raise ValueError("encounter provenance disagrees with the things plane")
            tracked_actors[(x, y)] = actor
        if not set(encounter.hidden_cells) <= {
                (x, y) for x, y, _ in encounter.cells}:
            raise ValueError("encounter records a hidden actor it does not own")
        if bool(encounter.patrol_kind) != bool(encounter.patrol_path):
            raise ValueError("patrol encounter metadata is incomplete")
    expected_actors = {(index % GRID, index // GRID): thing
                       for index, thing in enumerate(level.things)
                       if thing in ENEMY_CODES and thing not in BOSSES}
    if tracked_actors != expected_actors:
        raise ValueError("floor contains an actor outside an encounter composition")
    critical_ambushes = sorted(
        level.critical_route.index(encounter.room_index)
        for encounter in level.encounters
        if encounter.template == "blind-corner-ambush"
        and encounter.room_index in level.critical_route)
    if any(second - first <= 1 for first, second in zip(
            critical_ambushes, critical_ambushes[1:])):
        raise ValueError("critical route repeats ambushes without a recovery beat")
    ordinary_count = len(expected_actors)
    moving_count = sum(_patrol_actor_direction(actor) is not None
                       for actor in expected_actors.values())
    if (level.patrol_target >= 0.08 and ordinary_count >= 8 and moving_count == 0):
        raise ValueError("patrol setting produced an entirely stationary floor")
    if (level.patrol_target >= 0.15 and ordinary_count >= 12
            and moving_count / ordinary_count < level.patrol_target * 0.45):
        raise ValueError("realized patrol activity is far below its target")
    if level.room_tiers:
        if (len(level.room_tiers) != len(level.rooms)
                or len(level.room_roles) != len(level.rooms)):
            raise ValueError("room circulation metadata is incomplete")
        if level.circulation_skeleton not in CIRCULATION_SKELETONS:
            raise ValueError("unknown circulation skeleton")
        if level.progression_grammar not in PROGRESSION_GRAMMARS:
            raise ValueError("unknown progression grammar")
        if (not level.district_circulation
                or any(mode not in CIRCULATION_MODES
                       for mode in level.district_circulation)):
            raise ValueError("unknown district circulation mode")
        corridor_indices = {index for index, tier in enumerate(level.room_tiers)
                            if tier == "corridor"}
        if len(corridor_indices) < 2:
            raise ValueError("floor has no meaningful circulation hierarchy")
        if any(max(level.rooms[index].w, level.rooms[index].h)
               < 2 * min(level.rooms[index].w, level.rooms[index].h)
               for index in corridor_indices):
            raise ValueError("circulation node reads as a room, not a hallway")
        degrees = Counter(index for edge in level.edges for index in edge)
        if any(degrees[index] < 2 for index in corridor_indices):
            raise ValueError("circulation hallway ends without a destination")
        mediated = sum(first in corridor_indices or second in corridor_indices
                       for first, second in level.edges) / max(1, len(level.edges))
        if not 0.25 <= mediated <= 0.70:
            raise ValueError("corridor-mediated connection ratio is outside its quality band")

    if level.room_shapes:
        if len(level.room_shapes) != len(level.rooms):
            raise ValueError("room-shape metadata is incomplete")
        allowed_shapes = {"rectangle", "mirrored-notch", "stepped-cross",
                          "single-chamfer", "l-shaped", "shallow-t",
                          "paired-side-bays", "paired-end-bays",
                          "offset-side-bay", "swastika-profile"}
        if any(shape not in allowed_shapes and not shape.startswith("boss-")
               for shape in level.room_shapes):
            raise ValueError("unknown shaped-room family")
        shaped = sum(shape != "rectangle" for shape in level.room_shapes)
        if shaped > math.ceil(len(level.rooms) * 0.60):
            raise ValueError("non-rectangular rooms dominate the floor")
        counts = Counter(shape for shape in level.room_shapes
                         if shape != "rectangle" and not shape.startswith("boss-"))
        if shaped >= 4 and counts and max(counts.values()) / shaped > 0.50:
            raise ValueError("one shaped-room family dominates the floor")

    if level.rare_motif is not None:
        detail = level.rare_motif
        if (detail.kind != "swastika" or level.number not in (6, 7, 8, 9)
                or detail.room_index in level.critical_route
                or detail.room_index == level.boss_arena_room
                or any(objective.host_room == detail.room_index
                       for objective in level.key_objectives)
                or len(detail.endpoints) != 4):
            raise ValueError("rare plan motif owns progression or invalid geometry")
        if any(not _is_floor(_at(level.tiles, *cell)) for cell in detail.endpoints):
            raise ValueError("rare plan motif endpoint is not traversable")

    if level.lighting_families:
        if len(level.lighting_families) != len(level.rooms):
            raise ValueError("room-lighting metadata is incomplete")
        for room, family in zip(level.rooms, level.lighting_families):
            if family not in LIGHTING_FAMILY_ITEMS:
                raise ValueError("unknown room lighting family")
            fixtures = {_at(level.things, x, y)
                        for y in range(room.y, room.y + room.h)
                        for x in range(room.x, room.x + room.w)
                        if _at(level.things, x, y) in LIGHTING_ITEMS}
            if not fixtures <= LIGHTING_FAMILY_ITEMS[family]:
                raise ValueError("room mixes incompatible lighting families")

    # FAKEDOR(13) resembles an elevator door, so it is never decorative wall
    # dressing. The inert ELEV1(85) texture remains valid only in the
    # purpose-built arrival car checked above.
    if 13 in level.tiles:
        raise ValueError("fake elevator-door texture appears in generated map")

    sky_walls = [(index % GRID, index // GRID)
                 for index, tile in enumerate(level.tiles) if tile == 16]
    if sky_walls and len(sky_walls) not in {5, 7, 9}:
        raise ValueError("sky vista does not use one broad odd-span bay")
    if sky_walls and not (len({x for x, _ in sky_walls}) == 1
                          or len({y for _, y in sky_walls}) == 1):
        raise ValueError("sky vista is not one contiguous wall composition")
    if sky_walls:
        ordered_sky = sorted(sky_walls)
        if any(abs(first[0] - second[0]) + abs(first[1] - second[1]) != 1
               for first, second in zip(ordered_sky, ordered_sky[1:])):
            raise ValueError("sky vista contains a gap")
    recess_cells: list[tuple[int, int]] = []
    for x, y in sky_walls:
        visible_from = [(nx, ny) for nx, ny in
                        ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))
                        if _is_floor(_at(level.tiles, nx, ny))]
        if len(visible_from) != 1:
            raise ValueError("sky wall is not on an outside-most face")
        interior = visible_from[0]
        recess_cells.append(interior)
        dx, dy = x - interior[0], y - interior[1]
        ox, oy = x + dx, y + dy
        while 0 <= ox < GRID and 0 <= oy < GRID:
            if (_is_floor(_at(level.tiles, ox, oy))
                    or _at(level.tiles, ox, oy) in DOORS):
                raise ValueError("sky vista leaks into interior architecture")
            ox += dx
            oy += dy
    if sky_walls:
        supports = [index for index, cell in enumerate(recess_cells)
                    if _at(level.things, *cell) == 30]
        expected = ([0, len(recess_cells) - 1] if len(recess_cells) < 9
                    else [0, len(recess_cells) // 2, len(recess_cells) - 1])
        if supports != expected:
            raise ValueError("sky vista lacks balanced original-plane supports")
        if any(_at(level.things, *cell) not in ({30} if index in expected else {0})
               for index, cell in enumerate(recess_cells)):
            raise ValueError("sky vista recess contains unrelated decoration")
        if level.sky_vistas != (tuple(sorted(sky_walls)),):
            raise ValueError("sky vista metadata does not match realized geometry")
        if (level.sky_vista_recesses != (tuple(recess_cells),)
                or level.sky_vista_supports != (
                    tuple(recess_cells[index] for index in expected),)):
            raise ValueError("sky vista metadata omits its recess composition")
    elif (level.sky_vistas or level.sky_vista_recesses
          or level.sky_vista_supports):
        raise ValueError("sky vista metadata records absent geometry")

    glass_walls = {(index % GRID, index // GRID)
                   for index, tile in enumerate(level.tiles) if tile == 33}
    accounted_glass: set[tuple[int, int]] = set()
    for room in level.rooms:
        sides = (
            {(x, room.y - 1) for x in range(room.x - 1, room.x + room.w + 1)},
            {(x, room.y + room.h) for x in range(room.x - 1, room.x + room.w + 1)},
            {(room.x - 1, y) for y in range(room.y, room.y + room.h)},
            {(room.x + room.w, y) for y in range(room.y, room.y + room.h)},
        )
        room_glass = glass_walls & set().union(*sides)
        if not room_glass:
            continue
        if len(room_glass) % 2:
            raise ValueError("stained glass is not a matched wall composition")
        if any(not any(other != cell and
                       (other[0] == cell[0] or other[1] == cell[1])
                       for other in room_glass) for cell in room_glass):
            raise ValueError("stained glass has an isolated panel")
        accounted_glass.update(room_glass)
    if accounted_glass != glass_walls:
        raise ValueError("stained glass has no owning formal room")

    spear_positions = [(index % GRID, index // GRID)
                       for index, thing in enumerate(level.things) if thing == 69]
    for cell in spear_positions:
        owners = [index for index, room in enumerate(level.rooms)
                  if room.x <= cell[0] < room.x + room.w
                  and room.y <= cell[1] < room.y + room.h]
        if not owners:
            raise ValueError("spear display has no owning room")
        owner = owners[0]
        room = level.rooms[owner]
        if level.room_concepts and level.room_concepts[owner] not in SPEAR_CONCEPTS:
            raise ValueError("spear display does not fit its room identity")
        x, y = cell
        outside = []
        if x == room.x:
            outside.append((x - 1, y))
        if x == room.x + room.w - 1:
            outside.append((x + 1, y))
        if y == room.y:
            outside.append((x, y - 1))
        if y == room.y + room.h - 1:
            outside.append((x, y + 1))
        if not any(not _is_floor(_at(level.tiles, *neighbor))
                   and _at(level.tiles, *neighbor) not in DOORS
                   for neighbor in outside):
            raise ValueError("spear display is not purposefully wall backed")

    flag_positions = [(index % GRID, index // GRID)
                      for index, thing in enumerate(level.things) if thing == 62]
    for x, y in flag_positions:
        owners = [room for room in level.rooms
                  if room.x <= x < room.x + room.w
                  and room.y <= y < room.y + room.h]
        if not owners:
            raise ValueError("flag has no owning room")
        room = owners[0]
        outside = []
        if x == room.x:
            outside.append((x - 1, y))
        if x == room.x + room.w - 1:
            outside.append((x + 1, y))
        if y == room.y:
            outside.append((x, y - 1))
        if y == room.y + room.h - 1:
            outside.append((x, y + 1))
        if not any(not _is_floor(_at(level.tiles, *neighbor))
                   and _at(level.tiles, *neighbor) not in DOORS
                   for neighbor in outside):
            raise ValueError("flag is not purposefully wall backed")

    vine_positions = {(index % GRID, index // GRID)
                      for index, thing in enumerate(level.things) if thing == 70}
    documented_vines = {cell for screen in level.vine_screens for cell in screen.cells}
    if vine_positions != documented_vines:
        raise ValueError("vine exists outside a complete screen composition")
    for screen in level.vine_screens:
        if not screen.cells or any(_at(level.things, *cell) != 70
                                   for cell in screen.cells):
            raise ValueError("vine screen metadata disagrees with the things plane")
        if screen.kind == "room-divider":
            if not 0 <= screen.room_index < len(level.rooms):
                raise ValueError("vine divider has no host room")
            room = level.rooms[screen.room_index]
            xs = {x for x, _ in screen.cells}
            ys = {y for _, y in screen.cells}
            vertical = (len(xs) == 1 and ys == set(range(room.y, room.y + room.h)))
            horizontal = (len(ys) == 1
                          and xs == set(range(room.x, room.x + room.w)))
            if not (vertical or horizontal):
                raise ValueError("vine divider does not span the complete room")
        elif screen.kind == "hallway-run":
            if len(screen.cells) < 3 or screen.room_index != -1:
                raise ValueError("hallway vine run has invalid extent")
            horizontal = len({y for _, y in screen.cells}) == 1
            vertical = len({x for x, _ in screen.cells}) == 1
            ordered = sorted(screen.cells,
                             key=lambda cell: cell[0] if horizontal else cell[1])
            if (not (horizontal or vertical)
                    or any(abs(first[0] - second[0]) + abs(first[1] - second[1]) != 1
                           for first, second in zip(ordered, ordered[1:]))):
                raise ValueError("hallway vines do not fill one continuous length")
            for x, y in screen.cells:
                sides = (((x, y - 1), (x, y + 1)) if horizontal else
                         ((x - 1, y), (x + 1, y)))
                if any(_is_floor(_at(level.tiles, *side))
                       or _at(level.tiles, *side) in DOORS for side in sides):
                    raise ValueError("hallway vine escaped its one-tile corridor")
            if screen.ambush_anchor is not None:
                if (_at(level.things, *screen.ambush_anchor) not in ENEMY_CODES
                        or min(abs(screen.ambush_anchor[0] - endpoint[0])
                               + abs(screen.ambush_anchor[1] - endpoint[1])
                               for endpoint in (screen.cells[0], screen.cells[-1])) > 6):
                    raise ValueError("hallway vine ambush anchor is not credible")
        else:
            raise ValueError("unknown vine-screen composition")

    if level.pickup_placements:
        tracked: dict[tuple[int, int], int] = {}
        for placement in level.pickup_placements:
            secret_cache = placement.template == "secret-cache"
            if (placement.template not in AUTHORED_PICKUP_TEMPLATES
                    or (placement.room_index != -1 if secret_cache else
                        not 0 <= placement.room_index < len(level.rooms))
                    or not placement.cells):
                raise ValueError("pickup has invalid placement provenance")
            room = None if secret_cache else level.rooms[placement.room_index]
            for x, y, item in placement.cells:
                if item not in PICKUP_CODES or _at(level.things, x, y) != item:
                    raise ValueError("pickup provenance disagrees with the things plane")
                if (room is not None
                        and not (room.x <= x < room.x + room.w
                                 and room.y <= y < room.y + room.h)):
                    raise ValueError("authored pickup escaped its owning room")
                if (x, y) in tracked:
                    raise ValueError("pickup belongs to multiple authored compositions")
                tracked[(x, y)] = item
        expected = {(index % GRID, index // GRID): item
                    for index, item in enumerate(level.things)
                    if item in PICKUP_CODES
                    and _inside_room(list(level.rooms), index % GRID, index // GRID)}
        if expected.items() - tracked.items():
            raise ValueError("room contains an untracked pickup sprite")
    seen = {level.start}
    queue = deque([level.start])
    while queue:
        x, y = queue.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nxt = x + dx, y + dy
            if nxt in seen:
                continue
            tile = _at(level.tiles, *nxt)
            if _is_floor(tile) or tile in DOORS:
                seen.add(nxt); queue.append(nxt)
    if level.exit_stand not in seen:
        raise ValueError("elevator standing position is unreachable")
    sx, sy = level.exit_stand
    # The switch must sit on the stand's east/west axis with the shaft floor
    # opposite it: tile 21 cannot be activated from north or south.
    if not any(_at(level.tiles, sx + dx, sy) == ELEVATOR_TILE
               and _is_floor(_at(level.tiles, sx - dx, sy))
               for dx in (1, -1)):
        raise ValueError("elevator switch is not usable from its standing position")
    switch_dx = next(dx for dx in (1, -1)
                     if _at(level.tiles, sx + dx, sy) == ELEVATOR_TILE)
    threshold = (sx - switch_dx, sy)
    if any(_at(level.tiles, threshold[0], threshold[1] + side) != ELEVATOR_TILE
           for side in (-1, 1)):
        raise ValueError("elevator exposes a non-elevator wall inside the doorway")
    if level.rooms and level.critical_route:
        distances = _floor_distances(level.tiles, level.start)
        anchor_index = next((index for index, tier in enumerate(level.room_tiers)
                             if tier == "anchor"), -1)
        minimum_route_rooms = _minimum_critical_route_rooms(level.room_roles)
        required_post_anchor = next(
            (index for index, role in enumerate(level.room_roles)
             if role in ("victory", "recovery")), None)
        eligible_frontier = []
        for index in range(1, len(level.rooms)):
            room_route = _room_graph_path(len(level.rooms), list(level.edges), index)
            if (anchor_index in room_route[:-1]
                    and len(room_route) >= minimum_route_rooms
                    and (required_post_anchor is None
                         or required_post_anchor in room_route[:-1])):
                eligible_frontier.append(index)
        deepest_room = max((distances.get(level.rooms[index].center, 0)
                            for index in eligible_frontier), default=1) or 1
        if distances.get(level.exit_stand, 0) / deepest_room < 0.75:
            raise ValueError("elevator route is shallower than 75% of the floor")
        if len(level.critical_route) < minimum_route_rooms:
            raise ValueError("critical route visits too few authored rooms")
        edge_sets = [{first, second} for first, second in level.edges]
        if any({first, second} not in edge_sets
               for first, second in zip(level.critical_route,
                                        level.critical_route[1:])):
            raise ValueError("critical route is not continuous")
        if (level.room_districts
                and len({level.room_districts[index]
                         for index in level.critical_route}) < 2):
            raise ValueError("elevator route never crosses a district boundary")
        route = _shortest_floor_path(level.tiles, level.start, level.exit_stand)
        if len(route) < 2 or _path_bends(route) < 2:
            raise ValueError("elevator route is too visually direct")
    if not level.secret_rewards:
        raise ValueError("map has no rewarded secret")
    if (len(level.secret_details) != len(level.secret_rewards)
            or tuple(detail.shape for detail in level.secret_details)
            != level.secret_variants):
        raise ValueError("secret planning metadata is incomplete")
    exit_details = [detail for detail in level.secret_details if detail.secret_exit]
    if level.has_secret_exit:
        if len(exit_details) != 1:
            raise ValueError("secret elevator is not a separately planned pocket")
        detail = exit_details[0]
        if (detail.shape == "square" or detail.host_room < 0
                or detail.depth_ratio < 0.45
                or detail.hint_treatment != "symmetric-landmark"
                or detail.return_floor != level.number + 1):
            raise ValueError("secret elevator lacks a substantial deep host")
        px, py = detail.pushwall
        if (_at(level.tiles, px, py) not in DECOR_WALLS
                or not any(_at(level.tiles, px, py - offset)
                           == _at(level.tiles, px, py)
                           == _at(level.tiles, px, py + offset)
                           for offset in (1, 2))):
            raise ValueError("secret elevator lacks its symmetric landmark hint")
    elif exit_details:
        raise ValueError("ordinary floor reports a secret-elevator host")
    for detail in level.secret_details:
        if (_at(level.things, *detail.pushwall) != PUSHWALL
                or detail.reward_count != (7 if level.number == 9 else 3)):
            raise ValueError("secret detail disagrees with its realized pocket")
    pushwalls = [(i % GRID, i // GRID) for i, thing in enumerate(level.things)
                 if thing == PUSHWALL]
    if len(pushwalls) < len(level.secret_rewards):
        raise ValueError("secret reward has no pushwall")
    # Most pockets open eastward, while a secret elevator may use either
    # horizontal face of its host room. Track both its outer wall and the
    # second wall of a nested pocket so reachability models the real push.
    push_directions = {detail.pushwall: detail.push_direction
                       for detail in level.secret_details}
    for detail in level.secret_details:
        if detail.shape == "nested":
            x, y = detail.pushwall
            push_directions[(x + 4 * detail.push_direction, y)] = detail.push_direction
    rests = {(x + 2 * push_directions.get((x, y), 1), y)
             for x, y in pushwalls}
    for x, y in pushwalls:
        direction = push_directions.get((x, y), 1)
        if _is_floor(_at(level.tiles, x, y)):
            raise ValueError("pushwall trigger is not on a solid wall")
        if not _is_floor(_at(level.tiles, x - direction, y)):
            raise ValueError("pushwall has no movement clearance")
        if not all(_is_floor(_at(level.tiles, x + direction * step, y))
                   for step in (1, 2)):
            raise ValueError("pushwall has no two-tile backstop")
        if _at(level.tiles, x, y) not in SECRET_HINT_WALLS:
            raise ValueError("pushwall is not hinted by a decor wall tile")
    # Nested secrets become approachable in sequence. Simulate each push at
    # its real two-tile travel distance rather than pretending the inner wall
    # must be reachable while its outer wall is still closed.
    pending = set(pushwalls)
    pushed: set[tuple[int, int]] = set()
    while pending:
        opened = _reachable(level.tiles, level.start, locked_open=True,
                            extra_passable=pushed,
                            blocked={(x + 2 * push_directions.get((x, y), 1), y)
                                     for x, y in pushed})
        ready = sorted((wall for wall in pending
                        if (wall[0] - push_directions.get(wall, 1), wall[1])
                        in opened),
                       key=lambda cell: (cell[1], cell[0]))
        if not ready:
            raise ValueError("pushwall cannot be approached")
        wall = ready[0]
        pending.remove(wall); pushed.add(wall)
    # A pushwall guarding nothing is worse than no secret at all: the player
    # either finds a bypass and the "secret" never needed pushing, or pushes
    # it and sees no new floor because everything past it was already open.
    # Check each wall in isolation -- every OTHER wall already pushed (best
    # case for a bypass to exist) but this one still solid -- and confirm
    # the cell right behind it is unreachable any other way.
    for wall in pushwalls:
        others = pushed - {wall}
        bypass = _reachable(level.tiles, level.start, locked_open=True,
                            extra_passable=others,
                            blocked={(x + 2 * push_directions.get((x, y), 1), y)
                                     for x, y in others})
        direction = push_directions.get(wall, 1)
        if (wall[0] + direction, wall[1]) in bypass:
            raise ValueError("pushwall is bypassable without being pushed")
    opened = _reachable(level.tiles, level.start, locked_open=True,
                        extra_passable=pushed, blocked=rests)
    for reward in level.secret_rewards:
        if _at(level.things, *reward) not in (AMMO,) + TREASURE + (MACHINE_GUN, CHAINGUN, ONE_UP):
            raise ValueError("secret is missing a valuable reward")
        if reward in rests:
            raise ValueError("secret reward sits on the pushwall's resting cell")
        if reward not in opened:
            raise ValueError("secret reward is unreachable after opening pushwall")
    zone_count = level.tiles.count(SECRET_EXIT_ZONE)
    if level.has_secret_exit:
        if zone_count != 1:
            raise ValueError("designated secret-route map needs exactly one secret exit")
        index = level.tiles.index(SECRET_EXIT_ZONE)
        zx, zy = index % GRID, index // GRID
        direction = exit_details[0].push_direction
        if (_at(level.tiles, zx + direction, zy) != ELEVATOR_TILE
                or not _is_floor(_at(level.tiles, zx - direction, zy))
                or _at(level.tiles, zx - 2 * direction, zy) != DOOR_ELEVATOR):
            raise ValueError("secret exit is not inside a real elevator car")
        if any(_at(level.tiles, x, zy + side) != ELEVATOR_TILE
               for x in range(zx - 1, zx + 2) for side in (-1, 1)):
            raise ValueError("secret elevator is missing its side rails")
        outer_shell = ({(zx + 2 * direction, zy + side)
                        for side in range(-2, 3)}
                       | {(x, zy + side) for x in range(zx - 2, zx + 3)
                          for side in (-2, 2)})
        if any(_is_floor(_at(level.tiles, *cell))
               or _at(level.tiles, *cell) in DOORS for cell in outer_shell):
            raise ValueError("secret elevator is not rock bounded")
        closed_secret = _reachable(level.tiles, level.start, locked_open=True)
        if (zx, zy) in closed_secret:
            raise ValueError("secret elevator is reachable without its pushwall")
        if (zx, zy) not in opened:
            raise ValueError("secret elevator is unusable after opening its pushwall")
    elif zone_count:
        raise ValueError("secret exit zone on a floor with no secret route")
    validate_door_axes(level.tiles)
    actual_locks = [(index % GRID, index // GRID, tile)
                    for index, tile in enumerate(level.tiles) if tile in LOCKED_DOORS]
    if len(actual_locks) != level.locked_doors:
        raise ValueError("locked-door count does not match the progression plan")
    if bool(actual_locks) != bool(level.key_order):
        raise ValueError("locked doors and key order disagree")
    if actual_locks:
        if (len(level.key_objectives) != len(level.key_order)
                or tuple(objective.color for objective in level.key_objectives)
                != level.key_order):
            raise ValueError("key-objective metadata disagrees with progression order")
        physical_hosts = []
        for objective in level.key_objectives:
            if not 0 <= objective.host_room < len(level.rooms):
                raise ValueError("key objective has no host room")
            if objective.treatment == "boss-drop":
                if (_at(level.things, *objective.cell) not in KEY_DROP_BOSSES
                        or objective.color != "gold" or not level.boss):
                    raise ValueError("boss key objective is not backed by a native drop")
                continue
            expected_key = GOLD_KEY if objective.color == "gold" else SILVER_KEY
            if _at(level.things, *objective.cell) != expected_key:
                raise ValueError("physical key objective disagrees with things plane")
            if objective.detour < 2:
                raise ValueError("physical key lies directly on its lock route")
            if objective.cell == level.rooms[objective.host_room].center:
                raise ValueError("physical key defaults to the room center")
            clear_neighbors = sum(
                _is_floor(_at(level.tiles, objective.cell[0] + dx,
                              objective.cell[1] + dy))
                and _at(level.things, objective.cell[0] + dx,
                        objective.cell[1] + dy) == 0
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
            if clear_neighbors < 1:
                raise ValueError("physical key has no clear pickup approach")
            physical_hosts.append(objective.host_room)
        if len(physical_hosts) != len(set(physical_hosts)):
            raise ValueError("dual physical keys repeat the same host room")

        providers: dict[str, tuple[int, int]] = {}
        for color, thing in (("gold", GOLD_KEY), ("silver", SILVER_KEY)):
            positions = [(index % GRID, index // GRID)
                         for index, item in enumerate(level.things) if item == thing]
            if len(positions) > 1:
                raise ValueError(f"map has duplicate {color} keys")
            if positions:
                providers[color] = positions[0]
        boss_index = next((index for index, thing in enumerate(level.things)
                           if thing in BOSSES), None)
        if (level.boss and boss_index is not None
                and level.things[boss_index] in KEY_DROP_BOSSES):
            providers["gold"] = (boss_index % GRID, boss_index // GRID)

        opened_colors: set[str] = set()
        for color in level.key_order:
            if color not in providers:
                raise ValueError(f"locked map has no {color} key provider")
            open_codes = _codes_for_colors(opened_colors)
            normally_reachable = _reachable(
                level.tiles, level.start, locked_open=False,
                open_lock_codes=open_codes)
            bypass_reachable = _reachable(
                level.tiles, level.start, locked_open=False,
                extra_passable=set(pushwalls), blocked=rests,
                open_lock_codes=open_codes)
            if providers[color] not in normally_reachable:
                raise ValueError(f"{color} key is unreachable at its progression stage")
            if level.exit_stand in bypass_reachable:
                raise ValueError(f"exit bypasses the required {color} key")
            matching = GOLD_DOORS if color == "gold" else SILVER_DOORS
            lock_sides = {(x + dx, y + dy)
                          for x, y, tile in actual_locks if tile in matching
                          for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))}
            if (providers[color] in normally_reachable
                    and lock_sides & _door_zone(level.tiles, providers[color])):
                raise ValueError(f"{color} key shares a room with its lock")
            opened_colors.add(color)

        if level.exit_stand not in _reachable(
                level.tiles, level.start, locked_open=False,
                open_lock_codes=_codes_for_colors(opened_colors)):
            raise ValueError("exit is unreachable after obtaining every key")
        # Every color must independently remain necessary even if the other
        # key is treated as already collected and every secret is open.
        for color in opened_colors:
            other_colors = opened_colors - {color}
            if level.exit_stand in _reachable(
                    level.tiles, level.start, locked_open=False,
                    extra_passable=set(pushwalls), blocked=rests,
                    open_lock_codes=_codes_for_colors(other_colors)):
                raise ValueError(f"{color} lock is not individually necessary")
    if level.boss and sum(thing in BOSSES for thing in level.things) != 1:
        raise ValueError("boss floor must contain exactly one boss")
    if level.boss:
        boss_index = next(i for i, thing in enumerate(level.things) if thing in BOSSES)
        boss_position = boss_index % GRID, boss_index // GRID
        prior_colors = (set(level.key_order[:level.key_order.index("gold")])
                        if "gold" in level.key_order else set(level.key_order))
        if boss_position not in _reachable(
                level.tiles, level.start, locked_open=False,
                open_lock_codes=_codes_for_colors(prior_colors)):
            raise ValueError("boss is unreachable before the boss elevator lock")

    boss_families = {"throne-stronghold", "command-bunker",
                     "laboratory-gauntlet", "columned-fortress", "central-duel"}
    vault_families = {"central-vault", "museum-circuit", "nested-reliquary",
                      "abandoned-armory", "treasure-palace"}
    if level.number == 9:
        if level.special_family not in boss_families:
            raise ValueError("floor 9 has no boss-stronghold family")
        if (not 0 <= level.boss_arena_room < len(level.rooms)
                or level.boss_arena_room not in level.critical_route
                or level.room_roles[level.boss_arena_room] != "boss-arena"):
            raise ValueError("floor 9 arena is not a mandatory planned destination")
        arena = level.rooms[level.boss_arena_room]
        if min(arena.w, arena.h) < 14:
            raise ValueError("boss arena is too small for its encounter")
        if (level.boss_arena is None
                or level.boss_arena.family != level.special_family
                or len(level.boss_arena.geometry) < 2
                or len(level.boss_arena.decorations) < 3):
            raise ValueError("boss arena lacks its family-owned composition")
        interior_cover = sum(
            not _is_floor(_at(level.tiles, x, y))
            for y in range(arena.y + 2, arena.y + arena.h - 2)
            for x in range(arena.x + 2, arena.x + arena.w - 2))
        if interior_cover < 2:
            raise ValueError("boss arena has no symmetric sightline cover")
        if (not 0 <= level.preboss_room < len(level.rooms)
                or level.room_roles[level.preboss_room] != "staging"
                or _room_predecessor(len(level.rooms), list(level.edges),
                                     level.boss_arena_room) != level.preboss_room):
            raise ValueError("floor 9 lacks an immediate pre-boss staging room")
        if not any(placement.reason == "preboss-stockup"
                   and placement.room_index == level.preboss_room
                   for placement in level.pickup_placements):
            raise ValueError("pre-boss staging room has no authored stock-up")
        victory = next((index for index, role in enumerate(level.room_roles)
                        if role == "victory"), None)
        if (victory is None or victory not in level.critical_route
                or level.critical_route.index(victory)
                <= level.critical_route.index(level.boss_arena_room)):
            raise ValueError("boss arena does not lead into a victory space")
        victory_room = level.rooms[victory]
        if any(_at(level.things, x, y) in ENEMY_CODES
               for y in range(victory_room.y, victory_room.y + victory_room.h)
               for x in range(victory_room.x, victory_room.x + victory_room.w)):
            raise ValueError("floor 9 victory room is not a calm transition")
        if _at(level.things, *next(objective.cell for objective in level.key_objectives
                                   if objective.color == "gold")) not in KEY_DROP_BOSSES:
            raise ValueError("floor 9 completion is not boss gated")
    elif level.number == 10:
        if level.special_family not in vault_families:
            raise ValueError("floor 10 has no reward-expedition family")
        if level.secret_source and not 1 <= level.secret_source <= 6:
            raise ValueError("floor 10 has an invalid secret-exit source floor")
        if (level.locked_doors or level.key_order
                or not 0 <= level.premium_room < len(level.rooms)
                or level.premium_room not in level.critical_route
                or level.room_roles[level.premium_room] != "premium-vault"):
            raise ValueError("floor 10 premium chamber is not on its open route")
        premium = [placement for placement in level.pickup_placements
                   if placement.reason == "floor-ten-premium"
                   and placement.room_index == level.premium_room]
        expeditions = [placement for placement in level.pickup_placements
                       if placement.reason == "floor-ten-expedition"]
        if (len(premium) != 1 or not any(item in (ONE_UP, CHAINGUN, TREASURE[3])
                                        for _, _, item in premium[0].cells)
                or len({placement.room_index for placement in expeditions}) < 2):
            raise ValueError("floor 10 lacks its premium and expedition rewards")
        expedition_concepts = {level.room_concepts[placement.room_index]
                               for placement in expeditions}
        if len(expedition_concepts) < 2:
            raise ValueError("floor 10 reward expeditions repeat one concept")
        arrival = next((index for index, role in enumerate(level.room_roles)
                        if role == "arrival"), None)
        if arrival is None:
            raise ValueError("floor 10 lacks a calm arrival room")
        arrival_room = level.rooms[arrival]
        if any(_at(level.things, x, y) in ENEMY_CODES
               for y in range(arrival_room.y, arrival_room.y + arrival_room.h)
               for x in range(arrival_room.x, arrival_room.x + arrival_room.w)):
            raise ValueError("floor 10 arrival reveal is not calm")
        for index, thing in enumerate(level.things):
            if thing not in GHOSTS:
                continue
            x, y = index % GRID, index // GRID
            owner = next((room_index for room_index, room in enumerate(level.rooms)
                          if room.x <= x < room.x + room.w
                          and room.y <= y < room.y + room.h), None)
            if owner is None or owner in level.critical_route:
                raise ValueError("floor 10 ghost controls the mandatory route")
    validate_objects(level)
    validate_patrols(level)


def validate_objects(level: GeneratedMap) -> None:
    for index, thing in enumerate(level.things):
        if thing == 0:
            continue
        x, y = index % GRID, index // GRID
        tile = _at(level.tiles, x, y)
        if thing == PUSHWALL:
            if _is_floor(tile):
                raise ValueError(f"pushwall at {(x, y)} is not on a solid tile")
            continue
        if not _is_floor(tile):
            raise ValueError(f"thing {thing} at {(x, y)} is not on floor")
        if thing in ENEMY_CODES and thing not in BOSSES:
            distance = abs(x - level.start[0]) + abs(y - level.start[1])
            if distance < 6:
                raise ValueError(f"enemy at {(x, y)} is too close to player start")


def _patrol_actor_direction(code: int) -> int | None:
    """Decode a patrol actor's old-format code into this module's N/E/S/W index."""
    for patrol_family in PATROLS_BY_FAMILY.values():
        for tier in range(3):
            candidate = code - 36 * tier
            if candidate in patrol_family:
                return patrol_family.index(candidate)
    return None


def validate_patrols(level: GeneratedMap, steps: int = 512) -> None:
    """Simulate ECWolf's pathing movement and reject a route that dead-ends.

    In ECWolf, ``TryWalk`` tries only the actor's current direction; a
    PatrolPoint must set a new direction before the next tile would be solid.
    This deliberately models that limited algorithm rather than a helpful
    path finder, making the historical walking-in-place failure reproducible.
    """
    route_by_actor: dict[tuple[int, int], EncounterPlacement] = {}
    claimed_markers: set[tuple[int, int]] = set()
    allowed_kinds = {"room-loop", "compact-loop",
                     "hall-shuttle", "doorway-shuttle"}
    for encounter in level.encounters:
        if not encounter.patrol_kind:
            continue
        if (encounter.template != "patrol" or len(encounter.cells) != 1
                or encounter.patrol_kind not in allowed_kinds
                or len(encounter.patrol_path) < 3
                or len(set(encounter.patrol_path)) != len(encounter.patrol_path)):
            raise ValueError("patrol route has invalid encounter provenance")
        x, y, actor = encounter.cells[0]
        origin = (x, y)
        if (_patrol_actor_direction(actor) is None
                or origin not in encounter.patrol_path
                or origin in route_by_actor):
            raise ValueError("patrol actor does not own one declared route")
        route_by_actor[origin] = encounter
        path = encounter.patrol_path
        route_markers = {cell for cell in path
                         if _at(level.things, *cell) in PATROL_POINT_DIRECTIONS}
        expected_count = (2 if encounter.patrol_kind in
                          ("hall-shuttle", "doorway-shuttle") else 4)
        if len(route_markers) != expected_count or claimed_markers & route_markers:
            raise ValueError("patrol markers are missing, duplicated, or shared")
        claimed_markers.update(route_markers)
        for cell in route_markers:
            index = path.index(cell)
            if encounter.patrol_kind in ("hall-shuttle", "doorway-shuttle"):
                next_cell = path[1] if index == 0 else path[-2]
            else:
                next_cell = path[(index + 1) % len(path)]
            delta = next_cell[0] - cell[0], next_cell[1] - cell[1]
            expected = ((0, -1), (1, 0), (0, 1), (-1, 0)).index(delta)
            actual = PATROL_POINT_DIRECTIONS[_at(level.things, *cell)]
            if actual != expected:
                raise ValueError(f"patrol marker at {cell} points off its route")

    plane_markers = {(index % GRID, index // GRID)
                     for index, thing in enumerate(level.things)
                     if thing in PATROL_POINT_DIRECTIONS}
    if plane_markers != claimed_markers:
        raise ValueError("things plane contains an unowned patrol marker")

    for index, thing in enumerate(level.things):
        direction = _patrol_actor_direction(thing)
        if direction is None:
            continue
        x, y = index % GRID, index // GRID
        origin = (x, y)
        encounter = route_by_actor.get(origin)
        if encounter is None:
            raise ValueError(f"patrol actor at {origin} has no declared route")
        path_cells = set(encounter.patrol_path)
        for _ in range(steps):
            dx, dy = ((0, -1), (1, 0), (0, 1), (-1, 0))[direction]
            x, y = x + dx, y + dy
            if (x, y) not in path_cells:
                raise ValueError(f"patrol actor at {origin} walks off its declared route")
            tile = _at(level.tiles, x, y)
            if not (_is_floor(tile) or tile in DOORS):
                raise ValueError(f"patrol actor at {origin} dead-ends at {(x, y)}")
            occupant = _at(level.things, x, y)
            if occupant and occupant not in PATROL_POINT_DIRECTIONS and (x, y) != origin:
                raise ValueError(f"patrol actor at {origin} is blocked by thing at {(x, y)}")
            if occupant in PATROL_POINT_DIRECTIONS:
                direction = PATROL_POINT_DIRECTIONS[occupant]


def validate_door_axes(tiles: list[int]) -> None:
    for y in range(GRID):
        for x in range(GRID):
            tile = _at(tiles, x, y)
            if tile not in DOORS:
                continue
            ew = tile % 2 == 0
            along = ((_at(tiles, x - 1, y), _at(tiles, x + 1, y)) if ew else
                     (_at(tiles, x, y - 1), _at(tiles, x, y + 1)))
            across = ((_at(tiles, x, y - 1), _at(tiles, x, y + 1)) if ew else
                      (_at(tiles, x - 1, y), _at(tiles, x + 1, y)))
            if not all(_is_floor(value) or value in DOORS for value in along):
                raise ValueError(f"door at {(x, y)} is blocked on its opening axis")
            if any(_is_floor(value) or value in DOORS for value in across):
                raise ValueError(f"door at {(x, y)} can be bypassed around its jamb")
