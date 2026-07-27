"""Semantic identity and material treatment: what each space means.

Resolves one `RoomIdentity` per room -- role, tier, motif, district, variant,
concept and base theme -- and then dresses the walls to match: material family per
district, variant and damage treatment, landmarks, jail selection.

`RoomIdentity` is the single semantic decision every later system consumes. Wall
treatment, encounters, pickups, decoration and quality scoring all read it; none
of them re-decides what a room represents. That rule is what keeps a room from
being furnished as a barracks while its walls read as a crypt, and it is the
reason this module runs before population rather than alongside it.

Space *partitioning* deliberately lives in geometry.py: which cells form one
region is a connectivity question, and only the labels assigned here are semantic.
"""

from __future__ import annotations

from collections import Counter, deque
from itertools import combinations
import random

from .decorations import _decor_theme
from .grid import _at, _floor_components, _is_floor, _set
from .model import FloorVariant, KeyObjective, Room, RoomIdentity, RoomSpec
from .wl6 import (DAMAGED_WALL_CONCEPTS, DECOR_WALLS, DOORS,
                  FLOOR_TEN_STONE_THEME, GRID, JAIL_CANDIDATE_PROBABILITY,
                  MATERIAL_BY_BASE, PURPLE_MIN_FLOOR, WALL,
                  WALL_LANDMARK_CONCEPTS, WALL_THEMES)


def _room_identities(rooms: list[Room], specs: list[RoomSpec], districts: list[int],
                     edges: list[tuple[int, int]], variant: FloorVariant,
                     jail_rooms: frozenset[int],
                     component_of: dict[tuple[int, int], int],
                     group_theme: dict[int, tuple[int, tuple[int, ...]]],
                     exit_room: Room, boss_room: Room | None = None,
                     special_family: str = "standard",
                     key_objectives: tuple[KeyObjective, ...] = ()
                     ) -> list[RoomIdentity]:
    """Resolve grammar forward into compatible room concepts.

    Role/tier/motif and the floor variant are already fixed at this point;
    material and decoration only refine that earlier identity.
    """
    overrides = dict(variant.decor_overrides)
    resolved: list[tuple[str, str, int]] = []
    for index, (room, spec, district) in enumerate(zip(rooms, specs, districts)):
        theme = "jail" if index in jail_rooms else _decor_theme(spec.role, spec.tier)
        if index not in jail_rooms:
            theme = overrides.get(theme, theme)
        special = ("start" if index == 0 else "exit" if room == exit_room else
                   "boss" if boss_room is not None and room == boss_room else
                   spec.role if spec.role in
                   ("arrival", "staging", "victory", "premium-vault", "recovery") else
                   "jail" if index in jail_rooms else "")
        wall_base = group_theme[component_of[room.center]][0]
        resolved.append((theme, special, wall_base))

    # A kitchen is a deliberate floor-level set piece, not the default
    # interpretation of every lounge. Prefer a normal relief room and cap the
    # concept at one for the entire floor.
    kitchen_index: int | None = None
    if variant.name in ("quarters", "grand-halls"):
        candidates = [index for index, ((theme, special, _), spec) in
                      enumerate(zip(resolved, specs))
                      if theme == "lounge" and not special
                      and spec.tier not in ("hall", "closet")]
        if candidates:
            kitchen_index = min(candidates, key=lambda index: (
                specs[index].role != "relief",
                abs(rooms[index].w * rooms[index].h - 64),
                districts[index], index))

    palettes = {
        "guardpost": ("guardpost", "armory", "checkpoint"),
        "grand": ("war-room", "trophy-hall", "courtyard"),
        "barracks": (("crypt", "ossuary", "burial-chamber")
                     if variant.name == "catacombs" else
                     ("barracks", "ready-room", "training-room")),
        "storage": ("storage", "supply-cache", "workshop"),
        "lounge": (("gallery", "dining-hall", "lounge")
                   if variant.name == "grand-halls" else
                   ("officers-quarters", "lounge", "dining-hall")
                   if variant.name == "quarters" else
                   ("lounge", "dining-hall")),
        # Authored hallway-first scaffolds deliberately connect several
        # circulation rooms. Give those spaces compatible identities so the
        # normal neighbor-aware choice can vary an axis and its occupied arm
        # instead of producing an unavoidable run of identical corridors.
        "corridor": ("corridor", "checkpoint", "guardpost"),
        "jail": ("jail", "holding-cell", "interrogation-room"),
    }
    neighbors: dict[int, set[int]] = {index: set() for index in range(len(rooms))}
    for first, second in edges:
        neighbors[first].add(second)
        neighbors[second].add(first)
    concepts: list[str] = []
    counts: Counter[str] = Counter()
    boss_arena_concepts = {
        "throne-stronghold": "trophy-hall",
        "command-bunker": "war-room",
        "laboratory-gauntlet": "workshop",
        "columned-fortress": "courtyard",
        "central-duel": "war-room",
    }
    vault_palettes = {
        "central-vault": ("supply-cache", "gallery", "armory"),
        "museum-circuit": ("gallery", "trophy-hall", "war-room"),
        "nested-reliquary": ("burial-chamber", "ossuary", "gallery"),
        "abandoned-armory": ("armory", "supply-cache", "training-room"),
        "treasure-palace": ("trophy-hall", "dining-hall", "gallery"),
    }
    physical_key_hosts = {objective.host_room for objective in key_objectives
                          if objective.treatment != "boss-drop"}
    for index, ((theme, special, _), spec, district) in enumerate(
            zip(resolved, specs, districts)):
        if index == kitchen_index:
            concept = "mess-kitchen"
        elif index in physical_key_hosts:
            concept = ({"storage": "supply-cache", "grand": "war-room",
                        "lounge": "officers-quarters",
                        "barracks": "armory"}.get(theme, "checkpoint"))
        elif spec.role == "boss-arena":
            concept = boss_arena_concepts.get(special_family, "war-room")
        elif spec.role == "staging":
            concept = "ready-room"
        elif spec.role == "victory":
            concept = "trophy-hall"
        elif spec.role == "arrival":
            concept = "gallery"
        elif spec.role == "premium-vault":
            concept = vault_palettes.get(special_family,
                                         ("gallery",))[0]
        elif spec.role == "recovery":
            concept = "lounge"
        elif (special_family in vault_palettes and spec.role not in
              ("start", "exit", "circulation") and spec.tier != "corridor"):
            palette = vault_palettes[special_family]
            ordered = palette[(index + district) % len(palette):] + palette[:
                (index + district) % len(palette)]
            concept = min(ordered, key=lambda candidate: (
                sum(concepts[neighbor] == candidate
                    for neighbor in neighbors[index] if neighbor < len(concepts)),
                counts[candidate], ordered.index(candidate)))
        elif theme == "grand" and spec.role == "hub":
            concept = "courtyard"
        else:
            palette = palettes.get(theme, (theme,))
            offset = (index + district) % len(palette)
            ordered = palette[offset:] + palette[:offset]
            concept = min(ordered, key=lambda candidate: (
                sum(concepts[neighbor] == candidate
                    for neighbor in neighbors[index] if neighbor < len(concepts)),
                counts[candidate], ordered.index(candidate)))
        concepts.append(concept)
        counts[concept] += 1

    result = []
    for room, spec, district, (theme, special, wall_base), concept in zip(
            rooms, specs, districts, resolved, concepts):
        result.append(RoomIdentity(spec.role, spec.tier, spec.motif, district,
                                   variant.name, concept, theme, wall_base, special))
    return result


def _assign_area_themes(tiles: list[int], rooms: list[Room], districts: list[int],
                        rng: random.Random, number: int,
                        theme_pool: tuple[int, ...] = ()
                        ) -> tuple[dict[tuple[int, int], int],
                                   dict[int, tuple[int, tuple[int, ...]]]]:
    """Choose one wall family per door-bounded area without exposing seams.

    A bare wall shared by two floor components joins their theme groups before
    painting.  Different groups can therefore meet only at an actual door.
    """
    components = _floor_components(tiles)
    owner = {cell: index for index, component in enumerate(components)
             for cell in component}
    parents = list(range(len(components)))

    def find(component: int) -> int:
        while parents[component] != component:
            parents[component] = parents[parents[component]]
            component = parents[component]
        return component

    def union(first: int, second: int) -> None:
        first, second = find(first), find(second)
        if first != second:
            parents[second] = first

    for index, tile in enumerate(tiles):
        if tile != WALL:
            continue
        x, y = index % GRID, index // GRID
        neighbors = {owner[cell] for cell in ((x + 1, y), (x - 1, y),
                                              (x, y + 1), (x, y - 1))
                     if cell in owner}
        for first, second in combinations(sorted(neighbors), 2):
            union(first, second)

    component_of = {cell: find(component) for cell, component in owner.items()}
    groups = sorted(set(component_of.values()))
    votes: dict[int, dict[int, int]] = {group: {} for group in groups}
    for room, district in zip(rooms, districts):
        group = component_of[room.center]
        votes[group][district] = votes[group].get(district, 0) + 1
    assigned = {group: min(district for district, count in tally.items()
                           if count == max(tally.values()))
                for group, tally in votes.items() if tally}

    links = {group: set() for group in groups}
    for index, tile in enumerate(tiles):
        if tile not in DOORS:
            continue
        x, y = index % GRID, index // GRID
        neighbors = {component_of[cell] for cell in ((x + 1, y), (x - 1, y),
                                                       (x, y + 1), (x, y - 1))
                     if cell in component_of}
        for first, second in combinations(sorted(neighbors), 2):
            links[first].add(second); links[second].add(first)
    queue = deque(sorted(assigned))
    while queue:
        group = queue.popleft()
        for neighbor in sorted(links[group]):
            if neighbor not in assigned:
                assigned[neighbor] = assigned[group]
                queue.append(neighbor)
    for group in groups:
        assigned.setdefault(group, 0)

    distinct_districts = sorted(set(districts))
    deduped = list({theme[0]: theme for theme in WALL_THEMES}.values())
    # Floors 1--6 can own the campaign's hidden elevator. Plaster has no
    # symmetric in-family landmark suitable for that mandatory hint, so save
    # it for later administrative/reward districts rather than forcing a
    # conspicuous cross-family triptych around a secret exit.
    if number <= 6:
        deduped = [theme for theme in deduped if theme[0] != 48]
    # Purple reads as an unusually rich, ominous finish.  Reserve it for the
    # campaign's later half instead of letting an early grand-halls roll spend
    # that visual escalation on floor one or two.
    if number < PURPLE_MIN_FLOOR:
        deduped = [theme for theme in deduped if theme[0] != 19]
    if theme_pool:
        pooled = [theme for theme in deduped if theme[0] in theme_pool]
        # A pool too small to give every district its own material would
        # crash rng.sample; fall back to the full roster instead.
        if len(pooled) >= len(distinct_districts):
            deduped = pooled
    if number == 10 and rng.random() < 0.25:
        chosen = [FLOOR_TEN_STONE_THEME] + rng.sample(
            deduped, k=len(distinct_districts) - 1)
    else:
        chosen = rng.sample(deduped, k=len(distinct_districts))
    rng.shuffle(chosen)
    theme_by_district = dict(zip(distinct_districts, chosen))
    group_theme = {group: theme_by_district[assigned[group]] for group in groups}
    return component_of, group_theme


def _select_jail_rooms(rooms: list[Room], districts: list[int],
                       component_of: dict[tuple[int, int], int],
                       group_theme: dict[int, tuple[int, tuple[int, ...]]],
                       tiles: list[int], rng: random.Random,
                       jail_probability: float = JAIL_CANDIDATE_PROBABILITY
                       ) -> frozenset[int]:
    """Pick blue-stone rooms with a long enough unpainted wall for cells."""
    blue_rooms = [ridx for ridx, room in enumerate(rooms)
                  if group_theme[component_of[room.center]][0] == 8]
    selected = []
    for ridx, room in enumerate(rooms):
        base = group_theme[component_of[room.center]][0]
        if base != 8:
            continue
        sides = (
            [(x, room.y - 1) for x in range(room.x - 1, room.x + room.w + 1)],
            [(x, room.y + room.h) for x in range(room.x - 1, room.x + room.w + 1)],
            [(room.x - 1, y) for y in range(room.y, room.y + room.h)],
            [(room.x + room.w, y) for y in range(room.y, room.y + room.h)],
        )
        longest = 0
        for side in sides:
            run = 0
            for cell in side:
                run = run + 1 if _at(tiles, *cell) == WALL else 0
                longest = max(longest, run)
        if longest >= 5 and rng.random() < jail_probability:
            selected.append(ridx)
    # A blue-stone district still needs ordinary rooms so the cell treatment
    # reads as a deliberate sub-area instead of consuming the whole material
    # family. Keep jails a strict minority of all blue-stone rooms, including
    # high-probability catacomb variants.
    limit = max(0, (len(blue_rooms) - 1) // 2)
    if len(selected) > limit:
        rng.shuffle(selected)
        selected = selected[:limit]
    return frozenset(selected)


def _apply_wall_theme(tiles: list[int], things: list[int], rooms: list[Room],
                      districts: list[int], component_of: dict[tuple[int, int], int],
                      group_theme: dict[int, tuple[int, tuple[int, ...]]],
                      rng: random.Random,
                      jail_rooms: frozenset[int] = frozenset(),
                      identities: list[RoomIdentity] | None = None,
                      atmosphere: int = 3,
                      ) -> dict[int, list[tuple[int, int]]]:
    """Apply native WL6 materials without changing traversable geometry.

    Returns each room's landmark decor-wall cells (portraits, banners,
    insignia) so the decoration pass can frame them with furniture instead
    of placing pieces mid-room."""
    landmark_cells: dict[int, list[tuple[int, int]]] = {}
    for index, tile in enumerate(tiles):
        if tile != WALL:
            continue
        x, y = index % GRID, index // GRID
        group = next((component_of[cell] for cell in ((x + 1, y), (x - 1, y),
                                                       (x, y + 1), (x, y - 1))
                      if cell in component_of), None)
        if group is not None:
            tiles[index] = group_theme[group][0]
    for ridx, (room, district) in enumerate(zip(rooms, districts)):
        base, accents = group_theme[component_of[room.center]]
        sides = (
            [(x, room.y - 1) for x in range(room.x - 1, room.x + room.w + 1)],
            [(x, room.y + room.h) for x in range(room.x - 1, room.x + room.w + 1)],
            [(room.x - 1, y) for y in range(room.y, room.y + room.h)],
            [(room.x + room.w, y) for y in range(room.y, room.y + room.h)],
        )
        if ridx in jail_rooms and base == 8:
            other_accents = {41}
            for side in sides:
                run = [cell for cell in side if _at(tiles, *cell) == base]
                for i, (x, y) in enumerate(run):
                    if i % 3 == 0:
                        continue
                    # Keep a neutral stone buffer at nearby room seams, as
                    # the ordinary accent pass does below.
                    if any(_at(tiles, x + dx, y + dy) in other_accents
                           for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                        continue
                    tile = rng.choices((5, 7), weights=(9, 1))[0]
                    _set(tiles, x, y, tile)
                    # Plain bars sometimes get the loose remains that distinguish a
                    # neglected cellblock without turning the wall texture itself
                    # into a room-wide skeleton set piece.
                    if tile == 5 and rng.random() < 0.3:
                        interior = [(nx, ny) for nx, ny in ((x - 1, y), (x + 1, y),
                                                             (x, y - 1), (x, y + 1))
                                    if room.x <= nx < room.x + room.w
                                    and room.y <= ny < room.y + room.h
                                    and _is_floor(_at(tiles, nx, ny))
                                    and _at(things, nx, ny) == 0]
                        if interior:
                            _set(things, *rng.choice(interior), rng.choice((42, 64, 65, 66)))
            continue
        material = MATERIAL_BY_BASE.get(base)
        identity = (identities[ridx]
                    if identities is not None and ridx < len(identities) else None)
        concept = identity.concept if identity is not None else ""
        plain_variants = tuple(tile for tile in (material.plain_variants
                                                 if material else ())
                               if tile in accents)
        damage_variants = tuple(tile for tile in (material.damage_variants
                                                  if material else ())
                                if tile in accents)

        # A room may use one coherent surface variant, never a scatter of
        # unrelated tiles. Damage is additionally gated by both atmosphere
        # and semantic room identity.
        surface = base
        if (damage_variants and atmosphere >= 3
                and (identity is None or concept in DAMAGED_WALL_CONCEPTS)
                and rng.random() < min(0.65, 0.20 + atmosphere * 0.09)):
            surface = rng.choice(damage_variants)
        elif plain_variants and (identity is None or rng.random() < 0.58):
            surface = plain_variants[(ridx + district) % len(plain_variants)]
        if surface != base:
            for side in sides:
                for x, y in side:
                    if _at(tiles, x, y) == base:
                        _set(tiles, x, y, surface)

        material_landmarks = tuple(tile for tile in (material.landmarks
                                                      if material else accents)
                                   if tile in accents and tile in DECOR_WALLS)
        eligible_landmarks = tuple(
            tile for tile in material_landmarks
            if identity is None or concept in WALL_LANDMARK_CONCEPTS.get(tile, ()))
        formal = concept in {
            "guardpost", "checkpoint", "war-room", "trophy-hall", "gallery",
            "officers-quarters", "armory", "jail", "holding-cell",
            "interrogation-room",
        }
        place_landmark = bool(eligible_landmarks) and (
            identity is None or rng.random() < (0.30 if formal else 0.12))

        # Stained glass is a complete paired composition in prestigious
        # marble rooms, not a general-purpose material or isolated window.
        special_glass = (identity is not None and base == 42
                         and concept in {"gallery", "trophy-hall", "war-room"}
                         and rng.random() < 0.12)

        if special_glass or place_landmark:
            # Landmark tiles hang like pictures on the longest clean
            # (contiguous, same-base) wall run -- never the material for the
            # whole room. Short runs get one centered tile; longer runs get a
            # mirrored pair, and the longest a center-plus-pair triplet, so a
            # dressed wall reads as deliberately symmetric composition.
            runs: list[tuple[int, list[tuple[int, int]]]] = []
            for side_index, side in enumerate(sides):
                current: list[tuple[int, int]] = []
                for cell in side:
                    if _at(tiles, *cell) in ({base, surface} | set(plain_variants)
                                             | set(damage_variants)):
                        current.append(cell)
                    elif current:
                        runs.append((side_index, current))
                        current = []
                if current:
                    runs.append((side_index, current))
            side_index, run = max(runs, key=lambda item: len(item[1]), default=(-1, []))
            if run:
                selected_runs = [run]
                opposite = {0: 1, 1: 0, 2: 3, 3: 2}[side_index]
                compatible = [candidate for candidate_side, candidate in runs
                              if candidate_side == opposite
                              and abs(len(candidate) - len(run)) <= 2]
                # Opposing dressed faces are an occasional whole-room
                # composition, never an independently rolled second wall.
                if compatible and len(run) >= 9 and rng.random() < 0.25:
                    selected_runs.append(max(compatible, key=len))
                accent = (33 if special_glass else
                          eligible_landmarks[district % len(eligible_landmarks)])
                for selected in selected_runs:
                    mid = len(selected) // 2
                    if special_glass:
                        if len(selected) < 7:
                            continue
                        offset = max(2, len(selected) // 4)
                        spots = [selected[mid - offset], selected[mid + offset]]
                    elif accent == 7 or len(selected) < 9:
                        spots = [selected[mid]]
                    elif len(selected) < 13:
                        offset = max(2, len(selected) // 4)
                        spots = [selected[mid - offset], selected[mid + offset]]
                    else:
                        offset = max(3, len(selected) // 4)
                        spots = [selected[mid - offset], selected[mid], selected[mid + offset]]
                    for x, y in spots:
                        landmark = (rng.choices((5, 7), weights=(9, 1))[0]
                                    if accent == 7 else accent)
                        _set(tiles, x, y, landmark)
                        landmark_cells.setdefault(ridx, []).append((x, y))
                        if landmark == 5 and rng.random() < 0.3:
                            interior = [(nx, ny) for nx, ny in
                                        ((x - 1, y), (x + 1, y),
                                         (x, y - 1), (x, y + 1))
                                        if room.x <= nx < room.x + room.w
                                        and room.y <= ny < room.y + room.h
                                        and _is_floor(_at(tiles, nx, ny))
                                        and _at(things, nx, ny) == 0]
                            if interior:
                                _set(things, *rng.choice(interior),
                                     rng.choice((42, 64, 65, 66)))
    return landmark_cells
