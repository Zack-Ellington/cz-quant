"""The Monte Carlo engine: determinism, a valid distribution, tight error."""

from __future__ import annotations

import math

from strategy.simulation import OUTCOMES, simulate

# A tiny hand-built world: one 50/50 Senate race and one 50/50 House race, so
# the baselines decide the rest. Enough to exercise the mechanics deterministically.
PROBS = {
    "senate:A": 0.5,
    "senate:B": 0.9,
    "house:A": 0.5,
    "house:B": 0.7,
    "house:SAFE-D-000": 1.0,
    "house:SAFE-R-000": 0.0,
}


def test_same_seed_is_deterministic():
    a = simulate(PROBS, n=50_000, seed=7)
    b = simulate(PROBS, n=50_000, seed=7)
    assert a.combo == b.combo
    assert a.p_house_dem == b.p_house_dem


def test_outcomes_sum_to_one():
    result = simulate(PROBS, n=50_000, seed=1)
    assert set(result.combo) == set(OUTCOMES)
    assert math.isclose(sum(result.combo.values()), 1.0, abs_tol=1e-9)


def test_standard_error_below_threshold_at_100k():
    result = simulate(PROBS, n=100_000, seed=1)
    assert max(result.standard_error.values()) < 0.002


def test_different_seeds_agree_within_a_few_standard_errors():
    a = simulate(PROBS, n=100_000, seed=1)
    b = simulate(PROBS, n=100_000, seed=2)
    for code in OUTCOMES:
        se = math.hypot(a.standard_error[code], b.standard_error[code])
        assert abs(a.combo[code] - b.combo[code]) < 5 * se + 1e-9


def test_certain_races_are_not_random():
    # With everything certain, the outcome is a point mass and seed-independent.
    certain = {
        "senate:A": 1.0,
        "house:A": 1.0,
        "house:SAFE-D-000": 1.0,
    }
    a = simulate(certain, n=10_000, seed=1)
    b = simulate(certain, n=10_000, seed=999)
    assert a.combo == b.combo
