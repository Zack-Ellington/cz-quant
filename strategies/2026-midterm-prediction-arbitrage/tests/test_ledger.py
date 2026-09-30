"""Run directories, the flat accounting ledger, and the event log."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from strategy.ledger import LEDGER_COLUMNS, Run, Tee, read_events, read_ledger, read_runs, repo_root


def clock_at(*moments):
    it = iter(moments)
    last = [None]

    def tick():
        try:
            last[0] = next(it)
        except StopIteration:
            pass
        return last[0]

    return tick


T0 = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)


def test_start_creates_the_run_directory(tmp_path):
    run = Run.start(tmp_path / "runs", "my-strategy", {"mode": "paper", "api_base_url": "x"}, clock_at(T0))
    assert run.directory == tmp_path / "runs" / "my-strategy" / "20260929T120000Z"
    meta = json.loads((run.directory / "run.json").read_text(encoding="utf-8"))
    assert meta["strategy"] == "my-strategy" and meta["run_id"] == "20260929T120000Z"
    assert meta["started_at"] == "2026-09-29T12:00:00.000000Z" and meta["ended_at"] is None
    assert meta["mode"] == "paper" and meta["status"] == "running"
    assert {"commit", "dirty"} <= set(meta["git"]) and "command" in meta
    assert run.ledger_path.read_text() == "" and run.events_path.read_text() == ""
    assert run.label == "runs/my-strategy/20260929T120000Z"


def test_runs_in_the_same_second_get_distinct_directories(tmp_path):
    a = Run.start(tmp_path, "s", {}, clock_at(T0))
    b = Run.start(tmp_path, "s", {}, clock_at(T0))
    assert a.run_id == "20260929T120000Z" and b.run_id == "20260929T120000Z-2"


def test_every_ledger_row_has_the_same_columns(tmp_path):
    run = Run.start(tmp_path, "s", {}, clock_at(T0))
    run.ledger("fill", "P-1", "model:buy DD", "X-DD", "yes", 3, D("0.6600"), D("0.02"), D("-2.00"))
    run.ledger("mark", "P-1", "model:buy DD", "X-DD", "yes", 3, D("0.6500"), D("0"), D("0"))
    run.ledger("settlement", "P-1", "model:buy DD", "X-DD", "yes", 3, D("1"), D("0"), D("3"))
    lines = run.ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3  # each written and flushed as it happened
    for line in lines:
        assert tuple(json.loads(line)) == LEDGER_COLUMNS
    rows = read_ledger(tmp_path, "s")
    assert [r["type"] for r in rows] == ["fill", "mark", "settlement"]
    assert rows[0]["simulated"] is True and rows[0]["run_id"] == run.run_id


def test_money_round_trips_exactly(tmp_path):
    run = Run.start(tmp_path, "s", {}, clock_at(T0))
    run.ledger("fill", "P-1", "b", "X", "no", 1, D("0.6600"), D("0.005"), D("-0.060000"))
    record = read_ledger(tmp_path, "s")[0]
    assert D(record["price"]) == D("0.66") and D(record["cash_delta"]) == D("-0.06")
    assert D(record["fee"]) == D("0.005")


def test_events_go_to_their_own_file(tmp_path):
    run = Run.start(tmp_path, "s", {}, clock_at(T0))
    run.event("note", message="first")
    assert run.ledger_path.read_text() == ""
    record = json.loads(run.events_path.read_text(encoding="utf-8").splitlines()[0])
    assert {k: record[k] for k in ("run_id", "strategy", "type", "position_id", "message")} == {
        "run_id": run.run_id, "strategy": "s", "type": "note", "position_id": None, "message": "first",
    }


def test_unknown_types_are_rejected(tmp_path):
    run = Run.start(tmp_path, "s", {}, clock_at(T0))
    with pytest.raises(ValueError, match="unknown event type"):
        run.event("trade")
    with pytest.raises(ValueError, match="unknown ledger row type"):
        run.ledger("valuation", "P", "b", "X", "yes", 1, D("0.5"), D("0"), D("0"))
    with pytest.raises(ValueError, match="side"):
        run.ledger("fill", "P", "b", "X", "buy", 1, D("0.5"), D("0"), D("0"))


def test_finish_records_the_end(tmp_path):
    later = T0.replace(minute=5)
    run = Run.start(tmp_path, "s", {}, clock_at(T0, later))
    run.finish("failed", error="boom")
    meta = read_runs(tmp_path, "s")[0]
    assert meta["status"] == "failed" and meta["error"] == "boom"
    assert meta["ended_at"] == "2026-09-29T12:05:00.000000Z"


def test_rows_are_read_in_time_order_across_runs(tmp_path):
    first = Run.start(tmp_path, "s", {}, clock_at(T0))
    second = Run.start(tmp_path, "s", {}, clock_at(T0.replace(day=30)))
    second.ledger("mark", "P", "b", "X", "yes", 1, D("0.5"), D("0"), D("0"))
    first.ledger("fill", "P", "b", "X", "yes", 1, D("0.4"), D("0"), D("-0.4"))
    assert [r["type"] for r in read_ledger(tmp_path, "s")] == ["fill", "mark"]
    second.event("note", message="later")
    first.event("note", message="earlier")
    assert [e["message"] for e in read_events(tmp_path, "s")] == ["earlier", "later"]
    assert read_ledger(tmp_path, "nobody") == [] and read_events(tmp_path, "nobody") == []


def test_tee_writes_to_both(tmp_path):
    stream = io.StringIO()
    tee = Tee(stream, tmp_path / "run.log")
    print("hello", file=tee)
    tee.close()
    assert stream.getvalue() == "hello\n" == (tmp_path / "run.log").read_text(encoding="utf-8")


def test_a_quiet_tee_writes_the_log_only(tmp_path):
    tee = Tee(None, tmp_path / "run.log")
    assert tee.write("hello\n") == 6
    tee.flush()
    tee.close()
    assert (tmp_path / "run.log").read_text(encoding="utf-8") == "hello\n"


def test_repo_root_is_found():
    assert (repo_root() / ".git").exists()
