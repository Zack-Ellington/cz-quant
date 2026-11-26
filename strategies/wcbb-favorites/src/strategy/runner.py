"""Buy the favored team in women's college basketball games inside a price range.

Each scan lists open KXNCAAWBGAME events. An event qualifies when it has exactly
two markets (one per team) with fresh two-sided quotes. The favorite is the team
whose YES midpoint is at least 50%. If that midpoint is inside [low, high], its
spread is at most max_spread, and this portfolio has never ordered on the event,
the strategy sends one IOC bid for YES at the current ask, sized to the stake.

One order per event, ever: the first scan that sees the favorite inside the range
is the entry, matching the first-touch test in research/kalshi-calibration. Paper
settlement comes from the shared market monitor, not from this process.

Configuration is read from the environment (see Config.from_env). Paper only.
"""

from __future__ import annotations

import itertools
import math
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Iterable

SERIES = "KXNCAAWBGAME"
STRATEGY = "wcbb-favorites"


@dataclass(frozen=True)
class Config:
    low: Decimal = Decimal("0.80")     # favorite midpoint range, inclusive
    high: Decimal = Decimal("0.90")
    max_spread: Decimal = Decimal("0.02")
    stake: Decimal = Decimal("25")     # dollars of contracts per event (before fees)
    interval: float = 120.0            # seconds between scans

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ
        d = cls()
        c = cls(low=Decimal(env.get("WCBB_LOW", str(d.low))),
                high=Decimal(env.get("WCBB_HIGH", str(d.high))),
                max_spread=Decimal(env.get("WCBB_MAX_SPREAD", str(d.max_spread))),
                stake=Decimal(env.get("WCBB_STAKE", str(d.stake))),
                interval=float(env.get("WCBB_INTERVAL", str(d.interval))))
        if not (Decimal("0.5") <= c.low <= c.high < 1) or c.max_spread <= 0 or c.stake <= 0:
            raise ValueError(f"invalid configuration: {c}")
        return c


@dataclass(frozen=True)
class Quote:
    ticker: str
    bid: Decimal
    ask: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


@dataclass(frozen=True)
class Decision:
    event_ticker: str
    accepted: bool
    reason: str
    ticker: str | None = None
    mid: Decimal | None = None
    ask: Decimal | None = None
    spread: Decimal | None = None
    count: int = 0


def _dollars(m: dict[str, Any], key: str) -> Decimal | None:
    v = m.get(f"{key}_dollars")
    if v is not None:
        return Decimal(str(v))
    v = m.get(key)  # older payloads quote cents
    return None if v is None else Decimal(str(v)) / 100


def quote(m: dict[str, Any]) -> Quote | None:
    """Two-sided YES quote, or None for an empty or one-sided book."""
    bid, ask = _dollars(m, "yes_bid"), _dollars(m, "yes_ask")
    if bid is None or ask is None or bid <= 0 or ask >= 1 or ask < bid:
        return None
    return Quote(m["ticker"], bid, ask)


def decide(event: dict[str, Any], traded: set[str], cfg: Config) -> Decision:
    """The entry decision for one event. Pure, so it is tested without the network."""
    e = event["event_ticker"]
    if e in traded:
        return Decision(e, False, "already ordered on this event")
    markets = event.get("markets") or []
    if len(markets) != 2:
        return Decision(e, False, f"{len(markets)} markets, need 2")
    if any(m.get("status") not in ("active", "open") for m in markets):
        return Decision(e, False, "market not open")
    quotes = [quote(m) for m in markets]
    if any(q is None for q in quotes):
        return Decision(e, False, "no two-sided quote")
    fav = max((q for q in quotes if q is not None), key=lambda q: q.mid)
    base = dict(ticker=fav.ticker, mid=fav.mid, ask=fav.ask, spread=fav.spread)
    if fav.spread > cfg.max_spread:
        return Decision(e, False, "spread too wide", **base)
    if not cfg.low <= fav.mid <= cfg.high:
        return Decision(e, False, "favorite outside price range", **base)
    count = math.floor(cfg.stake / fav.ask)
    if count < 1:
        return Decision(e, False, "stake below one contract", **base)
    return Decision(e, True, "favorite in range", count=count, **base)


def open_events(client: Any) -> list[dict[str, Any]]:
    return list(client.pages("/events", "events", series_ticker=SERIES, status="open",
                             with_nested_markets="true", limit=200))


def traded_events(broker: Any) -> set[str]:
    """Events this portfolio has ever sent an order on (any run, any outcome)."""
    out = set()
    for ev in broker.store.events(broker.portfolio, kinds=("order",)):
        ctx = ev["data"].get("context") or {}
        if ev["data"].get("status") == "intent" and ctx.get("event_ticker"):
            out.add(ctx["event_ticker"])
    return out


def scan(broker: Any, client: Any, cfg: Config) -> list[Decision]:
    traded = traded_events(broker)
    decisions = [decide(e, traded, cfg) for e in open_events(client)]
    for d in decisions:
        if not d.accepted or d.ticker is None or d.ask is None:
            continue
        result = broker.submit({
            "ticker": d.ticker,
            "side": "bid",
            "count": f"{d.count}.00",
            "price": f"{d.ask:.4f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
        }, context={"event_ticker": d.event_ticker, "mid": str(d.mid), "ask": str(d.ask),
                    "spread": str(d.spread), "range": [str(cfg.low), str(cfg.high)]})
        print(f"{d.event_ticker}: bid {d.count} {d.ticker} @ {d.ask} -> "
              f"{result.get('status')} filled {result.get('filled')}", flush=True)
    counts: dict[str, int] = {}
    for d in decisions:
        counts[d.reason] = counts.get(d.reason, 0) + 1
    broker.observe("scan_complete", {"events": len(decisions), "reasons": counts,
                                     "available": str(broker.available)})
    return decisions


def _revision() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def run(once: bool = False, broker: Any = None, client: Any = None,
        scans: Iterable[int] | None = None) -> None:
    cfg = Config.from_env()
    if broker is None:
        from czquant import Broker, Store
        from czquant.kalshi import Kalshi
        from czquant.paper import Paper

        portfolio = os.environ.get("CZQUANT_PORTFOLIO")
        if not portfolio:
            raise ValueError("Set CZQUANT_PORTFOLIO to an explicitly created paper cash pool")
        client = Kalshi(authenticated=False)
        broker = Broker(Store(), portfolio, Paper(client))
    if broker.mode != "paper":
        raise ValueError(f"{STRATEGY} is paper only")
    broker.start(STRATEGY, _revision(), {k: str(v) for k, v in asdict(cfg).items()} | {"series": SERIES})
    for i in scans if scans is not None else (range(1) if once else itertools.count()):
        decisions = scan(broker, client, cfg)
        n = sum(d.accepted for d in decisions)
        print(f"scan {i}: {len(decisions)} open events, {n} entries, "
              f"available ${broker.available:.2f}", flush=True)
        if once or scans is not None:
            continue
        time.sleep(cfg.interval)
