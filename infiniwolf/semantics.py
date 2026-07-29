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

This module's tile writes are category 3 material substitution only. It
substitutes wall materials and never changes which cells are walkable.
"""

from __future__ import annotations

from collections import Counter, OrderedDict, deque
from dataclasses import dataclass
from itertools import combinations
import random

from .grid import _at, _floor_components, _is_floor, _room_probe, _set
from .model import (SET_PIECE_CONTRACTS, FloorVariant, KeyObjective,
                    LandmarkPlan, Room, RoomIdentity, RoomSpec, SetPiecePlan)
from .room_policy import base_theme
from .wl6 import (DAMAGED_WALL_CONCEPTS, DECOR_WALLS, DOORS,
                  FLOOR_TEN_STONE_THEME, GRID, JAIL_CANDIDATE_PROBABILITY,
                  MATERIAL_BY_BASE, PURPLE_MIN_FLOOR, WALL,
                  WALL_LANDMARK_CONCEPTS, WALL_THEMES)


class _SemanticIdentities(list):
    """Room identities plus semantics-private room-sequence composition.

    The shared model deliberately keeps ``RoomIdentity`` small. These values are
    consumed by the wall pass in this module and do not leak a second semantic
    decision into decoration or encounters.
    """

    def __init__(self, values, landmark_room: int,
                 monumentality: tuple[float, ...],
                 damage_gradient: tuple[float, ...],
                 set_piece_contracts):
        super().__init__(values)
        self.landmark_room = landmark_room
        self.monumentality = monumentality
        self.damage_gradient = damage_gradient
        self.set_piece_contracts = set_piece_contracts


@dataclass(frozen=True, slots=True)
class _VisibilityIntent:
    family: str
    observer_role: str
    subject_role: str
    observer_room: int
    subject_room: int

    @property
    def reason(self) -> str:
        return (f"setpiece-visibility:{self.family}:"
                f"{self.observer_role}->{self.subject_role}")


@dataclass(frozen=True, slots=True)
class _SetPieceSemanticContracts:
    visibility: tuple[_VisibilityIntent, ...] = ()
    landmark_rooms: frozenset[int] = frozenset()


def _set_piece_semantic_contracts(
        specs: list[RoomSpec],
        set_pieces: tuple[SetPiecePlan, ...] = (),
) -> _SetPieceSemanticContracts:
    """Resolve advisory role contracts onto the rooms that survived placement.

    ``PlacedPlan`` compacts room indices after dropping optional rooms. The motif
    tag is therefore the authoritative reverse lookup at this point; direct
    ``rooms_for_role`` results are retained when they still address the matching
    realized spec. The fallback contract table is needed by the historical
    generator call site, which hands semantics the tagged specs but not the
    enclosing ``FloorPlan``.
    """
    tagged: dict[tuple[str, str], list[int]] = {}
    family_order: list[str] = []
    for index, spec in enumerate(specs):
        parts = spec.motif.split(":", 2)
        if len(parts) != 3 or parts[0] != "setpiece":
            continue
        family, role = parts[1], parts[2]
        tagged.setdefault((family, role), []).append(index)
        if family not in family_order:
            family_order.append(family)

    def rooms_for(family: str, role: str,
                  plan: SetPiecePlan | None) -> tuple[int, ...]:
        tagged_rooms = tagged.get((family, role), ())
        if plan is None:
            return tuple(tagged_rooms)
        direct = tuple(
            index for index in plan.rooms_for_role(role)
            if 0 <= index < len(specs)
            and index in tagged_rooms)
        return direct + tuple(index for index in tagged_rooms
                              if index not in direct)

    visibility: list[_VisibilityIntent] = []
    landmark_rooms: set[int] = set()
    if set_pieces:
        sources = []
        for plan in set_pieces:
            role_pairs = tuple(plan.roles_for("visibility_contracts").items())
            sources.append((plan.family, plan, role_pairs,
                            plan.landmark_contract))
    else:
        sources = [
            (family, None,
             tuple(SET_PIECE_CONTRACTS.get(family, {}).get("visibility", ())),
             tuple(SET_PIECE_CONTRACTS.get(family, {}).get("landmark", ())))
            for family in family_order
        ]

    for family, plan, role_pairs, landmark_roles in sources:
        for observer_role, subject_role in role_pairs:
            for observer in rooms_for(family, observer_role, plan):
                for subject in rooms_for(family, subject_role, plan):
                    if observer != subject:
                        visibility.append(_VisibilityIntent(
                            family, observer_role, subject_role,
                            observer, subject))
        for role in landmark_roles:
            landmark_rooms.update(rooms_for(family, role, plan))
    # Repeated roles (the prison program has two cell blocks) can otherwise
    # repeat an identical resolved request after index compaction.
    visibility = list(dict.fromkeys(visibility))
    return _SetPieceSemanticContracts(tuple(visibility),
                                      frozenset(landmark_rooms))


def _room_identities(tiles: list[int],
                     rooms: list[Room], specs: list[RoomSpec], districts: list[int],
                     edges: list[tuple[int, int]], variant: FloorVariant,
                     jail_rooms: frozenset[int],
                     component_of: dict[tuple[int, int], int],
                     group_theme: dict[int, tuple[int, tuple[int, ...]]],
                     exit_room: Room | None, boss_room: Room | None = None,
                     special_family: str = "standard",
                     key_objectives: tuple[KeyObjective, ...] = (),
                     set_pieces: tuple[SetPiecePlan, ...] = (),
                     ) -> list[RoomIdentity]:
    """Resolve grammar forward into compatible room concepts.

    Role/tier/motif and the floor variant are already fixed at this point;
    material and decoration only refine that earlier identity.
    """
    overrides = dict(variant.decor_overrides)
    resolved: list[tuple[str, str, int]] = []
    for index, (room, spec, district) in enumerate(zip(rooms, specs, districts)):
        theme = "jail" if index in jail_rooms else base_theme(spec.role, spec.tier)
        if index not in jail_rooms:
            theme = overrides.get(theme, theme)
        special = ("start" if index == 0 else "exit" if room == exit_room else
                   "boss" if boss_room is not None and room == boss_room else
                   spec.role if spec.role in
                   ("arrival", "staging", "victory", "premium-vault", "recovery") else
                   "jail" if index in jail_rooms else "")
        wall_base = group_theme[component_of[_room_probe(tiles, room)]][0]
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
            decided = [concepts[neighbor] for neighbor in neighbors[index]
                       if neighbor < len(concepts)]
            # Repel duplicates first, then attract a functional partner, then
            # balance counts. Ordering matters: two adjacent identical rooms read
            # worse than a missed pairing, so repulsion stays dominant.
            concept = min(ordered, key=lambda candidate: (
                decided.count(candidate),
                -_affinity_with(candidate, decided),
                counts[candidate], ordered.index(candidate)))
        elif theme == "grand" and spec.role == "hub":
            concept = "courtyard"
        else:
            palette = palettes.get(theme, (theme,))
            offset = (index + district) % len(palette)
            ordered = palette[offset:] + palette[:offset]
            decided = [concepts[neighbor] for neighbor in neighbors[index]
                       if neighbor < len(concepts)]
            # Repel duplicates first, then attract a functional partner, then
            # balance counts. Ordering matters: two adjacent identical rooms read
            # worse than a missed pairing, so repulsion stays dominant.
            concept = min(ordered, key=lambda candidate: (
                decided.count(candidate),
                -_affinity_with(candidate, decided),
                counts[candidate], ordered.index(candidate)))
        concepts.append(concept)
        counts[concept] += 1

    result = []
    for room, spec, district, (theme, special, wall_base), concept in zip(
            rooms, specs, districts, resolved, concepts):
        result.append(RoomIdentity(spec.role, spec.tier, spec.motif, district,
                                   variant.name, concept, theme, wall_base, special))

    # The wall pass happens before the public landmark plan, but sequence-level
    # composition needs a destination now. The shortest start-to-terminus route
    # is the semantic information available at this stage; the later progression
    # route normally agrees and the landmark's structural salience dominates any
    # remaining tie.
    terminus = next((index for index, room in enumerate(rooms)
                     if room == (boss_room or exit_room)), len(rooms) - 1)
    approximate_route = _shortest_room_path(0, terminus, len(rooms), edges)
    set_piece_contracts = _set_piece_semantic_contracts(specs, set_pieces)
    preliminary = plan_landmarks(
        rooms, specs, [spec.role for spec in specs], edges, districts,
        approximate_route, tiles=tiles, set_pieces=set_pieces)
    landmark_room = next((plan.room_index for plan in preliminary
                          if plan.rank == "primary"), -1)
    monumentality, damage_gradient = _composition_profile(
        edges, districts, landmark_room,
        tuple(identity.concept in DAMAGED_WALL_CONCEPTS for identity in result))
    return _SemanticIdentities(result, landmark_room, monumentality,
                               damage_gradient, set_piece_contracts)


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
        group = component_of[_room_probe(tiles, room)]
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
                  if group_theme[component_of[_room_probe(tiles, room)]][0] == 8]
    selected = []
    for ridx, room in enumerate(rooms):
        base = group_theme[component_of[_room_probe(tiles, room)]][0]
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
                      damage_scale: float = 1.0,
                      ) -> dict[int, list[tuple[int, int]]]:
    """Apply native WL6 materials without changing traversable geometry.

    Returns each room's landmark decor-wall cells (portraits, banners,
    insignia) so the decoration pass can frame them with furniture instead
    of placing pieces mid-room."""
    landmark_cells: dict[int, list[tuple[int, int]]] = {}
    composed = isinstance(identities, _SemanticIdentities)
    landmark_room = identities.landmark_room if composed else -1
    set_piece_contracts = (
        identities.set_piece_contracts if composed
        else _SetPieceSemanticContracts())
    monumentality = (identities.monumentality if composed else
                     tuple(0.0 for _ in rooms))
    damage_gradient = (identities.damage_gradient if composed else
                       tuple(1.0 for _ in rooms))
    damage_threshold = ({district: rng.random() for district in sorted(set(districts))}
                        if composed and atmosphere >= 3 else {})
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
        base, accents = group_theme[component_of[_room_probe(tiles, room)]]
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
        damage_chance = min(0.72, (0.20 + atmosphere * 0.09) * damage_scale)
        if composed:
            damaged = (ridx < len(damage_gradient)
                       and damage_threshold.get(district, 1.0)
                       < damage_chance * damage_gradient[ridx])
        else:
            damaged = rng.random() < damage_chance
        if (damage_variants and atmosphere >= 3
                and (identity is None or concept in DAMAGED_WALL_CONCEPTS)
                # damage_scale carries the campaign's aesthetic arc: early floors
                # come out intact and late ones battered, within a band narrow
                # enough that the atmosphere setting and room identity still
                # dominate. Clamped so the arc can never force every wall.
                and damaged):
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
        if identity is None:
            place_landmark = (bool(eligible_landmarks)
                              and rng.random() < (0.30 if formal else 0.12))
        elif (ridx == landmark_room
              or ridx in set_piece_contracts.landmark_rooms):
            # When the material/concept contract supports an accent, the planned
            # destination -- or a set-piece role promised the same treatment --
            # culminates the sequence instead of having to win one more
            # unrelated per-room roll.
            place_landmark = bool(eligible_landmarks)
        else:
            emphasis = monumentality[ridx] if ridx < len(monumentality) else 0.0
            chance = (0.30 if formal else 0.12) * (0.55 + 0.75 * emphasis)
            place_landmark = bool(eligible_landmarks) and rng.random() < chance

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
    _remember_landmark_tiles(rooms, tiles, set_piece_contracts)
    return landmark_cells


# Functionally related room concepts. A garrison puts its mess beside the barracks
# and its armoury near the training floor; a catacomb keeps ossuary and burial
# chamber together. Placing these next to each other is one of the strongest
# signals that a floor was laid out by someone rather than shuffled, because it
# implies the building had a purpose before it had rooms.
#
# One table, two consumers: concept assignment below uses it to *attract* partners,
# and campaign.py's candidate scoring uses it to reward floors that realized them.
# It used to exist twice, and only the scoring copy could influence anything -- a
# floor was rewarded for adjacencies it had no mechanism to seek.
CONCEPT_AFFINITIES: frozenset[frozenset[str]] = frozenset({
    frozenset(("barracks", "mess-kitchen")),
    frozenset(("barracks", "armory")),
    frozenset(("armory", "training-room")),
    frozenset(("armory", "ready-room")),
    frozenset(("storage", "ready-room")),
    frozenset(("storage", "workshop")),
    frozenset(("storage", "mess-kitchen")),
    frozenset(("supply-cache", "checkpoint")),
    frozenset(("supply-cache", "storage")),
    frozenset(("officers-quarters", "war-room")),
    frozenset(("war-room", "checkpoint")),
    frozenset(("gallery", "trophy-hall")),
    frozenset(("crypt", "ossuary")),
    frozenset(("crypt", "burial-chamber")),
    frozenset(("ossuary", "burial-chamber")),
    frozenset(("holding-cell", "interrogation-room")),
    frozenset(("jail", "interrogation-room")),
    frozenset(("jail", "holding-cell")),
    frozenset(("lounge", "dining-hall")),
    frozenset(("dining-hall", "mess-kitchen")),
})


def _affinity_with(candidate: str, neighbour_concepts) -> int:
    """How many already-decided neighbours this concept belongs beside."""
    return sum(frozenset((candidate, other)) in CONCEPT_AFFINITIES
               for other in neighbour_concepts)


# Structural distinctiveness is only one part of usefulness. A huge anchor that
# can be seen only after entering it is less useful for navigation than a more
# modest room that repeatedly resolves real choices.
_LANDMARK_WEIGHTS = {
    "area": 1.0,
    "anchor_tier": 3.0,
    "graph_degree": 0.8,
    "on_critical_route": 1.5,
    "district_boundary": 1.2,
    "special_role": 2.5,
}

# Ranks below primary. Three is the ceiling on purpose: a floor where everything
# is emphatic has no hierarchy, which fails for the same reason as a floor with
# none.
_MAX_SECONDARY = 3


@dataclass(frozen=True, slots=True)
class LandmarkVisibility:
    """One honest tile-ray from a meaningful position to a landmark room.

    ``position_kind`` is either ``door-threshold`` or ``choice-point``. This
    records geometric visibility only. It intentionally makes no claim that a
    wall composition remains recognisable when approached from its reverse face;
    the tile plane has no silhouette/facing identity from which to prove that.
    """

    landmark_room: int
    position: tuple[int, int]
    position_kind: str
    observer_room: int
    observer_district: int


# ``plan_landmarks`` is constrained to its historical signature at the generator
# call site. The wall pass sees the same room-list object and remembers the tile
# plane long enough for the later semantic plan. The small bounded cache prevents
# failed attempts before planning from retaining a campaign's worth of planes.
_LANDMARK_TILE_CONTEXTS = OrderedDict()


def _remember_landmark_tiles(
        rooms: list[Room], tiles: list[int],
        set_piece_contracts: _SetPieceSemanticContracts = _SetPieceSemanticContracts(),
) -> None:
    key = id(rooms)
    _LANDMARK_TILE_CONTEXTS[key] = (rooms, tiles, set_piece_contracts)
    _LANDMARK_TILE_CONTEXTS.move_to_end(key)
    while len(_LANDMARK_TILE_CONTEXTS) > 12:
        _LANDMARK_TILE_CONTEXTS.popitem(last=False)


def _shortest_room_path(start: int, goal: int, count: int,
                        edges: list[tuple[int, int]]) -> tuple[int, ...]:
    if not (0 <= start < count and 0 <= goal < count):
        return ()
    neighbours = {index: set() for index in range(count)}
    for first, second in edges:
        if first in neighbours and second in neighbours:
            neighbours[first].add(second)
            neighbours[second].add(first)
    previous = {start: -1}
    queue = deque([start])
    while queue and goal not in previous:
        current = queue.popleft()
        for other in sorted(neighbours[current]):
            if other not in previous:
                previous[other] = current
                queue.append(other)
    if goal not in previous:
        return ()
    path = []
    current = goal
    while current >= 0:
        path.append(current)
        current = previous[current]
    return tuple(reversed(path))


def _composition_profile(edges: list[tuple[int, int]], districts: list[int],
                         landmark_room: int,
                         damage_eligible: tuple[bool, ...]
                         ) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Sequence-level emphasis toward a landmark and district damage foci."""
    count = len(districts)
    neighbours = {index: set() for index in range(count)}
    for first, second in edges:
        if first in neighbours and second in neighbours:
            neighbours[first].add(second)
            neighbours[second].add(first)

    distances = [count + 1] * count
    if 0 <= landmark_room < count:
        distances[landmark_room] = 0
        queue = deque([landmark_room])
        while queue:
            current = queue.popleft()
            for other in neighbours[current]:
                if distances[other] > distances[current] + 1:
                    distances[other] = distances[current] + 1
                    queue.append(other)
    finite = [distance for distance in distances if distance <= count]
    furthest = max(finite, default=1) or 1
    monumentality = tuple(
        round(max(0.0, 1.0 - distance / furthest), 3)
        if distance <= count else 0.0
        for distance in distances)

    damage = [0.0] * count
    for district in sorted(set(districts)):
        candidates = [index for index, own in enumerate(districts)
                      if own == district and index < len(damage_eligible)
                      and damage_eligible[index]]
        if not candidates:
            continue
        focus = max(candidates, key=lambda index: (
            distances[index] if distances[index] <= count else count + 1,
            len(neighbours[index]), -index))
        local_distance = {focus: 0}
        queue = deque([focus])
        while queue:
            current = queue.popleft()
            for other in neighbours[current]:
                if (districts[other] == district
                        and other not in local_distance):
                    local_distance[other] = local_distance[current] + 1
                    queue.append(other)
        for index, distance in local_distance.items():
            damage[index] = round(max(0.25, 1.0 - 0.22 * distance), 3)
    return monumentality, tuple(damage)


def _line_visible(tiles: list[int], origin: tuple[int, int],
                  target: tuple[int, int]) -> bool:
    """Bresenham tile ray through floor and real doors only."""
    x0, y0 = origin
    x1, y1 = target
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    error = dx - dy
    x, y = x0, y0
    while True:
        if (x, y) not in (origin, target):
            tile = _at(tiles, x, y)
            if not _is_floor(tile) and tile not in DOORS:
                return False
        if (x, y) == target:
            return True
        twice = 2 * error
        if twice > -dy:
            error -= dy
            x += sx
        if twice < dx:
            error += dx
            y += sy


def landmark_visibility(tiles: list[int], rooms: list[Room],
                        edges: list[tuple[int, int]], districts: list[int],
                        landmark_rooms=None) -> tuple[LandmarkVisibility, ...]:
    """Build the threshold/choice-point visibility graph supported by tiles."""
    if not rooms:
        return ()
    targets = tuple(range(len(rooms)) if landmark_rooms is None
                    else landmark_rooms)
    neighbours = {index: set() for index in range(len(rooms))}
    for first, second in edges:
        if first in neighbours and second in neighbours:
            neighbours[first].add(second)
            neighbours[second].add(first)
    probes = [_room_probe(tiles, room) for room in rooms]

    positions: list[tuple[tuple[int, int], str, int]] = []
    for offset, tile in enumerate(tiles):
        if tile not in DOORS:
            continue
        position = offset % GRID, offset // GRID
        observer = min(range(len(rooms)), key=lambda index: (
            abs(probes[index][0] - position[0])
            + abs(probes[index][1] - position[1]), index))
        positions.append((position, "door-threshold", observer))
    for index, adjacent in neighbours.items():
        if len(adjacent) >= 3:
            positions.append((probes[index], "choice-point", index))

    relationships = []
    for target_index in targets:
        if not 0 <= target_index < len(rooms):
            continue
        target = probes[target_index]
        for position, kind, observer in positions:
            if kind == "choice-point" and observer == target_index:
                continue
            if _line_visible(tiles, position, target):
                district = districts[observer] if observer < len(districts) else -1
                relationships.append(LandmarkVisibility(
                    target_index, position, kind, observer, district))
    return tuple(relationships)


def _has_loop_return(index: int, neighbours: dict[int, set[int]]) -> bool:
    """Whether two approaches reconnect without passing through the landmark."""
    approaches = sorted(neighbours[index])
    if len(approaches) < 2:
        return False
    allowed = set(neighbours) - {index}
    for start in approaches:
        seen = {start}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            for other in neighbours[current]:
                if other in allowed and other not in seen:
                    seen.add(other)
                    queue.append(other)
        if any(other in seen for other in approaches if other != start):
            return True
    return False


def plan_landmarks(rooms: list[Room], specs: list[RoomSpec], roles: list[str],
                   edges: list[tuple[int, int]], districts: list[int],
                   critical_route, *, tiles: list[int] | None = None,
                   set_pieces: tuple[SetPiecePlan, ...] = (),
                   ) -> tuple[LandmarkPlan, ...]:
    """Nominate landmarks by navigational usefulness, deterministically.

    ``LandmarkPlan.score`` is the exposed 0..1 usefulness measurement for future
    quality scoring. It combines repeated tile-visible orientation, district
    identity, loop return, choice-point help, and structural distinctiveness.
    """
    if not rooms:
        return ()
    set_piece_contracts = _set_piece_semantic_contracts(specs, set_pieces)
    if tiles is None:
        context = _LANDMARK_TILE_CONTEXTS.get(id(rooms))
        if context is not None and context[0] is rooms:
            tiles = context[1]
            if not set_pieces:
                set_piece_contracts = context[2]

    degree = {index: 0 for index in range(len(rooms))}
    neighbours = {index: set() for index in range(len(rooms))}
    for first, second in edges:
        if first not in neighbours or second not in neighbours:
            continue
        degree[first] += 1
        degree[second] += 1
        neighbours[first].add(second)
        neighbours[second].add(first)

    largest = max((room.w * room.h for room in rooms), default=1) or 1
    busiest = max(degree.values(), default=1) or 1
    route = set(critical_route)
    route_order = {room: order for order, room in enumerate(critical_route)}
    special = {"boss-arena", "premium-vault", "victory", "climax"}
    visible = (landmark_visibility(tiles, rooms, edges, districts)
               if tiles is not None else ())
    visible_by_room = {index: [] for index in range(len(rooms))}
    for relationship in visible:
        visible_by_room[relationship.landmark_room].append(relationship)

    scored = []
    for index, room in enumerate(rooms):
        spec = specs[index] if index < len(specs) else None
        role = roles[index] if index < len(roles) else ""
        if spec is not None and spec.tier in ("closet", "corridor"):
            continue
        if role in ("start", "arrival", "exit"):
            continue
        crosses = any(districts[other] != districts[index]
                      for other in neighbours[index]
                      if other < len(districts) and index < len(districts))
        parts = {
            "area": (room.w * room.h) / largest,
            "anchor_tier": 1.0 if spec is not None and spec.tier == "anchor" else 0.0,
            "graph_degree": degree[index] / busiest,
            "on_critical_route": 1.0 if index in route else 0.0,
            "district_boundary": 1.0 if crosses else 0.0,
            "special_role": 1.0 if role in special else 0.0,
        }
        salience = sum(_LANDMARK_WEIGHTS[key] * value
                       for key, value in parts.items()) / sum(_LANDMARK_WEIGHTS.values())
        views = visible_by_room[index]
        view_score = min(1.0, len({view.position for view in views}) / 4.0)
        choice_score = float(any(view.position_kind == "choice-point"
                                 for view in views))
        own_district = districts[index] if index < len(districts) else -1
        district_score = float(crosses or any(
            view.observer_district not in (-1, own_district) for view in views))
        observer_rooms = {view.observer_room for view in views}
        loop_score = float(_has_loop_return(index, neighbours)
                           and len(observer_rooms) >= 2)
        usefulness = (0.30 * view_score + 0.20 * choice_score
                      + 0.15 * district_score + 0.15 * loop_score
                      + 0.20 * min(1.0, salience))
        branch_destination = any(degree[other] >= 3 for other in neighbours[index])
        contributions = {
            "multi-vantage": 0.30 * view_score,
            "choice-orienting": 0.20 * choice_score,
            "district-identity": 0.15 * district_score,
            "loop-reappearing": 0.15 * loop_score,
            "structural-distinctiveness": 0.20 * min(1.0, salience),
        }
        purpose = max(contributions, key=lambda name: (contributions[name], name))
        scored.append((usefulness, index, purpose, crosses, branch_destination))

    contracted_primary = None
    for intent in set_piece_contracts.visibility:
        views = visible_by_room.get(intent.subject_room, ())
        if any(view.observer_room == intent.observer_room for view in views):
            contracted_primary = intent
            break

    if not scored and contracted_primary is None:
        return ()
    scored.sort(key=lambda item: (-item[0], item[1]))
    if contracted_primary is not None:
        primary_index = contracted_primary.subject_room
        record = next((item for item in scored if item[1] == primary_index),
                      None)
        primary_score = record[0] if record is not None else 0.0
        primary_reason = contracted_primary.reason
    else:
        primary_score, primary_index, primary_reason, _, _ = scored[0]

    def approach_of(index: int) -> int:
        on_route = [other for other in neighbours[index] if other in route_order]
        return (min(on_route, key=route_order.get) if on_route else
                min(neighbours[index]) if neighbours[index] else -1)

    primary_approach = (
        contracted_primary.observer_room
        if contracted_primary is not None else approach_of(primary_index))
    plans = [LandmarkPlan(primary_index, "primary", primary_reason,
                          round(primary_score, 3), primary_approach)]
    claimed = {primary_index} | neighbours[primary_index]
    # District transitions and destinations immediately beyond a choice are the
    # semantic jobs of secondaries. Fill any remaining budget by usefulness.
    secondary_order = sorted(
        (item for item in scored if item[1] != primary_index),
        key=lambda item: (
            0 if item[3] else 1 if item[4] else 2, -item[0], item[1]))
    for score, index, reason, transition, branch_destination in secondary_order:
        if len(plans) > _MAX_SECONDARY:
            break
        if index in claimed:
            continue
        if transition:
            reason = "district-transition"
        elif branch_destination:
            reason = "branch-destination"
        plans.append(LandmarkPlan(index, "secondary", reason, round(score, 3),
                                  approach_of(index)))
        claimed |= {index} | neighbours[index]
    return tuple(plans)
