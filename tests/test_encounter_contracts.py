import random

from infiniwolf.config import CampaignConfig
from infiniwolf.encounters import (
    _encounter_contracts_by_room,
    _place_population,
)
from infiniwolf.model import GuardRecess, Room, RoomIdentity, SetPiecePlan
from infiniwolf.wl6 import FLOOR, GRID, WALL


def _identity(motif: str, concept: str = "office") -> RoomIdentity:
    return RoomIdentity("beat", "standard", motif, 0, "", concept, "barracks")


def _open_rooms():
    rooms = [Room(3, 3, 8, 8), Room(18, 10, 10, 10), Room(38, 10, 10, 10)]
    tiles = [WALL] * (GRID * GRID)
    for room in rooms:
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                tiles[y * GRID + x] = FLOOR
    return rooms, tiles


def test_named_roles_resolve_through_realized_plan_indices():
    identities = [_identity(""), _identity("")]
    set_piece = SetPiecePlan(
        "test", "secondary", (7,), ("threshold",), (), "threshold", "threshold",
        encounter_contract=(("threshold", "guarded"), ("dropped", "ambush")))

    assert _encounter_contracts_by_room(
        identities, (set_piece,), (0, 7)) == {1: "guarded"}


def test_light_contract_overrides_a_high_pacing_room():
    """A memorial beside a war-room is thinned to the "light" band, not emptied.

    quality._intensity_class reserves zero actors for "calm" and calls 1-2
    "light", so the contract clamps the pacing-derived budget to one actor
    rather than deleting the room's population outright.
    """
    rooms, tiles = _open_rooms()
    things = [0] * len(tiles)
    identities = [
        _identity(""),
        _identity("setpiece:memorial-bay:memorial", "memorial"),
        _identity("", "war-room"),
    ]
    encounters = []

    _place_population(
        CampaignConfig(seed=4), 8, rooms, tiles, things, set(),
        random.Random(9), rooms[0].center, rooms[2], patrol_chance=0.0,
        identities=identities, critical_route=(0, 1, 2),
        encounter_out=encounters)

    owned = [item for item in encounters if item.room_index == 1]
    assert sum(len(item.cells) for item in owned) == 1


def test_patrolled_contract_forces_a_route_when_patrol_target_is_zero():
    rooms, tiles = _open_rooms()
    things = [0] * len(tiles)
    identities = [
        _identity(""),
        _identity("setpiece:barracks-mess:barracks", "barracks"),
        _identity(""),
    ]
    encounters = []

    _place_population(
        CampaignConfig(seed=5), 5, rooms, tiles, things, set(),
        random.Random(10), rooms[0].center, rooms[2], patrol_chance=0.0,
        identities=identities, encounter_out=encounters)

    patrols = [item for item in encounters
               if item.room_index == 1 and item.template == "patrol"]
    assert patrols
    assert patrols[0].patrol_path


def test_light_contract_yields_to_owned_guard_recess_geometry():
    rooms, tiles = _open_rooms()
    things = [0] * len(tiles)
    actor_cell = (rooms[1].x - 1, rooms[1].center[1])
    mirror_cell = (rooms[1].x + rooms[1].w, rooms[1].center[1])
    for x, y in (actor_cell, mirror_cell):
        tiles[y * GRID + x] = FLOOR
    recess = GuardRecess(1, (actor_cell, mirror_cell), actor_cell)
    encounters = []

    _place_population(
        CampaignConfig(seed=6), 5, rooms, tiles, things,
        {actor_cell, mirror_cell}, random.Random(11), rooms[0].center, rooms[2],
        patrol_chance=0.0,
        identities=[
            _identity(""),
            _identity("setpiece:memorial-bay:memorial", "memorial"),
            _identity(""),
        ],
        guard_recesses=(recess,), encounter_out=encounters)

    owned = [item for item in encounters if item.room_index == 1]
    assert owned
    assert owned[0].template == "blind-corner-ambush"
    assert actor_cell in owned[0].hidden_cells
