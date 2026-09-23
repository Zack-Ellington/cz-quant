"""Save and replay every quote a run uses.

A snapshot is one JSON file holding every normalized API response the run read:
events, order books, and series fee settings, keyed ``"event:<ticker>"``,
``"book:<ticker>"``, and ``"series:<ticker>"``. A response that was a 404 is
stored as ``null`` so a replay reproduces "no such market" too.

* ``RecordingClient`` wraps a live client and remembers what it returned.
* ``SnapshotClient`` serves a saved file. It is strict: asking for a key the
  snapshot does not contain raises ``SnapshotMiss`` instead of quietly treating
  it as a missing market, so a code change that reads new data cannot pass a
  reproducibility test by accident.

Records are written one per line, sorted, so a refreshed snapshot diffs cleanly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from strategy.api import Book, Client

FORMAT_VERSION = 1


class SnapshotMiss(KeyError):
    """A replay asked for data the snapshot does not contain."""


class RecordingClient:
    """Pass-through client that records every response for ``save``."""

    def __init__(self, inner: Client, *, source: str = "") -> None:
        self._inner = inner
        self._source = source
        self.records: dict[str, object] = {}

    def fetch_event(self, ticker: str) -> dict | None:
        return self._record(f"event:{ticker}", self._inner.fetch_event(ticker))

    def fetch_orderbook(self, ticker: str) -> Book | None:
        return self._record(f"book:{ticker}", self._inner.fetch_orderbook(ticker))

    def fetch_series(self, ticker: str) -> dict | None:
        return self._record(f"series:{ticker}", self._inner.fetch_series(ticker))

    def save(self, path: str | Path, *, captured_at: str | None = None) -> Path:
        captured_at = captured_at or datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        return save_snapshot(
            path, self.records, captured_at=captured_at, source=self._source
        )

    def _record(self, key: str, value):
        self.records[key] = value
        return value


class SnapshotClient:
    """Replays a saved snapshot. Unknown keys raise ``SnapshotMiss``."""

    def __init__(self, path: str | Path) -> None:
        data = load_snapshot(path)
        self.path = Path(path)
        self.captured_at: str = data["captured_at"]
        self.source: str = data.get("source", "")
        self._records: dict[str, object] = data["records"]

    def fetch_event(self, ticker: str) -> dict | None:
        return self._get(f"event:{ticker}")

    def fetch_orderbook(self, ticker: str) -> Book | None:
        return self._get(f"book:{ticker}")

    def fetch_series(self, ticker: str) -> dict | None:
        return self._get(f"series:{ticker}")

    def _get(self, key: str):
        if key not in self._records:
            raise SnapshotMiss(f"{key} is not in snapshot {self.path.name}")
        return self._records[key]


def save_snapshot(
    path: str | Path,
    records: dict[str, object],
    *,
    captured_at: str,
    source: str = "",
) -> Path:
    """Write ``records`` to ``path``, one record per line."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "{",
        f' "format": {FORMAT_VERSION},',
        f' "captured_at": {json.dumps(captured_at)},',
        f' "source": {json.dumps(source)},',
        ' "records": {',
    ]
    keys = sorted(records)
    for i, key in enumerate(keys):
        value = json.dumps(records[key], sort_keys=True, separators=(",", ":"))
        comma = "," if i < len(keys) - 1 else ""
        lines.append(f"  {json.dumps(key)}: {value}{comma}")
    lines += [" }", "}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


def load_snapshot(path: str | Path) -> dict:
    with Path(path).open(encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("format") != FORMAT_VERSION:
        raise ValueError(
            f"{path}: snapshot format {data.get('format')!r}, expected {FORMAT_VERSION}"
        )
    return data
