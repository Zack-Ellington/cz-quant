"""Race-probability estimators: turning thin order books into probabilities.

Each race is a mutually-exclusive Kalshi event with a Democratic leg, a
Republican leg, and sometimes independent candidates (Osborn in Nebraska,
Bodnar in Montana, Achilles in Idaho, Bengs in South Dakota, Hill in AK-AL).
An estimator takes every leg's quote and the race's prior and returns a
``RaceProb``: P(the Democratic nominee wins) and P(an independent wins). All
legs are normalized together; dropping the independent leg would overstate both
major parties. Whether an independent's seat counts as Democratic is decided
later, by the caucus share (``RaceProb.caucus``).

The estimators differ only in how much they trust a thin or wide book:

``midpoint``  Bid/ask midpoint of each leg, normalized across legs. Uses only the
              market; the prior appears only if every book is empty. The default.
``width``     The point of the tradable interval closest to the prior. The legs
              bound P(D): it is at least the D bid and 1 - (R ask + independent
              asks), and at most the D ask and 1 - (R bid + independent bids). A
              1-cent book pins the estimate to the quotes; a 30-cent book lets the
              prior move it up to the edge of the quotes, never beyond. It
              penalizes width without ever contradicting a tradable price.
``last``      Last traded prices, normalized across legs. Falls back to
              ``midpoint`` when no leg has traded.
``shrunk``    The midpoint shrunk toward the prior in log-odds, weighted by traded
              volume: w = V / (V + K), K = ``SHRINK_VOLUME`` contracts across all
              legs. 30,000 contracts traded is ~86% market; 1,000 is ~17%.

The priors come from ``constants`` and are rough seat ratings for the Democratic
nominee; ``width`` and ``shrunk`` use them for priced races, and they only move
the Democratic probability (the independent share is the midpoint's). Comparing
estimators on one snapshot is a robustness check: a conclusion that flips with
the estimator is not one the books support.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Sequence

from strategy.markets import Quote

SHRINK_VOLUME = 5_000.0
_EPS = 1e-6


@dataclass(frozen=True)
class RaceProb:
    dem: float  # P(the Democratic nominee wins)
    ind: float = 0.0  # P(an independent or third-party candidate wins)

    def caucus(self, share: float) -> float:
        """P(the seat caucuses with Democrats) if a fraction ``share`` of
        independent winners do."""
        return min(self.dem + share * self.ind, 1.0)


Estimator = Callable[[Quote | None, Quote | None, Sequence[Quote], float], RaceProb]


def midpoint(d: Quote | None, r: Quote | None, others: Sequence[Quote], prior: float) -> RaceProb:
    estimate = _normalized(
        d.mid if d else None, r.mid if r else None, [o.mid for o in others]
    )
    return RaceProb(prior) if estimate is None else estimate


def width(d: Quote | None, r: Quote | None, others: Sequence[Quote], prior: float) -> RaceProb:
    base = midpoint(d, r, others, prior)
    lows, highs = [], []
    if d is not None:
        if d.bid is not None:
            lows.append(d.bid)
        if d.ask is not None:
            highs.append(d.ask)
    if r is not None:
        # P(D) = 1 - P(R) - P(independents). The asks bound the others from
        # above (so P(D) from below) only if every one of them has an ask.
        other_asks = [o.ask for o in others]
        if r.ask is not None and all(a is not None for a in other_asks):
            lows.append(1.0 - r.ask - sum(other_asks))
        if r.bid is not None:
            highs.append(1.0 - r.bid - sum(o.bid or 0.0 for o in others))
    if not lows and not highs:
        return base
    lo, hi = max(lows, default=0.0), min(highs, default=1.0)
    if lo > hi:  # legs cross each other; no interval to project onto
        return base
    dem = min(max(prior, lo), hi)
    return RaceProb(dem, min(base.ind, 1.0 - dem))


def last_trade(d: Quote | None, r: Quote | None, others: Sequence[Quote], prior: float) -> RaceProb:
    estimate = _normalized(
        d.last if d else None, r.last if r else None, [o.last for o in others]
    )
    return midpoint(d, r, others, prior) if estimate is None else estimate


def shrunk(d: Quote | None, r: Quote | None, others: Sequence[Quote], prior: float) -> RaceProb:
    market = _normalized(d.mid if d else None, r.mid if r else None, [o.mid for o in others])
    if market is None:
        return RaceProb(prior)
    volume = sum(q.volume for q in (d, r, *others) if q is not None)
    w = volume / (volume + SHRINK_VOLUME)
    dem = _expit(w * _logit(market.dem) + (1.0 - w) * _logit(prior))
    return RaceProb(dem, min(market.ind, 1.0 - dem))


ESTIMATORS: dict[str, Estimator] = {
    "midpoint": midpoint,
    "width": width,
    "last": last_trade,
    "shrunk": shrunk,
}


def _normalized(d: float | None, r: float | None, others: Sequence[float | None]) -> RaceProb | None:
    ind = sum(p for p in others if p is not None)
    if d is not None and r is not None:
        total = d + r + ind
        if total <= 0:
            return None
        return RaceProb(d / total, ind / total)
    if d is not None:
        return RaceProb(d, min(ind, 1.0 - d))
    if r is not None:
        return RaceProb(max(1.0 - r - ind, 0.0), min(ind, 1.0 - r))
    return None


def _logit(p: float) -> float:
    p = min(max(p, _EPS), 1.0 - _EPS)
    return math.log(p / (1.0 - p))


def _expit(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))
