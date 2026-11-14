"""Download two-outcome Kalshi events and the hourly price history of one side of each.

Stages (each cached under the data directory, so a rerun resumes):

1. series   -- the full series catalog (category per series).
2. events   -- for every series, settled events with a market closing after `since`.
               Live events come with their markets nested; events whose markets were
               archived before the historical cutoff come without them.
3. archive  -- for series that can hold two-outcome events, the archived markets,
               paged newest-created first until creation is well before `since`.
4. candles  -- hourly candlesticks for one side of each qualifying event.

A qualifying event is mutually exclusive, has exactly two binary markets, and both
markets settled yes/no and closed inside [since, now].
"""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .api import Kalshi

ARCHIVE_LOOKBACK_DAYS = 400  # archived markets are sorted by creation; stop this far before `since`
MAX_PERIODS = 5000            # candlestick request limit
HOUR = 3600


def ts(iso: str | None) -> int | None:
    if not iso:
        return None
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def compact_market(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticker": m["ticker"],
        "event_ticker": m["event_ticker"],
        "market_type": m.get("market_type"),
        "result": m.get("result"),
        "open_ts": ts(m.get("open_time")),
        "close_ts": ts(m.get("close_time")),
        "settled_ts": ts(m.get("settlement_ts")),
        "volume": float(m.get("volume_fp") or m.get("volume") or 0),
        "title": m.get("title"),
    }


def pick_side(event_ticker: str, tickers: list[str]) -> str:
    """Deterministic coin flip, so each event contributes exactly one side."""
    h = int(hashlib.sha256(event_ticker.encode()).hexdigest(), 16)
    return sorted(tickers)[h % 2]


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue  # blank, or cut off when a run was stopped; that series is refetched


class Puller:
    def __init__(self, data: Path, since: int, rate: float = 3.0, workers: int = 4):
        self.data = data
        self.since = since
        self.now = int(time.time())
        self.k = Kalshi(rate=rate)
        self.workers = workers
        data.mkdir(parents=True, exist_ok=True)
        (data / "archive").mkdir(exist_ok=True)
        (data / "candles").mkdir(exist_ok=True)

    # -- stage 1 ---------------------------------------------------------
    def series(self) -> list[dict[str, Any]]:
        path = self.data / "series.json"
        if not path.exists():
            rows = [{"ticker": s["ticker"], "category": s.get("category"), "title": s.get("title")}
                    for s in self.k.get("/series")["series"]]
            path.write_text(json.dumps(rows), encoding="utf-8")
        return json.loads(path.read_text(encoding="utf-8"))

    # -- stage 2 ---------------------------------------------------------
    def _events_for(self, series: str) -> dict[str, Any]:
        out = []
        for e in self.k.pages("/events", "events", series_ticker=series, status="settled",
                              min_close_ts=self.since, with_nested_markets="true", limit=200):
            out.append({
                "event_ticker": e["event_ticker"],
                "mutually_exclusive": bool(e.get("mutually_exclusive")),
                "title": e.get("title"),
                "markets": [compact_market(m) for m in e.get("markets") or []],
            })
        return {"series": series, "events": out}

    def events(self, series: list[dict[str, Any]]) -> None:
        path = self.data / "events.jsonl"
        done = {row["series"] for row in read_jsonl(path)}
        todo = [s["ticker"] for s in series if s["ticker"] not in done]
        print(f"events: {len(done)} series cached, {len(todo)} to fetch")
        self._run(todo, self._events_for, path, "events")

    # -- stage 3 ---------------------------------------------------------
    def archive_targets(self) -> list[str]:
        """Series with mutually exclusive events whose markets were archived.

        If the series also has live events and none of them has two markets, the
        series is assumed not to list two-outcome events and is skipped.
        """
        targets = []
        for row in read_jsonl(self.data / "events.jsonl"):
            me = [e for e in row["events"] if e["mutually_exclusive"]]
            archived = [e for e in me if not e["markets"]]
            live = [e for e in me if e["markets"]]
            if archived and (not live or any(len(e["markets"]) == 2 for e in live)):
                targets.append(row["series"])
        return targets

    def _archive_for(self, series: str) -> dict[str, Any]:
        stop = self.since - ARCHIVE_LOOKBACK_DAYS * 86400
        out = []
        for m in self.k.pages("/historical/markets", "markets", series_ticker=series, limit=1000):
            created = ts(m.get("created_time"))
            if created is not None and created < stop:
                break
            out.append(compact_market(m))
        return {"series": series, "markets": out}

    def archive(self) -> None:
        targets = self.archive_targets()
        todo = [s for s in targets if not (self.data / "archive" / f"{s}.json").exists()]
        print(f"archive: {len(targets)} series, {len(todo)} to fetch")

        def job(series: str) -> None:
            row = self._archive_for(series)
            (self.data / "archive" / f"{series}.json").write_text(json.dumps(row), encoding="utf-8")

        self._run(todo, job, None, "archive")

    # -- qualifying events ----------------------------------------------
    def qualifying(self) -> list[dict[str, Any]]:
        category = {s["ticker"]: s["category"] for s in self.series()}
        archived: dict[str, list[dict[str, Any]]] = {}
        for f in (self.data / "archive").glob("*.json"):
            for m in json.loads(f.read_text(encoding="utf-8"))["markets"]:
                archived.setdefault(m["event_ticker"], []).append(m)
        rows = []
        for row in read_jsonl(self.data / "events.jsonl"):
            for e in row["events"]:
                if not e["mutually_exclusive"]:
                    continue
                markets = e["markets"] or archived.get(e["event_ticker"], [])
                if len(markets) != 2:
                    continue
                if any(m["market_type"] != "binary" or m["result"] not in ("yes", "no") for m in markets):
                    continue
                if any(m["close_ts"] is None or not (self.since <= m["close_ts"] <= self.now) for m in markets):
                    continue
                side = pick_side(e["event_ticker"], [m["ticker"] for m in markets])
                market = next(m for m in markets if m["ticker"] == side)
                rows.append({
                    "series": row["series"],
                    "category": category.get(row["series"]),
                    "event_ticker": e["event_ticker"],
                    "event_title": e["title"],
                    "archived": not e["markets"],
                    "results": sorted(m["result"] for m in markets),
                    **market,
                })
        return rows

    # -- stage 4 ---------------------------------------------------------
    def _candles_for(self, m: dict[str, Any]) -> None:
        start = m["open_ts"] or (m["close_ts"] - 30 * 86400)
        end = m["close_ts"]
        path = (f"/historical/markets/{m['ticker']}/candlesticks" if m["archived"]
                else f"/series/{m['series']}/markets/{m['ticker']}/candlesticks")
        rows = []
        lo = start
        while lo < end:
            hi = min(end, lo + MAX_PERIODS * HOUR)
            data = self.k.get(path, {"start_ts": lo, "end_ts": hi, "period_interval": 60})
            rows.extend(_candle(c) for c in data.get("candlesticks") or [])
            lo = hi
        (self.data / "candles" / f"{m['ticker']}.json").write_text(json.dumps(rows), encoding="utf-8")

    def candles(self, markets: list[dict[str, Any]]) -> None:
        todo = [m for m in markets if not (self.data / "candles" / f"{m['ticker']}.json").exists()]
        print(f"candles: {len(markets)} markets, {len(todo)} to fetch")
        self._run(todo, self._candles_for, None, "candles")

    # -- helpers ---------------------------------------------------------
    def _run(self, items: list[Any], fn: Any, sink: Path | None, label: str) -> None:
        failures = 0
        t0 = time.time()
        with ThreadPoolExecutor(self.workers) as pool, \
                (sink.open("a", encoding="utf-8") if sink else _Null()) as out:
            futures = {pool.submit(fn, item): item for item in items}
            for i, fut in enumerate(as_completed(futures), 1):
                try:
                    result = fut.result()
                    if sink:
                        out.write(json.dumps(result) + "\n")
                except Exception as exc:  # noqa: BLE001 -- keep going, report at the end
                    failures += 1
                    print(f"{label}: {futures[fut]!r:.80} failed: {exc}")
                if i % 100 == 0 or i == len(items):
                    lim = self.k.limiter
                    print(f"{label}: {i}/{len(items)} ({time.time() - t0:.0f}s, "
                          f"{lim.rate:.1f} req/s, {lim.throttles} throttled)", flush=True)
        if failures:
            print(f"{label}: {failures} failures (rerun to retry)")


class _Null:
    def __enter__(self) -> "_Null":
        return self

    def __exit__(self, *_: Any) -> None:
        pass

    def write(self, _: str) -> None:
        pass


def _dollars(d: dict[str, Any] | None, key: str) -> float | None:
    if not d:
        return None
    v = d.get(f"{key}_dollars", d.get(key))
    return None if v is None else float(v)


def _candle(c: dict[str, Any]) -> list[Any]:
    """[end_ts, yes_bid_close, yes_ask_close, trade_close, volume]"""
    return [
        c["end_period_ts"],
        _dollars(c.get("yes_bid"), "close"),
        _dollars(c.get("yes_ask"), "close"),
        _dollars(c.get("price"), "close"),
        float(c.get("volume_fp") or c.get("volume") or 0),
    ]
