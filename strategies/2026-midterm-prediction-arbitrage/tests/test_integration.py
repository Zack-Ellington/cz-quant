"""End to end on the committed snapshot of real quotes, and the paper ledger.

The snapshot replays every quote the run read, the simulation is seeded, and the
clock is fixed, so a whole run -- report and paper section -- reduces to one
golden string. Beyond that:

* a run writes ``run.json``, ``ledger.jsonl``, ``events.jsonl``, and ``run.log`` and
  sends nothing;
* the snapshot is saved before the model runs, so a failed run is replayable;
* issue #7's acceptance: two runs on two snapshots, the first opens a paper
  position and the second settles it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from strategy import runner
from strategy.estimators import ESTIMATORS
from strategy.ledger import read_events, read_ledger, read_runs
from strategy.pnl import positions_from_ledger
from strategy.runner import STRATEGY_ID, acquire, build_report, run
from strategy.snapshot import SnapshotClient
from tests.conftest import committed_snapshot

GOLDEN = Path(__file__).resolve().parent / "expected_output.txt"
SNAPSHOT = committed_snapshot()
SEED = 12345
DAY1 = datetime(2026, 9, 23, 17, 0, 0, tzinfo=timezone.utc)
DAY2 = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)


def at(moment):
    return lambda: moment


def test_run_prints_the_golden_report(tmp_path, capsys):
    run(seed=SEED, snapshot_in=str(SNAPSHOT), runs_dir=tmp_path / "runs", clock=at(DAY1))
    assert capsys.readouterr().out == GOLDEN.read_text(encoding="utf-8")


def test_a_run_writes_its_directory_and_sends_nothing(tmp_path, capsys, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("a replay must not open a live client")

    monkeypatch.setattr(runner, "KalshiClient", no_network)
    directory = run(seed=SEED, snapshot_in=str(SNAPSHOT), runs_dir=tmp_path / "runs", clock=at(DAY1))
    printed = capsys.readouterr().out
    assert directory == tmp_path / "runs" / STRATEGY_ID / "20260923T170000Z"
    meta = json.loads((directory / "run.json").read_text(encoding="utf-8"))
    assert meta["mode"] == "paper" and meta["status"] == "ok"
    assert meta["snapshot_in"] == str(SNAPSHOT) and meta["api_base_url"]
    assert (directory / "run.log").read_text(encoding="utf-8") == printed
    events = read_events(tmp_path / "runs", STRATEGY_ID)
    assert events[0]["type"] == "scan" and events[0]["model"] == "independent model"
    rows = read_ledger(tmp_path / "runs", STRATEGY_ID)
    assert rows and all(r["simulated"] for r in rows)
    assert {r["type"] for r in rows} == {"fill", "mark"}


def test_live_mode_is_refused(tmp_path):
    with pytest.raises(SystemExit, match="paper"):
        run(snapshot_in=str(SNAPSHOT), mode="live", runs_dir=tmp_path / "runs")


def test_only_the_independent_model_exists(tmp_path):
    with pytest.raises(ValueError, match="independent"):
        run(snapshot_in=str(SNAPSHOT), model="one-factor", runs_dir=tmp_path / "runs")


def test_snapshot_is_saved_before_the_model_runs(tmp_path, monkeypatch, capsys):
    def broken(*args, **kwargs):
        raise RuntimeError("simulation exploded")

    monkeypatch.setattr(runner, "simulate", broken)
    out = tmp_path / "acquired.json"
    with pytest.raises(RuntimeError, match="exploded"):
        run(seed=SEED, snapshot_in=str(SNAPSHOT), snapshot_out=str(out), runs_dir=tmp_path / "runs",
            clock=at(DAY1))
    replayed = acquire(SnapshotClient(out), "midpoint")
    assert replayed.markets.combo["DD"].ask == pytest.approx(0.64)
    assert read_runs(tmp_path / "runs", STRATEGY_ID)[0]["status"] == "failed"


def _derived_snapshots(tmp_path) -> tuple[Path, Path]:
    """Day 1: DD offered at 50c. Day 2: every control market has resolved to DD."""
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    records = data["records"]
    records["book:KXBALANCEPOWERCOMBO-27FEB-DD"] = {"yes": [[0.49, 300.0]], "no": [[0.5, 300.0]]}
    day1 = tmp_path / "day1.json"
    day1.write_text(json.dumps(data), encoding="utf-8")
    winners = {"KXBALANCEPOWERCOMBO-27FEB-DD", "CONTROLH-2026-D", "CONTROLS-2026-D",
               "KXSAMEPARTYCONGRESS-27FEB01"}
    for key in ("event:KXBALANCEPOWERCOMBO-27FEB", "event:CONTROLH-2026", "event:CONTROLS-2026",
                "event:KXSAMEPARTYCONGRESS-27FEB01"):
        for market in records[key]["markets"]:
            market["status"] = "finalized"
            market["result"] = "yes" if market["ticker"] in winners else "no"
    day2 = tmp_path / "day2.json"
    day2.write_text(json.dumps(data), encoding="utf-8")
    return day1, day2


def test_a_position_opened_in_one_run_settles_in_the_next(tmp_path, capsys):
    day1, day2 = _derived_snapshots(tmp_path)
    runs = tmp_path / "runs"
    run(seed=SEED, snapshot_in=str(day1), runs_dir=runs, clock=at(DAY1))
    opened = positions_from_ledger(read_ledger(runs, STRATEGY_ID))
    dd = next(p for p in opened.values() if p.basket == "model:buy DD")
    assert dd.is_open and dd.quantity > 0 and dd.value == D("0.49") * dd.quantity  # marked at the bid

    run(seed=SEED, snapshot_in=str(day2), runs_dir=runs, clock=at(DAY2))
    settled = positions_from_ledger(read_ledger(runs, STRATEGY_ID))[dd.position_id]
    assert settled.receipt == D(dd.quantity)
    assert settled.settled_pnl == D(dd.quantity) - dd.entry_capital
    assert not positions_from_ledger(read_ledger(runs, "another-strategy"))


@pytest.mark.parametrize("estimator", sorted(ESTIMATORS))
def test_every_estimator_replays_from_the_snapshot(estimator):
    acquired = acquire(SnapshotClient(SNAPSHOT), estimator)
    r = build_report(acquired, "snapshot", 10_000, SEED, "independent", estimator, 100)
    assert sum(r.independent.combo.values()) == pytest.approx(1.0)
