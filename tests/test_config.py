import ast
import unittest
from pathlib import Path

import infiniwolf.campaign as campaign

from infiniwolf.config import (CampaignConfig, Intensity, LittleEntropyMachine,
                               ThemeBias, resolve_seed)


class ConfigTests(unittest.TestCase):
    def test_numeric_seed(self):
        self.assertEqual(resolve_seed("0x2a"), 42)

    def test_text_seed_is_stable(self):
        self.assertEqual(resolve_seed("castle"), resolve_seed("castle"))

    def test_floor_subseeds_are_stable_and_distinct(self):
        config = CampaignConfig(seed=123, secrets=Intensity.HIGH)
        self.assertEqual(config.floor_seed(2), config.floor_seed(2))
        self.assertNotEqual(config.floor_seed(2), config.floor_seed(3))
        self.assertNotEqual(config.floor_seed(2), config.floor_seed(2, 1))

    def test_little_entropy_machine_is_the_named_seed_source(self):
        config = CampaignConfig(seed=123)
        source = LittleEntropyMachine(config.seed)
        self.assertEqual(config.floor_seed(2), source.floor(2))
        self.assertEqual(config.variant_seed(2), source.variant(2))
        self.assertEqual(config.lock_seed(), source.locks())
        self.assertEqual(config.circulation_seed(2), source.circulation(2))

    def test_hidden_stream_is_stable_and_separate(self):
        config = CampaignConfig(seed=123, say_aardwolf=True)
        self.assertEqual(config.aardwolf_seed(2), config.aardwolf_seed(2))
        self.assertNotEqual(config.aardwolf_seed(2), config.aardwolf_seed(3))
        self.assertNotEqual(config.aardwolf_seed(2), config.floor_seed(2))
        with self.assertRaises(ValueError):
            config.aardwolf_seed(0)

    def test_variant_seeds_are_stable_distinct_and_separate_from_floor_seeds(self):
        config = CampaignConfig(seed=123)
        self.assertEqual(config.variant_seed(2), config.variant_seed(2))
        self.assertNotEqual(config.variant_seed(2), config.variant_seed(3))
        self.assertNotEqual(config.variant_seed(2), config.floor_seed(2))
        with self.assertRaises(ValueError):
            config.variant_seed(0)

    def test_json_uses_numeric_intensities(self):
        encoded = CampaignConfig(seed=123, theme_bias=ThemeBias.CATACOMBS).to_json()
        self.assertIn('"guard_density": 3', encoded)
        self.assertIn('"decoration_amount": 3', encoded)
        self.assertIn('"theme_bias": "catacombs"', encoded)
        self.assertIn('"say_aardwolf": false', encoded)

    def test_lock_schedule_seed_is_stable_and_separate(self):
        config = CampaignConfig(seed=123)
        self.assertEqual(config.lock_seed(), CampaignConfig(seed=123).lock_seed())
        self.assertNotEqual(config.lock_seed(), config.variant_seed(1))


if __name__ == "__main__":
    unittest.main()


class CampaignScheduleTests(unittest.TestCase):
    """Campaign-scale choices must not move when a floor is re-generated.

    Every schedule in campaign.py derives from a LittleEntropyMachine stream
    that excludes `attempt`, so a floor rejected by validate_map is retried
    without its variant, circulation skeleton, progression grammar or lock quota
    shifting underneath it. That property is the reason those functions take a
    config rather than an rng, and it is easy to break silently by threading a
    floor rng into one of them.
    """

    def test_schedules_are_independent_of_retry_attempt(self):
        config = CampaignConfig(seed=20260726)
        # The schedules take no attempt argument at all, which is the structural
        # half of the guarantee; assert the seed streams they read are likewise
        # attempt-free, since that is what a future refactor could quietly undo.
        for floor in range(1, 11):
            with self.subTest(floor=floor):
                self.assertEqual(config.variant_seed(floor),
                                 config.variant_seed(floor))
                self.assertEqual(config.circulation_seed(floor),
                                 config.circulation_seed(floor))
                self.assertNotEqual(config.floor_seed(floor, 0),
                                    config.floor_seed(floor, 3),
                                    "attempt must still reroll the floor stream")
        self.assertEqual(config.lock_seed(), config.lock_seed())
        self.assertEqual(config.vine_seed(), config.vine_seed())
        self.assertEqual(config.guard_gallery_seed(), config.guard_gallery_seed())
        self.assertEqual(config.rare_motif_seed(), config.rare_motif_seed())

    def test_schedules_are_deterministic_and_respect_adjacency_rules(self):
        config = CampaignConfig(seed=20260726)
        variants = campaign._variant_sequence(config)
        skeletons = campaign._circulation_sequence(config)
        grammars = campaign._progression_sequence(config)
        self.assertEqual(len(variants), 10)
        self.assertEqual(variants, campaign._variant_sequence(config))
        self.assertEqual(skeletons, campaign._circulation_sequence(config))
        self.assertEqual(grammars, campaign._progression_sequence(config))
        # Adjacent floors must differ; these are the contracts generate_campaign
        # re-checks at the end of a run and raises RuntimeError over.
        for index, (first, second) in enumerate(zip(skeletons, skeletons[1:])):
            self.assertNotEqual(first, second, f"skeleton repeated at floor {index + 1}")
        for index, (first, second) in enumerate(zip(grammars, grammars[1:])):
            self.assertNotEqual(first, second, f"grammar repeated at floor {index + 1}")
        self.assertEqual(
            sum(s in campaign.HALLWAY_FIRST_SKELETONS for s in skeletons), 3,
            "campaign must schedule exactly three hallway-first floors")

    def test_resolved_schedule_is_frozen_and_retry_independent(self):
        """CampaignSchedule must be reproducible from the config alone.

        generate_campaign resolves it once before building any floor, so if any
        field were drawn from a floor's attempt stream a rejected floor would
        silently change the campaign's identity mid-run.
        """
        config = CampaignConfig(seed=31337)
        first = campaign.resolve_schedule(config)
        self.assertEqual(first, campaign.resolve_schedule(config))
        with self.assertRaises(Exception):
            first.vine_floor = 4          # frozen
        self.assertEqual(len(first.variants), 10)
        self.assertIn(first.secret_from, range(1, 7))
        self.assertIn(first.vine_budget, (1, 2))
        self.assertIn(first.vine_floor, range(2, 9))
        self.assertIn(first.gallery_floor, (0, *range(3, 9)))
        self.assertIn(first.rare_motif_floor, (0, 6, 7, 8, 9))
        self.assertIn(first.vista_parity, (0, 1))

    def test_candidate_scoring_cannot_rescue_an_invalid_map(self):
        """Ranking is separate from validation by construction.

        campaign.py owns _candidate_score and imports nothing that can validate,
        so a soft score has no way to mark a map acceptable. Pin that as an
        import property rather than a text search -- the module's own docstring
        mentions validate_map when explaining the separation, and a grep over
        source would fail on the prose while proving nothing about the code.
        """
        tree = ast.parse(Path(campaign.__file__).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        offenders = {name for name in imported
                     if "validat" in name or name.endswith("generator")}
        self.assertFalse(
            offenders,
            f"campaign.py must not import validation or the generator; "
            f"found {sorted(offenders)}")
        # And it must stay importable without them, which is what lets both the
        # generator and the validator read these schedules.
        self.assertEqual(
            sorted(n for n in imported if n.startswith((".", "config", "model"))
                   or n in ("config", "model")),
            ["config", "model"])


class CandidateScoreCalibrationTests(unittest.TestCase):
    """The density term must actually discriminate, not just subtract a constant.

    _candidate_score pulls object density toward a seed-varying target. If the
    target drifts far below what the generator really produces, abs(actual -
    target) becomes monotonic in object count and the term silently inverts into
    "prefer sparse", losing the tension rhythm it exists to express. That is what
    happened when the decoration overhaul tripled density and the target was left
    at its pre-overhaul value.
    """

    def test_density_target_brackets_what_the_generator_produces(self):
        for amount in (1, 3, 5):
            with self.subTest(decoration_amount=amount):
                config = CampaignConfig(seed=1, decoration_amount=Intensity(amount))
                # Reproduce the term's own target expression.
                base = 12.0 + amount * 0.79
                # Measured means were 12.77 / 14.14 / 15.94 at amounts 1 / 3 / 5.
                measured = {1: 12.77, 3: 14.14, 5: 15.94}[amount]
                self.assertLess(
                    abs(base - measured), 1.5,
                    f"target {base:.2f} has drifted from measured {measured:.2f}; "
                    f"the density term stops discriminating once it does")
                # And tension must be able to move the target either side of it,
                # or the rhythm cannot express a busier or calmer floor.
                self.assertLess(base - 1.05, measured)
                self.assertGreater(base + 1.05, measured)


class AestheticArcTests(unittest.TestCase):
    """The campaign's visual journey: bounded, seeded, and floor 9/10 pinned.

    The arc must modulate a floor without overriding it, so every band is narrow.
    That also means its effect is only visible *within* a variant -- comparing
    floor 1 to floor 8 across a campaign mostly compares a garrison to a catacomb.
    """

    def test_bands_stay_narrow_enough_to_modulate_not_override(self):
        from infiniwolf.campaign import aesthetic_phase
        config = CampaignConfig(seed=5150)
        for floor in range(1, 11):
            phase = aesthetic_phase(config, floor)
            for name in ("orderliness", "damage", "occupation",
                         "monumentality", "abandonment"):
                with self.subTest(floor=floor, field=name):
                    value = getattr(phase, name)
                    self.assertGreaterEqual(value, 0.70)
                    self.assertLessEqual(value, 1.35)

    def test_damage_rises_across_the_ordinary_campaign(self):
        from infiniwolf.campaign import aesthetic_phase
        config = CampaignConfig(seed=5150)
        damage = [aesthetic_phase(config, floor).damage for floor in range(1, 9)]
        self.assertEqual(damage, sorted(damage), f"not monotonic: {damage}")
        self.assertLess(damage[0], damage[-1])

    def test_special_floors_are_pinned_not_interpolated(self):
        """Floor 9 is the campaign's monument; floor 10 its ruin.

        Letting the curve decide would occasionally hand the stronghold a damp
        ruin and the reward expedition a pristine hall.
        """
        from infiniwolf.campaign import aesthetic_phase
        for seed in (1, 2, 3, 40, 41):
            config = CampaignConfig(seed=seed)
            nine = aesthetic_phase(config, 9)
            ten = aesthetic_phase(config, 10)
            with self.subTest(seed=seed):
                self.assertEqual(nine.monumentality, 1.30)
                self.assertEqual(nine.occupation, 1.30)
                self.assertEqual(ten.abandonment, 1.30)
                self.assertLess(ten.occupation, nine.occupation)

    def test_the_arc_is_seeded_so_runs_differ(self):
        """Two campaigns must escalate differently while both escalating."""
        from infiniwolf.campaign import aesthetic_phase
        curves = {
            seed: tuple(aesthetic_phase(CampaignConfig(seed=seed), floor).damage
                        for floor in range(1, 9))
            for seed in range(12)
        }
        self.assertGreater(len(set(curves.values())), 1,
                           "every seed produced the same curve")
        for seed, curve in curves.items():
            with self.subTest(seed=seed):
                self.assertEqual(list(curve), sorted(curve))

    def test_the_phase_is_reproducible(self):
        from infiniwolf.campaign import aesthetic_phase
        config = CampaignConfig(seed=777)
        for floor in range(1, 11):
            self.assertEqual(aesthetic_phase(config, floor),
                             aesthetic_phase(config, floor))
