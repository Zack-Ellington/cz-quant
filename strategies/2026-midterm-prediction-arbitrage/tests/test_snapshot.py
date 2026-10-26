"""Snapshot save/load: exact replay, fetch-time stamps, scans, strictness."""

from __future__ import annotations

import itertools
import json

import pytest

from strategy.snapshot import (
    RecordingClient,
    SnapshotClient,
    SnapshotMiss,
    save_snapshot,
    scan_view,
)
from tests.conftest import committed_snapshot


class FakeClient:
    def __init__(self):
        self.books = {"M-1": {"yes": [[0.4, 10.0]], "no": [[0.55, 3.0]]}}

    def fetch_event(self, ticker):
        if ticker == "GONE":
            return None
        return {"event_ticker": ticker, "title": "t", "mutually_exclusive": True, "markets": []}

    def fetch_orderbook(self, ticker):
        return self.books.get(ticker)

    def fetch_series(self, ticker):
        return {"fee_type": "quadratic", "fee_multiplier": 1.0}


def ticking_clock():
    counter = itertools.count()
    return lambda: f"2026-09-29T12:00:{next(counter):02d}.000000Z"


def _record(tmp_path):
    fake = FakeClient()
    rec = RecordingClient(fake, source="fake", clock=ticking_clock())
    live = (rec.fetch_event("E-1"), rec.fetch_orderbook("M-1"), rec.fetch_series("S"), rec.fetch_event("GONE"))
    fake.books["M-1"] = {"yes": [[0.45, 2.0]], "no": [[0.50, 1.0]]}  # the book moves
    confirmed = scan_view(rec, "confirm").fetch_orderbook("M-1")
    path = rec.save(tmp_path / "snap.json")
    return live, confirmed, path


def test_replay_returns_exactly_what_was_recorded(tmp_path):
    live, _, path = _record(tmp_path)
    replay = SnapshotClient(path)
    assert (replay.fetch_event("E-1"), replay.fetch_orderbook("M-1"), replay.fetch_series("S")) == live[:3]
    assert replay.fetch_event("GONE") is None  # a recorded 404 replays as a 404
    assert replay.source == "fake"
    assert replay.format == 2


def test_records_are_stamped_when_fetched_not_when_saved(tmp_path):
    _, _, path = _record(tmp_path)
    replay = SnapshotClient(path)
    assert replay.captured_at == "2026-09-29T12:00:00.000000Z"  # first fetch
    assert replay.completed_at == "2026-09-29T12:00:04.000000Z"  # the confirm fetch
    assert replay.fetched_at("book:M-1") == "2026-09-29T12:00:01.000000Z"


def test_a_confirmation_scan_is_recorded_separately(tmp_path):
    live, confirmed, path = _record(tmp_path)
    replay = SnapshotClient(path)
    assert replay.fetch_orderbook("M-1") == live[1]
    assert scan_view(replay, "confirm").fetch_orderbook("M-1") == confirmed != live[1]


def test_replay_is_strict(tmp_path):
    _, _, path = _record(tmp_path)
    replay = SnapshotClient(path)
    with pytest.raises(SnapshotMiss, match="book:NEVER-FETCHED"):
        replay.fetch_orderbook("NEVER-FETCHED")
    with pytest.raises(SnapshotMiss, match="confirm:series:S"):
        scan_view(replay, "confirm").fetch_series("S")
    assert issubclass(SnapshotMiss, KeyError)


def test_one_record_per_line(tmp_path):
    _, _, path = _record(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    record_lines = [line for line in lines if line.startswith('  "')]
    assert len(record_lines) == 5
    assert record_lines == sorted(record_lines)
    json.loads(path.read_text(encoding="utf-8"))
    assert not (tmp_path / "snap.json.tmp").exists()  # written atomically


def test_format_one_still_replays_with_one_scan(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"format": 1, "captured_at": "2026-09-23T16:25:13Z", "source": "x",
                                "records": {"book:M-1": {"yes": [], "no": []}}}), encoding="utf-8")
    replay = SnapshotClient(path)
    assert replay.fetch_orderbook("M-1") == {"yes": [], "no": []}
    assert scan_view(replay, "confirm").fetch_orderbook("M-1") == {"yes": [], "no": []}
    assert replay.fetched_at("book:M-1") is None


def test_unknown_format_rejected(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"format": 99, "captured_at": "x", "records": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="format"):
        SnapshotClient(path)


def test_save_snapshot_creates_parent_dirs(tmp_path):
    path = save_snapshot(tmp_path / "a" / "b.json", {"series:S": {"t": "2026-01-01T00:00:00.000000Z", "data": None}})
    assert SnapshotClient(path).fetch_series("S") is None


def test_committed_snapshot_is_named_for_its_capture_date():
    path = committed_snapshot()
    assert SnapshotClient(path).captured_at.startswith(path.stem)
