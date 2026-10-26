"""Positions, cash, and the aggregates on a small fixture ledger.

``tests/fixtures/runs`` holds two runs of this strategy -- one opens an
arbitrage basket (left open) and a DD position, the other settles the DD
position weeks later and re-marks the basket -- and one run of another
strategy that must be excluded.

    A-01  Senate control D + R (two NOs), 10 sets: 6.17 + 3.15 = 9.32 capital,
          marked 0.59 + 0.29 in run A, 0.61 + 0.28 in run B -> value 8.90, open
    A-02  buy DD, 20 contracts: 10.35 capital, marked 0.49, settled for 20.00
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from strategy.ledger import read_ledger
from strategy.pnl import free_cash, positions_from_ledger, summarize
from strategy.runner import STRATEGY_ID
from tests.conftest import FIXTURES

RUNS = FIXTURES / "runs"
ME = STRATEGY_ID
A = "20260923T170000Z"


@pytest.fixture
def positions():
    return positions_from_ledger(read_ledger(RUNS, ME))


def test_the_aggregates(positions):
    s = summarize(list(positions.values()))
    assert s.volume == D("19.00")  # 0.60*10 + 0.30*10 + 0.50*20
    assert (s.capital_open, s.capital_settled) == (D("9.32"), D("10.35"))
    assert s.open_value == D("8.90")  # 10 x 0.61 + 10 x 0.28, the latest marks
    assert s.settled_pnl == D("9.65")  # 20 - 10.35
    assert s.return_on_capital == D("9.65") / D("10.35")
    assert (s.open, s.settled) == (1, 1)


def test_positions_join_across_runs(positions):
    dd = positions[f"{A}-02"]
    assert dd.run_id == A and dd.settled_run == "20261105T120000Z"
    assert dd.receipt == D("20") and dd.received_at.startswith("2026-11-05")
    assert dd.quantity == 20 and dd.value is None  # settled positions have no value
    arb = positions[f"{A}-01"]
    assert arb.is_open and arb.marked_pnl == D("8.90") - D("9.32")
    assert set(positions) == {f"{A}-01", f"{A}-02"}


def test_an_unmarked_leg_makes_the_value_unknown(positions):
    arb = positions[f"{A}-01"]
    del arb.marks[("CONTROLS-2026-D", "no")]
    assert arb.value is None and summarize([arb]).open_value is None


def test_free_cash_follows_fills_and_receipts():
    assert free_cash(read_ledger(RUNS, ME), D("1000")) == D("1000.33")  # -9.32 - 10.35 + 20


def test_other_strategies_are_separate_ledgers():
    other = positions_from_ledger(read_ledger(RUNS, "some-other-strategy"))
    assert set(other) == {"OTHER-01"} and other["OTHER-01"].value == D("0.45")
