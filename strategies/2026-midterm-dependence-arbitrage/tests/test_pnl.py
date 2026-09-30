"""The five ledger aggregates on a small fixture ledger.

``tests/fixtures/runs`` holds two runs of this strategy -- one opens an
arbitrage basket (left open) and a DD position, the other settles the DD
position weeks later -- and one run of another strategy that must be excluded.

    A-01  Senate control D + R (two NOs), 10 sets: 6.17 + 3.15 = 9.32 capital,
          pays $1 per set in every state -> min payout 10.00, open
    A-02  buy DD, 20 contracts: 10.35 capital, settled for 20.00
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from strategy.ledger import read_events
from strategy.pnl import free_cash, positions_from_events, summarize
from strategy.runner import STRATEGY_ID
from tests.conftest import FIXTURES

RUNS = FIXTURES / "runs"
ME = STRATEGY_ID
A = "20260923T170000Z"


@pytest.fixture
def positions():
    return positions_from_events(read_events(RUNS, ME))


def test_the_five_aggregates(positions):
    s = summarize(list(positions.values()))
    assert s.volume == D("19.00")  # 0.60*10 + 0.30*10 + 0.50*20
    assert (s.capital_open, s.capital_settled) == (D("9.32"), D("10.35"))
    assert s.projected_min_profit == D("0.68")  # 10 x $1 - 9.32
    assert s.settled_pnl == D("9.65")  # 20 - 10.35
    assert s.return_on_capital == D("9.65") / D("10.35")
    assert (s.open, s.settled) == (1, 1)


def test_positions_join_across_runs(positions):
    dd = positions[f"{A}-02"]
    assert dd.run_id == A and dd.settled_run == "20261105T120000Z"
    assert dd.receipt == D("20") and dd.received_at.startswith("2026-11-05")
    assert positions[f"{A}-01"].is_open
    assert set(positions) == {f"{A}-01", f"{A}-02"}  # the rejected signal is not a position


def test_projected_minimum_is_kept_apart_from_earned_pnl(positions):
    open_ = [p for p in positions.values() if p.is_open]
    s = summarize(open_)
    assert s.settled_pnl == 0 and s.return_on_capital is None
    assert s.projected_min_profit == D("0.68")


def test_free_cash_follows_fills_and_receipts():
    assert free_cash(read_events(RUNS, ME), D("1000")) == D("1000.33")  # -9.32 - 10.35 + 20


def test_other_strategies_are_separate_ledgers():
    other = positions_from_events(read_events(RUNS, "some-other-strategy"))
    assert set(other) == {"OTHER-01"} and f"{A}-01" not in other
