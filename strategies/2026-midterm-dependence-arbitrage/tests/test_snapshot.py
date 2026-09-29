"""Snapshot save/load: exact replay, strictness, and the committed snapshot."""

from __future__ import annotations

import json

import pytest

from strategy.snapshot import RecordingClient, SnapshotClient, SnapshotMiss, save_snapshot
from tests.conftest import committed_snapshot


class FakeClient:
    def fetch_event(self, ticker):
        if ticker == "GONE":
            return None
        return {"event_ticker": ticker, "title": "t", "mutually_exclusive": True, "markets": []}

    def fetch_orderbook(self, ticker):
        return {"yes": [[0.4, 10.0]], "no": [[0.55, 3.0]]}

    def fetch_series(self, ticker):
        return {"fee_type": "quadratic", "fee_multiplier": 1.0}


def _record(tmp_path):
    rec = RecordingClient(FakeClient(), source="fake")
    live = (rec.fetch_event("E-1"), rec.fetch_orderbook("M-1"), rec.fetch_series("S"), rec.fetch_event("GONE"))
    path = rec.save(tmp_path / "snap.json", captured_at="2026-09-23T00:00:00Z")
    return live, path


def test_replay_returns_exactly_what_was_recorded(tmp_path):
    live, path = _record(tmp_path)
    replay = SnapshotClient(path)
    assert (replay.fetch_event("E-1"), replay.fetch_orderbook("M-1"), replay.fetch_series("S")) == live[:3]
    assert replay.fetch_event("GONE") is None  # a recorded 404 replays as a 404
    assert replay.captured_at == "2026-09-23T00:00:00Z"
    assert replay.source == "fake"


def test_replay_is_strict(tmp_path):
    _, path = _record(tmp_path)
    replay = SnapshotClient(path)
    with pytest.raises(SnapshotMiss, match="book:NEVER-FETCHED"):
        replay.fetch_orderbook("NEVER-FETCHED")
    assert issubclass(SnapshotMiss, KeyError)


def test_one_record_per_line(tmp_path):
    _, path = _record(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    record_lines = [line for line in lines if line.startswith('  "')]
    assert len(record_lines) == 4
    assert record_lines == sorted(record_lines)
    json.loads(path.read_text(encoding="utf-8"))  # still valid JSON


def test_unknown_format_rejected(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"format": 99, "captured_at": "x", "records": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="format"):
        SnapshotClient(path)


def test_save_snapshot_creates_parent_dirs(tmp_path):
    path = save_snapshot(tmp_path / "a" / "b.json", {"series:S": None}, captured_at="t")
    assert SnapshotClient(path).fetch_series("S") is None


def test_committed_snapshot_is_named_for_its_capture_date():
    path = committed_snapshot()
    snap = SnapshotClient(path)
    assert snap.captured_at.startswith(path.stem)
