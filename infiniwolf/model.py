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
class PillarPlacement:
    """Auditable WhitePillar composition committed by decoration."""
    source: str
    room_index: int
    cells: tuple[tuple[int, int], ...]

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
class AestheticPhase:
    """Bounded visual modifiers for one floor's position in the campaign.

    A campaign should feel like it goes somewhere: orderly and occupied early,
    older and damper in the middle, ceremonial and ruined late. Theme rotation
    already stops two adjacent floors looking alike, but it gives no direction --
    floor 2 and floor 8 were equally likely to be pristine.

    Every field is a multiplier or a probability in a deliberately narrow band, so
    the arc modulates a floor rather than defining it. Variant identity must stay
    the stronger signal: a catacomb on floor 2 is still a catacomb, just a
    better-kept one. Values are derived from the floor number and the campaign
    seed, so they are reproducible and recorded.
    """
    # `damage` is the field currently wired through, into the damaged-wall
    # treatment rate, and it demonstrably works: within every variant, a floor
    # appearing late is markedly more battered than the same variant appearing
    # early (catacombs 18.9 -> 54.9 damaged wall tiles, garrison 1.1 -> 5.7).
    #
    # The other four are declared and recorded but not yet consumed. A first
    # attempt tilted decoration's clutter palette by `abandonment` and
    # `occupation` and produced no measurable effect at all -- concept gating
    # dominates which gore or furniture a room can hold, so a +/-25% filter on the
    # palette was swamped. That code is not kept; a field that does nothing is
    # better declared honestly than faked.
    orderliness: float      # reserved: symmetry and intactness of composition
    damage: float           # scales the damaged-wall treatment rate
    occupation: float       # reserved: furniture implying people are still here
    monumentality: float    # reserved: landmark and colonnade emphasis
    abandonment: float      # reserved: gore, dust and disuse clutter


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
class VignettePlan:
    """Attempt-independent cross-system room-story intent."""
    family: str
    rooms: tuple[int, ...]
    required_concepts: tuple[str, ...]
    encounter_treatment: str
    pickup_treatment: str
    decoration_treatment: str
    focal_cells: tuple[tuple[int, int], ...] = ()
    approach_room: int = -1


@dataclass(frozen=True, slots=True)
class SetPiecePlan:
    """A concept-first space program allocated onto the abstract room graph.

    ``rooms`` and ``room_roles`` are parallel: the former are real indices in
    ``FloorPlan.specs`` and the latter say what those spaces are for. Required
    edges likewise use realized plan indices, so tests and later passes can
    distinguish a promised adjacency from a thematic suggestion.

    The contracts below say what a program *means* for the systems that fill it,
    so a checkpoint is guarded like a checkpoint rather than receiving whatever
    the generic per-room roll produced. Each is advisory: a contract states an
    intent and names the pass that owns it, and an unhonoured one degrades the
    program to ordinary rooms rather than failing the floor. That is the same
    rule vignettes follow, and it is why these are requests rather than
    invariants -- room placement can always refuse.

    Each contract is a tuple parallel to nothing: entries name a ROOM ROLE from
    ``room_roles``, not an index, so a program whose optional rooms were dropped
    still reads correctly.
    """
    family: str
    scale: str                       # "primary" or "secondary"
    rooms: tuple[int, ...]
    room_roles: tuple[str, ...]
    required_edges: tuple[tuple[int, int], ...]
    entry_role: str
    exit_role: str
    # (observer role, subject role) -- the subject should be visible on entering
    # the observer. Owned by semantics' sightline planning.
    visibility_contracts: tuple[tuple[str, str], ...] = ()
    # (room role, intent) where intent is one of "guarded", "ambush",
    # "patrolled", "overlook", "light". Owned by encounters.
    encounter_contract: tuple[tuple[str, str], ...] = ()
    # (room role, reward kind) where kind is one of "cache", "objective",
    # "resupply", "treasure". Owned by pickups.
    reward_contract: tuple[tuple[str, str], ...] = ()
    # Room roles that should carry a wall landmark. Owned by semantics.
    landmark_contract: tuple[str, ...] = ()

    def roles_for(self, contract: str) -> dict[str, str]:
        """Map room role -> intent for one contract, ignoring dropped rooms."""
        pairs = getattr(self, contract, ())
        return {role: intent for role, intent in pairs if role in self.room_roles}

    def rooms_for_role(self, role: str) -> tuple[int, ...]:
        """Realized plan indices carrying a role, empty if it was dropped."""
        return tuple(room for room, name in zip(self.rooms, self.room_roles)
                     if name == role)


# What each program MEANS for the systems that fill it. Without these a
# checkpoint was a checkpoint only in name: the generic per-room roll decided
# whether it was guarded, whether it held anything, and whether it read as a
# place. Roles are named rather than indexed so a program whose optional rooms
# were dropped still resolves. Every entry is advisory -- the owning pass may
# refuse, and the program degrades to ordinary rooms rather than failing.
SET_PIECE_CONTRACTS = {
    "checkpoint-administration": {
        "visibility": (("checkpoint", "administrative-office"),),
        "encounter": (("checkpoint", "guarded"), ("records-office", "ambush")),
        "reward": (("records-office", "cache"),),
        "landmark": ("administrative-office",),
    },
    "command-and-control": {
        "visibility": (("security-desk", "war-room"),),
        "encounter": (("security-desk", "guarded"), ("war-room", "objective")),
        "reward": (("war-room", "objective"), ("communications", "resupply")),
        "landmark": ("war-room",),
    },
    "barracks-support": {
        "visibility": (("checkpoint", "barracks"),),
        "encounter": (("barracks", "patrolled"), ("armory", "guarded")),
        "reward": (("armory", "cache"), ("mess-hall", "resupply")),
        "landmark": ("mess-hall",),
    },
    "storage-machinery-route": {
        "visibility": (("receiving", "bulk-storage"),),
        "encounter": (("machinery-control", "ambush"), ("dispatch", "guarded")),
        "reward": (("bulk-storage", "cache"), ("dispatch", "resupply")),
        "landmark": ("machinery-control",),
    },
    "prison-processing": {
        "visibility": (("processing-desk", "cell-block"),
                       ("guardroom", "cell-block")),
        "encounter": (("guardroom", "guarded"), ("exercise-yard", "overlook"),
                      ("cell-block", "light")),
        "reward": (("guardroom", "cache"), ("exercise-yard", "treasure")),
        "landmark": ("cell-block",),
    },
    "administrative-wing": {
        "visibility": (("checkpoint", "clerks-office"),
                       ("briefing-room", "command-office")),
        "encounter": (("checkpoint", "guarded"), ("command-office", "objective"),
                      ("clerks-office", "light")),
        "reward": (("records-office", "cache"), ("command-office", "treasure")),
        "landmark": ("command-office",),
    },
    "wayfinding-checkpoint": {
        "visibility": (), "encounter": (("checkpoint", "guarded"),),
        "reward": (), "landmark": ("checkpoint",),
    },
    "supply-depot": {
        "visibility": (), "encounter": (("supply-cache", "ambush"),),
        "reward": (("supply-cache", "resupply"),), "landmark": (),
    },
    "memorial-bay": {
        "visibility": (), "encounter": (("memorial", "light"),),
        "reward": (("memorial", "treasure"),), "landmark": ("memorial",),
    },
    "records-annex": {
        "visibility": (), "encounter": (("records-office", "ambush"),),
        "reward": (("records-office", "cache"),), "landmark": (),
    },
    # The two-room vignette families double as secondary programs, so they get
    # contracts too -- otherwise roughly two thirds of a floor's set pieces
    # would carry no intent at all and the whole layer would only bite on the
    # single primary.
    "guardpost-supply": {
        "visibility": (("checkpoint", "supply-cache"),),
        "encounter": (("checkpoint", "guarded"),),
        "reward": (("supply-cache", "resupply"),), "landmark": ("checkpoint",),
    },
    "barracks-mess": {
        "visibility": (("mess-hall", "barracks"),),
        "encounter": (("barracks", "patrolled"),),
        "reward": (("mess-hall", "resupply"),), "landmark": ("mess-hall",),
    },
    "prison-processing-pair": {
        "visibility": (("processing-desk", "holding-cell"),),
        "encounter": (("holding-cell", "light"),),
        "reward": (("holding-cell", "cache"),), "landmark": ("holding-cell",),
    },
    "officer-suite": {
        "visibility": (("briefing-room", "officers-office"),),
        "encounter": (("officers-office", "ambush"),),
        "reward": (("officers-office", "treasure"),),
        "landmark": ("officers-office",),
    },
    "crypt-ossuary": {
        "visibility": (("crypt", "ossuary"),),
        "encounter": (("ossuary", "ambush"),),
        "reward": (("ossuary", "treasure"),), "landmark": ("ossuary",),
    },
    "workshop-service": {
        "visibility": (), "encounter": (("workshop", "patrolled"),),
        "reward": (("parts-store", "cache"),), "landmark": ("workshop",),
    },
}


@dataclass(frozen=True, slots=True)
class RealizedVignette:
    """Audit record proving all cross-system components landed."""
    family: str
    rooms: tuple[int, ...]
    encounter_rooms: tuple[int, ...]
    pickup_rooms: tuple[int, ...]
    decoration_rooms: tuple[int, ...]


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
    set_pieces: tuple[SetPiecePlan, ...] = ()


@dataclass(slots=True)
class PlacedPlan:
    rooms: list[Room]
    spec_indices: list[int]
    edges: list[tuple[int, int]]
    loop_edges: list[tuple[int, int]]


@dataclass(frozen=True, slots=True)
class SharedVoid:
    """A building-scale space seen from several rooms and entered from none.

    A light well, a collapsed chamber, an inaccessible garden. The point is spatial
    recognition: glimpsing the same courtyard from two different corridors is what
    turns a set of rooms into a building, because it tells the player those rooms
    have a relationship in space rather than just on a graph.

    Built from the mechanism the guard gallery and the exterior vista already use --
    floor cells fronted by a complete line of blocking pillars, so the space is
    visible and impassable. Containment is proved, not assumed: with the screens
    treated as blocked, a flood fill from the player start must not reach a single
    interior cell.
    """
    family: str
    interior: tuple[tuple[int, int], ...]
    screens: tuple[tuple[int, int], ...]
    viewing_rooms: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class AuthoredSightline:
    """A long view kept on purpose instead of being broken up.

    The generator otherwise suppresses every unobstructed run past 21 cells,
    because an accidental one is a firing lane the player cannot answer and the
    clearest sign that geometry fell out of a router rather than a plan. But a
    building with no long views is equally artificial: seeing the anchor hall from
    its approach, or the exit lift across an antechamber, is how a real space tells
    you where you are going.

    So a small number of over-long runs are authored rather than repaired --
    specifically the ones that already terminate on a room worth looking at. The
    record exists so the view is auditable and so critique can tell a deliberate
    vista from an accident, which is otherwise the same measurement.
    """
    cells: tuple[tuple[int, int], ...]
    origin_room: int
    target_room: int
    purpose: str

    @property
    def length(self) -> int:
        return len(self.cells)


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
    # None only on a floor 9 whose boss ends the campaign when he dies: that
    # floor has no elevator, because the kill is the exit (see wl6.VICTORY_BOSSES).
    exit_stand: tuple[int, int] | None
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
    # The one composition each room realized, or "" for deliberately plain.
    room_motifs: tuple[str, ...] = ()
    authored_sightlines: tuple[AuthoredSightline, ...] = ()
    shared_void: SharedVoid | None = None
    vignette_plans: tuple[VignettePlan, ...] = ()
    realized_vignettes: tuple[RealizedVignette, ...] = ()
    pillar_placements: tuple[PillarPlacement, ...] = ()


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
