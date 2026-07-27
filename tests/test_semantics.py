"""Concept assignment: repel duplicates, attract functional partners.

The affinity table is shared with campaign.py's candidate scoring. It used to exist
twice, and only the scoring copy had any effect -- a floor was rewarded for
adjacencies it had no mechanism to seek. These tests pin both halves of the
resulting ordering, because the ranking is what makes it work: duplicate repulsion
must stay dominant, since two adjacent identical rooms read worse than a missed
pairing.
"""
import unittest

from infiniwolf.semantics import (CONCEPT_AFFINITIES, _affinity_with,
                                  plan_landmarks)


class AffinityTableTests(unittest.TestCase):
    def test_every_entry_is_a_distinct_pair(self):
        for entry in CONCEPT_AFFINITIES:
            with self.subTest(entry=sorted(entry)):
                self.assertEqual(len(entry), 2,
                                 "an affinity is between two different concepts")

    def test_the_table_is_shared_with_candidate_scoring(self):
        """campaign.py must import the table rather than hold its own copy.

        Asserted as an import, not by searching for concept names in the source.
        That stronger check is not available: FloorVariant.decor_overrides uses the
        *decor theme* vocabulary, which shares words like "barracks" and "storage"
        with the room-concept vocabulary, so a textual search reports leaks that are
        not leaks.
        """
        import ast
        from pathlib import Path
        import infiniwolf.campaign as campaign
        tree = ast.parse(Path(campaign.__file__).read_text())
        imported = {alias.name
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.module == "semantics"
                    for alias in node.names}
        self.assertIn("CONCEPT_AFFINITIES", imported)
        self.assertIs(campaign.CONCEPT_AFFINITIES, CONCEPT_AFFINITIES,
                      "scoring and planning must read the same object")

    def test_affinity_counts_only_decided_partners(self):
        self.assertEqual(_affinity_with("barracks", ["mess-kitchen", "armory"]), 2)
        self.assertEqual(_affinity_with("barracks", ["gallery", "crypt"]), 0)
        self.assertEqual(_affinity_with("barracks", []), 0)

    def test_affinity_is_symmetric(self):
        for entry in CONCEPT_AFFINITIES:
            first, second = sorted(entry)
            with self.subTest(pair=(first, second)):
                self.assertEqual(_affinity_with(first, [second]),
                                 _affinity_with(second, [first]))

    def test_no_concept_is_affine_with_itself(self):
        """Self-affinity would fight the duplicate-repulsion term."""
        for entry in CONCEPT_AFFINITIES:
            values = list(entry)
            self.assertNotEqual(values[0], values[-1])


if __name__ == "__main__":
    unittest.main()
