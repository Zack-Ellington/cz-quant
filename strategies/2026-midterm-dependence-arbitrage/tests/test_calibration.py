"""Calibration: recovers known parameters, and the factor-selection rule."""

from __future__ import annotations

import numpy as np
import pytest

from strategy.calibration import (
    FACTOR_TOLERANCE,
    ChamberTarget,
    calibrate,
    chamber_loss,
    select_model,
)
from strategy.estimators import RaceProb
from strategy.factor import FactorParams, solve
from strategy.markets import AggregateMarkets, Bucket, ControlMarket, Quote, SeatMarket
from strategy.probabilities import caucus_probs
from tests.test_factor import SAFE_DEM, SAFE_REP, synthetic_probs

TRUE = FactorParams.one_factor(0.6, flip_rep=0.03, flip_dem=0.005)
TRUE_SHARE = 0.7


def _race_probs() -> dict[str, RaceProb]:
    """The synthetic world, with one Senate race that has an independent leg."""
    race_probs = {rid: RaceProb(p) for rid, p in synthetic_probs().items()}
    race_probs["senate:S00"] = RaceProb(0.05, 0.60)
    return race_probs


def _seat_market(event, pmf, edges, size):
    """Buckets priced at the true model probabilities (bid = ask = truth)."""
    bounds = [0, *edges, size + 1]
    buckets = []
    for lo, nxt in zip(bounds, bounds[1:]):
        p = float(pmf[lo:nxt].sum())
        buckets.append(Bucket(lo, nxt - 1, Quote(f"{event}-{lo}", p, p)))
    return SeatMarket(event, tuple(buckets))


def _control(event, p):
    return ControlMarket(Quote(f"{event}-D", p, p), Quote(f"{event}-R", 1 - p, 1 - p))


@pytest.fixture(scope="module")
def truth():
    race_probs = _race_probs()
    result = solve(caucus_probs(race_probs, TRUE_SHARE), TRUE)
    markets = AggregateMarkets(
        combo={k: Quote(f"C-{k}", v, v) for k, v in result.combo.items()},
        house_control=_control("CONTROLH-2026", result.p_house_dem),
        senate_control=_control("CONTROLS-2026", result.p_senate_dem),
        same_party=None,
        house_seats=_seat_market("H", result.house_pmf, range(190, 250, 5), 435),
        senate_seats=_seat_market("S", result.senate_pmf, range(45, 58), 100),
    )
    return race_probs, markets, result


@pytest.fixture(scope="module")
def fitted(truth):
    race_probs, markets, _ = truth
    return calibrate(race_probs, markets)


def test_recovers_the_true_parameters(fitted):
    one = fitted.one
    assert one.params.sigma_house == pytest.approx(TRUE.sigma_house, abs=0.03)
    assert one.params.flip_rep == pytest.approx(TRUE.flip_rep, abs=0.005)
    assert one.params.flip_dem == pytest.approx(TRUE.flip_dem, abs=0.005)
    assert one.caucus_share == pytest.approx(TRUE_SHARE, abs=0.05)
    assert one.loss < 1e-4


def test_recovers_the_held_out_combo(fitted, truth):
    _, _, result = truth
    for code, p in result.combo.items():
        assert fitted.one.result.combo[code] == pytest.approx(p, abs=0.005)


def test_one_factor_selected_when_the_truth_has_one(fitted):
    assert fitted.selected == "one"
    assert fitted.chosen is fitted.one


def test_no_swing_baseline_fits_far_worse(fitted):
    assert fitted.baseline.params.sigma_house == 0.0
    assert fitted.baseline.loss > 20 * fitted.one.loss + 1e-3


def test_loss_is_zero_at_the_truth(truth):
    _, markets, result = truth
    seats = markets.house_seats
    target = ChamberTarget(
        tuple((b.lo, b.hi) for b in seats.buckets), tuple(seats.probabilities()), result.p_house_dem
    )
    assert chamber_loss(result.house_pmf, 218, target) == pytest.approx(0.0, abs=1e-12)
    shifted = np.roll(result.house_pmf, 5)
    assert chamber_loss(shifted, 218, target) > 0.01


def test_selection_rule():
    assert select_model(0.004, 0.001)[0] == "one"  # within a cent: keep one
    assert select_model(FACTOR_TOLERANCE, 0.0)[0] == "one"  # boundary is inclusive
    assert select_model(0.03, 0.005)[0] == "two"  # one misses, two fixes it
    assert select_model(0.03, 0.04)[0] == "one"  # two does not help
    assert select_model(0.001, 0.0, "two-factor")[0] == "two"
    assert select_model(0.5, 0.0, "one-factor")[0] == "one"


def test_unknown_model_rejected(truth):
    race_probs, markets, _ = truth
    with pytest.raises(ValueError, match="unknown"):
        calibrate(race_probs, markets, model="three-factor")
