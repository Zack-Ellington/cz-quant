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

from pnl import load, main, report, select, series

RUNS = Path(__file__).resolve().parents[1] / "strategies/2026-midterm-prediction-arbitrage/tests/fixtures/runs"
ME = "2026-midterm-prediction-arbitrage"
OTHER = "some-other-strategy"


@pytest.fixture(scope="module")
def df():
    return load(RUNS)


def test_every_row_of_every_strategy_loads(df):
    assert len(df) == 11 and set(df["strategy"]) == {ME, OTHER}
    assert list(df.columns)[:5] == ["ts", "run_id", "strategy", "type", "position_id"]
    assert df["ts"].is_monotonic_increasing


def test_series_one_point_per_run(df):
    s = series(select(df, [ME]))
    assert list(s["run_id"]) == ["20260923T170000Z", "20261105T120000Z"]
    a, b = s.iloc[0], s.iloc[1]
    assert (a["capital"], a["cash"], a["value"]) == pytest.approx((19.67, -19.67, 18.60))
    assert a["pnl"] == pytest.approx(-1.07) and a["open"] == 2
    assert (b["capital"], b["cash"], b["value"]) == pytest.approx((19.67, 0.33, 8.90))
    assert b["pnl"] == pytest.approx(9.23) and b["return"] == pytest.approx(9.23 / 19.67)
    assert b["open"] == 1 and b["positions"] == 2


def test_report_is_the_latest_point_per_strategy(df):
    table = report(select(df))
    assert list(table["strategy"]) == [ME, OTHER]
    other = table.set_index("strategy").loc[OTHER]
    assert other["capital"] == pytest.approx(0.54) and other["value"] == pytest.approx(0.45)
    assert other["pnl"] == pytest.approx(-0.09)


def test_window_selects_positions_by_entry_time(df):
    assert select(df, [ME], since=date(2026, 10, 1)).empty
    s = series(select(df, [ME], until=date(2026, 9, 30)))
    assert list(s["run_id"]) == ["20260923T170000Z"]  # the November run is after the window
    assert series(select(df, [OTHER], since=date(2026, 9, 24), until=date(2026, 9, 24)))["capital"].iloc[0] == pytest.approx(0.54)


def test_an_unmarked_open_leg_has_no_value(df):
    rows = select(df, [ME])
    rows = rows[~((rows["type"] == "mark") & (rows["ticker"] == "CONTROLS-2026-D"))]
    last = series(rows).iloc[-1]
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
