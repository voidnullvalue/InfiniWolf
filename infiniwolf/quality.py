"""Soft quality critique: what is wrong with a map that is nonetheless valid.

Strictly separate from validation. validate_map decides whether a floor may ship
at all and raises; this returns a tuple of named flags describing weaknesses --
no loop, flat hierarchy, corridor-heavy, motif imbalance, repeated encounter
templates. A flag never rejects a candidate; it only makes that candidate rank
below a cleaner one when several are hard-valid.

Every metric here must be deterministic, bounded, explainable and unit-tested. The
point is diagnosis, not a score: a flag names a concrete defect a reader can go
look at, which is why they are strings rather than weights.

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
from itertools import combinations

from .grid import _at, _floor_components, _is_floor
from .model import GeneratedMap
from .wl6 import BOSSES, DOORS, ENEMY_CODES, GRID, _patrol_actor_direction


def _critique(level: GeneratedMap) -> tuple[str, ...]:
    components = _floor_components(level.tiles)
    owner = {cell: index for index, component in enumerate(components) for cell in component}
    graph_edges: set[tuple[int, int]] = set()
    for index, tile in enumerate(level.tiles):
        if tile not in DOORS:
            continue
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
    if longest > 21:
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

    return tuple(flags)


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
