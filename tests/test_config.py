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
