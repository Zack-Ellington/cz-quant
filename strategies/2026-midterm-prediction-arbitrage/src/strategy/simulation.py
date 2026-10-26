"""Monte Carlo over the races to a distribution across the four control outcomes.

Each simulation draws every race independently from its Democratic win
probability, adds the seats that are not on the ballot (the Senate baseline) and
the safe House seats, applies the control rules, and buckets the run into one of
DD / DR / RD / RR. Averaging over ``n`` runs gives the model's probability for
each outcome.

Races are assumed independent. That is the model's main simplification: a real
national swing correlates races, so the true tails (a sweep either way) are
fatter than this produces. The independence assumption is what the arbitrage
table is implicitly testing against the market.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from strategy.constants import HOUSE_MAJORITY, SENATE_DEM_CONTROL, SENATE_NOT_UP_DEM

OUTCOMES = ("DD", "DR", "RD", "RR")


@dataclass(frozen=True)
class SimulationResult:
    """Outcome of a Monte Carlo run.

    ``combo`` is the ``{DD, DR, RD, RR}`` probability dict (it sums to 1).
    ``standard_error`` gives the Monte Carlo standard error of each of those
    probabilities. ``p_house_dem`` / ``p_senate_dem`` are the single-chamber
    Democratic-control marginals.
    """

    combo: dict[str, float]
    standard_error: dict[str, float]
    p_house_dem: float
    p_senate_dem: float
    n: int


def simulate(probs: dict[str, float], n: int = 100_000, seed: int | None = None) -> SimulationResult:
    """Run ``n`` simulations over ``probs`` and return the outcome distribution.

    ``probs`` maps ``race_id`` (prefixed ``senate:`` or ``house:``) to a
    Democratic win probability. Chambers are split on that prefix. Draws are made
    in a fixed, sorted race order so a given ``seed`` always yields the same
    result.
    """
    senate = _sorted_probs(probs, "senate:")
    house = _sorted_probs(probs, "house:")
    rng = np.random.default_rng(seed)

    senate_dem = SENATE_NOT_UP_DEM + _draw_seat_sums(rng, senate, n)
    house_dem = _draw_seat_sums(rng, house, n)

    senate_d = senate_dem >= SENATE_DEM_CONTROL
    house_d = house_dem >= HOUSE_MAJORITY

    counts = {
        "DD": int(np.count_nonzero(house_d & senate_d)),
        "DR": int(np.count_nonzero(house_d & ~senate_d)),
        "RD": int(np.count_nonzero(~house_d & senate_d)),
        "RR": int(np.count_nonzero(~house_d & ~senate_d)),
    }
    combo = {k: counts[k] / n for k in OUTCOMES}
    standard_error = {k: math.sqrt(p * (1.0 - p) / n) for k, p in combo.items()}
    return SimulationResult(
        combo=combo,
        standard_error=standard_error,
        p_house_dem=float(np.count_nonzero(house_d) / n),
        p_senate_dem=float(np.count_nonzero(senate_d) / n),
        n=n,
    )


def _sorted_probs(probs: dict[str, float], prefix: str) -> list[float]:
    return [probs[k] for k in sorted(probs) if k.startswith(prefix)]


def _draw_seat_sums(rng: np.random.Generator, race_probs: list[float], n: int) -> np.ndarray:
    """Democratic seats won across ``n`` simulations for one chamber.

    Races that are certain (prior 0 or 1) are added as a constant instead of
    drawn, which keeps the random matrix small and shrinks variance to only the
    races that are actually in doubt.
    """
    probs = np.asarray(race_probs, dtype=np.float64)
    sure_wins = int(np.count_nonzero(probs >= 1.0))
    uncertain = probs[(probs > 0.0) & (probs < 1.0)]
    seats = np.full(n, sure_wins, dtype=np.int64)
    if uncertain.size:
        draws = rng.random((n, uncertain.size)) < uncertain
        seats += draws.sum(axis=1)
    return seats
