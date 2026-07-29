"""Candidate selection: ranking may order valid maps, never rescue invalid ones.

generate_campaign now chooses among a pool of candidates for every setting rather
than only in novelty mode. Two properties keep that safe, and both are easy to
break with a plausible-looking refactor of the comparison key:

  * a candidate with critique flags must never outrank a clean one, however much
    more it contrasts with the floors already accepted; and
  * only candidates that survived validate_map ever reach the comparison at all,
    which is structural -- generate_map raises, so a rejected attempt is never
    appended to the pool.
"""
import unittest

from infiniwolf.campaign import _candidate_score
from infiniwolf.config import CampaignConfig, GenerationQuality
from infiniwolf.generator import _best_candidate
from infiniwolf.model import GeneratedMap, Room


def fake(number, critique, rooms=4, things=40):
    """A GeneratedMap carrying only the fields the comparison key reads."""
    return GeneratedMap(
        number=number, tiles=[], things=[1] * things, start=(0, 0),
        exit_stand=(1, 1), secret_rewards=[], seed=number,
        critique=tuple(critique),
        rooms=tuple(Room(1, 1, 4, 4) for _ in range(rooms)),
        room_concepts=tuple(f"concept-{i}" for i in range(rooms)),
        layout_signature=(f"sig-{number}",),
        enemy_tiers=(1, 1, 1))


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.config = CampaignConfig(seed=4242)

    def test_a_clean_candidate_always_beats_a_flagged_one(self):
        """Even when the flagged map would score far better on contrast.

        The flagged candidate here is given every contrast advantage: a different
        concept set, a different signature and a very different object count. It
        must still lose, because flag count dominates the key.
        """
        clean = fake(3, (), rooms=2, things=30)
        flagged = fake(3, ("no_loop", "corridor_heavy"), rooms=12, things=400)
        for pool in ([clean, flagged], [flagged, clean]):
            with self.subTest(order=[len(c.critique) for c in pool]):
                chosen = _best_candidate(pool, [c for c in pool if not c.critique],
                                         [], self.config)
                self.assertEqual(chosen.critique, ())

    def test_fewer_flags_wins_when_none_are_clean(self):
        one = fake(5, ("no_loop",))
        three = fake(5, ("no_loop", "secret_monotony", "shape_monotony"))
        chosen = _best_candidate([three, one], [], [], self.config)
        self.assertEqual(chosen.critique, ("no_loop",))

    def test_severe_defect_loses_even_with_higher_contrast(self):
        """Severity breaks the flat one-flag tie before campaign novelty."""
        previous = fake(1, (), rooms=4, things=40)
        diagnostic = fake(2, ("secret_monotony",), rooms=4, things=40)
        severe = fake(2, ("no_loop",), rooms=12, things=120)
        self.assertGreater(
            _candidate_score(severe, [previous], self.config),
            _candidate_score(diagnostic, [previous], self.config),
            "fixture must give the structurally worse map more contrast")
        chosen = _best_candidate(
            [severe, diagnostic], [], [previous], self.config)
        self.assertIs(chosen, diagnostic)

    def test_contrast_breaks_ties_between_equally_clean_candidates(self):
        """With flags equal, the accepted-floor history decides.

        A candidate repeating the previous floor's concepts must not be preferred
        over one that differs, or the pool buys nothing.
        """
        previous = fake(1, (), rooms=4)
        same = fake(2, ())
        different = fake(2, ())
        object.__setattr__(different, "room_concepts",
                           ("alpha", "beta", "gamma", "delta"))
        chosen = _best_candidate([same, different], [same, different],
                                 [previous], self.config)
        self.assertIs(chosen, different)

    def test_pool_sizes_are_ordered_and_fast_is_not_larger(self):
        sizes = [q.pool_size for q in GenerationQuality]
        self.assertEqual(sizes, sorted(sizes),
                         "a higher quality setting must not search less")
        self.assertLessEqual(GenerationQuality.FAST.pool_size,
                             GenerationQuality.THOROUGH.pool_size)


if __name__ == "__main__":
    unittest.main()
