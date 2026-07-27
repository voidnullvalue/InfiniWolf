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


class DecorPaletteIntegrityTests(unittest.TestCase):
    """A palette entry must match the registry it is placed through.

    The open commit path deliberately skips the keep_clear and reachability
    guards, because a non-solid item cannot block anything. That makes listing a
    solid prop in an open palette a correctness bug rather than a cosmetic one:
    TableChairs was listed in nine of them and a door-to-door traversal lane got
    blocked as a result.
    """

    def test_open_palettes_hold_no_solid_props(self):
        import infiniwolf.decorations as decorations
        from infiniwolf.wl6 import STATIC_BLOCKING
        for concept, items in decorations._DECOR_OPEN.items():
            for item in items:
                with self.subTest(concept=concept, item=item):
                    self.assertNotIn(
                        item, STATIC_BLOCKING,
                        f"{concept} lists solid prop {item} as open clutter; the "
                        f"open path does not check keep_clear or reachability")

    # Item 69, the spear display, is placed by three blocking palettes and has its
    # own rule in validate_map, yet appears in neither STATIC_BLOCKING nor
    # STATIC_OPEN -- and in none of the 43,122 decorations across the 207 authored
    # maps either. That is a pre-existing registry gap with a real consequence: the
    # spacing and clustering logic reads _ALL_DECOR, so a spear rack is invisible to
    # both. Resolving it needs a look in the engine, because adding it to the
    # registry changes the metrics while dropping it changes what rooms contain.
    KNOWN_REGISTRY_GAP = frozenset({69})

    def test_every_palette_item_is_a_known_decoration(self):
        import infiniwolf.decorations as decorations
        from infiniwolf.wl6 import STATIC_BLOCKING, STATIC_OPEN
        known = set(STATIC_BLOCKING) | set(STATIC_OPEN) | self.KNOWN_REGISTRY_GAP
        for table, name in ((decorations._DECOR_OPEN, "open"),
                            (decorations._DECOR_BLOCKING, "blocking")):
            for concept, items in table.items():
                for item in items:
                    with self.subTest(table=name, concept=concept, item=item):
                        self.assertIn(item, known)

    def test_the_registry_gap_has_not_grown(self):
        """If a second item drifts out of the registries, fail rather than widen."""
        import infiniwolf.decorations as decorations
        from infiniwolf.wl6 import STATIC_BLOCKING, STATIC_OPEN
        known = set(STATIC_BLOCKING) | set(STATIC_OPEN)
        used = {item for table in (decorations._DECOR_OPEN,
                                   decorations._DECOR_BLOCKING)
                for items in table.values() for item in items}
        self.assertEqual(used - known, set(self.KNOWN_REGISTRY_GAP))

    def test_blocking_palettes_hold_no_open_only_props(self):
        """The mirror case: an open-only item routed through the solid path would
        claim a reachability cost it does not have."""
        import infiniwolf.decorations as decorations
        from infiniwolf.wl6 import STATIC_BLOCKING, STATIC_OPEN
        open_only = set(STATIC_OPEN) - set(STATIC_BLOCKING)
        for concept, items in decorations._DECOR_BLOCKING.items():
            for item in items:
                with self.subTest(concept=concept, item=item):
                    self.assertNotIn(item, open_only)
