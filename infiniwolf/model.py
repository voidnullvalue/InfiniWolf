"""Shared structural records.

The record types more than one generation system reads. A type belongs here when
it is a *shared decision* -- something planning writes and geometry, progression,
semantics, encounters, pickups or decoration later consult -- not merely because
it is a dataclass. Subsystem-private records stay with their subsystem.

Imports only the WL6 vocabulary, so this is a leaf alongside `wl6` and `grid`.
Nothing here may import a generation module; that is what keeps the dependency
graph acyclic and lets validation and artifact encoding read these types without
reaching back into the generator.
"""

from __future__ import annotations

from dataclasses import dataclass

from .wl6 import JAIL_CANDIDATE_PROBABILITY, WALL


@dataclass(frozen=True, slots=True)
class Room:
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2

@dataclass(frozen=True, slots=True)
class RoomSpec:
    role: str
    tier: str
    district: int
    motif: str = "spine"

@dataclass(frozen=True, slots=True)
class RoomIdentity:
    """One semantic decision shared by wall, population and decor passes."""
    role: str
    tier: str
    motif: str
    district: int
    variant: str
    concept: str
    base_theme: str
    wall_base: int = WALL
    special: str = ""

@dataclass(frozen=True, slots=True)
class SpritePlacement:
    """Auditable proof that sprites belong to an authored composition."""
    reason: str
    template: str
    room_index: int
    cells: tuple[tuple[int, int, int], ...]

@dataclass(frozen=True, slots=True)
class VineScreen:
    """One complete, auditable vine pseudowall composition."""
    kind: str
    room_index: int
    cells: tuple[tuple[int, int], ...]
    ambush_anchor: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class FloorVariant:
    name: str
    notch_chance: float = 0.22        # restrained _carve_notches
    pillar_chance: float = 0.12       # rare structural _add_pillars landmark
    widen_chance: float = 0.80        # _widen_corridors
    hall_chance: float = 0.25         # _plan_floor spine-beat tier roll
    closet_weight: float = 0.45       # _plan_floor filler closet-vs-branch
    extra_motif_chance: float = 0.35  # _plan_floor motif budget roll
    motif_pref: tuple[str, ...] = ()  # motifs promoted ahead of the shuffle
    # Allowed wall-material bases; () = all. Must keep at least as many bases
    # as the floor has districts (up to 3) or the pool is ignored.
    theme_pool: tuple[int, ...] = ()
    jail_probability: float = JAIL_CANDIDATE_PROBABILITY
    decor_density: float = 1.0        # scales blocking/open decor budgets
    # Remaps applied to _decor_theme's result (never to jail rooms).
    decor_overrides: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class GatePlan:
    """Ordered key colors required by one floor's mandatory route."""
    colors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class KeyObjective:
    """A physical key staged as a measured exploration objective."""
    color: str
    cell: tuple[int, int]
    host_room: int
    stage: int
    detour: int
    treatment: str


@dataclass(frozen=True, slots=True)
class SecretDetail:
    """Host and progression metadata for one bespoke secret pocket."""
    shape: str
    reward_count: int
    host_room: int
    depth_ratio: float
    pushwall: tuple[int, int]
    secret_exit: bool = False
    hint_treatment: str = "single-landmark"
    return_floor: int = 0
    push_direction: int = 1


@dataclass(frozen=True, slots=True)
class RareMotifDetail:
    kind: str
    room_index: int
    realization: str
    endpoints: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class BossArenaDetail:
    family: str
    profile: str
    geometry: tuple[tuple[int, int], ...]
    decorations: tuple[tuple[int, int, int], ...]


@dataclass(frozen=True, slots=True)
class ArrivalDetail:
    """The inert elevator façade establishing how the player entered."""
    kind: str
    portal: tuple[int, int]
    player: tuple[int, int]
    facing: int
    footprint: tuple[tuple[int, int], ...]
    car_cells: tuple[tuple[int, int], ...] = ()
    clearance: tuple[tuple[int, int], ...] = ()
    item: tuple[int, int, int] | None = None


@dataclass(frozen=True, slots=True)
class GuardRecess:
    """A rare mirrored hallway composition built for a corner sentry."""
    room_index: int
    cells: tuple[tuple[int, int], tuple[int, int]]
    actor_cell: tuple[int, int]


@dataclass(frozen=True, slots=True)
class GuardGallery:
    """A symmetric, visible but physically inaccessible combat chamber."""
    room_index: int
    screen: tuple[tuple[int, int], ...]
    actor_cells: tuple[tuple[int, int], tuple[int, int]]
    rear_cells: tuple[tuple[int, int], ...]
    facing: int
    treatment: int = 30


@dataclass(frozen=True, slots=True)
class EncounterPlacement:
    """Auditable room-owned actor composition and its reveal behavior."""
    template: str
    room_index: int
    cells: tuple[tuple[int, int, int], ...]
    hidden_cells: tuple[tuple[int, int], ...] = ()
    patrol_kind: str = ""
    patrol_path: tuple[tuple[int, int], ...] = ()
    family: str = ""


@dataclass(frozen=True, slots=True)
class PatrolRoute:
    """Engine-valid path plus fixed direction changes at marker cells."""
    kind: str
    cells: tuple[tuple[int, int], ...]
    turns: tuple[tuple[tuple[int, int], int], ...]


@dataclass(slots=True)
class FloorPlan:
    specs: list[RoomSpec]
    edges: list[tuple[int, int]]
    loop_edges: list[tuple[int, int]]
    motifs: tuple[str, ...]
    # Realization metadata keeps grammar membership out of gameplay roles.
    critical: frozenset[int] = frozenset()
    size_groups: tuple[tuple[int, ...], ...] = ()
    skeleton: str = "bent-spine"
    district_circulation: tuple[str, ...] = ()
    special_family: str = "standard"
    progression_grammar: str = "axial-journey"
    motif_realizations: tuple[str, ...] = ()


@dataclass(slots=True)
class PlacedPlan:
    rooms: list[Room]
    spec_indices: list[int]
    edges: list[tuple[int, int]]
    loop_edges: list[tuple[int, int]]


@dataclass(frozen=True, slots=True)
class LandmarkPlan:
    """One room nominated to anchor a player's mental map of the floor.

    A landmark is not a room with more props. It is a space a player can navigate
    *by* -- distinctive enough to recognize on return and to describe to someone
    else. Rank separates the one space that should dominate from the two or three
    that support it; a floor with four equally emphatic rooms has no hierarchy at
    all, which is the same failure as a floor with none.

    Selected before decoration deliberately: decoration reinforces a landmark, it
    does not invent one. The approach room is recorded so later work can frame the
    view into it.
    """
    room_index: int
    rank: str                       # "primary" or "secondary"
    purpose: str                    # why this room earned it
    score: float
    approach_room: int = -1


@dataclass(slots=True)
class GeneratedMap:
    number: int
    tiles: list[int]
    things: list[int]
    start: tuple[int, int]
    exit_stand: tuple[int, int]
    secret_rewards: list[tuple[int, int]]
    seed: int
    has_secret_exit: bool = False
    locked_doors: int = 0
    boss: bool = False
    enemy_tiers: tuple[int, int, int] = (0, 0, 0)
    motifs: tuple[str, ...] = ()
    motif_rooms: tuple[str, ...] = ()
    secret_variants: tuple[str, ...] = ()
    shortcut_pushwalls: tuple[tuple[int, int], ...] = ()
    critique: tuple[str, ...] = ()
    rooms: tuple[Room, ...] = ()
    edges: tuple[tuple[int, int], ...] = ()
    jail_rooms: frozenset[int] = frozenset()
    variant: str = ""
    room_concepts: tuple[str, ...] = ()
    key_order: tuple[str, ...] = ()
    critical_route: tuple[int, ...] = ()
    room_districts: tuple[int, ...] = ()
    exit_depth_ratio: float = 0.0
    room_roles: tuple[str, ...] = ()
    room_tiers: tuple[str, ...] = ()
    circulation_skeleton: str = ""
    district_circulation: tuple[str, ...] = ()
    layout_signature: tuple[str, ...] = ()
    pickup_placements: tuple[SpritePlacement, ...] = ()
    room_shapes: tuple[str, ...] = ()
    lighting_families: tuple[str, ...] = ()
    vine_screens: tuple[VineScreen, ...] = ()
    key_objectives: tuple[KeyObjective, ...] = ()
    secret_details: tuple[SecretDetail, ...] = ()
    special_family: str = "standard"
    boss_arena_room: int = -1
    preboss_room: int = -1
    premium_room: int = -1
    expedition_rooms: tuple[int, ...] = ()
    secret_source: int = 0
    arrival: ArrivalDetail | None = None
    guard_recesses: tuple[GuardRecess, ...] = ()
    guard_galleries: tuple[GuardGallery, ...] = ()
    encounters: tuple[EncounterPlacement, ...] = ()
    patrol_target: float = 0.0
    progression_grammar: str = "axial-journey"
    motif_realizations: tuple[str, ...] = ()
    rare_motif: RareMotifDetail | None = None
    boss_arena: BossArenaDetail | None = None
    shape_target: float = 0.0
    primary_hall_geometry: tuple[tuple[int, int, int, int, int], ...] = ()
    barrel_families: tuple[str, ...] = ()
    sky_vistas: tuple[tuple[tuple[int, int], ...], ...] = ()
    sky_vista_recesses: tuple[tuple[tuple[int, int], ...], ...] = ()
    sky_vista_supports: tuple[tuple[tuple[int, int], ...], ...] = ()
    landmarks: tuple[LandmarkPlan, ...] = ()


@dataclass(frozen=True, slots=True)
class FloorCanvas:
    """The mutable working state of one floor under construction.

    Frozen in its *bindings* while the containers it holds stay mutable, which is
    the honest description of how generation works: the set of things a pass may
    touch is fixed, but the planes are edited in place. Passing this instead of
    eight loose locals is what lets a phase move out of the orchestrator at all --
    the secret installation reads sixteen free variables, and a sixteen-parameter
    function would be worse than the inline block it replaced.

    `reserved` is the shared cell-reservation set whose lack of provenance
    tools/reservation_sites.py inventories; it travels here so that when it becomes
    a typed ledger only this record changes.
    """
    tiles: list[int]
    things: list[int]
    rooms: list[Room]
    specs: list[RoomSpec]
    roles: list[str]
    edges: list[tuple[int, int]]
    reserved: set[tuple[int, int]]
    start: tuple[int, int]


@dataclass(frozen=True, slots=True)
class SecretInstallation:
    """Everything the secret pass produced, for the manifest and validation."""
    rewards: tuple[tuple[int, int], ...]
    variants: tuple[str, ...]
    details: tuple[SecretDetail, ...]
    shortcut_pushwalls: tuple[tuple[int, int], ...]
    # Every cell a pocket occupies, not just its focal reward. Downstream reads
    # this to record secret loot as an authored composition; a pocket can overlap
    # another room's rectangular bookkeeping, and without the full footprint its
    # sprites would look like loose untracked room loot.
    protected: frozenset[tuple[int, int]]
