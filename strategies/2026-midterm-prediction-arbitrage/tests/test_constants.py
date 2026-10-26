"""The seat arithmetic has to close, or the simulation counts the wrong Senate."""

from __future__ import annotations

from strategy import constants as c


def test_senate_seats_sum_to_100():
    up = len(c.SENATE_RACES)
    assert up == 35
    assert c.SENATE_NOT_UP_DEM + c.SENATE_NOT_UP_REP + up == c.SENATE_TOTAL


def test_house_seats_sum_to_435():
    modeled = len(c.HOUSE_MARKET_DISTRICTS)
    assert c.HOUSE_SAFE_DEM + c.HOUSE_SAFE_REP + modeled == c.HOUSE_TOTAL


def test_house_baseline_anchored_to_2024():
    # The 2024 result partitions the whole chamber, and the safe counts are that
    # result minus the districts that now have a market (by their 2024 holder).
    assert c.HOUSE_2024_DEM + c.HOUSE_2024_REP == c.HOUSE_TOTAL
    d_held = sum(1 for d in c.HOUSE_MARKET_DISTRICTS if d.held == "D")
    r_held = sum(1 for d in c.HOUSE_MARKET_DISTRICTS if d.held == "R")
    assert d_held + r_held == len(c.HOUSE_MARKET_DISTRICTS)
    assert c.HOUSE_SAFE_DEM == c.HOUSE_2024_DEM - d_held
    assert c.HOUSE_SAFE_REP == c.HOUSE_2024_REP - r_held


def test_control_thresholds():
    # A Republican VP breaks a 50-50 tie, so Democrats need 51.
    assert c.SENATE_DEM_CONTROL == c.SENATE_TOTAL // 2 + 1 == 51
    assert c.HOUSE_MAJORITY == c.HOUSE_TOTAL // 2 + 1 == 218


def test_each_state_appears_once():
    states = [seat.state for seat in c.SENATE_RACES]
    assert len(states) == len(set(states))


def test_combo_market_has_four_outcomes():
    assert set(c.COMBO_MARKETS) == {"DD", "DR", "RD", "RR"}
