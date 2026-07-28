"""Campaign-scale composition: what varies across a ten-map run.

Decisions here apply to the campaign, not to one map attempt. Two consequences
follow and both matter. First, every schedule derives from a stream that excludes
`attempt`, so a floor rejected by validate_map is re-generated without its
variant, skeleton, grammar or lock quota moving underneath it. Second, nothing in
this module may depend on a generated map, which is what keeps it importable by
both the generator and the validator.

Also owns candidate comparison: given several hard-valid maps, which one best
contrasts with the floors already accepted. That is ranking policy, deliberately
separate from validate_map -- a soft score may order valid candidates and may
never rescue an invalid one.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from collections import Counter
from itertools import combinations
import math
import random

from .config import CampaignConfig
from .wl6 import BOSSES, KEY_DROP_BOSSES
from .model import AestheticPhase, FloorVariant, GatePlan, GeneratedMap
from .quality import weighted_distance
from .semantics import CONCEPT_AFFINITIES


# A floor's "base variant": one named bundle of the parameters that used to
# be hard-coded module constants, so consecutive floors read as different
# places (a cramped catacomb, a stately hall) instead of re-rolls of one
# recipe. Every default equals the previous constant, so a default-valued
# variant reproduces the pre-variant generator's behavior knob-for-knob.
FLOOR_VARIANT_ROTATION = (
    # Tidy military bunker: hard materials, sparse cells, guard fittings.
    FloorVariant("garrison", pillar_chance=0.10, jail_probability=0.15,
                 theme_pool=(1, 15, 17, 35, 48),
                 decor_overrides=(("barracks", "guardpost"),)),
    # Cramped dungeon: bitten-into rooms, narrow halls, cellblocks, gore.
    FloorVariant("catacombs", notch_chance=0.32,
                 pillar_chance=0.14, widen_chance=0.55, jail_probability=0.6,
                 theme_pool=(8, 1, 24, 29, 44), decor_density=0.7,
                 decor_overrides=(("lounge", "barracks"),)),
    # Stately galleries: long halls, colonnades, wood and insignia panels.
    FloorVariant("grand-halls", hall_chance=0.4, extra_motif_chance=0.6,
                 motif_pref=("gallery",), pillar_chance=0.15, widen_chance=1.0,
                 theme_pool=(12, 40, 42, 48, 19), decor_density=1.25,
                 decor_overrides=(("barracks", "lounge"),)),
    # Supply depot: closet-heavy plan, loading niches, barrels everywhere.
    FloorVariant("storehouse", closet_weight=0.65,
                 pillar_chance=0.08, jail_probability=0.0,
                 theme_pool=(17, 15, 12, 35, 44, 48),
                 decor_density=1.15,
                 decor_overrides=(("barracks", "storage"), ("lounge", "storage"))),
    # Officers' quarters: smooth walls, wide halls, lived-in furniture.
    FloorVariant("quarters", notch_chance=0.14, widen_chance=0.9,
                 pillar_chance=0.08,
                 theme_pool=(12, 40, 1, 35, 48), decor_density=1.1,
                 decor_overrides=(("guardpost", "lounge"),)),
)
# Floors 9 and 10 keep their purpose-built inline treatments (boss arena,
# treasure vault); the forced variants exist so every floor has a named
# identity in the manifest and the decoration hooks apply uniformly.
VARIANT_STRONGHOLD = FloorVariant(
    "stronghold", theme_pool=(1, 15, 17, 19, 29, 35, 44))
VARIANT_VAULT = FloorVariant(
    "vault", theme_pool=(12, 19, 40, 42, 44, 48))

# Floor-wide circulation and district-scale organization are separate choices.
# Themes weight these vocabularies but never own one fixed topology, avoiding
# a recognizable "one garrison plan, one catacomb plan" generator fingerprint.
CIRCULATION_SKELETONS = (
    "bent-spine", "parallel-cross", "central-wings",
    "forked", "perimeter-loop", "staggered-grid",
    "central-axis", "plus-concourse", "t-concourse", "offset-boulevard",
)
HALLWAY_FIRST_SKELETONS = frozenset({
    "central-axis", "plus-concourse", "t-concourse", "offset-boulevard",
})
CIRCULATION_MODES = (
    "double-loaded", "single-loaded", "suite",
    "service-bays", "formal-axis", "tunnel-cluster",
)

PROGRESSION_GRAMMARS = (
    "axial-journey", "hub-relay", "offset-ladder",
    "clustered-chain", "nested-circuit", "bounded-perimeter",
)

RARE_MOTIF_CHANCE = 0.06

def _variant_sequence(config: CampaignConfig) -> tuple[FloorVariant, ...]:
    """The campaign's per-floor variants, a pure function of the seed.

    Each pick draws from its own variant_seed and excludes the previous
    floor's pick, so consecutive floors always differ and floor N's variant
    is derivable without generating floors 1..N-1. Floors 9/10 are the
    forced boss/vault identities."""
    picks: list[FloorVariant] = []
    for floor in range(1, 9):
        seed = (config.variant_aardwolf_seed(floor) if config.say_aardwolf
                else config.variant_seed(floor))
        rng = random.Random(seed)
        pool = [variant for variant in FLOOR_VARIANT_ROTATION
                if not picks or variant.name != picks[-1].name]
        if config.say_aardwolf and len(picks) > 1:
            distant = [variant for variant in pool
                       if variant.name != picks[-2].name]
            if distant:
                pool = distant
        bias = config.theme_bias.value
        if bias == "mixed":
            weights = ([1.0 + rng.random() * 2.5 for _ in pool]
                       if config.say_aardwolf else None)
            picks.append(rng.choices(pool, weights=weights, k=1)[0]
                         if weights else rng.choice(pool))
        else:
            weights = [(3 if variant.name == bias else 1)
                       * (1.0 + rng.random() * 1.5
                          if config.say_aardwolf else 1.0)
                       for variant in pool]
            picks.append(rng.choices(pool, weights=weights, k=1)[0])
    return tuple(picks) + (VARIANT_STRONGHOLD, VARIANT_VAULT)


def _aardwolf_variant(config: CampaignConfig, floor: int,
                      variant: FloorVariant) -> FloorVariant:
    if not config.say_aardwolf:
        return variant
    rng = random.Random(config.aardwolf_seed(floor))
    phase_rng = random.Random(config.aardwolf_seed(10)
                              ^ config.circulation_seed(10))
    phase = phase_rng.random() * math.tau
    pulse = math.sin(phase + floor * (math.tau / 3.7))
    order = list(range(8))
    rng.shuffle(order)
    amplitudes = [0.15] * 8
    for index in order[:2]:
        amplitudes[index] = 1.0
    for index in order[2:5]:
        amplitudes[index] = 0.45
    blend = lambda index, value: 1.0 + (value - 1.0) * amplitudes[index]
    material = list(variant.theme_pool)
    if len(material) > 3 and amplitudes[6] >= 0.45:
        shift = rng.randrange(len(material))
        material = material[shift:] + material[:shift]
        material = material[:rng.randrange(3, min(5, len(material)) + 1)]
    motifs = list(("hub", "wings", "gallery"))
    rng.shuffle(motifs)
    echo = ("hub", "wings", "gallery")[phase_rng.randrange(3)]
    if floor in (1, 4, 7, 9):
        motifs.remove(echo)
        motifs.insert(0, echo)
    scale = lambda low, high, value: min(high, max(low, value))
    return replace(
        variant,
        notch_chance=scale(0.07, 0.38, variant.notch_chance
                           * blend(0, 0.72 + rng.random() * 0.62
                                   + pulse * 0.08)),
        pillar_chance=scale(0.04, 0.18, variant.pillar_chance
                            * blend(1, 0.65 + rng.random() * 0.80
                                    - pulse * 0.08)),
        widen_chance=scale(0.48, 1.0, variant.widen_chance
                           * blend(2, 0.72 + rng.random() * 0.55
                                   - pulse * 0.08)),
        hall_chance=scale(0.12, 0.48, variant.hall_chance
                          * blend(3, 0.62 + rng.random() * 0.95
                                  + pulse * 0.10)),
        closet_weight=scale(0.28, 0.72, variant.closet_weight
                            * blend(4, 0.68 + rng.random() * 0.72
                                    - pulse * 0.08)),
        extra_motif_chance=scale(0.18, 0.72, variant.extra_motif_chance
                                 * blend(5, 0.58 + rng.random() * 1.05)),
        motif_pref=(tuple(motifs[:2]) if amplitudes[7] >= 0.45
                    else variant.motif_pref),
        theme_pool=tuple(material),
        jail_probability=(0.0 if variant.jail_probability == 0 else
                          scale(0.08, 0.65, variant.jail_probability
                                * blend(6, 0.55 + rng.random() * 1.05))),
        decor_density=scale(0.72, 1.30, variant.decor_density
                            * blend(7, 0.72 + rng.random() * 0.58
                                    + pulse * 0.10)),
    )


def _circulation_sequence(config: CampaignConfig) -> tuple[str, ...]:
    """Choose varied skeletons; themes are preferences, never mandates."""
    variants = _variant_sequence(config)
    preferences = {
        "garrison": ("central-wings", "parallel-cross", "bent-spine"),
        "catacombs": ("bent-spine", "forked", "perimeter-loop"),
        "grand-halls": ("central-wings", "parallel-cross", "perimeter-loop"),
        "storehouse": ("parallel-cross", "staggered-grid", "central-wings"),
        "quarters": ("bent-spine", "staggered-grid", "parallel-cross"),
        "stronghold": ("central-wings", "forked", "parallel-cross"),
        "vault": ("perimeter-loop", "central-wings", "staggered-grid"),
    }
    # Three of the eight ordinary floors receive a hallway-first scaffold.
    # Keeping floors 9/10 outside this schedule preserves their authored boss
    # and reward-expedition identities while making the new family exactly
    # thirty percent of a ten-map campaign.
    schedule_rng = random.Random(config.hallway_schedule_seed())
    rare_floor = _rare_motif_schedule(config)
    hallway_candidates = [floor for floor in range(1, 9)
                          if floor != rare_floor]
    hallway_floors = frozenset(schedule_rng.sample(hallway_candidates, 3))
    result: list[str] = []
    for floor, variant in enumerate(variants, 1):
        seed = (config.circulation_aardwolf_seed(floor) if config.say_aardwolf
                else config.circulation_seed(floor))
        rng = random.Random(seed)
        family = (HALLWAY_FIRST_SKELETONS if floor in hallway_floors
                  else set(CIRCULATION_SKELETONS) - HALLWAY_FIRST_SKELETONS)
        pool = [name for name in CIRCULATION_SKELETONS
                if name in family and (not result or name != result[-1])]
        if config.say_aardwolf and len(result) > 1:
            distant = [name for name in pool if name != result[-2]]
            if distant:
                pool = distant
        favored = preferences[variant.name]
        weights = [(3 if name in favored else 1)
                   * (1.0 + rng.random() * 2.0
                      if config.say_aardwolf else 1.0)
                   for name in pool]
        result.append(rng.choices(pool, weights=weights, k=1)[0])
    return tuple(result)


def _progression_sequence(config: CampaignConfig) -> tuple[str, ...]:
    """Choose macro progression grammars independently of floor retries."""
    result: list[str] = []
    for floor in range(1, 11):
        seed = (config.progression_aardwolf_seed(floor) if config.say_aardwolf
                else config.progression_schedule_seed(floor))
        rng = random.Random(seed)
        pool = [grammar for grammar in PROGRESSION_GRAMMARS
                if not result or grammar != result[-1]]
        if len(result) > 1:
            distant = [grammar for grammar in pool if grammar != result[-2]]
            if distant:
                pool = distant
        result.append(rng.choice(pool))
    return tuple(result)


def _rare_motif_schedule(config: CampaignConfig) -> int:
    """Return the nominated late floor for the rare plan motif, or zero."""
    rng = random.Random(config.rare_motif_seed())
    return rng.choice((6, 7, 8, 9)) if rng.random() < RARE_MOTIF_CHANCE else 0


def _lock_schedule(config: CampaignConfig) -> tuple[GatePlan, ...]:
    """Build a seeded campaign quota, weighted toward later floors.

    Floors 1--8 share a deliberate mixture of unlocked, single-key and
    dual-key maps. The seed chooses the exact quota and placement without
    permitting three gated floors in a row. Floor 9 always retains its gold
    boss-elevator gate and may add a silver pre-boss stage; floor 10 is open.
    """
    rng = random.Random(config.lock_seed())
    intensity = int(config.locked_doors)
    gated_ranges = {1: (0, 1), 2: (1, 2), 3: (3, 4),
                    4: (4, 5), 5: (5, 6)}
    dual_ranges = {1: (0, 0), 2: (0, 1), 3: (1, 2),
                   4: (2, 3), 5: (3, 4)}
    gated_count = rng.randint(*gated_ranges[intensity])
    dual_count = min(gated_count, rng.randint(*dual_ranges[intensity]))

    floor_sets = [choice for choice in combinations(range(1, 9), gated_count)
                  if not any(set(range(start, start + 3)) <= set(choice)
                             for start in range(1, 7))]
    if gated_count:
        weights = [math.prod(1.0 + floor * floor / 8.0 for floor in choice)
                   for choice in floor_sets]
        gated = set(rng.choices(floor_sets, weights=weights, k=1)[0])
    else:
        gated = set()
    if dual_count:
        choices = list(combinations(sorted(gated), dual_count))
        weights = [math.prod(floor for floor in choice) for choice in choices]
        dual = set(rng.choices(choices, weights=weights, k=1)[0])
    else:
        dual = set()

    plans = [GatePlan() for _ in range(10)]
    single_counts = {"gold": 0, "silver": 0}
    for floor in sorted(gated):
        if floor in dual:
            colors = (("gold", "silver") if rng.randrange(2)
                      else ("silver", "gold"))
        else:
            least = min(single_counts.values())
            available = [color for color, count in single_counts.items() if count == least]
            color = rng.choice(available)
            single_counts[color] += 1
            colors = (color,)
        plans[floor - 1] = GatePlan(colors)

    # Floor 9's gold gate is now conditional on the boss. The arena gates the exit
    # by position, so a boss who drops no key needs no lock -- and locking the
    # elevator anyway would strand the player, since nothing else on the floor
    # provides gold. Hans and Gretel keep the lock: their drop makes the kill
    # mandatory rather than merely the crossing.
    silver_boss_chance = (0.0, 0.0, 0.10, 0.25, 0.50, 0.70)[intensity]
    wants_silver = rng.random() < silver_boss_chance
    if choose_boss(config) in KEY_DROP_BOSSES:
        plans[8] = GatePlan(("silver", "gold") if wants_silver else ("gold",))
    else:
        plans[8] = GatePlan(("silver",) if wants_silver else ())
    plans[9] = GatePlan()
    return tuple(plans)

# Floor 9's boss. All six native WL6 bosses are eligible because the arena gates
# the exit topologically -- everything past it is reachable only through it -- so
# the exit no longer depends on a boss that drops a gold key. Hans and Gretel keep
# their key drop as an additional gate when they are chosen.
# wl6.BOSSES is already the curated native roster: it excludes FakeHitler, which
# neither drops a key nor calls A_BossDeath, and the Spear of Destiny bosses whose
# sprites live in SOD's VSWAP rather than wl6's.
BOSS_ROSTER = BOSSES


# The campaign's visual journey, as multipliers per floor. Not a fixed sequence: the
# seed shifts where the campaign sits in the curve, so two runs escalate differently
# while both escalating. Bands are narrow on purpose -- 0.75 to 1.30 -- because the
# arc must modulate a floor's variant, never override it.
def aesthetic_phase(config: CampaignConfig, floor: int) -> AestheticPhase:
    """Derive one floor's bounded visual modifiers.

    Floors 9 and 10 are pinned rather than interpolated: the stronghold is the
    campaign's most monumental and most occupied space by authorial intent, and the
    reward expedition is its most abandoned. Letting the curve decide would
    occasionally hand floor 9 a damp ruin.
    """
    if floor == 9:
        return AestheticPhase(orderliness=1.10, damage=0.85, occupation=1.30,
                              monumentality=1.30, abandonment=0.80)
    if floor == 10:
        return AestheticPhase(orderliness=0.85, damage=1.20, occupation=0.75,
                              monumentality=1.15, abandonment=1.30)
    # Position along the ordinary campaign, 0.0 at floor 1 to 1.0 at floor 8, with a
    # seeded offset so the same floor number is not always at the same point.
    span = max(1, 8 - 1)
    drift = random.Random(config.aesthetic_drift_seed()).uniform(-0.12, 0.12)
    position = min(1.0, max(0.0, (floor - 1) / span + drift))

    def band(low: float, high: float) -> float:
        return round(low + (high - low) * position, 3)

    return AestheticPhase(
        orderliness=band(1.20, 0.80),     # tidy garrison -> disordered depths
        damage=band(0.80, 1.30),          # intact -> battered
        occupation=band(1.20, 0.85),      # staffed -> emptying
        monumentality=band(0.85, 1.25),   # utilitarian -> ceremonial
        abandonment=band(0.75, 1.30),     # kept -> derelict
    )


def _boss_seed(config: CampaignConfig) -> int:
    """Attempt-independent stream for the floor-9 boss.

    Deliberately not drawn from the floor rng. When it was, every rejected floor-9
    attempt re-rolled the boss, and because two arena families could never validate
    the retries skewed the result 2:1 toward one boss.
    """
    return config.boss_schedule_seed()


def choose_boss(config: CampaignConfig) -> int:
    return random.Random(_boss_seed(config)).choice(BOSS_ROSTER)


@dataclass(frozen=True, slots=True)
class CampaignSchedule:
    """Every campaign-scale choice, resolved once before any floor is built.

    Frozen and derived purely from the config, so a floor rejected by
    validate_map is retried against the same schedule. That is the property the
    whole module exists to guarantee: a retry must reroll a layout, never a
    campaign identity.
    """
    secret_from: int
    variants: tuple[FloorVariant, ...]
    vine_floor: int
    vine_budget: int
    gallery_floor: int
    rare_motif_floor: int
    vista_parity: int
    boss: int
    void_floor: int
    config: CampaignConfig

    def floor_options(self, number: int) -> dict[str, object]:
        """The per-floor slice of this schedule, as generate_map keyword args.

        Answering "does floor N get the vine sector / the gallery / the rare
        motif / a vista" belongs to the schedule that made those choices, not to
        the loop that calls the generator. Keeping the translation here means the
        campaign-scale rules -- one vine floor, one gallery floor, one parity of
        vista floors -- are stated once, beside the draws that picked them.
        """
        return {
            "secret_exit": number == self.secret_from,
            "secret_source": self.secret_from if number == 10 else None,
            "hallway_vine_budget": (self.vine_budget
                                    if number == self.vine_floor else 0),
            "guard_gallery_enabled": number == self.gallery_floor,
            "rare_motif_enabled": number == self.rare_motif_floor,
            "sky_vista_enabled": number % 2 == self.vista_parity,
            "boss": self.boss if number == 9 else None,
            "phase": aesthetic_phase(self.config, number),
            "shared_void_enabled": number == self.void_floor,
        }


def resolve_schedule(config: CampaignConfig) -> CampaignSchedule:
    """Draw every campaign-scale choice from attempt-independent streams."""
    secret_seed = (config.secret_source_aardwolf_seed() if config.say_aardwolf
                   else config.floor_seed(10))
    secret_from = 1 + secret_seed % 6
    variants = _variant_sequence(config)
    vine_seed = (config.vine_aardwolf_seed() if config.say_aardwolf
                 else config.vine_seed())
    vine_rng = random.Random(vine_seed)
    vine_floors = list(range(2, 9))
    vine_weights = [4 if variants[floor - 1].name == "catacombs" else
                    2 if variants[floor - 1].name in ("storehouse", "grand-halls") else 1
                    for floor in vine_floors]
    vine_floor = vine_rng.choices(vine_floors, weights=vine_weights, k=1)[0]
    vine_budget = 2 if vine_rng.random() < 0.28 else 1
    gallery_seed = (config.guard_gallery_aardwolf_seed() if config.say_aardwolf
                    else config.guard_gallery_seed())
    gallery_rng = random.Random(gallery_seed)
    gallery_enabled = gallery_rng.random() < 0.22
    gallery_floors = list(range(3, 9))
    gallery_weights = [3 if variants[floor - 1].name in
                       ("garrison", "grand-halls") else 1
                       for floor in gallery_floors]
    gallery_floor = (gallery_rng.choices(gallery_floors, weights=gallery_weights, k=1)[0]
                     if gallery_enabled else 0)
    rare_motif_floor = _rare_motif_schedule(config)
    # Only one parity of floors may request a vista in a campaign. This keeps
    # the rare motif from appearing on consecutive maps without changing
    # standalone-map generation or tying it to a specific theme.
    vista_parity = random.Random(config.vista_schedule_seed()).randrange(2)
    # One shared void per campaign at most, on an ordinary floor. Rarity is the
    # feature: a building with one inaccessible courtyard has a landmark, a
    # building with five has a layout quirk. Scheduled campaign-wide like the vine
    # sector and the guard gallery so retries cannot multiply it.
    void_rng = random.Random(config.void_schedule_seed())
    void_floor = (void_rng.choice(range(2, 9))
                  if void_rng.random() < 0.45 else 0)
    return CampaignSchedule(
        secret_from=secret_from, variants=variants, vine_floor=vine_floor,
        vine_budget=vine_budget, gallery_floor=gallery_floor,
        rare_motif_floor=rare_motif_floor, vista_parity=vista_parity,
        boss=choose_boss(config), void_floor=void_floor,
        config=config)


def _set_distance(first: tuple[str, ...], second: tuple[str, ...]) -> float:
    left, right = set(first), set(second)
    union = left | right
    return len(left ^ right) / max(1, len(union))


def _candidate_score(level: GeneratedMap, previous: list[GeneratedMap],
                     config: CampaignConfig) -> float:
    score = 0.0
    for offset, other in enumerate(reversed(previous[-4:])):
        weight = 4.0 / (offset + 1)
        score += weight * (
            2.5 * (level.variant != other.variant)
            + 2.5 * (level.circulation_skeleton != other.circulation_skeleton)
            + 1.5 * _set_distance(level.layout_signature,
                                  other.layout_signature)
            # Concepts, shapes and lighting are multisets: how *many* armories a
            # floor has is part of how it reads, and set distance cannot see it.
            + 1.4 * weighted_distance(level.room_concepts,
                                      other.room_concepts)
            + 1.2 * _set_distance(level.motifs, other.motifs)
            + 1.1 * weighted_distance(level.room_shapes, other.room_shapes)
            + 0.9 * _set_distance(level.district_circulation,
                                  other.district_circulation)
            + 0.8 * _set_distance(level.secret_variants,
                                  other.secret_variants)
            + 0.7 * weighted_distance(
                tuple(encounter.family for encounter in level.encounters),
                tuple(encounter.family for encounter in other.encounters))
            + 0.6 * weighted_distance(level.lighting_families,
                                      other.lighting_families)
            + 0.5 * ((level.arrival.kind if level.arrival else "")
                     != (other.arrival.kind if other.arrival else ""))
            + min(1.0, abs(len(level.rooms) - len(other.rooms)) / 5.0)
            + min(1.0, abs(sum(bool(thing) for thing in level.things)
                               - sum(bool(thing) for thing in other.things)) / 24.0)
        )
    score += weighted_distance(level.room_concepts[:-1],
                               level.room_concepts[1:])
    score += 0.35 * len(set(level.room_concepts))
    # The same table planning used to seek these adjacencies, so the score rewards
    # a floor for something it actually tried to do.
    score += 0.6 * sum(
        frozenset((level.room_concepts[first], level.room_concepts[second]))
        in CONCEPT_AFFINITIES for first, second in level.edges)
    rhythm = random.Random(config.aardwolf_rhythm_seed())
    phase = rhythm.random() * math.tau
    tension = math.sin(phase + level.number * math.tau / 4.5)
    actor_density = sum(level.enemy_tiers) / max(1, len(level.rooms))
    target_actors = 0.45 + int(config.guard_density) * 0.20 + tension * 0.18
    score -= abs(actor_density - target_actors) * 1.8
    object_density = sum(bool(thing) for thing in level.things) / max(1, len(level.rooms))
    # Recalibrated against measurement. The old target (3.0 + amount * 0.55, so
    # 3.6-5.8 objects per room) predated the density overhaul, which brought
    # floors to a measured 12.8/14.1/15.9 at decoration_amount 1/3/5. Because the
    # actual value always exceeded the target, the penalty had become monotonically
    # decreasing in object count: it simply preferred sparser candidates and the
    # tension modulation that makes some floors deliberately busier was dead.
    # Slope 0.79 per setting step and intercept 12.0 are fitted to those
    # measurements; the tension amplitude keeps its original share of the target
    # rather than its absolute size.
    target_objects = (12.0 + int(config.decoration_amount) * 0.79
                      - tension * 1.05)
    score -= abs(object_density - target_objects) * 0.20
    center = sum(room.center[0] for room in level.rooms) / max(1, len(level.rooms))
    handedness = -1 if config.aardwolf_seed(level.number) & 1 else 1
    score += 0.5 if (center - level.start[0]) * handedness > 0 else 0.0
    return score


def validate_campaign_budgets(levels: list[GeneratedMap], schedule: CampaignSchedule) -> None:
    """Enforce the campaign schedule against the maps that actually realized."""
    realized_vine_floors = {
        level.number for level in levels
        if any(screen.kind == "hallway-run" for screen in level.vine_screens)}
    realized_vine_runs = sum(
        screen.kind == "hallway-run" for level in levels for screen in level.vine_screens)
    if (realized_vine_floors - {schedule.vine_floor}
            or len(realized_vine_floors) > 1
            or realized_vine_runs > schedule.vine_budget):
        raise RuntimeError("campaign hallway-vine budget was violated")
    realized_gallery_floors = {
        level.number for level in levels if level.guard_galleries}
    if (realized_gallery_floors - {schedule.gallery_floor}
            or len(realized_gallery_floors) > 1):
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
    expected_rare = [schedule.rare_motif_floor] if schedule.rare_motif_floor else []
    if realized_rare != expected_rare:
        raise RuntimeError("campaign rare-motif schedule was violated")


def _layout_signature(plan, specs, realized_shapes, guard_recesses,
                      encounters, edges) -> tuple[str, ...]:
    """A compact fingerprint of how this floor is organized.

    campaign.py compares these between adjacent floors to reject a campaign that
    repeats itself. It belongs beside the planning vocabulary it summarizes rather
    than in the orchestrator: every component is a plan or realization fact, and
    what counts as "the same shape of floor" is a campaign-composition judgement.
    """
    corridor_edges = sum(specs[first].tier == "corridor" or specs[second].tier == "corridor"
                         for first, second in edges)
    mediated_ratio = corridor_edges / max(1, len(edges))
    layout_signature = (
        plan.special_family, plan.progression_grammar, plan.skeleton,
        *plan.motif_realizations, *plan.district_circulation,
        f"corridors-{sum(spec.tier == 'corridor' for spec in specs)}",
        f"mediated-{round(mediated_ratio, 1):.1f}",
        f"shapes-{','.join(sorted(Counter(realized_shapes).elements()))}",
        f"recesses-{len(guard_recesses)}",
        f"patrols-{sum(bool(encounter.patrol_kind) for encounter in encounters)}",
    )
    return layout_signature
