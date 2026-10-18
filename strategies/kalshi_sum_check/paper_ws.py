"""WebSocket paper trader for the sum check. Paper only: nothing here can send an order.

Pipeline
  discovery   every --discover minutes over REST: open mutually exclusive events whose
              no-winner class is EXHAUSTIVE or UNKNOWN, top of book within --band of $1,
              plus every event already held. Their legs are subscribed on the feed.
  feed        Kalshi `orderbook_delta` WebSocket (needs an API key), or REST polling.
  evaluate    on every book change: both trades (buy YES on every leg / buy NO on every
              leg), walked through the books, Kalshi taker fees per leg, sized to
              maximize profit above the risk-free hurdle.
  allocate    candidates ranked by APR, highest first, each checked against the limits.
  execute     wait --latency, re-check on the newest books, then fill every leg or none.
  settle      every --settle minutes, held events are checked for results over REST.

Constraints on every trade
  * no-winner class EXHAUSTIVE or UNKNOWN (GAP, NOT_EXHAUSTIVE and RISKY are skipped);
    events without numeric strikes also need mid prices summing to >= 1 - --unknown-mid-gap
  * profit after fees >= --min-net
  * beats the risk-free rate over the time to payout:
        net > capital * ((1 + rf) ** (days / 365) - 1)
    i.e. compounded APR to the expected settlement date > rf (default 5%)
  * per-event and total capital caps, max open events, cash on hand, a per-event cooldown
  * all legs fill at the re-checked prices, or nothing does
  * kill switch: a file named KILL in runs/kalshi_sum_check/ws/ halts new trades, and so
    does any settled trade that loses money (it would mean an event wasn't exhaustive)

Usage
  uv run python paper_ws.py                      # websocket if a key is set, else REST polling
  uv run python paper_ws.py --feed rest --duration 1:00
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from classifier import CANDIDATE_REASON, EXHAUSTIVE, UNKNOWN, classify
from feeds import KalshiWSFeed, OrderBookStore, RestPollFeed
from kalshi_client import KalshiClient, load_credentials
from monitor import REPO_ROOT
from pricing import taker_fee

WS_RUNS = os.path.join(REPO_ROOT, "runs", "kalshi_sum_check", "ws")
APPROVALS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "approvals.json")
MIN_DAYS = 1 / 24
APR_CAP = 1e6  # keeps the ledger valid JSON for very short holds
log = logging.getLogger("paper_ws")


# --------------------------------------------------------------------------- config

@dataclass
class Config:
    feed: str = "auto"
    bankroll: float = 1000.0
    max_event_frac: float = 0.20
    max_total_frac: float = 1.00
    max_open_events: int = 25
    max_sets: int = 500
    risk_free: float = 0.05
    min_net: float = 0.10
    latency: float = 0.30
    cooldown: float = 30.0
    band: float = 0.03
    max_days: float | None = None
    stale_seconds: float = 60.0
    discover_minutes: float = 10.0
    settle_minutes: float = 5.0
    heartbeat_seconds: float = 30.0
    classes: tuple = (EXHAUSTIVE, UNKNOWN)
    # Every event without numeric strikes (UNKNOWN, and catch-all EXHAUSTIVE) must also
    # pass the market-implied check: if the mids sum below 1 - gap, the market itself
    # prices a real chance that no listed outcome wins. Rules text can't catch everything.
    unknown_mid_gap: float = 0.10
    # Paper fills stay netted from the book until their level has been gone this long.
    refill_seconds: float = 300.0
    # UNKNOWN candidate lists (elections, nominations) trade only if listed in approvals.json.
    require_candidate_approval: bool = True
    duration_minutes: float | None = None


# --------------------------------------------------------------------------- ledger

class Ledger:
    """Append-only JSONL, flushed and fsync'd per record."""

    def __init__(self, path: str, run_id: str):
        self.path, self.run_id = path, run_id
        self._f = open(path, "a", encoding="utf-8")

    def write(self, kind: str, **fields):
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
               "run": self.run_id, "type": kind, **fields}
        self._f.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
        self._f.flush()
        os.fsync(self._f.fileno())
        return rec

    def close(self):
        self._f.close()


def load_approvals(path: str = APPROVALS) -> dict:
    """{"events": [...], "series": [...]} of candidate-list events you've read and approved."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return {"events": set(d.get("events") or []), "series": set(d.get("series") or [])}
    except FileNotFoundError:
        return {"events": set(), "series": set()}


def rebuild_consumed(store, positions: dict):
    """Re-net every open paper trade's fills from the book (R1: no refills after a restart)."""
    for p in positions.values():
        for leg in p.get("legs", []):
            for price, qty in leg.get("levels", []):
                store.consume(leg["ticker"], "ask" if p["side"] == "long" else "bid", price, qty)


def read_ledgers(root: str = WS_RUNS):
    """Every record from every run, oldest run first."""
    if not os.path.isdir(root):
        return
    for run in sorted(os.listdir(root)):
        path = os.path.join(root, run, "ledger.jsonl")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            pass  # a partial last line from a crash


# --------------------------------------------------------------------------- pricing

@dataclass
class Quote:
    event: str
    side: str  # "long" = buy YES on every leg, "short" = buy NO on every leg
    sets: int
    cost: float
    fees: float
    capital: float
    payout: float
    net: float
    gross: float
    days: float
    hurdle: float  # risk-free return over the holding period, as a fraction of capital
    excess: float  # net - capital * hurdle
    apr: float
    legs: list = field(default_factory=list)


def _fill(levels, qty):
    """Per-level fills [(price, take)] for qty contracts, or None if depth is short."""
    out, left = [], qty
    for price, avail in levels:
        if left <= 1e-9:
            break
        take = min(avail, left)
        out.append((price, take))
        left -= take
    return out if left <= 1e-9 else None


def quote(event, tickers, books, side, sets, fee_type, fee_mult, days, rf) -> Quote | None:
    n = len(tickers)
    legs, cost, fees = [], 0.0, 0.0
    for t, b in zip(tickers, books):
        fills = _fill(b.yes_asks if side == "long" else b.yes_bids, sets)
        if fills is None:
            return None
        fee_base = sum(q * p * (1 - p) for p, q in fills)  # symmetric in YES/NO price
        fee = taker_fee(fee_base, fee_type, fee_mult)
        yes_notional = sum(p * q for p, q in fills)
        leg_cost = yes_notional if side == "long" else sets - yes_notional
        legs.append({"ticker": t, "qty": sets, "contract": "YES" if side == "long" else "NO",
                     "vwap": round(leg_cost / sets, 6), "fee": fee, "levels": [[p, q] for p, q in fills]})
        cost += leg_cost
        fees += fee
    payout = sets if side == "long" else (n - 1) * sets
    capital = cost + fees
    net = payout - capital
    hurdle = (1 + rf) ** (days / 365) - 1
    roi = net / capital if capital > 0 else -1
    try:
        apr = math.expm1(math.log1p(roi) * 365 / days) if roi > -1 else -1.0
    except OverflowError:
        apr = APR_CAP
    apr = min(apr, APR_CAP)
    return Quote(event, side, sets, round(cost, 6), round(fees, 2), round(capital, 6), payout, round(net, 6),
                 round(net + fees, 6), days, hurdle, round(net - capital * hurdle, 6), apr, legs)


def best_quote(event, tickers, books, fee_type, fee_mult, days, rf, max_sets, max_capital, min_net) -> tuple[Quote | None, str]:
    """Size each side to maximize profit above the hurdle; return the better side."""
    best, why = None, "no_edge"
    for side in ("long", "short"):
        levels = [b.yes_asks if side == "long" else b.yes_bids for b in books]
        depth = min(sum(q for _, q in lv) for lv in levels)
        top = min(int(math.floor(min(depth, max_sets) + 1e-9)), max_sets)
        if top < 1:
            continue
        # Capital rises with size: binary-search the largest size the limits allow.
        lo, hi = 0, top
        while lo < hi:
            mid = (lo + hi + 1) // 2
            q = quote(event, tickers, books, side, mid, fee_type, fee_mult, days, rf)
            if q and q.capital <= max_capital:
                lo = mid
            else:
                hi = mid - 1
        if lo < 1:
            why = "capital_limit"
            continue
        # Edge only changes where some leg moves to its next price level.
        cands = {lo}
        for lv in levels:
            cum = 0.0
            for _, q in lv:
                cum += q
                if cum >= lo:
                    break
                cands.add(int(math.floor(cum + 1e-9)))
        for s in sorted(c for c in cands if 1 <= c <= lo):
            q = quote(event, tickers, books, side, s, fee_type, fee_mult, days, rf)
            if q is None:
                continue
            if q.net < min_net:
                why = why if best else ("below_min_net" if q.gross > 0 else why)
                continue
            if q.excess <= 0:
                why = why if best else "below_risk_free"
                continue
            if best is None or q.excess > best.excess:
                best = q
    return best, ("ok" if best else why)


# --------------------------------------------------------------------------- settlement

FINAL_STATUSES = ("finalized", "settled")


def settle_trade(position: dict, by_ticker: dict) -> dict | None:
    """Payout for a paper trade, cross-checked against Kalshi. None until it's final.

    Waits for every traded leg to reach a final status (a `determined` market can
    still change) and for any market added after the trade to resolve. The payout
    uses Kalshi's own `settlement_value_dollars` per leg: a YES contract pays the
    value, a NO contract pays 1 - value. It's then compared with the payout the
    yes/no `result` implies, and every disagreement is listed in `mismatches`.
    """
    traded = [l["ticker"] for l in position["legs"]]
    extra = [t for t, m in by_ticker.items() if t not in traded]
    for t in traded:
        m = by_ticker.get(t)
        if m is None or m.get("status") not in FINAL_STATUSES:
            return None
    if any(by_ticker[t].get("result") not in ("yes", "no") and by_ticker[t].get("status") not in FINAL_STATUSES
           for t in extra):
        return None

    sets, long = position["sets"], position["side"] == "long"
    results, values, mismatches = {}, {}, []
    payout_value = payout_result = 0.0
    for t in traded:
        m = by_ticker[t]
        res = m.get("result")
        raw = m.get("settlement_value_dollars")
        results[t] = res
        by_result = {"yes": 1.0, "no": 0.0}.get(res)
        if raw in (None, ""):
            mismatches.append({"ticker": t, "issue": "no_settlement_value", "result": res})
            value = by_result
        else:
            value = float(raw)
            if by_result is not None and abs(value - by_result) > 1e-6:
                mismatches.append({"ticker": t, "issue": "value_vs_result", "result": res, "value": value})
        if value is None:
            mismatches.append({"ticker": t, "issue": "unpriceable", "result": res})
            value = 0.0
        values[t] = value
        payout_value += sets * (value if long else 1 - value)
        payout_result += sets * ((by_result or 0.0) if long else 1 - (by_result or 0.0))
    added_winners = [t for t in extra if by_ticker[t].get("result") == "yes"]
    if added_winners:
        mismatches.append({"issue": "added_market_won", "tickers": added_winners})
    if abs(payout_value - payout_result) > 0.005:
        mismatches.append({"issue": "payout_differs", "by_value": round(payout_value, 4), "by_result": round(payout_result, 4)})
    payout = round(payout_value, 4)
    return {"payout": payout, "payout_by_result": round(payout_result, 4), "pnl": round(payout - position["capital"], 6),
            "results": results, "values": values, "mismatches": mismatches,
            "settled_ts": max((by_ticker[t].get("settlement_ts") or "" for t in traded), default=None)}


# --------------------------------------------------------------------------- engine

class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run_dir = os.path.join(WS_RUNS, self.run_id)
        os.makedirs(self.run_dir, exist_ok=True)
        self.ledger = Ledger(os.path.join(self.run_dir, "ledger.jsonl"), self.run_id)
        self.store = OrderBookStore(on_update=self._on_book, refill_seconds=cfg.refill_seconds)
        self.approvals = load_approvals()
        self.discover_client = KalshiClient(max_rps=5)
        self.settle_client = KalshiClient(max_rps=2)
        self.universe: dict[str, dict] = {}
        self.ticker_event: dict[str, str] = {}
        self.dirty: set[str] = set()
        self.opps: dict[str, Quote] = {}
        self.positions: dict[str, dict] = {}  # trade_id -> trade record
        self.cooldown_until: dict[str, float] = {}
        self.cash = cfg.bankroll
        self.realized = 0.0
        self.halted: str | None = None
        self.counters = {"evaluations": 0, "opportunities": 0, "trades": 0, "rejects": 0}
        self._last_logged: dict[tuple, float] = {}
        self._warned_new_leg: set[str] = set()
        self._warned_reclass: set[str] = set()
        self.feed = None
        self._resume()

    # -- state ---------------------------------------------------------------

    def _resume(self):
        """Rebuild open positions and cash from earlier runs' ledgers."""
        settled = set()
        trades = {}
        for rec in read_ledgers():
            if rec.get("type") == "trade":
                trades[rec["trade_id"]] = rec
            elif rec.get("type") == "settle":
                settled.add(rec["trade_id"])
                self.realized += rec.get("pnl", 0.0)
        self.positions = {k: v for k, v in trades.items() if k not in settled}
        self.cash = self.cfg.bankroll + self.realized - sum(p["capital"] for p in self.positions.values())
        rebuild_consumed(self.store, self.positions)

    def exposure(self, event=None):
        return sum(p["capital"] for p in self.positions.values() if event is None or p["event"] == event)

    def open_events(self):
        return {p["event"] for p in self.positions.values()}

    def _throttled(self, key, every=300.0):
        now = time.monotonic()
        if now - self._last_logged.get(key, -1e9) < every:
            return True
        self._last_logged[key] = now
        return False

    def _on_book(self, ticker):
        ev = self.ticker_event.get(ticker)
        if ev:
            self.dirty.add(ev)

    def halt(self, reason):
        if not self.halted:
            self.halted = reason
            self.ledger.write("halt", reason=reason)
            log.error("HALT: %s", reason)

    # -- discovery -------------------------------------------------------------

    def _discover_sync(self):
        series = self.discover_client.all_series()
        held = self.open_events()
        uni, counts = {}, {}
        for ev in self.discover_client.iter_open_events():
            if not ev.get("mutually_exclusive"):
                continue
            legs = [m for m in ev.get("markets") or [] if m.get("status") == "active"]
            if len(legs) < 2:
                continue
            cls, why = classify(ev)
            counts[cls] = counts.get(cls, 0) + 1
            tk = ev["event_ticker"]
            approved = tk in self.approvals["events"] or ev.get("series_ticker") in self.approvals["series"]
            needs_approval = cls == UNKNOWN and why == CANDIDATE_REASON and self.cfg.require_candidate_approval
            eligible = cls in self.cfg.classes and (approved or not needs_approval)
            if tk in held and not eligible and tk not in self._warned_reclass:
                self._warned_reclass.add(tk)
                self.ledger.write("warning", event=tk, kind="held_event_ineligible", cls=cls,
                                  detail=f"Held position is no longer eligible: {cls} ({why}). It is kept to settlement.")
            if tk not in held:
                if not eligible:
                    continue
                asks = [float(m["yes_ask_dollars"]) if m.get("yes_ask_dollars") else None for m in legs]
                bids = [float(m.get("yes_bid_dollars") or 0) for m in legs]
                sum_ask = sum(asks) if all(a is not None and a > 0 for a in asks) else math.inf
                if not (sum_ask <= 1 + self.cfg.band or sum(bids) >= 1 - self.cfg.band):
                    continue
            exp = [m.get("expected_expiration_time") or m.get("close_time") for m in legs]
            settle = max(datetime.fromisoformat(x.replace("Z", "+00:00")) for x in exp if x)
            settle_ts = settle.timestamp() + max(int(m.get("settlement_timer_seconds") or 0) for m in legs)
            days = (settle_ts - time.time()) / 86400
            if self.cfg.max_days and days > self.cfg.max_days and tk not in held:
                continue
            s = series.get(ev.get("series_ticker"), {})
            uni[tk] = {"title": ev.get("title"), "series": ev.get("series_ticker"), "category": ev.get("category"),
                       "class": cls, "class_reason": why, "settle_ts": settle_ts,
                       "numeric": why.startswith("numeric"),
                       "fee_type": s.get("fee_type", "quadratic"), "fee_mult": float(s.get("fee_multiplier", 1)),
                       "tickers": [m["ticker"] for m in legs],
                       "outcomes": {m["ticker"]: m.get("yes_sub_title") or m.get("title") for m in legs}}
        return uni, counts

    async def discover_loop(self):
        while True:
            try:
                uni, counts = await asyncio.to_thread(self._discover_sync)
                self.universe = uni
                self.ticker_event = {t: ev for ev, u in uni.items() for t in u["tickers"]}
                self.feed.set_tickers(list(self.ticker_event))
                self.dirty.update(uni)
                self.ledger.write("universe", events=len(uni), markets=len(self.ticker_event), classes=counts)
                log.info("universe: %d events, %d markets", len(uni), len(self.ticker_event))
            except Exception as e:
                log.exception("discovery failed")
                self.ledger.write("error", where="discovery", error=str(e)[:300])
            await asyncio.sleep(self.cfg.discover_minutes * 60)

    # -- evaluation and allocation ---------------------------------------------

    def limits(self, event):
        c = self.cfg
        return min(self.cash,
                   c.bankroll * c.max_total_frac - self.exposure(),
                   c.bankroll * c.max_event_frac - self.exposure(event))

    def evaluate(self, event, max_capital=None) -> tuple[Quote | None, str]:
        u = self.universe.get(event)
        if not u:
            return None, "not_in_universe"
        books = [self.store.book(t) for t in u["tickers"]]
        if any(b is None for b in books):
            return None, "no_book"
        if max(self.store.age(t) for t in u["tickers"]) > self.cfg.stale_seconds:
            return None, "stale_book"
        if u["class"] == UNKNOWN or not u["numeric"]:
            mid = sum(((b.best_bid or 0.0) + (b.best_ask if b.best_ask is not None else 1.0)) / 2 for b in books)
            if mid < 1 - self.cfg.unknown_mid_gap:
                return None, "likely_non_exhaustive"
        days = max((u["settle_ts"] - time.time()) / 86400, MIN_DAYS)
        cap = self.limits(event) if max_capital is None else max_capital
        self.counters["evaluations"] += 1
        return best_quote(event, u["tickers"], books, u["fee_type"], u["fee_mult"], days,
                          self.cfg.risk_free, self.cfg.max_sets, max(cap, 0.0), self.cfg.min_net)

    async def allocate_loop(self):
        while True:
            await asyncio.sleep(0.2)
            if os.path.exists(os.path.join(WS_RUNS, "KILL")):
                self.halt("kill switch file present")
            dirty, self.dirty = self.dirty, set()
            for ev in dirty:
                # Evaluate against the full bankroll so blocked opportunities are still seen.
                q, why = self.evaluate(ev, max_capital=self.cfg.bankroll * self.cfg.max_event_frac)
                if q:
                    if ev not in self.opps:
                        self.counters["opportunities"] += 1
                    prev = self.opps.get(ev)
                    self.opps[ev] = q
                    if prev is None or abs(q.apr - prev.apr) > 0.1 * max(abs(prev.apr), 0.05):
                        if not self._throttled(("opp", ev), 60):
                            self._log_opp(q)
                else:
                    self.opps.pop(ev, None)
            if self.halted:
                continue
            for q in sorted(self.opps.values(), key=lambda q: q.apr, reverse=True):  # highest APR first
                block = self._risk_block(q.event)
                if block:
                    self._reject(q.event, block, apr=q.apr)
                    continue
                await self.execute(q)

    def _log_opp(self, q: Quote):
        u = self.universe[q.event]
        self.ledger.write("opportunity", event=q.event, title=u["title"], cls=u["class"], side=q.side, sets=q.sets,
                          net=q.net, gross=q.gross, fees=q.fees, capital=q.capital, apr=q.apr,
                          hurdle_apr=self.cfg.risk_free, excess=q.excess, days=q.days)

    def _risk_block(self, event):
        c = self.cfg
        if time.time() < self.cooldown_until.get(event, 0):
            return "cooldown"
        if event not in self.open_events() and len(self.open_events()) >= c.max_open_events:
            return "max_open_events"
        if self.limits(event) < 1.0:
            if self.cash < 1.0:
                return "no_cash"
            if self.exposure() >= c.bankroll * c.max_total_frac - 1.0:
                return "total_limit"
            return "event_limit"
        return None

    def _reject(self, event, reason, **extra):
        # Blocked candidates are re-checked every tick, so count and log each reason
        # at most once per event every 5 minutes.
        if not self._throttled(("rej", event, reason)):
            self.counters["rejects"] += 1
            u = self.universe.get(event, {})
            self.ledger.write("reject", event=event, title=u.get("title"), reason=reason, **extra)

    async def execute(self, q: Quote):
        decided = time.monotonic()
        await asyncio.sleep(self.cfg.latency)
        # Re-check on the newest books, within the current limits.
        r, why = self.evaluate(q.event)
        self.opps.pop(q.event, None)
        if r is None or r.side != q.side:
            self._reject(q.event, "recheck_failed", apr=q.apr, why=why if r is None else "side_flipped")
            self.cooldown_until[q.event] = time.time() + self.cfg.cooldown
            return
        u = self.universe[q.event]
        trade_id = uuid.uuid4().hex[:12]
        for leg in r.legs:
            leg["outcome"] = u["outcomes"].get(leg["ticker"])
            for price, qty in leg["levels"]:
                self.store.consume(leg["ticker"], "ask" if r.side == "long" else "bid", price, qty)
        rec = self.ledger.write(
            "trade", trade_id=trade_id, event=r.event, title=u["title"], category=u["category"], cls=u["class"],
            side=r.side, sets=r.sets, legs=r.legs, cost=r.cost, fees=r.fees, capital=r.capital, payout=r.payout,
            net=r.net, gross=r.gross, apr=r.apr, hurdle=r.hurdle, excess=r.excess, days=r.days,
            settle_eta=datetime.fromtimestamp(u["settle_ts"], timezone.utc).isoformat(),
            decided_apr=q.apr, latency_ms=round((time.monotonic() - decided) * 1000), feed=self.feed.mode)
        self.positions[trade_id] = rec
        self.cash -= r.capital
        self.counters["trades"] += 1
        self.cooldown_until[r.event] = time.time() + self.cfg.cooldown
        self.dirty.add(r.event)  # re-evaluate against the book net of this fill
        log.info("TRADE %s %s x%d net $%.2f APR %.1f%%", r.event, r.side, r.sets, r.net, r.apr * 100)

    # -- settlement ------------------------------------------------------------

    def _check_settlement_sync(self, event):
        d = self.settle_client.get(f"/events/{event}", {"with_nested_markets": "true"})
        return d["event"].get("markets") or d.get("markets") or []

    async def settle_loop(self):
        while True:
            await asyncio.sleep(self.cfg.settle_minutes * 60)
            for event in sorted(self.open_events()):
                try:
                    markets = await asyncio.to_thread(self._check_settlement_sync, event)
                except Exception as e:
                    self.ledger.write("error", where="settlement", event=event, error=str(e)[:200])
                    continue
                by_ticker = {m["ticker"]: m for m in markets}
                for trade_id, p in list(self.positions.items()):
                    if p["event"] != event:
                        continue
                    traded = [l["ticker"] for l in p["legs"]]
                    extra = [t for t, m in by_ticker.items() if t not in traded and m.get("result") != "no"]
                    if extra and trade_id not in self._warned_new_leg:
                        self._warned_new_leg.add(trade_id)
                        self.ledger.write("warning", trade_id=trade_id, event=event, kind="leg_added",
                                          detail=f"{len(extra)} market(s) added after the trade: {extra[:5]}")
                    st = settle_trade(p, by_ticker)
                    if st is None:
                        continue
                    payout, pnl = st["payout"], st["pnl"]
                    self.ledger.write("settle", trade_id=trade_id, event=event, side=p["side"], sets=p["sets"],
                                      payout=payout, payout_by_result=st["payout_by_result"], capital=p["capital"],
                                      pnl=pnl, expected_net=p["net"], results=st["results"],
                                      settlement_values=st["values"], kalshi_settled_ts=st["settled_ts"],
                                      verified=not st["mismatches"], mismatches=st["mismatches"])
                    if st["mismatches"]:
                        self.ledger.write("warning", trade_id=trade_id, event=event, kind="settlement_mismatch",
                                          detail=f"Kalshi settlement disagrees with the expected payout: {st['mismatches'][:3]}")
                    self.cash += payout
                    self.realized += pnl
                    del self.positions[trade_id]
                    if pnl < -0.005:
                        self.halt(f"settled trade {trade_id} on {event} lost ${-pnl:.2f}: event was not exhaustive")

    # -- heartbeat -------------------------------------------------------------

    async def heartbeat_loop(self):
        last_msgs, last_t = 0, time.monotonic()
        while True:
            await asyncio.sleep(self.cfg.heartbeat_seconds)
            now = time.monotonic()
            rate = (self.store.messages - last_msgs) / (now - last_t)
            last_msgs, last_t = self.store.messages, now
            best = max(self.opps.values(), key=lambda q: q.apr, default=None)
            ready = sum(1 for t in self.ticker_event if self.store.age(t) <= self.cfg.stale_seconds)
            self.ledger.write("heartbeat", cash=round(self.cash, 2), exposure=round(self.exposure(), 2),
                              realized=round(self.realized, 2), open_trades=len(self.positions),
                              open_events=len(self.open_events()), universe=len(self.universe),
                              markets=len(self.ticker_event), books_fresh=ready, msgs_per_s=round(rate, 1),
                              opps_now=len(self.opps), best_apr_now=best.apr if best else None,
                              best_event_now=best.event if best else None, halted=self.halted,
                              feed=self.feed.status(), counters=dict(self.counters))

    # -- run -------------------------------------------------------------------

    async def run(self):
        c = self.cfg
        use_ws = c.feed == "ws" or (c.feed == "auto" and load_credentials()[1] is not None)
        self.feed = KalshiWSFeed(self.store) if use_ws else RestPollFeed(self.store, KalshiClient(max_rps=8))
        with open(os.path.join(self.run_dir, "run.json"), "w", encoding="utf-8") as f:
            json.dump({"run_id": self.run_id, "started": datetime.now(timezone.utc).isoformat(),
                       "feed": self.feed.mode, "config": asdict(c), "paper": True}, f, indent=2)
        self.ledger.write("run_start", feed=self.feed.mode, config=asdict(c), cash=self.cash,
                          resumed_positions=len(self.positions), realized=self.realized)
        tasks = [asyncio.create_task(t) for t in (self.feed.run(), self.discover_loop(), self.allocate_loop(),
                                                    self.settle_loop(), self.heartbeat_loop())]
        try:
            if c.duration_minutes:
                await asyncio.sleep(c.duration_minutes * 60)
            else:
                await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            self.ledger.write("run_stop", cash=round(self.cash, 2), exposure=round(self.exposure(), 2),
                              realized=round(self.realized, 2), counters=dict(self.counters))
            self.ledger.close()


def _duration(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    d = Config()
    p.add_argument("--feed", choices=["auto", "ws", "rest"], default=d.feed)
    p.add_argument("--bankroll", type=float, default=d.bankroll, help="paper capital in dollars (default 1000)")
    p.add_argument("--max-event-frac", type=float, default=d.max_event_frac, help="cap per event, share of bankroll (0.20)")
    p.add_argument("--max-total-frac", type=float, default=d.max_total_frac, help="cap on all open trades (1.0)")
    p.add_argument("--max-open-events", type=int, default=d.max_open_events)
    p.add_argument("--max-sets", type=int, default=d.max_sets, help="contract sets per trade (500)")
    p.add_argument("--risk-free", type=float, default=d.risk_free, help="hurdle APR, compounded to payout (0.05)")
    p.add_argument("--min-net", type=float, default=d.min_net, help="minimum profit after fees per trade, $ (0.10)")
    p.add_argument("--latency", type=float, default=d.latency, help="seconds between decision and re-check (0.3)")
    p.add_argument("--cooldown", type=float, default=d.cooldown, help="seconds before trading an event again (30)")
    p.add_argument("--band", type=float, default=d.band, help="discovery prescreen: top-of-book sums within X of $1 (0.03)")
    p.add_argument("--unknown-mid-gap", type=float, default=d.unknown_mid_gap,
                   help="UNKNOWN events need mid prices summing to >= 1 - X (0.10)")
    p.add_argument("--refill-seconds", type=float, default=d.refill_seconds,
                   help="seconds a filled level must be gone before its liquidity counts as new (300)")
    p.add_argument("--allow-unapproved-candidates", action="store_true",
                   help="trade UNKNOWN candidate lists without listing them in approvals.json")
    p.add_argument("--max-days", type=float, default=None, help="skip events settling later than this")
    p.add_argument("--stale-seconds", type=float, default=d.stale_seconds)
    p.add_argument("--discover", type=float, default=d.discover_minutes, help="minutes between universe refreshes (10)")
    p.add_argument("--settle", type=float, default=d.settle_minutes, help="minutes between settlement checks (5)")
    p.add_argument("--duration", type=_duration, default=None, help="run for H:MM, then stop")
    a = p.parse_args(argv)
    cfg = Config(feed=a.feed, bankroll=a.bankroll, max_event_frac=a.max_event_frac, max_total_frac=a.max_total_frac,
                 max_open_events=a.max_open_events, max_sets=a.max_sets, risk_free=a.risk_free, min_net=a.min_net,
                 latency=a.latency, cooldown=a.cooldown, band=a.band, max_days=a.max_days, unknown_mid_gap=a.unknown_mid_gap,
                 refill_seconds=a.refill_seconds, require_candidate_approval=not a.allow_unapproved_candidates,
                 stale_seconds=a.stale_seconds, discover_minutes=a.discover, settle_minutes=a.settle,
                 duration_minutes=a.duration)
    eng = Engine(cfg)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(os.path.join(eng.run_dir, "run.log"))])
    try:
        asyncio.run(eng.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
