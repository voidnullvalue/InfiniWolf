"""The reservation ledger: a drop-in set that remembers why.

Adoption depended on it being behaviourally identical to `set`, since it is passed
into every placement pass. These tests pin that equivalence, and pin the one place
it deliberately differs: a hard claim cannot be released by another owner.
"""
import unittest

from infiniwolf.ledger import Claim, Ledger, reserve


class SetCompatibilityTests(unittest.TestCase):
    """Anything a pass already did to a plain set must still work."""

    def test_behaves_as_a_set_for_every_operation_placement_uses(self):
        led = Ledger()
        led.add((1, 1))
        led.update({(2, 2), (3, 3)})
        self.assertIn((1, 1), led)
        self.assertEqual(len(led), 3)
        self.assertEqual(led | {(4, 4)}, {(1, 1), (2, 2), (3, 3), (4, 4)})
        self.assertEqual(led - {(1, 1)}, {(2, 2), (3, 3)})
        self.assertEqual(led & {(2, 2)}, {(2, 2)})
        led.discard((2, 2))
        led.difference_update({(3, 3)})
        self.assertEqual(set(led), {(1, 1)})
        self.assertTrue({(1, 1)} <= led)
        led.remove((1, 1))
        self.assertFalse(led)

    def test_constructed_from_existing_cells(self):
        led = Ledger({(5, 5), (6, 6)}, owner="geometry", reason="seed")
        self.assertEqual(len(led), 2)
        self.assertIn("geometry:seed", led.explain((5, 5)))

    def test_claims_never_outlive_their_cells(self):
        """A stale claim would make explain() lie about a freed cell."""
        led = Ledger()
        led.reserve([(1, 1)], "progression", "gate")
        led.discard((1, 1))
        self.assertEqual(led.report()["progression"], 0)
        self.assertIn("not reserved", led.explain((1, 1)))


class ProvenanceTests(unittest.TestCase):
    def test_reserve_records_owner_and_reason(self):
        led = Ledger()
        led.reserve([(2, 3)], "progression", "pushwall-travel", room_index=4)
        self.assertIn("progression:pushwall-travel room 4", led.explain((2, 3)))

    def test_first_writer_wins(self):
        """Whoever got there first is why the cell is unavailable.

        A second claim adds nothing to the set, so it must not rewrite history --
        otherwise the diagnostic names the pass that merely tried, not the one
        actually holding the cell.
        """
        led = Ledger()
        led.reserve([(1, 1)], "progression", "exit-stand")
        led.reserve([(1, 1)], "decorations", "density-fill")
        self.assertIn("progression:exit-stand", led.explain((1, 1)))

    def test_unattributed_writes_are_visible_rather_than_silent(self):
        led = Ledger()
        led.add((9, 9))
        self.assertEqual(led.report()["unattributed"], 1)

    def test_hard_claims_resist_release_by_another_owner(self):
        """The invariant that makes the ledger more than bookkeeping.

        Decoration must not be able to free a progression reservation. This is the
        shape of the bug where a repair pass moved one member of a matched pair:
        nothing recorded that the position was owned.
        """
        led = Ledger()
        led.reserve([(4, 4)], "progression", "secret-footprint")
        self.assertEqual(led.release([(4, 4)], "decorations", "wants-the-cell"), [])
        self.assertIn((4, 4), led)
        # Its own owner may release it.
        self.assertEqual(led.release([(4, 4)], "progression", "done"), [(4, 4)])
        self.assertNotIn((4, 4), led)

    def test_soft_claims_are_releasable_by_anyone(self):
        led = Ledger()
        led.reserve([(7, 7)], "geometry", "shape-anchor", hard=False)
        self.assertEqual(led.release([(7, 7)], "decorations", "reclaim"), [(7, 7)])


class HelperTests(unittest.TestCase):
    def test_helper_attributes_a_ledger_and_tolerates_a_plain_set(self):
        """Subsystem tests pass plain sets; production passes a ledger.

        Without this the modules could not be migrated one at a time, because a
        decoration unit test would have to build a ledger to check lamp placement.
        """
        led = Ledger()
        reserve(led, [(1, 2)], "decorations", "density-fill")
        self.assertIn("decorations:density-fill", led.explain((1, 2)))

        plain = set()
        reserve(plain, [(3, 4)], "decorations", "density-fill")
        self.assertEqual(plain, {(3, 4)})

    def test_claim_renders_readably(self):
        self.assertEqual(str(Claim("encounters", "patrol-route")),
                         "encounters:patrol-route")
        self.assertIn("(soft)", str(Claim("geometry", "anchor", hard=False)))


if __name__ == "__main__":
    unittest.main()
