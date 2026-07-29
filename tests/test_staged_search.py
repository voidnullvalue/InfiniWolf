"""Determinism and ownership contracts for staged campaign search."""

from unittest import mock

from infiniwolf.config import CampaignConfig
from infiniwolf.generator import (
    _geometry_candidate,
    _plan_candidate,
    generate_map,
)


def test_stage_a_is_abstract_and_deterministic():
    config = CampaignConfig.with_seed("stage-a")
    with mock.patch("infiniwolf.generator._place_planned_rooms") as place:
        first = _plan_candidate(
            config, 4, 7, rare_motif_enabled=False, boss=None)
        second = _plan_candidate(
            config, 4, 7, rare_motif_enabled=False, boss=None)
    place.assert_not_called()
    assert first.plan == second.plan
    assert first.score == second.score
    assert first.attempt == 7


def test_candidates_use_explicit_attempt_streams():
    config = CampaignConfig.with_seed("candidate-streams")
    first = _plan_candidate(
        config, 5, 3, rare_motif_enabled=False, boss=None)
    second = _plan_candidate(
        config, 5, 4, rare_motif_enabled=False, boss=None)
    assert first.attempt != second.attempt
    assert first.plan != second.plan


def test_stage_b_probe_does_not_mutate_checkpoint():
    config = CampaignConfig.with_seed("stage-b")
    planned = _plan_candidate(
        config, 3, 2, rare_motif_enabled=False, boss=None)
    geometry = _geometry_candidate(config, 3, planned, boss=None)
    rooms = tuple(geometry.placed.rooms)
    edges = tuple(geometry.placed.edges)
    indices = tuple(geometry.placed.spec_indices)

    repeated = _geometry_candidate(config, 3, planned, boss=None)

    assert tuple(geometry.placed.rooms) == rooms
    assert tuple(geometry.placed.edges) == edges
    assert tuple(geometry.placed.spec_indices) == indices
    assert geometry == repeated
    assert geometry.score[0] in (0.0, 1.0)


def test_checkpoint_resume_matches_direct_attempt():
    config = CampaignConfig.with_seed("checkpoint")
    planned = _plan_candidate(
        config, 3, 1, rare_motif_enabled=False, boss=None)
    geometry = _geometry_candidate(config, 3, planned, boss=None)

    direct = generate_map(config, 3, 1)
    resumed = generate_map(
        config, 3, 1, _plan=geometry.plan, _placed=geometry.placed,
        _geometry_state=geometry.geometry_state)

    assert resumed.tiles == direct.tiles
    assert resumed.things == direct.things
    assert resumed.layout_signature == direct.layout_signature
    assert resumed.critique == direct.critique
