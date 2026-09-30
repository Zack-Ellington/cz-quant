"""Paper sessions (``--duration``): back-to-back passes until a deadline, marked
in the ledger. The loop runs on a fake clock that each pass advances, so a
session of hours takes a few passes and no waiting."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from functools import partial

import pytest

from strategy import cli, runner
from strategy.ledger import Run, read_events, read_runs
from strategy.output import ledger_report
from strategy.runner import STRATEGY_ID
from strategy.session import format_duration, parse_duration, run_session
from tests.conftest import committed_snapshot

T0 = datetime(2026, 9, 23, 17, 0, 0, tzinfo=timezone.utc)
SNAPSHOT = committed_snapshot()
FAST = {"model": "independent", "simulations": 10_000}  # the loop is under test, not the model


class FakeClock:
    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


@pytest.mark.parametrize("text, minutes", [("24:00", 1440), ("1:30", 90), ("0:05", 5), ("100:59", 6059)])
def test_durations_are_hours_and_minutes(text, minutes):
    assert parse_duration(text) == timedelta(minutes=minutes)
    assert format_duration(parse_duration(text)) == text


@pytest.mark.parametrize("text", ["90", "1:60", "1:5", "0:00", "-1:00", "1.5", "", "1:30:00"])
def test_bad_durations_are_rejected(text):
    with pytest.raises(ValueError):
        parse_duration(text)


def _fake_pass(tmp_path, clock, minutes=20, fail_on=(), interrupt_on=()):
    """A pass that writes a run directory, one note, and takes ``minutes``."""
    infos = []

    def one_pass(info):
        infos.append(info)
        clock.advance(minutes=minutes)
        if info["pass"] in interrupt_on:
            raise KeyboardInterrupt
        if info["pass"] in fail_on:
            raise RuntimeError("API down")
        run = Run.start(tmp_path, "s", {"session": info}, clock, tags={"session": info["id"]})
        run.event("note", message="pass")
        run.finish("ok")
        return run.directory

    return one_pass, infos


def test_passes_run_back_to_back_until_the_deadline(tmp_path):
    clock, out, sleeps = FakeClock(), [], []
    one_pass, infos = _fake_pass(tmp_path, clock)
    session = run_session(one_pass, timedelta(hours=1, minutes=30), tmp_path, "s", D("1000"),
                          clock=clock, sleep=sleeps.append, out=_Lines(out))
    # Passes start at 0, 20, 40, 60, 80 minutes; the one starting at 80 ends after the deadline.
    assert [i["pass"] for i in infos] == [1, 2, 3, 4, 5] and not sleeps
    assert session.stopped == "deadline reached" and session.failed == 0
    assert {i["id"] for i in infos} == {"20260923T170000Z"}
    assert infos[0]["ends_at"].startswith("2026-09-23T18:30:00") and infos[0]["duration"] == "1:30"
    assert out[0].startswith("Paper session 20260923T170000Z: running back to back for 1:30")
    assert out[1].startswith("  pass 1 20260923T172000Z: 0 opened, 0 settled, 0 rejected")


def test_a_failed_pass_backs_off_and_the_session_continues(tmp_path):
    clock, sleeps = FakeClock(), []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds=seconds)

    one_pass, infos = _fake_pass(tmp_path, clock, fail_on={1, 2})
    session = run_session(one_pass, timedelta(hours=1), tmp_path, "s", D("1000"),
                          clock=clock, sleep=sleep, out=_Lines([]))
    assert sleeps == [15.0, 30.0]  # doubling backoff, reset by the first success
    assert [status for _, _, status in session.passes][:3] == ["failed", "failed", "ok"]
    assert session.stopped == "deadline reached" and session.failed == 2


def test_too_many_failures_in_a_row_stop_the_session(tmp_path):
    clock, sleeps = FakeClock(), []
    one_pass, infos = _fake_pass(tmp_path, clock, minutes=0, fail_on=set(range(1, 100)))
    session = run_session(one_pass, timedelta(hours=24), tmp_path, "s", D("1000"),
                          clock=clock, sleep=sleeps.append, out=_Lines([]), max_failures=4)
    assert len(infos) == 4 and sleeps == [15.0, 30.0, 60.0]
    assert session.stopped == "stopped after 4 failed passes in a row"


def test_backoff_never_sleeps_past_the_deadline(tmp_path):
    clock, sleeps = FakeClock(), []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds=seconds)

    one_pass, _ = _fake_pass(tmp_path, clock, minutes=0, fail_on=set(range(1, 100)))
    run_session(one_pass, timedelta(minutes=1), tmp_path, "s", D("1000"), clock=clock, sleep=sleep,
                out=_Lines([]), max_failures=100)
    assert sum(sleeps) <= 60 and sleeps[-1] < 30


def test_ctrl_c_stops_the_session_cleanly(tmp_path):
    clock = FakeClock()
    one_pass, infos = _fake_pass(tmp_path, clock, interrupt_on={2})
    session = run_session(one_pass, timedelta(hours=5), tmp_path, "s", D("1000"), clock=clock,
                          sleep=lambda s: None, out=_Lines([]))
    assert len(infos) == 2 and session.stopped == "interrupted during pass 2"
    assert session.passes[-1][2] == "interrupted"


# --- With the real strategy, on the committed snapshot --------------------------------------


def _real_pass(tmp_path, clock, minutes=20):
    def one_pass(info):
        directory = runner.run(seed=12345, snapshot_in=str(SNAPSHOT), runs_dir=tmp_path / "runs", **FAST,
                               clock=clock, session=info, quiet=True)
        clock.advance(minutes=minutes)
        return directory

    return one_pass


def test_every_event_of_a_session_is_marked_with_it(tmp_path, capsys):
    clock = FakeClock()
    session = run_session(_real_pass(tmp_path, clock), timedelta(minutes=30), tmp_path / "runs",
                          STRATEGY_ID, D("1000"), clock=clock)
    printed = capsys.readouterr().out
    assert len(session.passes) == 2 and session.stopped == "deadline reached"
    assert "Outcome" not in printed  # quiet: the reports went to run.log
    runs = read_runs(tmp_path / "runs", STRATEGY_ID)
    assert [m["session"]["pass"] for m in runs] == [1, 2]
    assert all(m["session"]["id"] == session.session_id and m["status"] == "ok" for m in runs)
    for m in runs:
        log = (tmp_path / "runs" / STRATEGY_ID / m["run_id"] / "run.log").read_text(encoding="utf-8")
        assert "Outcome" in log and "Paper trading" in log
    events = read_events(tmp_path / "runs", STRATEGY_ID)
    assert events and all(e["session"] == session.session_id for e in events)
    # What pass 1 opened, pass 2 does not open again.
    first, second = runs[0]["run_id"], runs[1]["run_id"]
    opened = {e["basket"] for e in events if e["run_id"] == first and e["type"] == "signal" and e["accepted"]}
    again = {e["basket"]: e["reason"] for e in events if e["run_id"] == second and e["type"] == "signal"}
    assert all(again[b].startswith("already open as") for b in opened)
    text = ledger_report(tmp_path / "runs", STRATEGY_ID, n=1000, session=session.session_id)
    assert f"session {session.session_id}" in text and "2 run(s) on disk" in text
    assert "0 position(s) selected" in ledger_report(tmp_path / "runs", STRATEGY_ID, n=1000, session="other")


def test_the_duration_flag_runs_a_session_and_reports_it(tmp_path, monkeypatch, capsys):
    clock = FakeClock()

    def replay(**options):  # the CLI's run(), on the snapshot and the fake clock
        options.update(snapshot_in=str(SNAPSHOT), seed=12345)
        directory = runner.run(clock=clock, **options)
        clock.advance(minutes=20)
        return directory

    monkeypatch.setattr(cli, "run", replay)
    monkeypatch.setattr(cli, "run_session", partial(run_session, clock=clock))
    quotes = tmp_path / "quotes.json"
    cli.main(["--duration", "0:30", "--runs-dir", str(tmp_path / "runs"), "--snapshot-out", str(quotes),
              "--model", "independent", "--simulations", "10000"])
    out = capsys.readouterr().out
    assert "  pass 1 " in out and "  pass 2 " in out
    assert "Session 20260923T170000Z ended: deadline reached; 2 pass(es), 0 failed" in out
    assert "Paper ledger" in out and "session 20260923T170000Z" in out
    saved = sorted(p.name for p in tmp_path.glob("quotes-*.json"))
    assert saved == ["quotes-20260923T170000Z-0001.json", "quotes-20260923T170000Z-0002.json"]
    assert json.loads((tmp_path / saved[0]).read_text(encoding="utf-8"))["records"]


def test_a_session_on_a_snapshot_is_refused(tmp_path):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--duration", "1:00", "--snapshot-in", str(SNAPSHOT), "--runs-dir", str(tmp_path)])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        cli.main(["--duration", "1:60", "--runs-dir", str(tmp_path)])
    assert exc.value.code == 2


def test_a_live_session_is_refused_before_anything_is_sent(tmp_path, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("no client may be opened")

    monkeypatch.setattr(runner, "KalshiClient", no_network)
    with pytest.raises(SystemExit, match="paper"):
        cli.main(["--duration", "1:00", "--mode", "live", "--runs-dir", str(tmp_path)])


class _Lines:
    """A text stream that collects printed lines."""

    def __init__(self, lines):
        self.lines = lines

    def write(self, text):
        self.lines.extend(line for line in text.split("\n") if line)
        return len(text)

    def flush(self):
        pass
