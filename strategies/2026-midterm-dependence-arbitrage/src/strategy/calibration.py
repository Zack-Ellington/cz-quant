"""Fit the swing to the seat-count and control markets; choose one or two factors.

Targets
-------
Four markets, none of them the combo:

* the Democratic House seat-count buckets (``KXDHOUSESEATS``),
* the Democratic Senate seat-count buckets (``KXDSENATESEATS``),
* House control and Senate control (``CONTROLH`` / ``CONTROLS``).

Bucket prices are normalized midpoints; control prices are the normalized D/R
midpoints. The loss is the Kullback-Leibler divergence from each market's
distribution to the model's, summed with equal weight per market. The combo is
deliberately left out: it is what the model is tested against, so fitting to it
would make the comparison circular.

Parameters
----------
* ``sigma`` -- the swing. One factor: one sigma for both chambers. Two factors:
  ``sigma_house`` and ``sigma_senate``.
* ``flip_rep`` / ``flip_dem`` -- baseline flip chances of the House seats Kalshi
  does not price. Always fitted (House only), because without them the model
  cannot reach the upper House buckets at all.
* ``caucus_share`` -- the fraction of independent winners (Osborn, Bodnar,
  Achilles, Bengs; Hill in AK-AL) that caucus with Democrats. The seat-count
  markets count caucus members, so this moves the Senate mean. Fitted on the
  Senate, in [0, 1], and applied to every independent leg.

The flip rates and the caucus share are baseline parameters, not dependence:
they are profiled out, so for every candidate sigma the best values are found
first and sigma is judged on its best fit.

Choosing the number of factors
------------------------------
Both are fitted, and the choice is made on the calibration targets only, never
on the combo. The one-factor model is kept if it reproduces both chamber-control
markets -- the most liquid markets in the set, with one-cent spreads -- to within
``FACTOR_TOLERANCE`` (0.01). The second factor is added only if the one-factor
model misses one of them by more than that *and* the two-factor model misses by
less. A second factor that is not needed to fit the targets is not used, however
much it would change the combo: the split of the swing between a national and a
chamber-only part is not identified by single-chamber markets, so a difference it
makes is an assumption, not evidence.

The baseline fit (sigma = 0, flip rates still fitted) is kept for the report: it
shows how much of the improvement is the dependence and how much is the
unpriced seats.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize, minimize_scalar

from strategy.estimators import RaceProb
from strategy.factor import (
    OUTCOMES,
    Chamber,
    FactorParams,
    FactorResult,
    chambers,
    gauss_hermite,
    priced_pmf,
    seat_pmf,
    solve,
)
from strategy.markets import AggregateMarkets
from strategy.probabilities import caucus_probs

SIGMA_MAX = 3.0
FACTOR_TOLERANCE = 0.01
FLIP_BOUNDS = (1e-5, 0.25)
_FLOOR = 1e-300  # tail probabilities are exact, so keep their gradient


@dataclass(frozen=True)
class ChamberTarget:
    ranges: tuple[tuple[int, int], ...]
    probs: tuple[float, ...]
    control: float | None  # market P(Democratic control)


@dataclass(frozen=True)
class Targets:
    house: ChamberTarget | None
    senate: ChamberTarget | None


def targets_from_markets(markets: AggregateMarkets) -> Targets:
    def target(seats, control) -> ChamberTarget | None:
        if seats is None:
            return None
        return ChamberTarget(
            tuple((b.lo, b.hi) for b in seats.buckets),
            tuple(seats.probabilities()),
            control.p_dem(),
        )

    return Targets(
        house=target(markets.house_seats, markets.house_control),
        senate=target(markets.senate_seats, markets.senate_control),
    )


# --- Loss --------------------------------------------------------------------


def bucket_probs(pmf: np.ndarray, ranges) -> np.ndarray:
    return np.array([pmf[lo : hi + 1].sum() for lo, hi in ranges])


def chamber_loss(pmf: np.ndarray, threshold: int, target: ChamberTarget) -> float:
    """KL(market || model) over the buckets, plus the Bernoulli KL of control."""
    q = np.asarray(target.probs)
    p = np.maximum(bucket_probs(pmf, target.ranges), _FLOOR)
    mask = q > 0
    loss = float(np.sum(q[mask] * np.log(q[mask] / p[mask])))
    if target.control is not None:
        loss += _bernoulli_kl(target.control, float(pmf[threshold:].sum()))
    return loss


def _bernoulli_kl(q: float, p: float) -> float:
    p = min(max(p, _FLOOR), 1.0 - _FLOOR)
    total = 0.0
    if q > 0:
        total += q * math.log(q / p)
    if q < 1:
        total += (1.0 - q) * math.log((1.0 - q) / (1.0 - p))
    return total


# --- Chamber fits --------------------------------------------------------------


@dataclass(frozen=True)
class ChamberFit:
    sigma: float
    flip_rep: float
    flip_dem: float
    loss: float


def fit_house(
    house: Chamber,
    target: ChamberTarget,
    sigma: float,
    start: tuple[float, float] | None = None,
) -> ChamberFit:
    """Best flip rates for the House at a fixed ``sigma``.

    ``start`` warm-starts the search from nearby flip rates (the calibration
    passes the previous solution); without it three spread-out starts are tried.
    """
    u, w = gauss_hermite()
    priced = priced_pmf(house, sigma, u)

    def loss(theta: np.ndarray) -> float:
        fr, fd = _expit(theta[0]), _expit(theta[1])
        pmf = w @ seat_pmf(house, sigma, fr, fd, u, priced=priced)
        return chamber_loss(pmf, house.threshold, target)

    bounds = [(_logit(FLIP_BOUNDS[0]), _logit(FLIP_BOUNDS[1]))] * 2
    best = None
    # 1%/1%, 5%/0.25%, 0.25%/5%, 15%/15% (the last reaches the wide optimum a
    # no-swing fit needs).
    starts = [(-4.6, -4.6), (-3.0, -6.0), (-6.0, -3.0), (-1.7, -1.7)]
    if start is not None:
        starts = [(_logit(start[0]), _logit(start[1]))]
    for x0 in starts:
        res = minimize(loss, np.array(x0), method="L-BFGS-B", bounds=bounds)
        if best is None or res.fun < best.fun:
            best = res
    return ChamberFit(sigma, _expit(best.x[0]), _expit(best.x[1]), float(best.fun))


def senate_loss(senate: Chamber, target: ChamberTarget, sigma: float) -> float:
    u, w = gauss_hermite()
    pmf = w @ seat_pmf(senate, sigma, 0.0, 0.0, u)
    return chamber_loss(pmf, senate.threshold, target)


@dataclass(frozen=True)
class SenateFit:
    sigma: float
    caucus_share: float
    loss: float


def fit_senate(race_probs: dict[str, RaceProb], target: ChamberTarget, sigma: float) -> SenateFit:
    """Best caucus share for the Senate at a fixed ``sigma``."""

    def loss(share: float) -> float:
        _, senate = chambers(caucus_probs(race_probs, share))
        return senate_loss(senate, target, sigma)

    res = minimize_scalar(loss, bounds=(0.0, 1.0), method="bounded", options={"xatol": 1e-4})
    share, value = float(res.x), float(res.fun)
    for edge in (0.0, 1.0):  # the bounded method never evaluates the endpoints
        if (v := loss(edge)) < value:
            share, value = edge, v
    return SenateFit(sigma, share, value)


def _minimize_1d(f, lo: float = 0.0, hi: float = SIGMA_MAX) -> float:
    """Grid search, then a bounded Brent refine around the best grid point."""
    grid = np.linspace(lo, hi, 13)
    values = [f(x) for x in grid]
    i = int(np.argmin(values))
    a, b = grid[max(i - 1, 0)], grid[min(i + 1, len(grid) - 1)]
    res = minimize_scalar(f, bounds=(a, b), method="bounded", options={"xatol": 1e-4})
    return float(res.x) if res.fun <= values[i] else float(grid[i])


# --- Full calibration -----------------------------------------------------------


@dataclass(frozen=True)
class ModelFit:
    params: FactorParams
    caucus_share: float
    loss: float
    probs: dict[str, float]  # race probabilities at this caucus share
    result: FactorResult


@dataclass(frozen=True)
class Calibration:
    targets: Targets
    baseline: ModelFit  # sigma = 0, flip rates fitted
    one: ModelFit
    two: ModelFit
    change: float  # largest gap between one- and two-factor probabilities
    selected: str  # "one" or "two"
    reason: str  # why that model was selected

    @property
    def chosen(self) -> ModelFit:
        return self.one if self.selected == "one" else self.two


def calibrate(
    race_probs: dict[str, RaceProb], markets: AggregateMarkets, model: str = "auto"
) -> Calibration:
    """Fit the baseline, one-factor, and two-factor models; pick one.

    ``model`` is ``"auto"`` (apply the one-cent rule), ``"one-factor"``, or
    ``"two-factor"``.
    """
    if model not in ("auto", "one-factor", "two-factor"):
        raise ValueError(f"unknown factor model {model!r}")
    targets = targets_from_markets(markets)
    if targets.house is None or targets.senate is None:
        raise ValueError("calibration needs both seat-count markets")

    house_cache: dict[float, Chamber] = {}

    def house_chamber(share: float) -> Chamber:
        if share not in house_cache:
            house_cache[share] = chambers(caucus_probs(race_probs, share))[0]
        return house_cache[share]

    def senate_at(sigma: float) -> SenateFit:
        return fit_senate(race_probs, targets.senate, sigma)

    warm: list[tuple[float, float]] = []

    def house_at(sigma: float, share: float) -> ChamberFit:
        fit = fit_house(house_chamber(share), targets.house, sigma, warm[-1] if warm else None)
        warm.append((fit.flip_rep, fit.flip_dem))
        return fit

    def fitted(sigma_h: float, sigma_s: float) -> ModelFit:
        sf = senate_at(sigma_s)
        hf = house_at(sigma_h, sf.caucus_share)
        params = FactorParams(sigma_h, sigma_s, hf.flip_rep, hf.flip_dem)
        probs = caucus_probs(race_probs, sf.caucus_share)
        return ModelFit(params, sf.caucus_share, hf.loss + sf.loss, probs, solve(probs, params))

    baseline = fitted(0.0, 0.0)

    def one_factor_loss(sigma: float) -> float:
        sf = senate_at(sigma)
        return sf.loss + house_at(sigma, sf.caucus_share).loss

    sigma_one = _minimize_1d(one_factor_loss)
    one = fitted(sigma_one, sigma_one)

    sigma_s = _minimize_1d(lambda s: senate_at(s).loss)
    share = senate_at(sigma_s).caucus_share
    sigma_h = _minimize_1d(lambda s: house_at(s, share).loss)
    two = fitted(sigma_h, sigma_s)

    change = max(
        [abs(one.result.combo[k] - two.result.combo[k]) for k in OUTCOMES]
        + [
            abs(one.result.p_house_dem - two.result.p_house_dem),
            abs(one.result.p_senate_dem - two.result.p_senate_dem),
        ]
    )
    selected, reason = select_model(control_miss(one, targets), control_miss(two, targets), model)
    return Calibration(targets, baseline, one, two, change, selected, reason)


def select_model(one_miss: float, two_miss: float, model: str = "auto") -> tuple[str, str]:
    """Apply the factor rule. Returns ``("one" | "two", reason)``.

    ``one_miss`` / ``two_miss`` are each model's largest gap to the control
    markets. One factor is enough if it is within ``FACTOR_TOLERANCE``; a second
    is added only if one factor misses and two factors miss by less.
    """
    if model == "one-factor":
        return "one", "forced with --model one-factor"
    if model == "two-factor":
        return "two", "forced with --model two-factor"
    if one_miss <= FACTOR_TOLERANCE:
        return "one", f"one factor matches both control markets within {one_miss:.3f} <= {FACTOR_TOLERANCE}"
    if two_miss < one_miss:
        return "two", f"one factor misses control by {one_miss:.3f}; two factors by {two_miss:.3f}"
    return "one", f"a second factor does not reduce the control miss ({one_miss:.3f})"


def control_miss(fit: ModelFit, targets: Targets) -> float:
    """Largest gap between the model's and the market's chamber-control odds."""
    misses = []
    if targets.house is not None and targets.house.control is not None:
        misses.append(abs(fit.result.p_house_dem - targets.house.control))
    if targets.senate is not None and targets.senate.control is not None:
        misses.append(abs(fit.result.p_senate_dem - targets.senate.control))
    return max(misses, default=0.0)


def _logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def _expit(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))
