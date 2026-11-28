"""Append-only facts. SQLite transactions serialize portfolio reservations."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
import warnings
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
        self.root = Path(root or os.environ.get("STRATEGY_RUNS_DIR", "runs")).expanduser()
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
            CREATE INDEX IF NOT EXISTS events_pool_type ON events(portfolio_id, type, sequence);
            CREATE INDEX IF NOT EXISTS events_pool_observation
                ON events(portfolio_id, json_extract(data, '$.kind'), sequence) WHERE type='observation';
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY, portfolio_id TEXT NOT NULL, run_id TEXT,
                request TEXT NOT NULL, context TEXT NOT NULL, status TEXT NOT NULL,
                reserved TEXT NOT NULL, filled TEXT NOT NULL DEFAULT '0', venue_id TEXT);
            CREATE INDEX IF NOT EXISTS orders_portfolio ON orders(portfolio_id);
            CREATE INDEX IF NOT EXISTS orders_venue ON orders(venue_id);
            CREATE TABLE IF NOT EXISTS checkpoints (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cash_pools (
                id TEXT PRIMARY KEY REFERENCES portfolios(id), venue TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reporting_portfolios (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, pools TEXT NOT NULL, label TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY, venue TEXT NOT NULL, mode TEXT NOT NULL, created_at TEXT NOT NULL,
                metadata TEXT NOT NULL);
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
        pool = self.db.execute('SELECT venue FROM cash_pools WHERE id=?', (portfolio,)).fetchone()
        if pool and kind == 'transaction':
            from .accounting import venue_of
            data = dict(data)
            venue = data.setdefault('venue', pool['venue'])
            if venue != pool['venue'] or any(venue_of(leg['market']) != venue for leg in data.get('legs', [])):
                raise ValueError('economic transaction belongs to a different cash-pool venue')
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
        """Deprecated single-venue creation spelling; use create_pool with an explicit venue."""
        warnings.warn('Store.create is deprecated; use create_pool(id, venue, cash)', DeprecationWarning, stacklevel=2)
        venues = metadata.pop('venues', None)
        if venues and len(venues) != 1:
            raise ValueError('create a separate cash pool for each venue')
        venue = metadata.pop('venue', None) or (venues[0] if venues else 'kalshi')
        self.create_pool(identifier, venue, cash, mode, **metadata)

    def create_pool(self, identifier: str, venue: str, cash: str = "1000", mode: str = "paper",
                    fund: bool = True, **metadata: Any) -> None:
        """Create one persistent venue allocation. Paper funding can be arbitrarily large.
        `fund=False` creates it empty, for a pool funded by allocation (a virtual subaccount)."""
        if not identifier or not venue or metadata.get('venues'):
            raise ValueError('a cash pool needs an ID and exactly one venue')
        if mode not in ("paper", "live") or decimal(cash) < 0:
            raise ValueError("invalid cash-pool mode or initial cash")
        with self.transaction():
            if self.db.execute("SELECT 1 FROM portfolios WHERE id=?", (identifier,)).fetchone() or self.db.execute(
                    'SELECT 1 FROM reporting_portfolios WHERE id=?', (identifier,)).fetchone():
                raise ValueError(f"cash pool or portfolio already exists: {identifier}; reopen it instead")
            self.db.execute("INSERT INTO portfolios VALUES(?,?,?,?)", (identifier, mode, utc(), encode(metadata)))
            self.db.execute('INSERT INTO cash_pools VALUES(?,?)', (identifier, venue))
            if mode == "paper" and fund:
                self.append("transaction", {"reason": "funding", "cash_delta": str(decimal(cash)),
                            "external_delta": str(decimal(cash)), "fee": "0", "legs": []}, identifier,
                            source=f"initial-funding:{identifier}")

    def pools(self, venue: str | None = None) -> list[dict[str, Any]]:
        """Executable cash pools; old aggregate portfolios remain report-only."""
        venues = dict(self.db.execute('SELECT id,venue FROM cash_pools'))
        return [{**p, 'venue': venues[p['id']]} for p in self.portfolios()
                if p['id'] in venues and (venue is None or venues[p['id']] == venue)]

    def create_portfolio(self, identifier: str, pools: list[str], label: str = '') -> None:
        """A flat, immutable reporting selection. It owns no cash or orders."""
        with self.transaction():
            known = {p['id']: p for p in self.pools()}
            if not pools or len(set(pools)) != len(pools) or not set(pools) <= known.keys():
                raise ValueError('select distinct, existing cash pools')
            if len({known[p]['mode'] for p in pools}) != 1:
                raise ValueError('a reporting portfolio cannot mix paper and live pools')
            if self.db.execute('SELECT 1 FROM portfolios WHERE id=?', (identifier,)).fetchone():
                raise ValueError('portfolio ID conflicts with a cash pool or legacy portfolio')
            self.db.execute('INSERT INTO reporting_portfolios VALUES(?,?,?,?)',
                            (identifier, utc(), encode(pools), label))

    def add_to_portfolio(self, identifier: str, pools: list[str]) -> None:
        """Add pools to a reporting portfolio, e.g. a virtual subaccount opened mid-run.

        Only a pool with no economic history but funding can join: its equity then equals
        its funding, so counting it from its start adds no P&L the portfolio didn't earn.
        Pools never leave; a closed sub keeps its realized P&L at zero equity."""
        with self.transaction():
            group = next((g for g in self.reporting_portfolios() if g['id'] == identifier), None)
            if group is None or group.get('retired_at'):
                raise ValueError(f'no active reporting portfolio {identifier}')
            known = {p['id']: p for p in self.pools()}
            current = [known[p] for p in group['pools'] if p in known]
            if not pools or len(set(pools)) != len(pools) or not set(pools) <= known.keys() or set(pools) & set(group['pools']):
                raise ValueError('add distinct existing cash pools not already in the portfolio')
            if {known[p]['mode'] for p in pools} != {current[0]['mode']}:
                raise ValueError('a reporting portfolio cannot mix paper and live pools')
            for pid in pools:
                for e in self.events(pid, kinds=('transaction',)):
                    if e['data'].get('legs') or e['data'].get('reason') != 'funding':
                        raise ValueError(f'{pid} has trading history; only a pool with funding alone can join')
            self.db.execute('UPDATE reporting_portfolios SET pools=? WHERE id=?',
                            (encode(group['pools'] + list(pools)), identifier))
            self.append('observation', {'kind': 'portfolio_members_added', 'portfolio': identifier, 'pools': list(pools)})

    def account_key(self, pool: dict[str, Any]) -> tuple | None:
        """Which real (or hypothetical) account a pool's cash belongs to. Pools with the
        same key share one balance; None means the pool stands alone."""
        meta, venue = pool['metadata'], pool['venue']
        if pool['mode'] == 'live':
            if venue == 'kalshi' and 'subaccount' in meta:
                return ('kalshi', meta['subaccount'], meta.get('exchange_index', 0))
            if venue == 'polymarket_us':
                return ('polymarket_us',)
        if meta.get('account'):
            return (pool['mode'], venue, meta['account'])
        return None

    def reporting_portfolios(self) -> list[dict[str, Any]]:
        retired = {e['data']['portfolio']: e['effective_at'] for e in
                   self.events(kinds=('observation',), observations=('portfolio_retired',))}
        return [{**dict(r), 'pools': json.loads(r['pools']),
                 **({'retired_at': retired[r['id']]} if r['id'] in retired else {})}
                for r in self.db.execute('SELECT * FROM reporting_portfolios ORDER BY id')]

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
        # Split selective pool observations from economic facts so neither query
        # walks historical quote captures. UNION ALL retains every original row.
        split = (portfolio is not None and observations is not None
                 and (kinds is None or "observation" in kinds))
        if observations is not None and not split:
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
        common, common_args = list(conditions), list(args)
        if split:
            kinds = tuple(k for k in (kinds or ()) if k != 'observation') if kinds is not None else None
            conditions.append("type != 'observation'")
        if kinds is not None:
            if not kinds:
                conditions.append("0")
            else:
                conditions.append("type IN (" + ",".join("?" for _ in kinds) + ")")
                args.extend(kinds)
        table = ("events INDEXED BY events_pool_type" if kinds is not None else
                 "events INDEXED BY events_portfolio") if portfolio is not None else "events"
        sql = f"SELECT * FROM {table}" + (" WHERE " + " AND ".join(conditions) if conditions else "")
        if split and observations:
            selected = common + ["type='observation'", "json_extract(data, '$.kind') IN ("
                                 + ",".join("?" for _ in observations) + ")"]
            sql += " UNION ALL SELECT * FROM events INDEXED BY events_pool_observation WHERE " + " AND ".join(selected)
            args.extend([*common_args, *observations])
        return [{**dict(r), "data": json.loads(r["data"])} for r in self.db.execute(sql + " ORDER BY effective_at,sequence", args)]

    def latest_health(self, component: str) -> dict[str, Any] | None:
        """The newest health record a monitor component wrote, or None.

        Reads backwards along the (type, effective_at) index and stops at the first
        match, without loading the component's complete history."""
        row = self.db.execute(
            "SELECT * FROM events WHERE type='health' AND json_extract(data,'$.component')=? "
            "ORDER BY effective_at DESC, sequence DESC LIMIT 1", (component,)).fetchone()
        return {**dict(row), "data": json.loads(row["data"])} if row else None

    def reserved(self, portfolio: str) -> Decimal:
        return sum((decimal(r[0]) for r in self.db.execute("SELECT reserved FROM orders WHERE portfolio_id=?", (portfolio,))), Decimal(0))

    def backup(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(destination) as target:
            self.db.backup(target)

    def close(self) -> None:
        self.db.close()
