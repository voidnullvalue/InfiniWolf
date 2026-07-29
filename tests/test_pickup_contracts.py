"""Reward-contract placement coverage."""

import random

from infiniwolf.grid import _set
from infiniwolf.model import Room, RoomIdentity, SetPiecePlan
from infiniwolf.pickups import (
    AUTHORED_PICKUP_TEMPLATES,
    _PlacementGrammar,
    _SET_PIECE_REWARD_TREATMENTS,
    _place_set_piece_rewards,
    _set_pieces_from_motifs,
)
from infiniwolf.wl6 import FLOOR, GOLD_KEY, GRID, SILVER_KEY, WALL


def _identity(family: str, role: str, district: int) -> RoomIdentity:
    return RoomIdentity(
        "branch", "standard", f"setpiece:{family}:{role}", district,
        "default", role, "stone")


def _grammar_fixture():
    roles = ("cache-room", "objective-room", "resupply-room", "treasure-room")
    rooms = [Room(4 + 12 * index, 10, 7, 7)
             for index in range(len(roles))]
    tiles = [WALL] * (GRID * GRID)
    for room in rooms:
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                _set(tiles, x, y, FLOOR)
    identities = [_identity("test-program", role, index)
                  for index, role in enumerate(roles)]
    return roles, rooms, tiles, identities


def _realize(seed: int, blocked: bool = False):
    roles, rooms, tiles, identities = _grammar_fixture()
    things = [0] * (GRID * GRID)
    reserved = ({
        (x, y)
        for room in rooms
        for y in range(room.y, room.y + room.h)
        for x in range(room.x, room.x + room.w)
    } if blocked else set())
    placements = []
    grammar = _PlacementGrammar(
        rooms, tiles, things, reserved, identities, random.Random(seed),
        placements)
    plan = SetPiecePlan(
        "test-program", "primary", tuple(range(4)), roles, (),
        roles[0], roles[-1],
        reward_contract=tuple(zip(
            roles, ("cache", "objective", "resupply", "treasure"))))

    def place_group(items, reason, candidates, templates):
        return any(grammar.place(room, items, reason, templates) is not None
                   for room in candidates)

    honoured = _place_set_piece_rewards((plan,), identities, place_group)
    return honoured, tuple(placements), tuple(things)


def test_each_reward_kind_uses_its_existing_authored_template_family():
    honoured, placements, _ = _realize(73)

    assert honoured == 4
    assert len(placements) == 4
    for kind, placement in zip(
            ("cache", "objective", "resupply", "treasure"), placements):
        _, templates = _SET_PIECE_REWARD_TREATMENTS[kind]
        assert placement.template in templates
        assert placement.template in AUTHORED_PICKUP_TEMPLATES
    assert not any(item in (GOLD_KEY, SILVER_KEY)
                   for placement in placements
                   for _, _, item in placement.cells)


def test_reward_contract_placement_is_deterministic_on_its_rng_stream():
    assert _realize(991) == _realize(991)
    assert _realize(991)[1] != _realize(992)[1]


def test_unplaceable_and_dropped_contract_entries_fail_soft():
    honoured, placements, _ = _realize(18, blocked=True)
    assert honoured == 0
    assert placements == ()

    identities = [_identity("partial", "survivor", 0)]
    plan = SetPiecePlan(
        "partial", "secondary", (0,), ("survivor",), (),
        "survivor", "survivor",
        reward_contract=(("survivor", "cache"), ("dropped", "objective")))
    calls = []

    def place_group(items, reason, candidates, templates):
        calls.append((reason, candidates))
        return True

    assert _place_set_piece_rewards((plan,), identities, place_group) == 1
    assert len(calls) == 1
    assert calls[0][1] == [0]


def test_room_motifs_recover_the_production_plan_reward_view():
    identities = [
        _identity("command-and-control", "war-room", 0),
        _identity("command-and-control", "communications", 0),
        _identity("other-family", "ordinary", 1),
    ]

    recovered = _set_pieces_from_motifs(identities)

    assert len(recovered) == 1
    assert recovered[0].roles_for("reward_contract") == {
        "war-room": "objective",
        "communications": "resupply",
    }
    assert recovered[0].rooms_for_role("war-room") == (0,)
