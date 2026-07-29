"""Authored secret-deduction grammar and scoring coverage."""
from dataclasses import replace
import random
import unittest

from infiniwolf.config import CampaignConfig, Intensity
from infiniwolf.grid import _at, _is_floor, _reachable, _set
from infiniwolf.model import Room, SecretDetail
from infiniwolf.progression import (
    SecretDeductionDetail, _DEDUCTION_LANDMARK, _hint_secrets,
    _install_fair_secret_frame, _score_secret_deductions,
)
from infiniwolf.wl6 import FLOOR, GRID, PUSHWALL, WALL


class SecretDeductionTests(unittest.TestCase):
    def _wall_hint(self, x, y):
        tiles = [WALL] * (GRID * GRID)
        things = [0] * (GRID * GRID)
        component_of = {}
        for offset in range(-3, 4):
            _set(tiles, x, y + offset, 40)
            component_of[x - 1, y + offset] = 0
        _set(things, x, y, PUSHWALL)
        treatments = _hint_secrets(
            tiles, things, component_of, {0: (40, (34, 36))},
            random.Random(0))
        return tiles, treatments[x, y]

    def test_repeated_wall_run_has_one_deliberate_anomaly(self):
        # x+y odd selects the repeated-run grammar.
        x, y = 20, 21
        tiles, treatment = self._wall_hint(x, y)
        self.assertEqual(treatment, "repeated-wall-anomaly")
        self.assertNotEqual(_at(tiles, x, y), 40)
        self.assertTrue(all(_at(tiles, x, y + offset) == 40
                            for offset in (-2, -1, 1, 2)))

    def test_two_sided_composition_has_a_missing_counterpart(self):
        # x+y even selects the lateral incomplete-wall grammar.
        x, y = 20, 20
        tiles, treatment = self._wall_hint(x, y)
        self.assertEqual(treatment, "two-sided-incompleteness")
        self.assertEqual(_at(tiles, x, y - 2), _at(tiles, x, y))
        self.assertEqual(_at(tiles, x, y + 2), 40)

    def test_secret_rich_frame_is_symmetric_and_does_not_disconnect_room(self):
        room = Room(10, 10, 7, 7)
        tiles = [WALL] * (GRID * GRID)
        things = [0] * (GRID * GRID)
        for y in range(room.y, room.y + room.h):
            for x in range(room.x, room.x + room.w):
                _set(tiles, x, y, FLOOR)
        pushwall = room.x + room.w, room.center[1]
        _set(things, *pushwall, PUSHWALL)
        detail = SecretDetail("square", 3, 0, 0.45, pushwall)
        before = _reachable(tiles, room.center, locked_open=True)
        reserved = set()
        framed = _install_fair_secret_frame(
            tiles, things, [room], [detail], room.center, reserved, (0,))
        self.assertEqual(framed, pushwall)
        landmarks = {(index % GRID, index // GRID)
                     for index, item in enumerate(things)
                     if item == _DEDUCTION_LANDMARK}
        self.assertEqual(len(landmarks), 2)
        self.assertEqual({y for _, y in landmarks},
                         {2 * pushwall[1] - y for _, y in landmarks})
        after = _reachable(tiles, room.center, locked_open=True, blocked=landmarks)
        self.assertEqual(len(after), len(before) - 2)
        self.assertTrue(landmarks <= reserved)

    def test_score_fields_survive_generator_style_replace(self):
        detail = SecretDetail("vault", 3, 2, 0.45, (20, 20))
        scored = _score_secret_deductions(
            [detail], [(0, 1), (1, 2)], (0, 1),
            int(CampaignConfig(seed=1).secret_reward_quality), (20, 20))[0]
        self.assertIsInstance(scored, SecretDeductionDetail)
        self.assertEqual(scored.deduction_grammar, "symmetric-counterpart")
        self.assertEqual(scored.misleading_false_positives, 0)
        self.assertEqual(scored.detour_cost, 1)
        self.assertGreaterEqual(scored.deducibility_score, 0.65)
        degraded = replace(scored, hint_treatment="plain-wall")
        self.assertIsInstance(degraded, SecretDeductionDetail)
        self.assertEqual(degraded.deduction_grammar, "plain-wall")
        self.assertLess(degraded.deducibility_score, scored.deducibility_score)

    def test_high_secret_setting_meets_fair_score_threshold(self):
        config = CampaignConfig(seed=1, secrets=Intensity.HIGH)
        self.assertGreaterEqual(int(config.secrets), 4)
        detail = SecretDetail("gallery", 3, 1, 0.55, (30, 31))
        scored = _score_secret_deductions(
            [detail], [(0, 1)], (0,), int(config.secret_reward_quality),
            detail.pushwall)
        self.assertGreaterEqual(max(item.deducibility_score for item in scored), 0.65)


if __name__ == "__main__":
    unittest.main()
