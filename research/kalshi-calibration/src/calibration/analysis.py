"""Price paths, touch and snapshot observations, and the significance tests.

Price
-----
The hourly midpoint of the YES bid and ask at the close of each candle. Hours with
no two-sided quote, or a spread wider than `max_spread`, are skipped: an empty book
shows bid 0 / ask 1, whose midpoint of 50% is not a forecast.

Touch
-----
For a threshold X >= 50%, the market "touched X" at the first hour its midpoint was
at or above X; for X < 50%, at or below X. The forecast recorded for that touch is
the midpoint at that hour, not X. Hourly sampling can overshoot (a price that jumps
from 60% to 97% "touches" 90% at 97%), and using the observed price keeps the
comparison fair. If prices are calibrated, the win rate among touches equals the
mean recorded price (optional stopping for a martingale).

Snapshot
--------
The last midpoint at or before `close - horizon`, provided it is no older than
`max_age`. Bucketed by price.

Tests (two-sided, level alpha)
------------------------------
* Bucket test: one-sample t-test that the mean of (outcome - price) is zero.
  Holm's step-down adjustment across the buckets of a table is reported too,
  because a table of ~19 tests at 2% will throw up false positives by chance.
* Slope test: OLS of outcome on price with heteroskedasticity-robust (HC1)
  standard errors; t-test that the slope is 1. Since each event contributes a
  randomly chosen side, the overall mean error is ~0 by construction, and the
  slope is the test that picks up systematic over/under-confidence such as the
  favorite-longshot bias (slope > 1: favorites under-priced, longshots over-priced).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np
from scipy import stats

THRESHOLDS = [round(0.05 * i, 2) for i in range(1, 20)]  # 5% .. 95%
BUCKETS = [(round(0.1 * i, 1), round(0.1 * (i + 1), 1)) for i in range(10)]
HOUR = 3600


def mids(candles: Sequence[Sequence[float | None]], max_spread: float = 0.10,
         until: int | None = None) -> list[tuple[int, float]]:
    out = []
    for row in candles:
        t, bid, ask = int(row[0]), row[1], row[2]
        if until is not None and t > until:
            break
        if bid is None or ask is None or ask <= bid - 1e-9 or ask - bid > max_spread + 1e-9:
            continue
        if bid <= 0 and ask >= 1:
            continue
        out.append((t, (bid + ask) / 2))
    return out


def touches(path: list[tuple[int, float]], thresholds: Sequence[float] = THRESHOLDS
            ) -> dict[float, tuple[int, float]]:
    """threshold -> (time, price) of the first touch."""
    found: dict[float, tuple[int, float]] = {}
    for t, p in path:
        for x in thresholds:
            if x in found:
                continue
            if (x >= 0.5 and p >= x - 1e-9) or (x < 0.5 and p <= x + 1e-9):
                found[x] = (t, p)
        if len(found) == len(thresholds):
            break
    return found


def snapshot(path: list[tuple[int, float]], at: int, max_age: int) -> float | None:
    last = None
    for t, p in path:
        if t > at:
            break
        last = (t, p)
    if last is None or at - last[0] > max_age:
        return None
    return last[1]


def bucket_of(p: float) -> int:
    return min(int(p * 10 + 1e-9), 9)


@dataclass
class TTest:
    n: int
    mean_price: float
    win_rate: float
    diff: float          # win_rate - mean_price
    ci_low: float        # (1 - alpha) CI for diff
    ci_high: float
    t: float
    p: float
    p_holm: float = math.nan

    def significant(self, alpha: float, adjusted: bool = False) -> bool:
        v = self.p_holm if adjusted else self.p
        return not math.isnan(v) and v < alpha


def ttest(y: Sequence[float], p: Sequence[float], alpha: float) -> TTest:
    y_arr, p_arr = np.asarray(y, float), np.asarray(p, float)
    n = len(y_arr)
    if n == 0:
        return TTest(0, math.nan, math.nan, math.nan, math.nan, math.nan, math.nan, math.nan)
    d = y_arr - p_arr
    mean = float(d.mean())
    if n < 2 or d.std(ddof=1) == 0:
        return TTest(n, float(p_arr.mean()), float(y_arr.mean()), mean, math.nan, math.nan, math.nan, math.nan)
    se = float(d.std(ddof=1) / math.sqrt(n))
    t = mean / se
    pval = float(2 * stats.t.sf(abs(t), n - 1))
    q = float(stats.t.ppf(1 - alpha / 2, n - 1))
    return TTest(n, float(p_arr.mean()), float(y_arr.mean()), mean, mean - q * se, mean + q * se, t, pval)


def holm(tests: list[TTest]) -> None:
    """Fill p_holm in place (Holm-Bonferroni step-down)."""
    valid = sorted((t for t in tests if not math.isnan(t.p)), key=lambda t: t.p)
    m = len(valid)
    running = 0.0
    for i, t in enumerate(valid):
        running = max(running, min(1.0, (m - i) * t.p))
        t.p_holm = running


@dataclass
class SlopeTest:
    n: int
    intercept: float
    slope: float
    se: float
    t: float      # (slope - 1) / se
    p: float
    brier: float


def slope_test(y: Sequence[float], p: Sequence[float]) -> SlopeTest:
    y_arr, p_arr = np.asarray(y, float), np.asarray(p, float)
    n = len(y_arr)
    if n < 3 or np.ptp(p_arr) == 0:
        return SlopeTest(n, math.nan, math.nan, math.nan, math.nan, math.nan, math.nan)
    X = np.column_stack([np.ones(n), p_arr])
    beta, *_ = np.linalg.lstsq(X, y_arr, rcond=None)
    resid = y_arr - X @ beta
    xtx_inv = np.linalg.inv(X.T @ X)
    meat = X.T @ (X * (resid ** 2)[:, None])
    cov = xtx_inv @ meat @ xtx_inv * n / (n - 2)   # HC1
    se = float(math.sqrt(cov[1, 1]))
    t = float((beta[1] - 1) / se)
    pval = float(2 * stats.t.sf(abs(t), n - 2))
    brier = float(np.mean((p_arr - y_arr) ** 2))
    return SlopeTest(n, float(beta[0]), float(beta[1]), se, t, pval, brier)
