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
        self.assertEqual(led.explain((5, 5)), [Claim("geometry", "seed")])

    def test_claims_never_outlive_their_cells(self):
        """A stale claim would make explain() lie about a freed cell."""
        led = Ledger()
        led.reserve([(1, 1)], "progression", "gate")
        led.release([(1, 1)], "progression", "done")
        self.assertEqual(led.report().get("progression", {}).get("claims", 0), 0)
        self.assertEqual(led.explain((1, 1)), [])


class ProvenanceTests(unittest.TestCase):
    def test_reserve_records_owner_and_reason(self):
        led = Ledger()
        led.reserve([(2, 3)], "progression", "pushwall-travel", room_index=4)
        self.assertEqual(led.explain((2, 3)), [
            Claim("progression", "pushwall-travel", room_index=4)])

    def test_explain_lists_claims_in_insertion_order(self):
        led = Ledger()
        led.reserve([(1, 1)], "progression", "exit-stand")
        led.reserve([(1, 1)], "decorations", "density-fill")
        self.assertEqual(led.explain((1, 1)), [
            Claim("progression", "exit-stand"),
            Claim("decorations", "density-fill"),
        ])

    def test_unattributed_writes_are_visible_rather_than_silent(self):
        led = Ledger()
        led.add((9, 9))
        self.assertEqual(led.report()["unattributed"]["claims"], 1)

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

    def test_release_removes_only_its_owners_claims(self):
        led = Ledger()
        led.reserve([(7, 7)], "geometry", "shape-anchor", hard=False)
        self.assertEqual(led.release([(7, 7)], "decorations", "reclaim"), [])
        self.assertIn((7, 7), led)

    def test_one_owner_release_keeps_another_owners_claim(self):
        led = Ledger()
        led.reserve([(8, 8)], "geometry", "anchor")
        led.reserve([(8, 8)], "decorations", "blocking-prop")
        self.assertEqual(led.release([(8, 8)], "geometry", "done"), [(8, 8)])
        self.assertIn((8, 8), led)
        self.assertEqual(led.explain((8, 8)), [
            Claim("decorations", "blocking-prop")])

    def test_cell_leaves_only_after_its_last_claim_is_released(self):
        led = Ledger()
        led.reserve([(8, 8)], "geometry", "anchor")
        led.reserve([(8, 8)], "decorations", "blocking-prop")
        led.release([(8, 8)], "geometry", "done")
        self.assertIn((8, 8), led)
        led.release([(8, 8)], "decorations", "done")
        self.assertNotIn((8, 8), led)

    def test_report_counts_claims_and_unique_cells_per_owner(self):
        led = Ledger()
        led.reserve([(1, 1), (2, 2)], "geometry", "anchor")
        led.reserve([(1, 1)], "geometry", "screen")
        report = led.report()
        self.assertEqual(report["geometry"]["claims"], 3)
        self.assertEqual(report["geometry"]["cells"], 2)

    def test_duplicate_owner_reason_claims_do_not_accumulate(self):
        led = Ledger()
        led.reserve([(3, 3)], "geometry", "anchor", hard=False)
        led.reserve([(3, 3)], "geometry", "anchor", hard=True, room_index=4)
        self.assertEqual(led.explain((3, 3)), [
            Claim("geometry", "anchor", hard=False)])


class HelperTests(unittest.TestCase):
    def test_helper_attributes_a_ledger_and_tolerates_a_plain_set(self):
        """Subsystem tests pass plain sets; production passes a ledger.

        Without this the modules could not be migrated one at a time, because a
        decoration unit test would have to build a ledger to check lamp placement.
        """
        led = Ledger()
        reserve(led, [(1, 2)], "decorations", "density-fill")
        self.assertEqual(led.explain((1, 2)), [
            Claim("decorations", "density-fill")])

        plain = set()
        reserve(plain, [(3, 4)], "decorations", "density-fill")
        self.assertEqual(plain, {(3, 4)})

    def test_claim_renders_readably(self):
        self.assertEqual(str(Claim("encounters", "patrol-route")),
                         "encounters:patrol-route")
        self.assertIn("(soft)", str(Claim("geometry", "anchor", hard=False)))


if __name__ == "__main__":
    unittest.main()
