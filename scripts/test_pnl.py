"""The reporting tool on the prediction strategy's fixture runs.

Fixture (see that strategy's ``tests/test_pnl.py``): run A opens an arbitrage
basket (9.32 capital) and buys DD (10.35), marked 18.60 in all; run B settles DD
for 20.00 and re-marks the basket at 8.90. Another strategy has one open
position of 0.54 marked at 0.45.
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import pytest

from pnl import load, main

RUNS = Path(__file__).resolve().parents[1] / "strategies/2026-midterm-prediction-arbitrage/tests/fixtures/runs"
ME = "2026-midterm-prediction-arbitrage"
OTHER = "some-other-strategy"


@pytest.fixture(scope="module")
def ledger():
    return load(RUNS)


def test_every_row_and_run_loads(ledger):
    df = ledger.rows
    assert len(df) == 11 and set(df["strategy"]) == {ME, OTHER}
    assert list(df.columns)[:5] == ["ts", "run_id", "strategy", "type", "position_id"]
    assert df["ts"].is_monotonic_increasing
    assert len(ledger.runs) == 3 and (ledger.runs["ts"] >= df.groupby("run_id")["ts"].max().min()).all()


def test_series_one_point_per_run(ledger):
    s = ledger.select([ME]).series()
    assert list(s["run_id"]) == ["20260923T170000Z", "20261105T120000Z"]
    a, b = s.iloc[0], s.iloc[1]
    assert (a["capital"], a["cash"], a["value"]) == pytest.approx((19.67, -19.67, 18.60))
    assert a["pnl"] == pytest.approx(-1.07) and a["open"] == 2
    assert (b["capital"], b["cash"], b["value"]) == pytest.approx((19.67, 0.33, 8.90))
    assert b["pnl"] == pytest.approx(9.23) and b["return"] == pytest.approx(9.23 / 19.67)
    assert b["open"] == 1 and b["positions"] == 2


def test_report_is_the_latest_point_per_strategy(ledger):
    table = ledger.select().report()
    assert list(table["strategy"]) == [ME, OTHER]
    other = table.set_index("strategy").loc[OTHER]
    assert other["capital"] == pytest.approx(0.54) and other["value"] == pytest.approx(0.45)
    assert other["pnl"] == pytest.approx(-0.09)


def test_window_selects_positions_by_entry_time(ledger):
    november = ledger.select([ME], since=date(2026, 10, 1))
    assert november.rows.empty  # the position was entered in September
    s = november.series()
    assert list(s["run_id"]) == ["20261105T120000Z"] and s["capital"].iloc[0] == 0  # a run, at zero
    s = ledger.select([ME], until=date(2026, 9, 30)).series()
    assert list(s["run_id"]) == ["20260923T170000Z"]  # the November run is after the window
    s = ledger.select([OTHER], since=date(2026, 9, 24), until=date(2026, 9, 24)).series()
    assert s["capital"].iloc[0] == pytest.approx(0.54)


def test_a_strategy_that_never_traded_is_a_line_at_zero(ledger):
    from pnl import Ledger
    quiet = Ledger(ledger.rows.iloc[0:0], ledger.runs[ledger.runs["strategy"] == ME])
    s = quiet.series()
    assert len(s) == 2 and (s["pnl"] == 0).all() and (s["capital"] == 0).all()
    assert s["return"].isna().all()


def test_an_unmarked_open_leg_has_no_value(ledger):
    from pnl import Ledger
    chosen = ledger.select([ME])
    rows = chosen.rows[~((chosen.rows["type"] == "mark") & (chosen.rows["ticker"] == "CONTROLS-2026-D"))]
    last = Ledger(rows, chosen.runs).series().iloc[-1]
    assert math.isnan(last["value"]) and math.isnan(last["pnl"])


def test_the_commands_run(tmp_path, capsys):
    main(["csv", "--runs-dir", str(RUNS), "-o", str(tmp_path / "all.csv")])
    assert len((tmp_path / "all.csv").read_text().splitlines()) == 12  # header + 11 rows
    main(["report", "--runs-dir", str(RUNS), "--strategies", ME])
    assert "9.2300" in capsys.readouterr().out
    main(["plot", "--runs-dir", str(RUNS), "-o", str(tmp_path / "pnl.html")])
    html = (tmp_path / "pnl.html").read_text(encoding="utf-8")
    assert "plotly" in html and ME in html and OTHER in html
    assert len((tmp_path / "pnl.csv").read_text().splitlines()) == 4  # header + 3 points
