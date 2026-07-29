"""Focused contracts for sector-first planned-room placement."""
import random
import unittest

from infiniwolf.geometry import _place_planned_rooms
from infiniwolf.model import FloorPlan, RoomSpec, SetPiecePlan


class SectorPackingTests(unittest.TestCase):
    def test_required_edge_changes_local_placement_without_becoming_fatal(self):
        specs = [
            RoomSpec("start", "standard", 0),
            RoomSpec("beat", "standard", 0),
            RoomSpec("climax", "anchor", 1),
            RoomSpec("exit", "standard", 1),
            RoomSpec("branch", "standard", 1),
        ]
        # Room 4's greedy parent is room 1.  The request instead names room 2,
        # proving geometry consumes required_edges rather than merely observing
        # the ordinary parent attachment.
        request = SetPiecePlan(
            "request-proof", "secondary", (2, 4), ("owner", "partner"),
            ((2, 4),), "owner", "partner")
        plan = FloorPlan(
            specs,
            [(0, 1), (1, 2), (2, 3), (1, 4), (2, 4)],
            [], (), frozenset(range(4)), set_pieces=(request,))

        placed = _place_planned_rooms(random.Random(12), plan, 5)
        by_spec = dict(zip(placed.spec_indices, placed.rooms))
        owner, partner = by_spec[2], by_spec[4]
        x_gap = max(owner.x, partner.x) - min(
            owner.x + owner.w, partner.x + partner.w)
        y_gap = max(owner.y, partner.y) - min(
            owner.y + owner.h, partner.y + partner.h)
        self.assertTrue(
            (2 <= x_gap <= 3 and y_gap <= -3)
            or (2 <= y_gap <= 3 and x_gap <= -3))


if __name__ == "__main__":
    unittest.main()
