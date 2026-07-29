import random

from infiniwolf.geometry import _grand_anchor_selected


def test_grand_anchor_selection_does_not_advance_geometry_rng():
    for seed in range(40):
        rng = random.Random(seed)
        before = rng.getstate()

        _grand_anchor_selected(rng, number=4)

        assert rng.getstate() == before


def test_grand_anchor_selection_is_a_rare_tail():
    selected = sum(
        _grand_anchor_selected(random.Random(seed), number=4)
        for seed in range(1000)
    )

    # A broad statistical guard catches an accidental global anchor expansion
    # without coupling the test to CPython's exact random sequence.
    assert 70 <= selected <= 130


def test_boss_floor_keeps_its_authored_arena_and_rng_state():
    rng = random.Random(31)
    before = rng.getstate()
