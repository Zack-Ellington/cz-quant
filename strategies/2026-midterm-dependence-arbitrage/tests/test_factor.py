"""The latent-margin factor model: exactness, marginals, and what the swing does."""

from __future__ import annotations

import math

import numpy as np
import pytest

from strategy.factor import (
    FactorParams,
    chambers,
    latent_correlation,
    simulate_factor,
    solve,
)
from strategy.simulation import simulate

SAFE_DEM, SAFE_REP, PRICED_HOUSE = 188, 187, 60  # 188 + 187 + 60 = 435


def synthetic_probs(seed: int = 0) -> dict[str, float]:
    """A small synthetic world near both majority lines."""
    rng = np.random.default_rng(seed)
    probs = {f"senate:S{i:02d}": float(p) for i, p in enumerate(rng.uniform(0.05, 0.95, 35))}
    probs.update({f"house:P{i:02d}": float(p) for i, p in enumerate(rng.uniform(0.1, 0.9, PRICED_HOUSE))})
    probs.update({f"house:SAFE-D-{i:03d}": 1.0 for i in range(SAFE_DEM)})
    probs.update({f"house:SAFE-R-{i:03d}": 0.0 for i in range(SAFE_REP)})
    return probs


PROBS = synthetic_probs()


def test_chambers_split_the_races():
    house, senate = chambers(PROBS)
    assert (len(house.x), house.unpriced_dem, house.unpriced_rep, house.fixed_dem) == (60, 188, 187, 0)
    assert (len(senate.x), senate.fixed_dem, senate.threshold) == (35, 34, 51)


def test_zero_swing_is_the_independent_model():
    exact = solve(PROBS, FactorParams.one_factor(0.0))
    mc = simulate(PROBS, n=200_000, seed=3)
    for code, p in exact.combo.items():
        assert abs(p - mc.combo[code]) < 4 * mc.standard_error[code] + 1e-9


@pytest.mark.parametrize(
    "params",
    [FactorParams.one_factor(0.7, 0.02, 0.01), FactorParams(1.0, 0.4, 0.02, 0.01), FactorParams(0.3, 0.9)],
)
def test_quadrature_matches_monte_carlo(params):
    exact = solve(PROBS, params)
    mc = simulate_factor(PROBS, params, n=200_000, seed=11)
    for code in exact.combo:
        assert exact.combo[code] == pytest.approx(mc.combo[code], abs=0.01)
    assert exact.p_house_dem == pytest.approx(mc.p_house_dem, abs=0.01)
    assert exact.p_senate_dem == pytest.approx(mc.p_senate_dem, abs=0.01)


def test_race_marginals_are_preserved_under_a_large_swing():
    mc = simulate_factor(PROBS, FactorParams.one_factor(1.5), n=100_000, seed=5)
    worst = max(abs(rate - PROBS[rid]) for rid, rate in mc.race_win_rates.items())
    assert worst < 0.01


def test_outputs_are_distributions():
    r = solve(PROBS, FactorParams(0.8, 0.5, 0.03, 0.01))
    assert sum(r.combo.values()) == pytest.approx(1.0)
    for pmf in (r.house_pmf, r.senate_pmf):
        assert pmf.sum() == pytest.approx(1.0)
        assert (pmf >= 0).all()


def test_swing_keeps_the_mean_and_widens_the_distribution():
    flips = dict(flip_rep=0.03, flip_dem=0.01)
    calm = solve(PROBS, FactorParams.one_factor(0.0, **flips))
    wave = solve(PROBS, FactorParams.one_factor(1.2, **flips))
    expected_house = sum(p for k, p in PROBS.items() if k.startswith("house:P")) + SAFE_REP * 0.03 + SAFE_DEM * 0.99
    assert calm.house_moments()[0] == pytest.approx(expected_house, abs=1e-6)
    assert wave.house_moments()[0] == pytest.approx(expected_house, abs=1e-6)
    assert wave.house_moments()[1] > 2 * calm.house_moments()[1]
    assert wave.senate_moments()[0] == pytest.approx(calm.senate_moments()[0], abs=1e-6)


def test_a_shared_swing_makes_sweeps_more_likely():
    sweep = [solve(PROBS, FactorParams.one_factor(s)).combo for s in (0.0, 0.5, 1.5)]
    totals = [c["DD"] + c["RR"] for c in sweep]
    assert totals[0] < totals[1] < totals[2]


def test_parameter_helpers():
    one = FactorParams.one_factor(0.6)
    two = FactorParams(0.4, 0.8)
    assert (one.factors, two.factors) == (1, 2)
    assert two.common == 0.4
    assert two.cross_correlation == pytest.approx(0.5)
    assert one.cross_correlation == 1.0
    assert latent_correlation(1.0) == pytest.approx(0.5)
    assert latent_correlation(0.0) == 0.0
    assert math.isclose(latent_correlation(0.56), 0.56**2 / (1 + 0.56**2))
