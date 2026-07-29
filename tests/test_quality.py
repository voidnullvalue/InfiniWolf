"""Soft critique metrics: deterministic, bounded, and able to fire.

Each flag here is a diagnosis a reader can act on, so each gets a case that makes
it fire and a case that does not. A metric that can never fire is worse than no
metric -- it looks like coverage while measuring nothing, which is how the
pre-existing set-based contrast scoring came to treat a floor with one armory and
a floor with five as identical.
"""
import unittest

from infiniwolf.model import (EncounterPlacement, GeneratedMap, Room, SecretDetail,
                              SpritePlacement)
from infiniwolf.quality import (UNMEASURED, _critique, quality_report,
                                    weighted_distance)
from infiniwolf.wl6 import FLOOR, GRID, WALL


def level(**kwargs):
    """A GeneratedMap with real planes but no interesting geometry.

    _critique opens with a flood fill, so the planes must be full size. A single
    small floor patch keeps the topology flags (no_loop, no_anchor and friends)
    from interfering with the sequence flags under test -- those fire on the
    tiles, while the flags here read the room and route metadata.
    """
    tiles = [WALL] * (GRID * GRID)
    for y in range(10, 16):
        for x in range(10, 16):
            tiles[y * GRID + x] = FLOOR
    base = dict(number=3, tiles=tiles, things=[0] * (GRID * GRID),
                start=(11, 11), exit_stand=(14, 14), secret_rewards=[], seed=1)
    base.update(kwargs)
    return GeneratedMap(**base)


class WeightedDistanceTests(unittest.TestCase):
    def test_bounded_and_zero_for_equal_inputs(self):
        for a, b in (((), ()), (("x",), ("x",)), (("x", "x"), ("x", "x"))):
            self.assertEqual(weighted_distance(a, b), 0.0)
        for a, b in ((("x",), ("y",)), ((), ("y",))):
            self.assertEqual(weighted_distance(a, b), 1.0)

    def test_sees_frequency_where_set_distance_cannot(self):
        """One armory versus five is the case that motivated this metric."""
        few, many = ("armory", "hall"), ("armory",) * 5 + ("hall",)
        self.assertGreater(weighted_distance(few, many), 0.5)
        # A set-based comparison would call these identical.
        self.assertEqual(set(few), set(many))

    def test_symmetric(self):
        a, b = ("x", "x", "y"), ("y", "z")
        self.assertEqual(weighted_distance(a, b), weighted_distance(b, a))


class QualityReportTests(unittest.TestCase):
    SCALARS = (
        "spatial_composition", "route_quality", "navigational_legibility",
        "encounter_quality", "pacing_quality", "secret_quality",
        "landmark_quality", "corpus_similarity", "campaign_contrast",
    )

    def test_report_is_bounded_and_unmeasured_fields_are_honest(self):
        sample = level(rooms=(Room(10, 10, 6, 6),),
                       critical_route=(0,), room_concepts=("hall",),
                       room_shapes=("rectangle",), room_districts=(0,))
        report = quality_report(sample, [], object(), campaign_contrast=4.0)
        for name in self.SCALARS:
            with self.subTest(metric=name):
                self.assertGreaterEqual(getattr(report, name), 0.0)
                self.assertLessEqual(getattr(report, name), 1.0)
        self.assertEqual(report.encounter_quality, UNMEASURED)
        self.assertEqual(report.pacing_quality, UNMEASURED)
        self.assertEqual(report.campaign_contrast, 1.0)
        self.assertEqual(report.severe_defects, ("no_loop",))
        self.assertIn("no_loop", report.diagnostics)

    def test_report_is_deterministic_and_does_not_read_config(self):
        sample = level(rooms=(Room(10, 10, 6, 6),),
                       critical_route=(0,), room_concepts=("hall",),
                       room_shapes=("rectangle",), room_districts=(0,))
        first = quality_report(sample, [level()], object())
        second = quality_report(sample, [], {"different": "config"})
        self.assertEqual(first, second)


class CritiqueFlagTests(unittest.TestCase):
    """Each new flag: one map that trips it, one that does not."""

    def test_concept_monotony_fires_on_a_repetitive_route(self):
        same = level(critical_route=tuple(range(6)),
                     room_concepts=("armory",) * 6,
                     room_shapes=("rectangle",) * 6)
        varied = level(critical_route=tuple(range(6)),
                       room_concepts=("armory", "barracks", "crypt", "hall",
                                      "storage", "war-room"),
                       room_shapes=("rectangle",) * 6)
        self.assertIn("concept_monotony", _critique(same))
        self.assertNotIn("concept_monotony", _critique(varied))

    def test_shape_monotony_needs_near_total_repetition(self):
        flat = level(critical_route=tuple(range(6)),
                     room_concepts=tuple(f"c{i}" for i in range(6)),
                     room_shapes=("rectangle",) * 6)
        mixed = level(critical_route=tuple(range(6)),
                      room_concepts=tuple(f"c{i}" for i in range(6)),
                      room_shapes=("rectangle", "mirrored-notch", "rectangle",
                                   "chamfer", "rectangle", "offset-bay"))
        self.assertIn("shape_monotony", _critique(flat))
        self.assertNotIn("shape_monotony", _critique(mixed))

    def test_flat_area_rhythm_fires_when_every_room_is_the_same_size(self):
        boxes = tuple(Room(0, 0, 7, 7) for _ in range(6))
        varied = (Room(0, 0, 5, 5), Room(0, 0, 12, 10), Room(0, 0, 6, 7),
                  Room(0, 0, 13, 12), Room(0, 0, 5, 6), Room(0, 0, 9, 9))
        common = dict(critical_route=tuple(range(6)),
                      room_concepts=tuple(f"c{i}" for i in range(6)),
                      room_shapes=tuple(f"s{i}" for i in range(6)))
        self.assertIn("flat_area_rhythm", _critique(level(rooms=boxes, **common)))
        self.assertNotIn("flat_area_rhythm", _critique(level(rooms=varied, **common)))

    def test_dead_end_unrewarded_fires_only_for_barren_branches(self):
        rooms = tuple(Room(0, 0, 6, 6) for _ in range(5))
        # Rooms 3 and 4 hang off room 1 and are not on the critical route.
        edges = ((0, 1), (1, 2), (1, 3), (1, 4))
        barren = level(rooms=rooms, edges=edges, critical_route=(0, 1, 2),
                       room_concepts=tuple(f"c{i}" for i in range(5)))
        self.assertIn("dead_end_unrewarded", _critique(barren))

        paid = level(rooms=rooms, edges=edges, critical_route=(0, 1, 2),
                     room_concepts=tuple(f"c{i}" for i in range(5)),
                     pickup_placements=(SpritePlacement("supply", "wall-cache", 3,
                                                        ((0, 0, 24),)),),
                     encounters=(EncounterPlacement("sentry", 4, ((1, 1, 180),)),))
        self.assertNotIn("dead_end_unrewarded", _critique(paid))

    def test_a_secret_counts_as_a_dead_end_payoff(self):
        rooms = tuple(Room(0, 0, 6, 6) for _ in range(5))
        edges = ((0, 1), (1, 2), (1, 3), (1, 4))
        detail = SecretDetail("vault", 3, 3, 0.8, (2, 2))
        with_secret = level(
            rooms=rooms, edges=edges, critical_route=(0, 1, 2),
            room_concepts=tuple(f"c{i}" for i in range(5)),
            secret_details=(detail,),
            encounters=(EncounterPlacement("sentry", 4, ((1, 1, 180),)),))
        self.assertNotIn("dead_end_unrewarded", _critique(with_secret))

    def test_thresholds_sit_inside_the_measured_range(self):
        """A threshold outside the observed range is a flag that cannot fire.

        All four sequence flags shipped once with thresholds that no real floor
        could reach -- shape monotony at 0.80 against a measured maximum of 0.50,
        and flat area rhythm at 0.45 against a measured *minimum* of 1.27. They
        read as coverage while measuring nothing. These bounds come from 70
        generated floors across seven seeds; if the generator's behaviour moves far
        enough that they no longer bracket it, this fails and the thresholds get
        re-measured rather than quietly going dead again.
        """
        # (name, threshold, comparison, measured p50, measured extreme)
        active = [
            ("shape_monotony", 0.40, "above", 0.20, 0.50),
            ("flat_area_rhythm", 1.40, "below", 1.91, 1.27),
        ]
        for name, threshold, sense, median, extreme in active:
            with self.subTest(flag=name):
                if sense == "above":
                    self.assertGreater(threshold, median,
                                       "would fire on a median floor")
                    self.assertLess(threshold, extreme,
                                    "unreachable: above the measured maximum")
                else:
                    self.assertLess(threshold, median,
                                    "would fire on a median floor")
                    self.assertGreater(threshold, extreme,
                                       "unreachable: below the measured minimum")

    def test_tripwire_flags_stay_silent_on_a_healthy_route(self):
        """concept_monotony and dead_end_unrewarded are regression guards.

        Both measured zero across the sample, so they are deliberately set above
        the generator's ceiling. This test states that expectation, so that a
        future change making them fire routinely is a signal to investigate the
        generator rather than to loosen the threshold.
        """
        rooms = tuple(Room(0, 0, 6 + index, 6 + index) for index in range(6))
        healthy = level(
            rooms=rooms, edges=((0, 1), (1, 2), (2, 3), (3, 4), (4, 5)),
            critical_route=tuple(range(6)),
            room_concepts=("checkpoint", "barracks", "armory", "hall",
                           "war-room", "storage"),
            room_shapes=("rectangle", "chamfer", "rectangle", "mirrored-notch",
                         "rectangle", "offset-bay"))
        flags = _critique(healthy)
        self.assertNotIn("concept_monotony", flags)
        self.assertNotIn("dead_end_unrewarded", flags)

    def test_critique_is_deterministic(self):
        sample = level(critical_route=tuple(range(6)),
                       room_concepts=("armory",) * 6,
                       room_shapes=("rectangle",) * 6,
                       rooms=tuple(Room(0, 0, 7, 7) for _ in range(6)))
        self.assertEqual(_critique(sample), _critique(sample))


if __name__ == "__main__":
    unittest.main()
