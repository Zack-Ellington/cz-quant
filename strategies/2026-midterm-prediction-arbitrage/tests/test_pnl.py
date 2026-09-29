"""The five ledger aggregates and the simulated P&L, on a small fixture ledger.

``tests/fixtures/runs`` holds two runs of this strategy -- one opens an
arbitrage basket (left open) and a DD position, the other settles the DD
position weeks later -- and one run of another strategy that must be excluded.

    A-01  Senate control D + R (two NOs), 10 sets: 6.17 + 3.15 = 9.32 capital,
          pays $1 per set in every state -> min payout 10.00, open
    A-02  buy DD, 20 contracts: 10.35 capital, settled for 20.00
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from strategy.ledger import read_events
from strategy.output import ledger_report
from strategy.runner import STRATEGY_ID
from strategy.pnl import (
    free_cash,
    latest_model,
    positions_from_events,
    select_positions,
    simulate_pnl,
    summarize,
)
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


def test_simulated_pnl_of_a_riskless_basket_is_its_minimum(positions):
    open_ = [p for p in positions.values() if p.is_open]
    sim = simulate_pnl(open_, {"DD": 0.6, "DR": 0.3, "RD": 0.02, "RR": 0.08}, "test model", 10_000, seed=1)
    assert sim.mean == pytest.approx(0.68) and sim.sd == pytest.approx(0.0, abs=1e-9)
    assert sim.prob_loss == 0.0


def test_simulated_pnl_of_a_directional_position():
    positions = positions_from_events(read_events(RUNS, ME))
    dd = positions[f"{A}-02"]
    dd.receipt = None  # value it as if still open
    sim = simulate_pnl([dd], {"DD": 0.6, "DR": 0.3, "RD": 0.02, "RR": 0.08}, "test model", 200_000, seed=2)
    assert sim.mean == pytest.approx(0.6 * 20 - 10.35, abs=0.05)
    assert sim.prob_loss == pytest.approx(0.4, abs=0.01)
    assert (sim.p05, sim.p95) == (pytest.approx(-10.35), pytest.approx(9.65))


def test_simulation_is_reproducible(positions):
    open_ = [p for p in positions.values() if p.is_open]
    probs = {"DD": 0.5, "RR": 0.5}
    assert simulate_pnl(open_, probs, "m", 1000, 7) == simulate_pnl(open_, probs, "m", 1000, 7)


def test_latest_model_comes_from_the_last_scan():
    name, probs, run_id = latest_model(read_events(RUNS, ME))
    assert (name, run_id, probs["DD"]) == ("one factor model", A, 0.6)


def test_positions_selected_by_entry_date(positions):
    assert len(select_positions(positions, date(2026, 9, 23), date(2026, 9, 23))) == 2
    assert select_positions(positions, date(2026, 10, 1), None) == []
    assert select_positions(positions, None, date(2026, 9, 22)) == []


def test_ledger_report_joins_runs_and_excludes_other_strategies():
    text = ledger_report(RUNS, ME, n=10_000, seed=0)
    assert text.count(f"{A}-02") == 1  # reported once, though it spans two runs
    assert "settled in 20261105T120000Z" in text and "P&L $9.65" in text
    assert "OTHER-01" not in text
    assert "+93.2%" in text  # return on the settled position's entry capital
    total = next(line for line in text.splitlines() if line.startswith("Total"))
    assert "$19.00" in total and "$9.32" in total and "$10.35" in total and "$9.65" in total
    assert "Simulated P&L of the 1 open position(s)" in text


def test_ledger_report_date_window():
    text = ledger_report(RUNS, ME, since=date(2026, 10, 1), n=1000)
    assert "0 position(s) selected" in text
    other = ledger_report(RUNS, "some-other-strategy", n=1000)
    assert "OTHER-01" in other and f"{A}-01" not in other
