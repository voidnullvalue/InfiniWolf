"""Abstract floor planning: the building program before any tile exists.

Produces the room graph -- how many rooms, what structural role and tier each
holds, which connect, which belong to the mandatory progression, which district
and motif owns each -- and nothing else. It never touches the tile or thing
planes, which is what makes a plan unit-testable without generating geometry and
what lets Stage 14's concept-first programs slot in here rather than being
retrofitted after rooms are already placed.

Room *sizing* deliberately lives with geometry, not here: a tier is a plan
decision, the dimensions realizing it are a placement decision that has to
negotiate with the map bounds.
"""

from __future__ import annotations

import random

from .campaign import (CIRCULATION_MODES, CIRCULATION_SKELETONS,
                       HALLWAY_FIRST_SKELETONS, PROGRESSION_GRAMMARS)
from .model import FloorPlan, FloorVariant, RoomSpec, SetPiecePlan
from .vignettes import VIGNETTE_REQUEST_FAMILIES

_PRIMARY_SET_PIECE_FAMILIES = (
    ("checkpoint-administration",
     ("checkpoint", "administrative-office", "records-office"),
     ((0, 1), (1, 2)), "checkpoint", "records-office"),
    ("command-and-control",
     ("security-desk", "communications", "war-room"),
     ((0, 1), (1, 2)), "security-desk", "war-room"),
    ("barracks-support",
     ("checkpoint", "barracks", "mess-hall", "armory"),
     ((0, 1), (1, 2), (2, 3)), "checkpoint", "armory"),
    ("storage-machinery-route",
     ("receiving", "bulk-storage", "machinery-control", "dispatch"),
     ((0, 1), (1, 2), (2, 3)), "receiving", "dispatch"),
    ("prison-processing",
     ("processing-desk", "guardroom", "cell-block", "cell-block", "exercise-yard"),
     ((0, 1), (1, 2), (2, 3), (3, 4)), "processing-desk", "exercise-yard"),
    ("administrative-wing",
     ("checkpoint", "clerks-office", "records-office", "briefing-room", "command-office"),
     ((0, 1), (1, 2), (2, 3), (3, 4)), "checkpoint", "command-office"),
)

_SECONDARY_SOLOS = (
    ("wayfinding-checkpoint", ("checkpoint",), (), "checkpoint", "checkpoint"),
    ("supply-depot", ("supply-cache",), (), "supply-cache", "supply-cache"),
    ("memorial-bay", ("memorial",), (), "memorial", "memorial"),
    ("records-annex", ("records-office",), (), "records-office", "records-office"),
)


def _set_piece_tag(family: str, room_role: str) -> str:
    return f"setpiece:{family}:{room_role}"


def _program_tier(room_role: str) -> str:
    if room_role in {
            "armory", "bulk-storage", "cell-block", "parts-store",
            "records-office", "supply-cache"}:
        return "closet"
    if room_role in {"briefing-room", "machinery-control", "mess-hall"}:
        return "hall"
    return "standard"


def _reserve_set_pieces(rng: random.Random, specs: list[RoomSpec],
                        edges: list[tuple[int, int]], middle_beats: list[int],
                        complexity: int, target: int,
                        reserved_after: int) -> tuple[SetPiecePlan, ...]:
    """Allocate authored programs before topology motifs and generic rooms."""
    capacity = target - len(specs) - reserved_after - 1  # one loop room
    if capacity < 2:
        raise ValueError("ordinary floor lacks room budget for a primary set piece")

    secondary_count = 2 if complexity >= 3 else 1
    secondary_additions = min(secondary_count, max(0, capacity - 2))
    primary_additions = min(4, capacity - secondary_additions)
    primary_size = primary_additions + 1
    primary_pool = [item for item in _PRIMARY_SET_PIECE_FAMILIES
                    if len(item[1]) == primary_size]
    primary = rng.choice(primary_pool)

    primary_windows = [tuple(middle_beats[start:start + 3])
                       for start in range(len(middle_beats) - 2)]
    if not primary_windows:
        raise ValueError("ordinary floor lacks a three-room primary core")
    primary_hosts = rng.choice(primary_windows)
    secondary_hosts = [index for index in middle_beats
                       if index not in primary_hosts]
    rng.shuffle(secondary_hosts)
    secondary_hosts.sort(key=lambda index: specs[index].tier == "corridor")
    plans: list[SetPiecePlan] = []

    def allocate(template, scale: str,
                 existing_hosts: tuple[int, ...]) -> SetPiecePlan:
        family, room_roles, local_edges, entry_role, exit_role = template
        mapped = list(existing_hosts)
        for host, room_role in zip(mapped, room_roles):
            host_spec = specs[host]
            specs[host] = RoomSpec(host_spec.role, host_spec.tier,
                                   host_spec.district,
                                   _set_piece_tag(family, room_role))
        if mapped:
            district = specs[mapped[0]].district
        else:
            district = specs[middle_beats[0]].district
        for room_role in room_roles[len(mapped):]:
            node = len(specs)
            specs.append(RoomSpec("branch", _program_tier(room_role),
                                  district,
                                  _set_piece_tag(family, room_role)))
            mapped.append(node)
        required = tuple((mapped[first], mapped[second])
                         for first, second in local_edges)
        known_edges = {frozenset(edge) for edge in edges}
        for edge in required:
            if frozenset(edge) not in known_edges:
                edges.append(edge)
        return SetPiecePlan(family, scale, tuple(mapped), tuple(room_roles),
                            required, entry_role, exit_role)

    plans.append(allocate(primary, "primary", primary_hosts))
    used_families = {primary[0]}
    for index in range(secondary_count):
        if index < secondary_additions:
            pool = [item for item in VIGNETTE_REQUEST_FAMILIES
                    if item[0] not in used_families]
        else:
            pool = [item for item in _SECONDARY_SOLOS
                    if item[0] not in used_families]
        template = rng.choice(pool)
        host = secondary_hosts.pop()
        plans.append(allocate(template, "secondary",
                              (host,)))
        used_families.add(template[0])
    return tuple(plans)


def _plan_floor(rng: random.Random, complexity: int, number: int,
                variant: FloorVariant | None = None,
                skeleton: str | None = None,
                progression_grammar: str | None = None,
                rare_motif: bool = False,
                boss_ends_floor: bool = False) -> FloorPlan:
    variant = variant or FloorVariant("default")
    skeleton = skeleton or rng.choice(CIRCULATION_SKELETONS)
    if skeleton not in CIRCULATION_SKELETONS:
        raise ValueError("unknown circulation skeleton")
    progression_grammar = progression_grammar or rng.choice(PROGRESSION_GRAMMARS)
    if progression_grammar not in PROGRESSION_GRAMMARS:
        raise ValueError("unknown progression grammar")
    special_family = "standard"
    if number == 9:
        special_family = rng.choice((
            "throne-stronghold", "command-bunker", "laboratory-gauntlet",
            "columned-fortress", "central-duel"))
    elif number == 10:
        special_family = rng.choice((
            "central-vault", "museum-circuit", "nested-reliquary",
            "abandoned-armory", "treasure-palace"))
    # Density comes from additional destinations, not inflated room boxes.
    # The two highest settings now reach beyond Normal's twenty-room plan;
    # local filler recovery below raises realized density at every setting.
    target = min(24, 14 + 2 * complexity)
    if number == 10:
        # The reward expedition uses larger room footprints and consequently
        # loses more optional placements. Give it four additional destinations
        # to realize instead of making its already-large rooms even bigger.
        target = min(24, target + 4)
    # Roughly 55% of authored rooms belong to the mandatory progression
    # spine. Optional motifs and side rooms still provide exploration, but
    # the elevator can no longer sit only five rooms away in a twenty-room
    # floor. Floor 10's enlarged rooms need one fewer critical placement to
    # remain reliable inside the native 64x64 grid.
    spine_count = min(9 if number == 10 else 11,
                      max(7, round(target * 0.55)))
    beat_count = spine_count - 4
    tiers = ["standard"] + [("hall" if rng.random() < variant.hall_chance else "standard")
                            for _ in range(beat_count)]
    tiers += ["anchor", rng.choice(("closet", "standard")), "standard"]
    roles = ["start"] + ["beat"] * beat_count + ["climax", "relief", "exit"]
    if number == 9 and boss_ends_floor:
        # A boss who calls A_BossDeath ends the campaign where he falls, so this
        # floor has no elevator to walk to and nothing to put after the arena:
        # the spine runs out at the anchor. Two rooms that would have been the
        # victory transition and the lift room stay in the program as ordinary
        # beats before the staging pause, which keeps the room count, the spine
        # count and every draw above identical to the elevator plan.
        roles[-4:] = ["beat", "beat", "staging", "boss-arena"]
        tiers[-3] = "standard"
        tiers[-2] = "standard"
        tiers[-1] = "anchor"
    elif number == 9:
        # The final four beats are an explicit dramatic sequence. The arena
        # remains the unique anchor; the room before it is a readable pause,
        # and the room after it is a victory transition to the elevator.
        roles[-4:] = ["staging", "boss-arena", "victory", "exit"]
        tiers[-4] = "standard"
    elif number == 10:
        roles[0] = "arrival"
        roles[-3:] = ["premium-vault", "recovery", "exit"]

    # Convert selected spine beats into real narrow circulation spaces. The
    # floor skeleton controls their rhythm while later district modes decide
    # whether surrounding rooms form offices, suites, bays, or chambers.
    beat_indices = list(range(1, beat_count + 1))
    fractions = {
        "axial-journey": (0.32, 0.70),
        "hub-relay": (0.22, 0.50, 0.78),
        "offset-ladder": (0.27, 0.55, 0.82),
        "clustered-chain": (0.38, 0.68),
        "nested-circuit": (0.20, 0.48, 0.76),
        "bounded-perimeter": (0.18, 0.50, 0.82),
    }[progression_grammar]
    desired = 3 if beat_count >= 6 and len(fractions) >= 3 else 2
    corridor_indices: list[int] = []
    if skeleton in HALLWAY_FIRST_SKELETONS:
        middle = beat_indices[len(beat_indices) // 2]
        authored = {
            "central-axis": (middle - 1, middle, middle + 1),
            "plus-concourse": (middle - 1, middle, middle + 1),
            "t-concourse": (middle - 1, middle, middle + 1),
            "offset-boulevard": (middle - 1, middle, middle + 1),
        }[skeleton]
        corridor_indices.extend(index for index in authored
                                if index in beat_indices)
    for fraction in fractions:
        index = beat_indices[min(len(beat_indices) - 1,
                                 round((len(beat_indices) - 1) * fraction))]
        if index not in corridor_indices:
            corridor_indices.append(index)
        if len(corridor_indices) >= desired:
            break
    while len(corridor_indices) < desired:
        remaining = [index for index in beat_indices if index not in corridor_indices]
        index = max(remaining, key=lambda candidate: (
            min((abs(candidate - placed) for placed in corridor_indices),
                default=len(beat_indices)),
            -abs(candidate - beat_indices[len(beat_indices) // 2])))
        corridor_indices.append(index)
    for index in corridor_indices:
        roles[index] = "circulation"
        tiers[index] = "corridor"
    # Formal long rooms are destinations, not extra corridor segments. Keep
    # them away from circulation nodes and from one another so the spine does
    # not collapse into a visually repetitive run of the same concept.
    previous_hall = False
    for index in beat_indices:
        if tiers[index] != "hall":
            previous_hall = False
            continue
        beside_corridor = any(0 <= neighbor < len(tiers)
                              and tiers[neighbor] == "corridor"
                              for neighbor in (index - 1, index + 1))
        if beside_corridor or previous_hall:
            tiers[index] = "standard"
            previous_hall = False
        else:
            previous_hall = True

    district_count = 3 if spine_count >= 7 and rng.random() < 0.6 else 2
    cuts = sorted(rng.sample(range(2, spine_count - 1), district_count - 1))
    districts = [sum(index >= cut for cut in cuts) for index in range(spine_count)]
    mode_preferences = {
        "garrison": ("double-loaded", "formal-axis", "single-loaded"),
        "catacombs": ("tunnel-cluster", "suite", "single-loaded"),
        "grand-halls": ("formal-axis", "single-loaded", "suite"),
        "storehouse": ("service-bays", "double-loaded", "single-loaded"),
        "quarters": ("single-loaded", "suite", "double-loaded"),
        "stronghold": ("formal-axis", "double-loaded", "service-bays"),
        "vault": ("formal-axis", "suite", "service-bays"),
        "default": ("double-loaded", "single-loaded", "suite"),
    }[variant.name if variant.name in {
        "garrison", "catacombs", "grand-halls", "storehouse", "quarters",
        "stronghold", "vault"} else "default"]
    district_circulation: list[str] = []
    for _ in range(district_count):
        pool = [mode for mode in CIRCULATION_MODES
                if not district_circulation or mode != district_circulation[-1]]
        weights = [3 if mode in mode_preferences else 1 for mode in pool]
        district_circulation.append(rng.choices(pool, weights=weights, k=1)[0])
    specs = [RoomSpec(role, tier, district)
             for role, tier, district in zip(roles, tiers, districts)]
    edges = [(index, index + 1) for index in range(spine_count - 1)]
    loops: list[tuple[int, int]] = []
    critical = set(range(spine_count))
    groups: list[tuple[int, ...]] = []
    arm_count = {
        "plus-concourse": 2,
        "t-concourse": 1,
        "offset-boulevard": 1,
    }.get(skeleton, 0)
    reserved_arm_rooms = arm_count * 2
    middle_beats = list(range(1, 1 + beat_count))
    set_pieces: tuple[SetPiecePlan, ...] = ()
    if number not in (9, 10):
        set_pieces = _reserve_set_pieces(
            rng, specs, edges, middle_beats, complexity, target,
            reserved_arm_rooms)
    budget = min(3, 1 + (complexity >= 3) + (rng.random() < variant.extra_motif_chance))
    if number not in (9, 10):
        budget = 1
    if skeleton in HALLWAY_FIRST_SKELETONS:
        # The hallway scaffold and its occupied arms spend the macro-layout
        # budget on this floor; stacking extra hub/wing/gallery families over
        # it would blur the form and exceed the configured room target.
        budget = 1
    if number == 10:
        budget = max(2, budget)
    loop_realizations = {
        "axial-journey": ("asymmetric-detour", "short-room-loop"),
        "hub-relay": ("courtyard-circuit", "short-room-loop"),
        "offset-ladder": ("ladder-rung", "service-loop"),
        "clustered-chain": ("service-loop", "asymmetric-detour"),
        "nested-circuit": ("nested-room-loop", "courtyard-circuit"),
        "bounded-perimeter": ("bounded-perimeter",),
    }[progression_grammar]
    primary_loop = rng.choice(loop_realizations)
    primary_motif = ("courtyard" if "courtyard" in primary_loop else
                     "service" if "service" in primary_loop else
                     "ladder" if "ladder" in primary_loop else "ring")
    motifs = [primary_motif]
    motif_realizations = [primary_loop]
    remaining = ["hub", "wings", "gallery", "courtyard", "service"]
    rng.shuffle(remaining)
    family_preferences = {
        "throne-stronghold": ("hub", "wings"),
        "command-bunker": ("wings", "hub"),
        "laboratory-gauntlet": ("gallery", "wings"),
        "columned-fortress": ("hub", "gallery"),
        "central-duel": ("wings", "gallery"),
        "central-vault": ("hub", "wings"),
        "museum-circuit": ("gallery", "hub"),
        "nested-reliquary": ("gallery", "wings"),
        "abandoned-armory": ("wings", "gallery"),
        "treasure-palace": ("hub", "gallery"),
    }.get(special_family, ())
    for preferred in reversed(family_preferences):
        if preferred in remaining:
            remaining.remove(preferred)
            remaining.insert(0, preferred)
    for preferred in reversed(variant.motif_pref):
        if preferred in remaining:
            remaining.remove(preferred)
            remaining.insert(0, preferred)
    motifs += remaining[:budget - 1]
    motif_realizations += [f"{motif}-{rng.choice(('compact', 'offset', 'staggered'))}"
                           for motif in motifs[1:]]

    # The first motif always spends topology budget on a real reconvergence.
    pairs = [(i, j) for i in range(1, spine_count - 2)
             for j in range(i + 2, min(i + 4, spine_count - 1))]
    weights = [spine_count - abs((i + j) - (spine_count - 1)) for i, j in pairs]
    left, right = rng.choices(pairs, weights=weights, k=1)[0]
    parent = left
    # The reconvergence may add variety but must never be a faster route to
    # the elevator than the spine segment it parallels.
    ring_rooms = max(right - left - 1, rng.randrange(1, 3))
    if primary_loop in ("courtyard-circuit", "nested-room-loop", "bounded-perimeter"):
        ring_rooms = min(3, ring_rooms + 1)
    if number not in (9, 10):
        ring_rooms = min(ring_rooms,
                         target - len(specs) - reserved_arm_rooms)
    for _ in range(ring_rooms):
        node = len(specs)
        tier = "hall" if primary_loop == "courtyard-circuit" else "standard"
        specs.append(RoomSpec("ring", tier, districts[left], primary_motif))
        edges.append((parent, node))
        parent = node
    edges.append((parent, right)); loops.append((parent, right))

    if "hub" in motifs:
        if number in (9, 10):
            # Special floors own a fixed dramatic anchor on the mandatory
            # route. A hub family grows optional wings from that anchor
            # without moving the boss arena or premium vault earlier.
            hub = next(index for index, spec in enumerate(specs)
                       if spec.tier == "anchor")
        else:
            hub = rng.choice([index for index in middle_beats
                              if specs[index].tier != "corridor"])
            specs[hub] = RoomSpec("hub", "anchor", districts[hub], "hub")
            climax = roles.index("climax")
            specs[climax] = RoomSpec("climax", "standard", districts[climax])
        for role in ["branch", "branch"] + ["closet"] * rng.randrange(1, 3):
            node = len(specs)
            specs.append(RoomSpec(role, "closet" if role == "closet" else "standard",
                                  districts[hub], "hub"))
            edges.append((hub, node))
    if "wings" in motifs:
        parent = rng.choice(middle_beats)
        wings = tuple(range(len(specs), len(specs) + 2))
        for node in wings:
            specs.append(RoomSpec("branch", "standard", districts[parent], "wings"))
            edges.append((parent, node))
        groups.append(wings)
    if "gallery" in motifs:
        parent = rng.choice(middle_beats)
        district = districts[parent]
        gallery = []
        for _ in range(rng.randrange(2, 4)):
            node = len(specs)
            specs.append(RoomSpec("closet", "closet", district, "gallery"))
            edges.append((parent, node)); gallery.append(node)
            parent = node
        groups.append(tuple(gallery))

    if "courtyard" in motifs and primary_motif != "courtyard":
        parent = rng.choice(middle_beats)
        node = len(specs)
        specs.append(RoomSpec("branch", "hall", districts[parent], "courtyard"))
        edges.append((parent, node))
    if "service" in motifs and primary_motif != "service":
        left = rng.choice(middle_beats[:-2])
        right = min(middle_beats[-1], left + rng.randrange(2, 4))
        node = len(specs)
        specs.append(RoomSpec("branch", "corridor", districts[left], "service"))
        edges.extend(((left, node), (node, right)))
        loops.append((node, right))

    if (rare_motif and number in (6, 7, 8, 9)
            and len(specs) + 1 + reserved_arm_rooms <= target):
        parent = rng.choice(middle_beats)
        node = len(specs)
        specs.append(RoomSpec("branch", "motif", districts[parent], "swastika"))
        edges.append((parent, node))
        motifs.append("swastika")
        motif_realizations.append("swastika-room-profile")

    # Reserve ordinary room budget for occupied concourse arms. Each corridor
    # arm is immediately followed by a destination room, so the strong form
    # is present in realized geometry rather than only in skeleton metadata.
    hall_parent = corridor_indices[len(corridor_indices) // 2]
    for _ in range(arm_count):
        branch = len(specs)
        district = specs[hall_parent].district
        specs.append(RoomSpec("branch", "corridor", district,
                              "hallway-arm"))
        edges.append((hall_parent, branch))
        destination = len(specs)
        specs.append(RoomSpec("branch", "standard", district,
                              "hallway-destination"))
        edges.append((branch, destination))
        critical.update((branch, destination))

    filler_tips: list[int] = []
    # Only the budget left after authored programs becomes generic utility space.
    generic_count = target - len(specs)
    for _ in range(generic_count):
        suite_tips = [tip for tip in filler_tips
                      if district_circulation[specs[tip].district]
                      in ("suite", "tunnel-cluster")]
        if suite_tips and rng.random() < 0.35:
            parent = rng.choice(suite_tips)
            filler_tips.remove(parent)
        else:
            degrees = [sum(index in edge for edge in edges) for index in middle_beats]
            weights = []
            for index, degree in zip(middle_beats, degrees):
                mode = district_circulation[specs[index].district]
                corridor_bias = (3.0 if specs[index].tier == "corridor"
                                 and mode not in ("suite", "tunnel-cluster")
                                 else 1.6 if specs[index].tier == "corridor" else 1.0)
                weights.append(corridor_bias / degree)
            parent = rng.choices(middle_beats, weights=weights, k=1)[0]
        role = "closet" if rng.random() < variant.closet_weight else "branch"
        tier = "closet" if role == "closet" else rng.choice(("standard", "standard", "hall"))
        node = len(specs)
        specs.append(RoomSpec(role, tier, specs[parent].district, "filler"))
        edges.append((parent, node))
        filler_tips.append(node)

    if sum(spec.tier == "anchor" for spec in specs) != 1:
        raise ValueError("floor plan must have exactly one anchor")
    return FloorPlan(specs, edges, loops, tuple(motifs), frozenset(critical), tuple(groups),
                     skeleton, tuple(district_circulation), special_family,
                     progression_grammar, tuple(motif_realizations), set_pieces)
