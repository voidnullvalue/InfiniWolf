"""Soft quality critique: what is wrong with a map that is nonetheless valid.

Strictly separate from validation. validate_map decides whether a floor may ship
at all and raises; this returns a tuple of named flags describing weaknesses --
no loop, flat hierarchy, corridor-heavy, motif imbalance, repeated encounter
templates. A flag never rejects a candidate; it only makes that candidate rank
below a cleaner one when several are hard-valid.

Every metric here must be deterministic, bounded, explainable and unit-tested.
Flags remain concrete diagnoses a reader can go look at, while QualityReport adds
separate bounded evidence for lexicographic candidate comparison. No aggregate
score can erase a named defect.

Thresholds are calibrated against measured distributions, not guessed. A flag that
can never fire is worse than no flag -- it reads as coverage while measuring
nothing -- and one that always fires carries no information either. Where the
generator turns out to be reliably good at something, the flag is kept as an
explicit regression tripwire with its threshold above the observed ceiling and
labelled as such, rather than being tuned down until it fires on healthy maps. Nothing in this module
may consult randomness or the config -- the same map must always draw the same
critique.

Keeping the two apart in separate modules is the point. A soft score that could
reach validation would eventually be tempted to excuse a hard-invalid map, and
campaign.py's selection deliberately compares only among candidates that already
passed.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from itertools import combinations

from .grid import _at, _floor_components, _is_floor
from .model import GeneratedMap
from .simulation import simulate_player_experience
from .wl6 import BOSSES, DOORS, ENEMY_CODES, GRID, _patrol_actor_direction


UNMEASURED = 0.0
# Compatibility name retained for callers that imported the former stub value.
# Every QualityReport field is measured below; the sentinel is no longer used.


@dataclass(frozen=True)
class QualityReport:
    severe_defects: tuple[str, ...]
    diagnostics: tuple[str, ...]
    spatial_composition: float
    route_quality: float
    navigational_legibility: float
    encounter_quality: float
    pacing_quality: float
    secret_quality: float
    landmark_quality: float
    corpus_similarity: float
    campaign_contrast: float


@dataclass(frozen=True)
class _Topology:
    components: tuple[frozenset[tuple[int, int]], ...]
    graph_edges: frozenset[tuple[int, int]]
    links: tuple[frozenset[int], ...]
    cycles: int
    sizes: tuple[int, ...]
    total: int
    room_floor: frozenset[tuple[int, int]]
    all_floor: frozenset[tuple[int, int]]
    door_count: int

    @property
    def largest_share(self) -> float:
        return self.sizes[0] / self.total if self.sizes else 0.0

    @property
    def dead_end_ratio(self) -> float:
        return (sum(len(neighbors) == 1 for neighbors in self.links)
                / len(self.links) if self.links else 0.0)


def _topology(level: GeneratedMap) -> _Topology:
    """Compute the floor partition and door graph once for all quality views."""
    components = _floor_components(level.tiles)
    owner = {cell: index for index, component in enumerate(components) for cell in component}
    graph_edges: set[tuple[int, int]] = set()
    door_count = 0
    for index, tile in enumerate(level.tiles):
        if tile not in DOORS:
            continue
        door_count += 1
        x, y = index % GRID, index // GRID
        neighbors = {owner[cell] for cell in ((x + 1, y), (x - 1, y),
                                              (x, y + 1), (x, y - 1))
                     if cell in owner}
        graph_edges.update(tuple(sorted(edge)) for edge in combinations(neighbors, 2))
    links = {index: set() for index in range(len(components))}
    for a, b in graph_edges:
        links[a].add(b); links[b].add(a)
    graph_components = 0
    unseen = set(links)
    while unseen:
        graph_components += 1
        queue = [unseen.pop()]
        while queue:
            for nxt in links[queue.pop()] & unseen:
                unseen.remove(nxt); queue.append(nxt)
    cycles = len(graph_edges) - len(components) + graph_components
    sizes = sorted((len(component) for component in components), reverse=True)
    total = sum(sizes) or 1
    room_floor = {cell for room in level.rooms
                  for y in range(room.y, room.y + room.h)
                  for x in range(room.x, room.x + room.w)
                  for cell in ((x, y),) if _is_floor(_at(level.tiles, x, y))}
    all_floor = {(x, y) for y in range(GRID) for x in range(GRID)
                 if _is_floor(_at(level.tiles, x, y))}
    return _Topology(
        components=tuple(frozenset(component) for component in components),
        graph_edges=frozenset(graph_edges),
        links=tuple(frozenset(links[index]) for index in range(len(components))),
        cycles=cycles,
        sizes=tuple(sizes),
        total=total,
        room_floor=frozenset(room_floor),
        all_floor=frozenset(all_floor),
        door_count=door_count,
    )


def _critique(level: GeneratedMap,
              topology: _Topology | None = None) -> tuple[str, ...]:
    topology = topology or _topology(level)
    cycles = topology.cycles
    sizes = topology.sizes
    total = topology.total
    room_floor = topology.room_floor
    all_floor = topology.all_floor
    flags = []
    if cycles == 0:
        flags.append("no_loop")
    if sizes and sizes[0] / total < 0.10:
        flags.append("no_anchor")
    if sum(sizes[:3]) / total < 0.25:
        flags.append("flat_hierarchy")
    if all_floor and len(all_floor - room_floor) / len(all_floor) > 0.45:
        flags.append("corridor_heavy")
    longest = 0
    for horizontal in (True, False):
        for fixed in range(GRID):
            run = 0
            for moving in range(GRID):
                x, y = (moving, fixed) if horizontal else (fixed, moving)
                run = run + 1 if _is_floor(_at(level.tiles, x, y)) else 0
                longest = max(longest, run)
    # Regression tripwire, not a live diagnosis. The old 21 came from the
    # manual's pistol falloff range, not from any map: id's own 60 maps run a
    # median 27 with a p90 of 48, and the 227-map fan corpus agrees at 27/49. A
    # flag set below the corpus median fires on maps that look like id's. 46 sits
    # just under both p90s, so it catches a genuinely fused pair of spaces
    # without calling an authored great hall a defect.
    if longest > 46:
        flags.append("long_sightline")
    motif_counts = {motif: level.motif_rooms.count(motif) for motif in level.motifs}
    if level.rooms and any(count / len(level.rooms) > 0.40
                           for count in motif_counts.values()):
        flags.append("motif_imbalance")
    if (len(level.secret_variants) >= 3
            and set(level.secret_variants) == {"square"}):
        flags.append("secret_monotony")
    encounter_templates = [encounter.template for encounter in level.encounters
                           if encounter.template not in
                           ("novelty", "boss-support", "patrol")]
    if (len(encounter_templates) >= 5
            and max(Counter(encounter_templates).values())
            / len(encounter_templates) > 0.55):
        flags.append("encounter_repetition")
    ordinary_actors = [thing for thing in level.things
                       if thing in ENEMY_CODES and thing not in BOSSES]
    moving = sum(_patrol_actor_direction(actor) is not None
                 for actor in ordinary_actors)
    if (level.patrol_target and len(ordinary_actors) >= 8
            and moving / len(ordinary_actors) < level.patrol_target * 0.75):
        flags.append("patrol_sparse")
    # --- Sequence monotony along the mandatory route ---
    # Set membership cannot see repetition: a floor whose critical route reads
    # armory, armory, armory has the same concept *set* as one that varies, and
    # scored identically before these flags existed. Consecutive repeats are what
    # a player actually experiences as sameness.
    route = [index for index in level.critical_route
             if index < len(level.room_concepts)]
    if len(route) >= 4:
        concepts = [level.room_concepts[index] for index in route]
        repeats = sum(first == second
                      for first, second in zip(concepts, concepts[1:]))
        # Regression tripwire, not a live diagnosis. Measured over 70 floors the
        # consecutive-repeat rate never exceeded 0.12 and its median was 0.00 --
        # semantics already varies concepts along a route. The threshold sits well
        # above that ceiling so this stays silent today and fires if a future
        # change starts handing adjacent rooms the same concept.
        if repeats / (len(concepts) - 1) > 0.25:
            flags.append("concept_monotony")
        shapes = [level.room_shapes[index] for index in route
                  if index < len(level.room_shapes)]
        if len(shapes) >= 4:
            shape_repeats = sum(first == second
                                for first, second in zip(shapes, shapes[1:]))
            # Calibrated to fire on roughly the worst tenth: measured p50 0.20,
            # p90 0.40, max 0.50. The original 0.80 was unreachable, which made
            # the flag look like coverage while measuring nothing.
            if shape_repeats / (len(shapes) - 1) > 0.40:
                flags.append("shape_monotony")

    # --- Area rhythm along the mandatory route ---
    # A route of near-identical rooms reads as a corridor of boxes however varied
    # their contents. Measured as the spread of room areas relative to their mean,
    # so it is scale-free and comparable between a compact and a sprawling floor.
    if len(route) >= 5 and level.rooms:
        areas = [level.rooms[index].w * level.rooms[index].h for index in route
                 if index < len(level.rooms)]
        if len(areas) >= 5:
            mean_area = sum(areas) / len(areas)
            spread = max(areas) - min(areas)
            # Measured spread/mean ran 1.27 to 3.39 with a median of 1.91 --
            # room tiers already guarantee variety, and the original 0.45 was
            # below the observed minimum by a factor of three. 1.40 catches
            # roughly the flattest twentieth.
            if mean_area and spread / mean_area < 1.40:
                flags.append("flat_area_rhythm")

    # --- Dead-end payoff ---
    # A branch that costs a walk and returns nothing teaches the player to stop
    # exploring. Degree-one rooms off the critical route should hold a pickup, a
    # secret or an encounter; counting them is cheap and the intent is explicit.
    if level.rooms and level.edges:
        degree = Counter(index for edge in level.edges for index in edge)
        critical = set(level.critical_route)
        rewarded = {placement.room_index for placement in level.pickup_placements}
        rewarded |= {encounter.room_index for encounter in level.encounters}
        rewarded |= {detail.host_room for detail in level.secret_details}
        dead_ends = [index for index in range(len(level.rooms))
                     if degree[index] <= 1 and index not in critical]
        barren = [index for index in dead_ends if index not in rewarded]
        # Regression tripwire. Every dead end in the measured sample was already
        # rewarded -- the barren fraction was 0.00 on all 69 qualifying floors --
        # so this cannot fire today by design and exists to catch a future change
        # that stops paying off branches.
        if len(dead_ends) >= 2 and len(barren) / len(dead_ends) > 0.34:
            flags.append("dead_end_unrewarded")

    # --- Landmark hierarchy ---
    # A regression tripwire, not a live diagnosis: plan_landmarks nominates exactly
    # one primary on every floor measured, so this is silent by construction and
    # fires if selection starts failing to find a dominant space, or starts naming
    # several. Both are the same defect -- a floor where everything is emphatic is
    # as unnavigable as one where nothing is.
    if level.rooms and level.landmarks is not None:
        primaries = sum(plan.rank == "primary" for plan in level.landmarks)
        if len(level.rooms) >= 8 and primaries != 1:
            flags.append("landmark_hierarchy_broken")

    return tuple(flags)


# Flags that have never fired on a healthy floor and are not expected to. Each
# docstring above says so individually; naming them as a set makes the claim
# checkable and stops them being read as live signal. Measured over 84 floors
# spanning 12 seeds, every one of these stayed silent, so counting them in a
# selection key adds a term that is always zero -- coverage theatre. They stay
# because a regression tripwire is exactly what they are good at.
TRIPWIRE_FLAGS = frozenset({
    "concept_monotony",
    "corridor_heavy",
    "dead_end_unrewarded",
    "landmark_hierarchy_broken",
    "long_sightline",
    "motif_imbalance",
})

# Defects that make a floor structurally worse to play rather than merely
# less varied. Selection treats these as disqualifying rather than as one more
# unit of flag count: a floor with no loop and a floor with monotonous secrets
# both scored "one flag" before, which is why the loopless floors kept winning.
SEVERE_FLAGS = frozenset({"no_loop", "no_anchor", "flat_hierarchy"})


def tripwires(level: GeneratedMap) -> tuple[str, ...]:
    """Regression-only flags. A non-empty result means something regressed."""
    return tuple(flag for flag in _critique(level) if flag in TRIPWIRE_FLAGS)


def diagnostics(level: GeneratedMap) -> tuple[str, ...]:
    """Live critique: the flags that describe this floor rather than guard the
    generator against a future change."""
    return tuple(flag for flag in _critique(level) if flag not in TRIPWIRE_FLAGS)


_CORPUS_REFERENCES = (
    # metric, reference, tolerance. Tolerances are the full meaningful scale for
    # ratios/counts (two cycles for the discrete cycle count), so a metric reaches
    # zero before an extreme floor can dominate the average.
    ("room_aspect_p50", 1.40, 1.00),
    ("largest_room_share", 0.22, 0.22),
    ("door_graph_cycles", 0.92, 2.00),
    ("dead_end_ratio", 0.30, 0.30),
    ("doors", 19.0, 19.0),
)


def _bounded(value: float) -> float:
    return min(1.0, max(0.0, value))


def _mean(values) -> float:
    values = tuple(values)
    return sum(values) / len(values) if values else 0.0


def _median(values) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if not ordered:
        return 0.0
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _closeness(value: float, reference: float, tolerance: float) -> float:
    return 1.0 - min(1.0, abs(value - reference) / tolerance)


def _corpus_similarity(level: GeneratedMap, topology: _Topology) -> float:
    aspects = (max(room.w, room.h) / max(1, min(room.w, room.h))
               for room in level.rooms)
    measured = {
        "room_aspect_p50": _median(aspects),
        "largest_room_share": topology.largest_share,
        "door_graph_cycles": float(topology.cycles),
        "dead_end_ratio": topology.dead_end_ratio,
        "doors": float(topology.door_count),
    }
    return _mean(
        _closeness(measured[name], reference, tolerance)
        for name, reference, tolerance in _CORPUS_REFERENCES
    )


def _spatial_composition(level: GeneratedMap, topology: _Topology) -> float:
    top_three_share = sum(topology.sizes[:3]) / topology.total
    corridor_share = (len(topology.all_floor - topology.room_floor)
                      / len(topology.all_floor) if topology.all_floor else 1.0)
    areas = [room.w * room.h for room in level.rooms]
    if len(areas) >= 2 and _mean(areas):
        area_rhythm = _bounded((max(areas) - min(areas)) / _mean(areas) / 1.40)
    else:
        area_rhythm = 0.0
    return _mean((
        _bounded(topology.largest_share / 0.10),
        _bounded(top_three_share / 0.25),
        1.0 - _bounded((corridor_share - 0.45) / 0.55),
        area_rhythm,
    ))


def _dead_end_payoff(level: GeneratedMap) -> float:
    if not level.rooms or not level.edges:
        return 0.0
    degree = Counter(index for edge in level.edges for index in edge)
    critical = set(level.critical_route)
    rewarded = {placement.room_index for placement in level.pickup_placements}
    rewarded |= {encounter.room_index for encounter in level.encounters}
    rewarded |= {detail.host_room for detail in level.secret_details}
    dead_ends = [index for index in range(len(level.rooms))
                 if degree[index] <= 1 and index not in critical]
    if not dead_ends:
        return 1.0
    return 1.0 - sum(index not in rewarded for index in dead_ends) / len(dead_ends)


def _route_quality(level: GeneratedMap, topology: _Topology) -> float:
    route = [index for index in level.critical_route
             if 0 <= index < len(level.rooms)]
    distinct_route = len(set(route)) / len(route) if route else 0.0
    route_extent = _bounded(len(route) / max(1, min(6, len(level.rooms))))
    return _mean((
        _bounded(topology.cycles),
        _bounded(level.exit_depth_ratio),
        distinct_route,
        route_extent,
        _dead_end_payoff(level),
    ))


def _sequence_legibility(values: list[str]) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return 1.0
    repeats = sum(first == second for first, second in zip(values, values[1:]))
    return 1.0 - repeats / (len(values) - 1)


def _navigational_legibility(level: GeneratedMap) -> float:
    route = [index for index in level.critical_route
             if 0 <= index < len(level.rooms)]
    concepts = [level.room_concepts[index] for index in route
                if index < len(level.room_concepts)]
    shapes = [level.room_shapes[index] for index in route
              if index < len(level.room_shapes)]
    districts = [str(level.room_districts[index]) for index in route
                 if index < len(level.room_districts)]
    metadata_coverage = _mean((
        _bounded(len(level.room_concepts) / max(1, len(level.rooms))),
        _bounded(len(level.room_shapes) / max(1, len(level.rooms))),
        _bounded(len(level.room_districts) / max(1, len(level.rooms))),
    ))
    return _mean((
        _sequence_legibility(concepts),
        _sequence_legibility(shapes),
        _sequence_legibility(districts),
        metadata_coverage,
    ))


def _normalized_diversity(values: list[str]) -> float:
    """Simpson diversity normalized so all-unique evidence reaches one."""
    if len(values) < 2:
        return 0.0
    counts = Counter(values)
    raw = 1.0 - sum((count / len(values)) ** 2 for count in counts.values())
    return raw * len(values) / (len(values) - 1)


def _room_sound_zone(level: GeneratedMap, room_index: int) -> int:
    """Dominant assigned floor zone in a room, or -1 when unavailable."""
    if (len(level.tiles) != GRID * GRID
            or not 0 <= room_index < len(level.rooms)):
        return -1
    room = level.rooms[room_index]
    zones = Counter(
        _at(level.tiles, x, y)
        for y in range(room.y, room.y + room.h)
        for x in range(room.x, room.x + room.w)
        if _is_floor(_at(level.tiles, x, y)))
    return min(zones, key=lambda zone: (-zones[zone], zone)) if zones else -1


def _multi_room_grammars(level: GeneratedMap) -> frozenset[str]:
    """Recognize only encounter sequences proven by finished-map evidence."""
    by_room: dict[int, list] = {}
    for encounter in level.encounters:
        by_room.setdefault(encounter.room_index, []).append(encounter)
    templates = {room: {encounter.template for encounter in encounters}
                 for room, encounters in by_room.items()}
    route = [index for index in level.critical_route
             if 0 <= index < len(level.rooms)]
    route_set = set(route)
    zones = {index: _room_sound_zone(level, index)
             for index in range(len(level.rooms))}
    evidence: set[str] = set()

    for objective in level.key_objectives:
        if objective.host_room in route:
            position = route.index(objective.host_room)
            if (position > 0
                    and "objective-guard" in templates.get(objective.host_room, ())
                    and route[position - 1] in by_room):
                evidence.add("objective-defense")

    patrol_rooms = {
        room for room, encounters in by_room.items()
        if any(encounter.patrol_path for encounter in encounters)}
    for front in route:
        if (front >= len(level.room_concepts)
                or level.room_concepts[front] not in ("checkpoint", "guardpost")
                or "visible-sentry" not in templates.get(front, ())
                or zones.get(front, -1) < 0):
            continue
        if any(response not in route_set
               and zones.get(response, -1) == zones.get(front, -2)
               and (abs(level.rooms[response].center[0] - level.rooms[front].center[0])
                    + abs(level.rooms[response].center[1]
                          - level.rooms[front].center[1]) <= 24)
               for response in patrol_rooms):
            evidence.add("checkpoint-response")

    degree = Counter(index for edge in level.edges for index in edge)
    for flanker in patrol_rooms - route_set:
        if (degree[flanker] >= 2
                and zones.get(flanker, -1) >= 0
                and "staggered-flank" in templates.get(flanker, ())
                and any(zones.get(direct, -1) == zones.get(flanker, -2)
                        and "visible-sentry" in templates.get(direct, ())
                        for direct in route)):
            evidence.add("crossfire-loop")

    actor_counts = {room: sum(len(encounter.cells) for encounter in encounters)
                    for room, encounters in by_room.items()}
    for front, rear in zip(route, route[1:]):
        if ("visible-sentry" in templates.get(front, ())
                and "strongpoint" in templates.get(rear, ())
                and actor_counts.get(front, 0) < actor_counts.get(rear, 0)
                and zones.get(front, -1) >= 0
                and zones.get(rear, -1) >= 0
                and zones.get(front, -1) != zones.get(rear, -1)):
            evidence.add("layered-breach")

    for room_index in route:
        room = level.rooms[room_index]
        if any(encounter.patrol_kind == "doorway-shuttle"
               and any(not (room.x <= x < room.x + room.w
                            and room.y <= y < room.y + room.h)
                       for x, y in encounter.patrol_path)
               for encounter in by_room.get(room_index, ())):
            evidence.add("patrol-intersection")
    return frozenset(evidence)


def _encounter_quality(level: GeneratedMap) -> float:
    """Measure template variety, tactical evidence and physical sequences.

    This intentionally does not reconstruct unrecorded lethality or line of
    sight. Reveal slots, patrol paths, templates, graph/route order and assigned
    sound zones are finished-map evidence and are the only dimensions averaged.
    """
    encounters = [encounter for encounter in level.encounters
                  if encounter.template not in ("novelty", "boss-support")]
    if not encounters:
        return 0.0
    tactics: set[str] = set()
    for encounter in encounters:
        actor_cells = {(x, y) for x, y, _ in encounter.cells}
        hidden = set(encounter.hidden_cells)
        if actor_cells - hidden:
            tactics.add("visible")
        if hidden:
            tactics.add("hidden")
        if encounter.patrol_path:
            tactics.add("moving")
        if encounter.template in {"strongpoint", "objective-guard",
                                  "guard-gallery"}:
            tactics.add("defended-position")
    grammars = _multi_room_grammars(level)
    return _mean((
        _normalized_diversity([encounter.template for encounter in encounters]),
        len(tactics) / 4.0,
        _bounded(len(grammars)),
        _bounded(len(grammars) / 3.0),
    ))


def _route_actor_counts(level: GeneratedMap,
                        route: list[int]) -> list[int]:
    """Count actual actors in each route room from the finished things plane."""
    counts = []
    valid_plane = len(level.things) == GRID * GRID
    for room_index in route:
        if not valid_plane or not 0 <= room_index < len(level.rooms):
            counts.append(0)
            continue
        room = level.rooms[room_index]
        counts.append(sum(
            _at(level.things, x, y) in ENEMY_CODES
            for y in range(room.y, room.y + room.h)
            for x in range(room.x, room.x + room.w)))
    return counts


def _intensity_class(actor_count: int) -> str:
    if actor_count == 0:
        return "calm"
    if actor_count <= 2:
        return "light"
    if actor_count <= 5:
        return "medium"
    return "heavy"


def _pacing_quality(level: GeneratedMap) -> float:
    """Measure contrast, medium-repeat avoidance, recovery and scale rhythm.

    Actor counts are the realized combat beats, not a reconstruction of the
    generator's depth/config inputs. Room areas add one independent physical
    pacing track. Recovery is included only when a peak exists; no score is
    invented for a sub-metric that the route cannot exhibit.
    """
    route = [index for index in level.critical_route
             if 0 <= index < len(level.rooms)]
    if len(route) < 2:
        return 0.0
    counts = _route_actor_counts(level, route)
    classes = [_intensity_class(count) for count in counts]
    transitions = sum(first != second
                      for first, second in zip(classes, classes[1:]))
    medium_repeats = sum(first == second == "medium"
                         for first, second in zip(classes, classes[1:]))
    scores = [
        transitions / (len(classes) - 1),
        1.0 - medium_repeats / (len(classes) - 1),
        ((max(counts) - min(counts)) / max(counts)
         if max(counts) else 0.0),
    ]

    peaks = [index for index, value in enumerate(counts[:-1])
             if value == max(counts) and value > 0]
    if peaks:
        scores.append(_mean(
            any(later < counts[index]
                for later in counts[index + 1:index + 3])
            for index in peaks))

    areas = [level.rooms[index].w * level.rooms[index].h for index in route]
    median_area = _median(areas)
    if median_area:
        area_classes = [
            "small" if area < median_area * 0.75 else
            "large" if area > median_area * 1.35 else "medium"
            for area in areas]
        scores.append(_sequence_legibility(area_classes))
    return _mean(scores)


def _secret_quality(level: GeneratedMap) -> float:
    count = max(len(level.secret_variants), len(level.secret_details))
    if not count:
        return 0.0
    variety = (len(set(level.secret_variants)) / len(level.secret_variants)
               if level.secret_variants else 0.0)
    rewarded = (_mean(detail.reward_count > 0 for detail in level.secret_details)
                if level.secret_details else 0.0)
    hinted = (_mean(bool(detail.hint_treatment) for detail in level.secret_details)
              if level.secret_details else 0.0)
    return _mean((_bounded(count / 3.0), variety, rewarded, hinted))


def _landmark_quality(level: GeneratedMap) -> float:
    if not level.rooms or level.landmarks is None:
        return 0.0
    primaries = [plan for plan in level.landmarks if plan.rank == "primary"]
    secondaries = [plan for plan in level.landmarks if plan.rank == "secondary"]
    hierarchy = 1.0 if len(primaries) == 1 else 0.0
    support = _bounded(len(secondaries) / 2.0)
    approaches = (_mean(plan.approach_room >= 0 for plan in level.landmarks)
                  if level.landmarks else 0.0)
    purposes = (len({plan.purpose for plan in level.landmarks}) / len(level.landmarks)
                if level.landmarks else 0.0)
    return _mean((hierarchy, support, approaches, purposes))


def quality_report(level: GeneratedMap, previous, config, *,
                   campaign_contrast: float = 0.0) -> QualityReport:
    """Describe one hard-valid candidate with bounded, deterministic metrics.

    ``previous`` and ``config`` remain in the public signature because campaign
    selection owns that context. This module deliberately does not inspect
    either. campaign.py computes its existing score and passes the normalized
    value in, which keeps the relative-import graph acyclic and keeps randomness
    and configuration state out of floor-quality measurement.
    """
    del previous, config
    valid_plane = len(level.tiles) == GRID * GRID
    topology = (_topology(level) if valid_plane else
                _Topology((), frozenset(), (), 0, (), 1,
                          frozenset(), frozenset(), 0))
    flags = (_critique(level, topology) if valid_plane
             else tuple(level.critique))
    live = tuple(flag for flag in flags if flag not in TRIPWIRE_FLAGS)
    severe = tuple(flag for flag in live if flag in SEVERE_FLAGS)
    experience = simulate_player_experience(level)
    encounter_evidence = _encounter_quality(level)
    pacing_evidence = _pacing_quality(level)
    return QualityReport(
        severe_defects=severe,
        diagnostics=live,
        spatial_composition=_bounded(_spatial_composition(level, topology)),
        route_quality=_bounded(_route_quality(level, topology)),
        navigational_legibility=_bounded(_navigational_legibility(level)),
        # Recorded encounter grammar remains the stronger signal; the walk
        # deepens it with finished-plane sight, movement and retreat evidence.
        encounter_quality=_bounded(
            (2.0 * encounter_evidence
             + experience.encounter_affordance) / 3.0),
        # Sequence rhythm likewise remains primary. The simulation contributes
        # resource strain and exposed recovery time without inventing a new
        # QualityReport dimension for the selector.
        pacing_quality=_bounded(
            (2.0 * pacing_evidence
             + experience.pacing_sustainability) / 3.0),
        secret_quality=_bounded(_secret_quality(level)),
        landmark_quality=_bounded(_landmark_quality(level)),
        corpus_similarity=_bounded(_corpus_similarity(level, topology)),
        campaign_contrast=_bounded(campaign_contrast),
    )


def weighted_distance(first: tuple[str, ...], second: tuple[str, ...]) -> float:
    """Jaccard distance over multiplicities rather than membership.

    campaign.py's contrast scoring used set symmetric difference, which cannot
    distinguish a floor with one armory from a floor with five: both have the same
    concept set and scored as identical. Weighting by count makes frequency
    visible while staying bounded in [0, 1] and returning 0.0 for equal inputs.
    """
    left, right = Counter(first), Counter(second)
    if not left and not right:
        return 0.0
    intersection = sum((left & right).values())
    union = sum((left | right).values())
    return 1.0 - intersection / union if union else 0.0
