"""Append-only facts. SQLite transactions serialize portfolio reservations."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator


_clock_lock = threading.Lock()
_last_time = datetime.min.replace(tzinfo=timezone.utc)


def utc() -> str:
    """Current UTC time, strictly increasing within the process.

    Windows' clock can return the same value for many consecutive calls, which made
    events written back to back share a timestamp and fall on report-window edges.
    Each call is at least one microsecond after the previous one.
    """
    global _last_time
    with _clock_lock:
        now = datetime.now(timezone.utc)
        if now <= _last_time:
            now = _last_time + timedelta(microseconds=1)
        _last_time = now
    return now.isoformat(timespec="microseconds")


def canonical_time(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires a UTC offset")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def encode(value: Any) -> str:
    return json.dumps(value, default=lambda x: str(x) if isinstance(x, (Decimal, Path)) else _unsupported(x),
                      allow_nan=False, separators=(",", ":"), sort_keys=True)


def _unsupported(value: Any) -> Any:
    raise TypeError(f"not JSON-compatible: {type(value).__name__}")


def decimal(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("nonfinite monetary value")
    return result


class Store:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.environ.get("STRATEGY_RUNS_DIR", "runs"))
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "accounting.sqlite3"
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        # portfolio -> cached accounting replay (see accounting.portfolio_state).
        self.state_cache: dict[str, Any] = {}
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS portfolios (
                id TEXT PRIMARY KEY, mode TEXT NOT NULL, created_at TEXT NOT NULL,
                metadata TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id TEXT NOT NULL UNIQUE, source_key TEXT UNIQUE,
                schema_version INTEGER NOT NULL DEFAULT 1,
                portfolio_id TEXT, run_id TEXT, type TEXT NOT NULL,
                effective_at TEXT NOT NULL, recorded_at TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_portfolio ON events(portfolio_id, sequence);
            CREATE INDEX IF NOT EXISTS events_type ON events(type, effective_at);
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY, portfolio_id TEXT NOT NULL, run_id TEXT,
                request TEXT NOT NULL, context TEXT NOT NULL, status TEXT NOT NULL,
                reserved TEXT NOT NULL, filled TEXT NOT NULL DEFAULT '0', venue_id TEXT);
            CREATE TABLE IF NOT EXISTS checkpoints (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self.db.in_transaction:
            yield
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            # Cached replays may include rows this transaction wrote and just discarded.
            self.state_cache.clear()
            raise
        else:
            self.db.commit()

    def append(self, kind: str, data: dict[str, Any], portfolio: str | None = None,
               run: str | None = None, source: str | None = None,
               at: str | None = None) -> str:
        payload = encode(data)  # Validate before insertion, even on duplicate delivery.
        if source:
            prior = self.db.execute("SELECT record_id,data,type,portfolio_id FROM events WHERE source_key=?", (source,)).fetchone()
            if prior:
                if prior["data"] != payload or prior["type"] != kind or prior["portfolio_id"] != portfolio:
                    raise ValueError(f"conflicting evidence for source {source}")
                return prior["record_id"]
        identifier, now = uuid.uuid4().hex, utc()
        self.db.execute("INSERT INTO events(record_id,source_key,portfolio_id,run_id,type,effective_at,recorded_at,data) VALUES(?,?,?,?,?,?,?,?)",
                        (identifier, source, portfolio, run, kind, canonical_time(at) if at else now, now, payload))
        return identifier

    def create(self, identifier: str, cash: str = "1000", mode: str = "paper", **metadata: Any) -> None:
        if mode not in ("paper", "live") or decimal(cash) < 0:
            raise ValueError("invalid portfolio mode or initial cash")
        with self.transaction():
            if self.db.execute("SELECT 1 FROM portfolios WHERE id=?", (identifier,)).fetchone():
                raise ValueError(f"portfolio already exists: {identifier}; reopen it instead")
            self.db.execute("INSERT INTO portfolios VALUES(?,?,?,?)", (identifier, mode, utc(), encode(metadata)))
            if mode == "paper":
                self.append("transaction", {"reason": "funding", "cash_delta": str(decimal(cash)),
                            "external_delta": str(decimal(cash)), "fee": "0", "legs": []}, identifier,
                            source=f"initial-funding:{identifier}")

    def portfolios(self) -> list[dict[str, Any]]:
        return [{**dict(r), "metadata": json.loads(r["metadata"])} for r in self.db.execute("SELECT * FROM portfolios ORDER BY id")]

    def events(self, portfolio: str | None = None, until: str | None = None,
               recorded_until: str | None = None, kinds: tuple[str, ...] | None = None,
               observations: tuple[str, ...] | None = None,
               after_sequence: int | None = None) -> list[dict[str, Any]]:
        """Events in effective-time order.

        `observations` keeps only observations whose data `kind` is listed (other event
        types are unaffected), so callers can skip heartbeats and diagnostics.
        `after_sequence` returns only events recorded after that sequence number.
        """
        conditions, args = [], []
        if observations is not None:
            conditions.append("(type != 'observation' OR json_extract(data, '$.kind') IN ("
                              + ",".join("?" for _ in observations) + "))" if observations else "type != 'observation'")
            args.extend(observations)
        if after_sequence is not None:
            conditions.append("sequence > ?")
            args.append(after_sequence)
        for col, value in (("portfolio_id", portfolio), ("effective_at", until), ("recorded_at", recorded_until)):
            if value is not None:
                conditions.append(col + ("=?" if col == "portfolio_id" else "<=?"))
                args.append(value if col == "portfolio_id" else canonical_time(value))
        if kinds is not None:
            if not kinds:
                return []
            conditions.append("type IN (" + ",".join("?" for _ in kinds) + ")")
            args.extend(kinds)
        sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(conditions) if conditions else "")
        return [{**dict(r), "data": json.loads(r["data"])} for r in self.db.execute(sql + " ORDER BY effective_at,sequence", args)]

    def reserved(self, portfolio: str) -> Decimal:
        return sum((decimal(r[0]) for r in self.db.execute("SELECT reserved FROM orders WHERE portfolio_id=?", (portfolio,))), Decimal(0))

    def backup(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(destination) as target:
            self.db.backup(target)

    def close(self) -> None:
        self.db.close()
