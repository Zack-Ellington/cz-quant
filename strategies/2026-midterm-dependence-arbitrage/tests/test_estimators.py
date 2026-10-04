"""Thin-book estimators: normalization, width penalty, last trade, shrinkage."""

from __future__ import annotations

import math

import pytest

from strategy.estimators import SHRINK_VOLUME, RaceProb, last_trade, midpoint, shrunk, width
from strategy.markets import Quote


def Q(bid, ask, last=None, volume=0.0):
    return Quote("X", bid, ask, last, volume)


def test_midpoint_normalizes_the_two_legs():
    p = midpoint(Q(0.54, 0.56), Q(0.40, 0.42), (), 0.3)
    assert p.dem == pytest.approx(0.55 / 0.96)
    assert p.ind == 0.0


def test_midpoint_counts_the_independent_leg():
    # Nebraska-like: R 68.5, independent 31.5, the D nominee ~0.
    p = midpoint(Q(None, 0.005), Q(0.68, 0.69), (Q(0.31, 0.32),), 0.1)
    total = 0.005 + 0.685 + 0.315
    assert p.dem == pytest.approx(0.005 / total)
    assert p.ind == pytest.approx(0.315 / total)


def test_prior_when_nothing_is_quoted():
    for fn in (midpoint, width, last_trade, shrunk):
        assert fn(None, None, (), 0.3) == RaceProb(0.3)
        assert fn(Q(None, None), Q(None, None), (), 0.3) == RaceProb(0.3)


def test_width_pins_a_tight_book_to_the_quotes():
    # Interval: [max(0.93, 1 - 0.07), min(0.94, 1 - 0.06)] = [0.93, 0.94].
    assert width(Q(0.93, 0.94), Q(0.06, 0.07), (), 0.5).dem == pytest.approx(0.93)


def test_width_lets_the_prior_move_a_wide_book_but_not_past_it():
    d = Q(0.40, 0.70)
    assert width(d, None, (), 0.65).dem == pytest.approx(0.65)  # inside: the prior
    assert width(d, None, (), 0.90).dem == pytest.approx(0.70)  # stops at the ask
    assert width(d, None, (), 0.10).dem == pytest.approx(0.40)  # stops at the bid


def test_width_bounds_include_independent_quotes():
    # No D book: 1 - (R ask + I ask) <= P(D) <= 1 - (R bid + I bid) = [0.06, 0.10].
    p = width(None, Q(0.60, 0.62), (Q(0.30, 0.32),), 0.5)
    assert p.dem == pytest.approx(0.10)


def test_last_trade_and_its_fallback():
    assert last_trade(Q(0.6, 0.8, last=0.7), Q(0.2, 0.4, last=0.3), (), 0.5).dem == pytest.approx(0.7)
    no_trades = last_trade(Q(0.6, 0.8), Q(0.2, 0.4), (), 0.5)
    assert no_trades == midpoint(Q(0.6, 0.8), Q(0.2, 0.4), (), 0.5)


def test_shrinkage_is_weighted_by_volume():
    d, r = Q(0.89, 0.91), Q(0.09, 0.11)
    market = midpoint(d, r, (), 0.5).dem
    deep = shrunk(Q(0.89, 0.91, volume=1e9), Q(0.09, 0.11), (), 0.5).dem
    thin = shrunk(d, r, (), 0.5).dem  # zero volume: all prior
    half = shrunk(Q(0.89, 0.91, volume=SHRINK_VOLUME), r, (), 0.5).dem
    assert deep == pytest.approx(market, abs=1e-6)
    assert thin == pytest.approx(0.5)
    logit = math.log(market / (1 - market))
    assert half == pytest.approx(1 / (1 + math.exp(-logit / 2)))  # halfway in log-odds


def test_caucus_share():
    assert RaceProb(0.1, 0.3).caucus(0.5) == pytest.approx(0.25)
    assert RaceProb(0.1, 0.3).caucus(0.0) == pytest.approx(0.1)
    assert RaceProb(0.8, 0.3).caucus(1.0) == 1.0  # capped
