"""Save and replay every quote a run uses.

A snapshot is one JSON file holding every normalized API response the run read:
events (with each market's rules text, status, and result), order books, and
series fee settings. Keys are ``"event:<ticker>"``, ``"book:<ticker>"``, and
``"series:<ticker>"``; a response that was a 404 is stored as ``null`` so a
replay reproduces "no such market" too.

Format 2 (written now) stores each record as ``{"t": <fetched at>, "data":
...}``, stamped when it was *fetched*, not when the file was saved, and puts the
first and last fetch times at the top. It also supports scan tags: the paper
trader re-reads the order books of its candidate trades in a second,
confirmation scan, recorded under ``"confirm:book:<ticker>"`` so the replay sees
the same two scans the live run did.

Format 1 (the 2026-09-23 snapshot) has bare records and one scan. It still
replays; a confirmation scan on it re-reads the same quotes.

* ``RecordingClient`` wraps a live client, remembers what it returned, and can
  ``save`` at any point -- the runner saves right after acquiring quotes, before
  calibration, so a failed fit still leaves a replayable snapshot.
* ``SnapshotClient`` serves a saved file. It is strict: asking for a key the
  snapshot does not contain raises ``SnapshotMiss`` rather than quietly treating
  it as a missing market.

Records are written one per line, sorted, so a refreshed snapshot diffs cleanly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from strategy.api import Book, Client

FORMAT_VERSION = 2
READABLE_FORMATS = (1, 2)


class SnapshotMiss(KeyError):
    """A replay asked for data the snapshot does not contain."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def scan_view(client: Client, tag: str) -> Client:
    """The client as seen by a tagged scan (e.g. ``"confirm"``).

    Recording and replaying clients key the scan's records separately; a live
    client has nothing to separate and is returned as is.
    """
    view = getattr(client, "with_tag", None)
    return view(tag) if view is not None else client


class RecordingClient:
    """Pass-through client that records every response for ``save``."""

    def __init__(self, inner: Client, *, source: str = "", clock=utc_now) -> None:
        self._inner = inner
        self._source = source
        self._clock = clock
        self._tag = ""
        self.records: dict[str, dict] = {}

    def with_tag(self, tag: str) -> "RecordingClient":
        view = RecordingClient.__new__(RecordingClient)
        view.__dict__.update(self.__dict__)
        view._tag = f"{tag}:" if tag else ""
        return view  # shares ``records`` with the original

    def fetch_event(self, ticker: str) -> dict | None:
        return self._record(f"event:{ticker}", lambda: self._inner.fetch_event(ticker))

    def fetch_orderbook(self, ticker: str) -> Book | None:
        return self._record(f"book:{ticker}", lambda: self._inner.fetch_orderbook(ticker))

    def fetch_series(self, ticker: str) -> dict | None:
        return self._record(f"series:{ticker}", lambda: self._inner.fetch_series(ticker))

    def save(self, path: str | Path) -> Path:
        """Write everything recorded so far. Safe to call more than once."""
        return save_snapshot(path, self.records, source=self._source)

    def _record(self, key: str, fetch):
        value = fetch()
        self.records[self._tag + key] = {"t": self._clock(), "data": value}
        return value


class SnapshotClient:
    """Replays a saved snapshot. Unknown keys raise ``SnapshotMiss``."""

    def __init__(self, path: str | Path) -> None:
        data = load_snapshot(path)
        self.path = Path(path)
        self.format: int = data["format"]
        self.captured_at: str = data["captured_at"]
        self.completed_at: str = data.get("completed_at", data["captured_at"])
        self.source: str = data.get("source", "")
        self._records: dict[str, object] = data["records"]
        self._tag = ""

    def with_tag(self, tag: str) -> "SnapshotClient":
        view = SnapshotClient.__new__(SnapshotClient)
        view.__dict__.update(self.__dict__)
        # Format 1 predates tagged scans: a tagged scan re-reads the one scan.
        view._tag = f"{tag}:" if tag and self.format >= 2 else ""
        return view

    def fetch_event(self, ticker: str) -> dict | None:
        return self._get(f"event:{ticker}")

    def fetch_orderbook(self, ticker: str) -> Book | None:
        return self._get(f"book:{ticker}")

    def fetch_series(self, ticker: str) -> dict | None:
        return self._get(f"series:{ticker}")

    def fetched_at(self, key: str) -> str | None:
        record = self._records.get(key)
        return record["t"] if self.format >= 2 and record is not None else None

    def _get(self, key: str):
        key = self._tag + key
        if key not in self._records:
            raise SnapshotMiss(f"{key} is not in snapshot {self.path.name}")
        record = self._records[key]
        return record["data"] if self.format >= 2 else record


def save_snapshot(path: str | Path, records: dict[str, dict], *, source: str = "") -> Path:
    """Write format-2 ``records`` (each ``{"t", "data"}``) one per line."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    times = sorted(r["t"] for r in records.values())
    lines = [
        "{",
        f' "format": {FORMAT_VERSION},',
        f' "captured_at": {json.dumps(times[0] if times else utc_now())},',
        f' "completed_at": {json.dumps(times[-1] if times else utc_now())},',
        f' "source": {json.dumps(source)},',
        ' "records": {',
    ]
    keys = sorted(records)
    for i, key in enumerate(keys):
        value = json.dumps(records[key], sort_keys=True, separators=(",", ":"))
        comma = "," if i < len(keys) - 1 else ""
        lines.append(f"  {json.dumps(key)}: {value}{comma}")
    lines += [" }", "}"]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    tmp.replace(path)  # never leave a half-written snapshot behind
    return path


def load_snapshot(path: str | Path) -> dict:
    with Path(path).open(encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("format") not in READABLE_FORMATS:
        raise ValueError(
            f"{path}: snapshot format {data.get('format')!r}, expected one of {READABLE_FORMATS}"
        )
    return data
