"""Plan invariants, asserted without generating a single tile.

This is the payoff of separating planning from realization: every property below
used to require a full generate_map call at several seconds each, and could only
be observed indirectly through the finished map. Here they are checked directly
against the plan, in milliseconds, which also means a violation names the plan
rule that broke rather than whichever downstream pass first noticed.
"""
import random
import unittest

import infiniwolf.campaign as campaign
import infiniwolf.planning as planning


def plan(seed, complexity=3, number=1, **kwargs):
    return planning._plan_floor(random.Random(seed), complexity, number, **kwargs)


class PlanningTests(unittest.TestCase):
    SEEDS = range(40)

    def test_every_plan_has_exactly_one_anchor(self):
        """One dominant room per floor, or spatial hierarchy collapses.

        _critique flags flat_hierarchy after the fact; the plan is where the
        guarantee belongs. The hub motif promotes a middle beat to anchor and
        must demote the climax to compensate, which is the case most likely to
        produce two.
        """
        for seed in self.SEEDS:
            for number in (1, 5, 9, 10):
                with self.subTest(seed=seed, floor=number):
                    specs = plan(seed, number=number).specs
                    anchors = [s for s in specs if s.tier == "anchor"]
                    self.assertEqual(
                        len(anchors), 1,
                        f"{len(anchors)} anchors: {[s.role for s in anchors]}")
            with self.subTest(seed=seed, floor="9-no-elevator"):
                # The arena moves to the end of the spine when the boss ends the
                # campaign himself, taking the anchor tier with it.
                specs = plan(seed, number=9, boss_ends_floor=True).specs
                anchors = [s for s in specs if s.tier == "anchor"]
                self.assertEqual(len(anchors), 1)
                self.assertEqual(anchors[0].role, "boss-arena")

    def test_plan_graph_is_connected_and_start_is_room_zero(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                p = plan(seed)
                self.assertEqual(p.specs[0].role, "start")
                links = {i: set() for i in range(len(p.specs))}
                for a, b in p.edges:
                    links[a].add(b); links[b].add(a)
                seen, queue = {0}, [0]
                while queue:
                    for nxt in links[queue.pop()] - seen:
                        seen.add(nxt); queue.append(nxt)
                self.assertEqual(
                    len(seen), len(p.specs),
                    f"{len(p.specs) - len(seen)} rooms unreachable in the plan graph")

    def test_mandatory_route_reaches_an_exit(self):
        for seed in self.SEEDS:
            for number in (1, 5, 9, 10):
                with self.subTest(seed=seed, floor=number):
                    p = plan(seed, number=number)
                    roles = [s.role for s in p.specs]
                    self.assertIn("exit", roles)
                    self.assertTrue(p.critical, "plan recorded no critical spine")
                    self.assertIn(roles.index("exit"), p.critical,
                                  "the exit is not on the mandatory route")
            with self.subTest(seed=seed, floor="9-no-elevator"):
                # A floor 9 whose boss ends the game has no elevator to reach:
                # the arena is the terminus and must be the mandatory route's
                # last room, with nothing planned past it.
                p = plan(seed, number=9, boss_ends_floor=True)
                roles = [s.role for s in p.specs]
                self.assertNotIn("exit", roles)
                self.assertNotIn("victory", roles)
                terminus = roles.index("boss-arena")
                self.assertIn(terminus, p.critical)
                # The spine chain stops here. Optional rooms may still open off
                # the arena -- they are grabbed during the fight, not after it --
                # but no further mandatory beat follows.
                self.assertNotIn((terminus, terminus + 1), p.edges)

    def test_special_floors_carry_their_authored_spine(self):
        """Floor 9 must plan a boss arena, floor 10 a premium vault.

        These are contracts, not tendencies -- generate_map raises if the arena
        is missing, so a plan that omits it wastes fifty attempts.
        """
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                nine = [s.role for s in plan(seed, number=9).specs]
                self.assertIn("boss-arena", nine)
                self.assertIn("victory", nine)
                terminal = [s.role for s in
                            plan(seed, number=9, boss_ends_floor=True).specs]
                self.assertIn("boss-arena", terminal)
                self.assertIn("staging", terminal)
                ten = [s.role for s in plan(seed, number=10).specs]
                self.assertIn("premium-vault", ten)
                self.assertIn("recovery", ten)

    def test_corridor_tier_rooms_are_never_dead_ends(self):
        """A hallway with one connection is a hallway to nowhere."""
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                p = plan(seed)
                degree = {i: 0 for i in range(len(p.specs))}
                for a, b in p.edges:
                    degree[a] += 1; degree[b] += 1
                for index, spec in enumerate(p.specs):
                    if spec.tier == "corridor":
                        self.assertGreaterEqual(
                            degree[index], 2,
                            f"corridor room {index} has degree {degree[index]}")

    def test_complexity_scales_planned_room_count_monotonically(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                counts = [len(plan(seed, complexity=c).specs) for c in range(1, 6)]
                self.assertEqual(sorted(counts), counts,
                                 f"room count not monotonic in complexity: {counts}")
                self.assertLessEqual(max(counts), 24, "exceeded the 24-room ceiling")

    def test_planning_is_deterministic_for_a_seed(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                first, second = plan(seed), plan(seed)
                self.assertEqual([s.role for s in first.specs],
                                 [s.role for s in second.specs])
                self.assertEqual(first.edges, second.edges)
                self.assertEqual(first.motifs, second.motifs)

    def test_declared_vocabularies_are_the_only_ones_realized(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                p = plan(seed, skeleton="bent-spine")
                self.assertIn(p.skeleton, campaign.CIRCULATION_SKELETONS)
                self.assertIn(p.progression_grammar, campaign.PROGRESSION_GRAMMARS)
                for mode in p.district_circulation:
                    self.assertIn(mode, campaign.CIRCULATION_MODES)

    def test_hallway_first_skeletons_plan_at_least_three_corridors(self):
        for skeleton in sorted(campaign.HALLWAY_FIRST_SKELETONS):
            for seed in range(12):
                with self.subTest(skeleton=skeleton, seed=seed):
                    p = plan(seed, skeleton=skeleton)
                    corridors = sum(s.tier == "corridor" for s in p.specs)
                    self.assertGreaterEqual(
                        corridors, 3,
                        f"{skeleton} planned only {corridors} corridor rooms")


if __name__ == "__main__":
    unittest.main()
