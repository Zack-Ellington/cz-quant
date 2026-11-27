"""Independent cross-market sum check using one broker per venue.

Select one experiment with --experiment FILE; its mode selects paper or live.
The strategy reads one prepared pair and owns
sizing, sequential IOC execution, partial-fill repairs, and monitor-health gates.
Brokers retain execution evidence in the shared economic log. See README.md for
configuration, collectors, recovery, and the supported venue modes.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from czquant import Broker, Store
from czquant.accounting import active_events, combine_states, venue_of
from czquant.book import levels as book_levels
from czquant.kalshi import Kalshi
from czquant.live import KalshiLive
from czquant.paper import Paper
from czquant.polymarket import Polymarket, PolymarketPaper, kalshi_layout, normalize_market
from czquant.polymarket_us import DEFAULT_THETA, BookStream, PolymarketUS, QuoteUnavailable, PolymarketUSLive, PolymarketUSPaper
from czquant.store import decimal

from experiments import Experiment, Outcome, SelectedPair
from kalshi_stream import KalshiStreamBook
from scan_capture import measure
from slippage import split_limits, second_limit, cost, fee_bound
from sum_check import Basket, best, kalshi_offer, polymarket_offer, polymarket_us_offer

STRATEGY = "cross-market-polymarket"
HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
FEE_TYPES = ("quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees")
TERMINAL = {"filled", "canceled", "rejected", "failed", "dry_run"}
log = logging.getLogger("cross_market_polymarket")


@dataclass
class Config:
    experiment: Experiment
    interval_seconds: float = 15.0
    profit_slippage: bool = False
    execution_websocket: bool = False
    quote_max_age: float = 2.0
    max_capital_frac: float = 0.10
    max_sets: int = 20
    risk_free: float = 0.05
    min_net: float = 0.0  # no separate basket-profit floor unless explicitly configured
    min_edge: float = 0.01  # per $1 set: margin for rule and settlement risk between venues
    # The recheck just before sending reprices the chosen legs. The spread often narrows
    # between the two reads; the basket still commits while its after-fee edge is at least
    # this much per $1 set (0.0001 = 0.01 cents), with no separate basket-profit floor.
    recheck_min_edge: float = 0.0001
    max_apr: float | None = None
    require_cross: bool = True  # baskets need a leg on each venue
    kalshi_latency: float = 0.30
    polymarket_latency: float = 0.50
    cooldown: float = 0.0  # seconds before trading the pair again after an attempt; 0: none
    duration_minutes: float | None = None
    dry_run: bool = False  # live, but build and sign orders without sending them
    pmus_theta: float | None = None  # Polymarket US taker coefficient; None: each market's own
    max_basket_dollars: float | None = 25.0  # hard cap on one basket's capital
    max_total_dollars: float | None = 100.0  # hard cap on capital at risk across open trades
    monitor_max_age: float = 300.0  # live: seconds a monitor's last health check stays valid

    def __post_init__(self) -> None:
        self.experiment.validate()
        if not 0 < self.max_capital_frac <= 1 or self.max_sets < 1 or self.interval_seconds <= 0:
            raise ValueError("capital fraction, basket size and scan interval must be positive")
        if self.cooldown < 0:
            raise ValueError("invalid cooldown")
        if not 0 < self.quote_max_age < float("inf"):
            raise ValueError("quote_max_age must be finite and positive")
        if not 0 <= self.recheck_min_edge <= self.min_edge:
            raise ValueError("recheck_min_edge must be between 0 and min_edge")
        if self.dry_run and self.experiment.mode != "live":
            raise ValueError("dry_run requires live mode")


class Engine:
    def __init__(self, cfg: Config, kalshi: Any = None, poly: Polymarket | None = None,
                 revision: str | None = None, us: PolymarketUS | None = None, kalshi_live: Any = None):
        self.cfg, self.experiment = cfg, cfg.experiment
        self.pair = self.experiment.pair
        self.live = self.experiment.mode == "live"
        self.us = self.pair.venue == "polymarket_us"
        self.theta_override = None if cfg.pmus_theta is None else Decimal(str(cfg.pmus_theta))
        kalshi_pool = self.experiment.portfolio["kalshi"]
        poly_pool = self.experiment.portfolio[self.pair.venue]
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        self.store = Store()
        self.diagnostics = os.path.join(str(self.store.root), "diagnostics", STRATEGY, kalshi_pool, poly_pool)
        self.kalshi = kalshi or Kalshi(authenticated=False)
        if self.us:
            self.poly: Any = us or PolymarketUS.from_env(authenticated=self.live, dry_run=cfg.dry_run)
            if us is None:
                self.start_book_stream()
            poly_adapter: Any = (PolymarketUSLive(self.poly) if self.live else
                                 PolymarketUSPaper(self.poly, latency=cfg.polymarket_latency,
                                                   theta=self.theta_override))
        else:
            self.poly = poly or Polymarket.from_env()
            poly_adapter = PolymarketPaper(self.poly, latency=cfg.polymarket_latency)
        kalshi_adapter = (KalshiLive(kalshi_live or Kalshi(authenticated=True), dry_run=cfg.dry_run)
                          if self.live else Paper(self.kalshi, latency=cfg.kalshi_latency))
        self.kalshi_broker = Broker(self.store, kalshi_pool, kalshi_adapter, self.run_id)
        self.poly_broker = Broker(self.store, poly_pool, poly_adapter, self.run_id)
        self.brokers = {broker.venue: broker for broker in (self.kalshi_broker, self.poly_broker)}
        expected_mode = "live" if self.live else "paper"
        if any(b.mode != expected_mode for b in self.brokers.values()):
            raise ValueError(f"both cash pools must be {expected_mode}")
        if self.live and not self.poly.authenticated:
            raise ValueError("live trading needs POLYMARKET_US_KEY_ID and POLYMARKET_US_SECRET_KEY_PATH")
        self.validate_inventory()
        revision = revision or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()
        for broker in self.brokers.values():
            broker.start(STRATEGY, revision, asdict(cfg))
        self.cooldown_until = 0.0
        self.halted: str | None = None
        self._halts: dict[str, dict[str, Any]] = {}
        self.repairs: list[tuple[dict[str, Any], Decimal, dict[str, Any]]] = []
        self.positions: dict[str, dict[str, Any]] = {}
        self.counters = {"scans": 0, "baskets": 0, "rejects": 0, "unwinds": 0}
        self.last_quotes: list[dict[str, Any]] = []
        self._last_logged: dict[tuple, float] = {}
        self.resume()
        self.kalshi_stream: KalshiStreamBook | None = None
        # With execution_websocket, execution prices only from the streams: the shared
        # Polymarket US stream (started above) read strictly, and a Kalshi stream.
        self.stream_book: BookStream | None = None
        if cfg.execution_websocket:
            stream = getattr(self.poly, "stream", None) if self.us else None
            if stream is None:
                raise ValueError("execution WebSocket requires the Polymarket US market stream (and its credentials)")
            self.stream_book = stream
            kalshi_client = Kalshi(authenticated=True)
            if not kalshi_client.key:
                raise ValueError("execution WebSocket requires Kalshi credentials")
            self.kalshi_stream = KalshiStreamBook(kalshi_client,
                sorted({name for name, _ in self.pair.contracts("kalshi")}), cfg.quote_max_age)
            self.kalshi_stream.start()

    def kalshi_execution_book(self, ticker: str) -> dict[str, Any]:
        if self.kalshi_stream is not None:
            return self.kalshi_stream.snapshot(ticker)
        return self.kalshi.book(ticker)

    def us_execution_book(self) -> dict[str, Any]:
        if self.stream_book is not None:
            return self.stream_book.snapshot(self.pair.polymarket_market)
        return self.poly.books([self.pair.polymarket_market])[self.pair.polymarket_market]

    def validate_inventory(self) -> None:
        """The selected portfolio may contain only this run's pair."""
        for venue, broker in self.brokers.items():
            for lot in broker.state().lots.values():
                if (lot["market"], lot["side"].upper()) not in self.pair.contracts(venue):
                    raise ValueError(f"incompatible existing holdings in {broker.pool_id}; reconcile before starting this pair")

    def budget_block(self, basket: Basket) -> str | None:
        """Check limit costs and broker reservations against each selected pool."""
        need: dict[str, Decimal] = {}
        for leg in basket.legs:
            venue = venue_of(leg["ticker"])
            contract = (leg["ticker"], leg["contract"])
            if venue not in self.brokers or contract not in self.pair.contracts(venue):
                raise ValueError("basket contains a contract outside the selected pair")
            price = decimal(leg["limit"])
            checked = self.brokers[venue].check({"ticker": leg["ticker"], **buy(leg), "count": str(basket.sets)})
            if checked["status"] != "ready":
                return f"{venue}: {checked.get('detail', checked['reason'])}"
            # Broker reservation and the priced fee can differ. Budget for both.
            required = max(decimal(checked["reserved"]), basket.sets * price + decimal(leg["fee"]))
            need[venue] = need.get(venue, Decimal(0)) + required
        if any(self.brokers[venue].available < amount for venue, amount in need.items()):
            return "selected allocation cannot fund the full basket at its limit prices"
        return None

    def check_basket(self, basket: Basket) -> str | None:
        self.validate_inventory()
        block = "halted" if self.halted else self.live_block()
        if block:
            return block
        if len({leg["ticker"] for leg in basket.legs}) != len(basket.legs):
            return "basket contains opposite sides of the same market; fills would net"
        block = self.budget_block(basket)
        if block:
            return block
        # Opposite inventory would close an existing position instead of buying a set.
        for leg in basket.legs:
            if any(lot["market"] == leg["ticker"] and lot["side"].upper() != leg["contract"]
                   for lot in self.brokers[venue_of(leg["ticker"])].state().lots.values()):
                return "basket conflicts with existing inventory; reconcile before adding a set"
        return None

    # -- state ---------------------------------------------------------------

    def resume(self) -> None:
        """Recover actual holdings and the halt state from the shared store."""
        # Read orders before lots so reconciliation cannot make older lots look incomplete.
        awaiting = {o["context"].get("trade_id") for b in self.brokers.values()
                    for o in b.orders(("pending", "unknown", "acknowledged"))}
        state = combine_states([b.state() for b in self.brokers.values()])
        self.cash = sum((b.available for b in self.brokers.values()), Decimal(0))
        self.bankroll = state.contributed
        events = active_events(sorted(
            [e for b in self.brokers.values() for e in self.store.events(b.pool_id,
             kinds=("transaction", "observation", "correction", "order"), observations=("halt", "halt_cleared", "basket_settled", "basket_execution"))],
            key=lambda e: (e["effective_at"], e["sequence"])))
        empty_cooldowns = {e["data"]["context"]["trade_id"]: float(e["data"]["zero_fill_cooldown_until"])
                           for e in events if e["type"] == "observation"
                           and e["data"].get("kind") == "basket_execution"
                           and "zero_fill_cooldown_until" in e["data"]}
        for event in events:
            data = event["data"]
            if event["type"] == "order" and data.get("status") == "intent":
                game = data.get("context", {}).get("kalshi_event")
                if game == self.pair.kalshi_event:
                    until = empty_cooldowns.get(data.get("context", {}).get("trade_id"),
                              datetime.fromisoformat(event["effective_at"]).timestamp() + self.cfg.cooldown)
                    self.cooldown_until = max(self.cooldown_until, until)
        observations = [e for e in events if e["type"] == "observation"]
        cleared = {e["data"]["halt_id"] for e in observations if e["data"].get("kind") == "halt_cleared"}
        reported = {e["data"]["trade_id"] for e in observations if e["data"].get("kind") == "basket_settled"}
        # Recognize old execution halts only for orders owned by these pools.
        legacy_recovery = {"incomplete basket repair; reconcile holdings and orders before clearing the halt",
                           "basket repair failed; reconcile holdings and orders before clearing the halt"}
        for e in events:
            d = e["data"]
            if e["type"] == "order" and d.get("status") == "intent":
                order = d["order"]
                legacy_recovery.update({f"order on {order['ticker']} has an unknown outcome",
                    f"order {order['client_order_id']} may have executed; reconcile before repairing this experiment"})
        self._halts = {}
        self.halted = None
        for e in observations:
            if e["data"].get("kind") == "halt" and e["record_id"] not in cleared:
                d = e["data"]
                self._halts[d["reason"]] = {"id": e["record_id"],
                    "recoverable": d.get("recoverable", d["reason"] in legacy_recovery)}
                self.halted = d["reason"]
        self.positions = {}
        for lot in state.lots.values():
            context = lot["context"]
            if not context.get("trade_id"):
                self.halt("existing inventory has no basket provenance; reconcile before adding exposure")
            else:
                position = self.positions.setdefault(context["trade_id"], {**context, "capital": Decimal(0)})
                position["capital"] += lot["cost"]
        totals: dict[str, Decimal] = {}
        contexts: dict[str, dict[str, Any]] = {}
        filled: dict[str, dict[tuple[str, str], Decimal]] = {}
        repaired = set()
        settled = set()
        for e in events:
            d = e["data"]
            tid = (d.get("context") or {}).get("trade_id")
            if e["type"] != "transaction" or not tid:
                continue
            totals[tid] = totals.get(tid, Decimal(0)) + decimal(d["cash_delta"])
            contexts[tid] = d["context"]
            if d["context"].get("repair"):
                repaired.add(tid)
            if d["reason"] == "settlement":
                settled.add(tid)
            for leg in d.get("legs", []):
                if d["reason"] == "fill" and decimal(leg["quantity_delta"]) > 0 and not d["context"].get("repair"):
                    key = (leg["market"], leg["side"])
                    filled.setdefault(tid, {})[key] = filled.get(tid, {}).get(key, Decimal(0)) + decimal(leg["quantity_delta"])
        for tid, pnl in totals.items():
            if tid in self.positions:
                continue
            intended = {(leg["ticker"], leg["contract"].lower()): decimal(leg["qty"])
                        for leg in contexts[tid].get("intended_legs", [])}
            status = ("complete" if filled.get(tid) == intended else "partial") if intended else "unknown"
            if tid in repaired:
                status = "repaired"
            if tid not in reported:
                self.kalshi_broker.observe("basket_settled", {"trade_id": tid, "net_pnl": str(pnl), "fill_status": status,
                    "context": contexts[tid], "filled_legs": [{"ticker": t, "side": s, "quantity": str(q)}
                                                              for (t, s), q in filled.get(tid, {}).items()]})
            # Only a complete basket's loss contradicts the arbitrage; a partial one is directional.
            if pnl < Decimal("-0.005") and status not in ("partial", "repaired"):
                self.halt(f"settled trade {tid} lost ${-pnl:.2f}")

        # Account for fungible contracts across baskets: FIFO closes can change
        # lot provenance without changing the experiment's matched quantities.
        self.repairs = []
        if awaiting or settled:
            # Once a selected market starts settling, wait for the other payout.
            # FIFO may have moved lot provenance between equivalent baskets.
            return
        by_role: list[list[dict[str, Any]]] = [[], []]
        for lot in state.lots.values():
            for role, outcome in enumerate(self.pair.outcomes):
                if (lot["market"], lot["side"].upper()) in {
                    (outcome.kalshi, outcome.kalshi_side), (outcome.polymarket, outcome.polymarket_side)}:
                    by_role[role].append(lot)
        quantities = [sum((lot["quantity"] for lot in lots), Decimal(0)) for lots in by_role]
        keep = min(quantities)
        for role, lots in enumerate(by_role):
            excess = quantities[role] - keep
            for lot in reversed(lots):
                count = min(excess, lot["quantity"])
                if count > 0:
                    leg = {"ticker": lot["market"], "contract": lot["side"].upper(),
                           "venue": venue_of(lot["market"])}
                    self.repairs.append((leg, count, lot["context"]))
                    excess -= count
        if self.repairs:
            self.halt("unmatched inventory requires automatic sell recovery", recoverable=True)

    def halt(self, reason: str, *, recoverable: bool = False) -> None:
        if reason not in self._halts:
            self.halted = reason
            identifier = self.kalshi_broker.observe("halt", {"reason": reason, "recoverable": recoverable})
            self._halts[reason] = {"id": identifier, "recoverable": recoverable}
            log.error("HALT: %s", reason)

    def exposure(self) -> Decimal:
        return sum((p["capital"] for p in self.positions.values()), Decimal(0))

    def limits(self) -> Decimal:
        cap = min(self.cash, self.bankroll * Decimal(str(self.cfg.max_capital_frac)) - self.exposure())
        if self.cfg.max_total_dollars is not None:
            cap = min(cap, Decimal(str(self.cfg.max_total_dollars)) - self.exposure()
                      - sum((b.state().cash - b.available for b in self.brokers.values()), Decimal(0)))
        if self.cfg.max_basket_dollars is not None:
            cap = min(cap, Decimal(str(self.cfg.max_basket_dollars)))
        return max(cap, Decimal(0))

    def live_block(self) -> str | None:
        """Unknown writes and monitor discrepancies halt; acknowledged orders and
        missing or stale monitor checks pause until reconciliation catches up."""
        unresolved = sum(len(b.orders(("pending", "unknown"))) for b in self.brokers.values())
        if unresolved:
            self.halt(f"{unresolved} order(s) awaiting reconciliation", recoverable=True)
            return "halted"
        busy = any(b.orders(("acknowledged",)) for b in self.brokers.values())
        if not self.live or self.cfg.dry_run:
            return "awaiting_reconciliation" if busy else None
        now = datetime.now(timezone.utc)
        for component in ("account", "polymarket_us"):
            last = self.store.latest_health(component)
            if last is None:
                return f"no_{component}_monitor"
            age = (now - datetime.fromisoformat(last["effective_at"])).total_seconds()
            if age > self.cfg.monitor_max_age or last["data"].get("status") == "failed":
                return f"stale_{component}_monitor"
            mine = [d for d in last["data"].get("discrepancies") or [] if d.get("portfolio") in {b.pool_id for b in self.brokers.values()}]
            if mine:
                self.halt(f"{component} monitor reports {mine[0].get('kind')}: reconcile before trading")
                return "halted"
        return "awaiting_reconciliation" if busy else None

    def _throttled(self, key: tuple, every: float = 300.0) -> bool:
        now = time.monotonic()
        if now - self._last_logged.get(key, -1e18) < every:
            return True
        self._last_logged[key] = now
        return False

    def reject(self, event: str, reason: str, **extra: Any) -> None:
        if not self._throttled(("reject", event, reason)):
            self.counters["rejects"] += 1
            self.kalshi_broker.observe("reject", {"event": event, "reason": reason, **extra})

    # -- selected market data and pricing ------------------------------------

    def start_book_stream(self) -> None:
        """Read this pair's Polymarket US book from the market WebSocket (live snapshots,
        ~10/s in play) instead of the CDN-cached REST book. Paper runs use the exported
        credentials if present; without them, or while the stream is stale, books come
        from REST."""
        try:
            streamer = self.poly if self.poly.authenticated else PolymarketUS.from_env(authenticated=True)
            self.poly.stream = streamer.start_stream([self.pair.polymarket_market], max_age=self.cfg.quote_max_age)
        except (ValueError, OSError) as exc:
            log.warning("Polymarket US book stream unavailable (%s); reading books over REST", exc)
            self.poly.stream = None

    def stream_status(self) -> dict[str, Any] | None:
        stream = getattr(self.poly, "stream", None) if self.us else None
        if stream is None:
            return None
        return {"connected": stream.connected, "messages": stream.messages, "fallbacks": stream.fallbacks,
                "reconnects": stream.reconnects, "last_error": stream.last_error}

    def price_pair(self, recheck: set[tuple[str, str]] | None = None) -> tuple[Basket | None, str]:
        """Read only the prepared pair. No listings, league scans or matching.

        With `recheck` (the (ticker, contract) legs already chosen), only those legs are
        priced, at recheck_min_edge and no min_net: the second read decides whether the
        chosen basket is still profitable, not whether it still clears the entry bar."""
        pair = self.pair
        self.last_quotes = []
        self.scan_quote = {"experiment_id": self.experiment.id, "status": "error",
                           "legs": [], "routes": [], "books": {},
                           "interval": self.cfg.interval_seconds,
                           "started_at": datetime.now(timezone.utc).isoformat()}
        markets = {name: self.kalshi.market(name) for name, _ in pair.contracts("kalshi")}
        for name, market in markets.items():
            if market["ticker"] != name or market["event_ticker"] != pair.kalshi_event:
                raise ValueError("Kalshi metadata differs from the prepared pair")
            if market["status"] not in ("active", "open"):
                return None, "kalshi_not_trading"
        schedule = self.kalshi.get(f"/series/{pair.kalshi_series}")["series"]
        if schedule["fee_type"] not in FEE_TYPES:
            return None, "unsupported_kalshi_fees"
        multiplier = decimal(schedule.get("fee_multiplier", "1"))
        try:
            kbooks = {name: self.kalshi_execution_book(name) for name in markets}
        except QuoteUnavailable as exc:
            return None, str(exc)
        self.scan_quote["fee_multiplier"] = multiplier
        self.scan_quote["books"] = {"kalshi": kbooks}
        self.scan_quote["kalshi_read_at"] = datetime.now(timezone.utc).isoformat()
        offers: dict[str, list] = {}
        if self.us:
            market = self.poly.market_raw(pair.polymarket_market)
            if market["slug"] != pair.polymarket_market:
                raise ValueError("Polymarket metadata differs from the prepared pair")
            if market["status"] != "MARKET_STATUS_OPEN":
                return None, "polymarket_not_trading"
            try:
                pbook = self.us_execution_book()
            except QuoteUnavailable as exc:
                return None, str(exc)
            self.scan_quote["books"]["polymarket"] = pbook
            self.scan_quote["source"] = pbook.get("execution_quote", {"source": "polymarket_us_rest"})
            theta = self.theta_override if self.theta_override is not None else decimal(market.get("feeCoefficient", DEFAULT_THETA))
            self.scan_quote["fee_coefficient"] = theta
        else:
            market = self.poly.market_by_slug(pair.polymarket_event)
            if str(market["id"]) != pair.polymarket_market:
                raise ValueError("Polymarket metadata differs from the prepared pair")
            self.poly.remember(market)
            tokens = [o.polymarket.removeprefix("PM:") for o in pair.outcomes]
            pbooks = {token: kalshi_layout(raw) for token, raw in self.poly.books(tokens).items()}
            self.scan_quote["books"]["polymarket"] = pbooks
            self.scan_quote["source"] = {"source": "polymarket_rest"}
        for index, outcome in enumerate(pair.outcomes):
            role = str(index)
            ways = [kalshi_offer(role, outcome.kalshi, kbooks[outcome.kalshi], multiplier, outcome.kalshi_side)]
            if self.us:
                ways.append(polymarket_us_offer(role, outcome.polymarket, pbook, theta, outcome.polymarket_side,
                                                 decimal(market.get("orderPriceMinTickSize") or "0.01")))
            else:
                token = outcome.polymarket.removeprefix("PM:")
                normalized = normalize_market(market, token)
                if normalized["status"] != "active":
                    return None, "polymarket_not_trading"
                ways.append(polymarket_offer(role, outcome.polymarket, pbooks[token], normalized["fee_schedule"]))
            offers[role] = ways
        if len(pair.outcomes) == 2 and recheck is None:
            # The capture compares both cross-venue routes, so it reads every offer.
            names = {alias: name for name, alias in pair.evidence.get("kalshi", [])}
            self.scan_quote.update(measure(offers,
                [names.get(o.kalshi.rsplit("-", 1)[-1], o.kalshi.rsplit("-", 1)[-1]) for o in pair.outcomes],
                self.cfg.max_sets, self.limits(), lambda b: self.budget_block(b) is None))
        self.scan_quote["status"] = "ok"
        if recheck is not None:
            offers = {role: [w for w in ways if (w.ticker, w.contract) in recheck] for role, ways in offers.items()}
        # Diagnostics only: each outcome's best ask (price, size) at each venue.
        self.last_quotes = [{"role": way.role, "venue": way.venue, "ticker": way.ticker, "contract": way.contract,
                             "ask": str(way.asks[0][0]) if way.asks else None,
                             "size": str(way.asks[0][1]) if way.asks else None}
                            for ways in offers.values() for way in ways]
        days = max((datetime.fromisoformat(pair.resolves_at).timestamp() - time.time()) / 86400, 1 / 24)
        # The entry read already cleared every bar. The recheck only asks whether the
        # chosen basket is still profitable: no basket floor and no return hurdle.
        min_net, min_edge, hurdle = ((0, self.cfg.recheck_min_edge, 0.0) if recheck is not None
                                     else (self.cfg.min_net, self.cfg.min_edge, self.cfg.risk_free))
        return best(offers, days, hurdle, self.cfg.max_sets, self.limits(),
                    Decimal(str(min_net)), Decimal(str(min_edge)), self.cfg.max_apr,
                    self.cfg.require_cross, fundable=lambda basket: self.budget_block(basket) is None)

    def scan(self) -> Basket | None:
        self.counters["scans"] += 1
        basket, reason = None, "scan_error"
        try:
            basket, reason = self.price_pair()
        finally:
            self.kalshi_broker.observe("scan_complete", {"reason": reason, "candidates": int(basket is not None),
                                      "pair": self.pair.kalshi_event, "basket": basket.summary() if basket else None,
                                      "quotes": self.last_quotes, "quote": {**self.scan_quote, "reason": reason}})
        return basket

    # -- execution -------------------------------------------------------------

    def risk_block(self) -> str | None:
        if time.time() < self.cooldown_until:
            return "cooldown"
        if self.limits() < 1:
            return "no_cash" if self.cash < 1 else "capital_limit"
        return None

    def fill_bound(self, leg: dict[str, Any], identifier: str,
                   got: Decimal) -> tuple[Decimal, Decimal, Decimal, bool] | None:
        """(worst price per contract, fees, all-in cost, exact) for a leg's confirmed fill.

        Polymarket US and paper brokers book receipts as the order returns; use them.
        Kalshi fills are booked later by the account monitor, so a confirmed IOC fill
        with no receipt yet is bounded by its limit: a buy never fills above it, and
        fee_bound covers every fill price at or below it."""
        receipts = [e["data"] for e in self.store.events(kinds=("execution",))
                    if e["data"].get("order_id") == identifier]
        booked = sum((decimal(r["quantity"]) for r in receipts), Decimal(0))
        if receipts and booked == got:
            prices = [decimal(r["yes_price"]) if leg["contract"] == "YES" else 1 - decimal(r["yes_price"])
                      for r in receipts]
            fees = sum((decimal(r["fee"]) for r in receipts), Decimal(0))
            spent = sum((decimal(r["quantity"]) * p for r, p in zip(receipts, prices)), Decimal(0)) + fees
            return max(prices), fees, spent, True
        if venue_of(leg["ticker"]) == "kalshi" and booked <= got:
            limit = decimal(leg["limit"])
            fees = fee_bound(leg, limit, int(got))
            return limit, fees, got * limit + fees, False
        return None

    def execute(self, basket: Basket) -> None:
        event = self.pair.kalshi_event
        intended = {(leg["ticker"], leg["contract"]) for leg in basket.legs}
        current, why = self.price_pair(recheck=intended)
        if current is None or {(leg["ticker"], leg["contract"]) for leg in current.legs} != intended:
            self.reject(event, "recheck_failed", why=why if current is None else "legs_changed")
            return
        capital_limit = self.limits()
        edge = max(Decimal(str(self.cfg.min_edge)), 1 - capital_limit / current.sets)
        # Slippage spends only the profit above min_edge. A basket the recheck kept below
        # min_edge (down to recheck_min_edge) has none to spend: it goes at its book prices.
        widen = bool(self.us and self.cfg.profit_slippage and current.edge >= edge)
        if widen:
            def fundable(leg: dict[str, Any], price: Decimal, quantity: int = current.sets) -> bool:
                candidate = {**leg, "limit": str(price)}
                return self.brokers[leg["venue"]].check({"ticker": leg["ticker"], **buy(candidate),
                                                       "count": str(quantity)})["status"] == "ready"
            widened = split_limits(current.legs, current.sets, edge, fundable)
            if widened is None:
                self.reject(event, "slippage_profit_floor")
                return
            current.legs = widened
            if sum(current.sets * decimal(l["limit"]) + decimal(l["fee"]) for l in widened) > self.limits():
                self.reject(event, "slippage_capital_limit")
                return
        blocked = self.check_basket(current)
        if blocked:
            self.reject(event, "basket_preflight", detail=blocked)
            return
        trade_id = uuid.uuid4().hex[:12]
        context = {"trade_id": trade_id, "experiment_id": self.experiment.id, "strategy": STRATEGY,
                   "kalshi_event": event, "polymarket_event": self.pair.polymarket_event,
                   "pair": asdict(self.pair), "sets": current.sets, "intended_legs": current.legs,
                   "expected_net": str(current.net), "edge": str(current.edge), "apr": current.apr, "days": current.days}
        # The thinnest book first: it is the leg most likely to come up short.
        legs = sorted(current.legs, key=lambda leg: decimal(leg["depth"]))
        target, outcomes, sent = Decimal(current.sets), [], []
        failure: BaseException | None = None
        attempted: tuple[dict[str, Any], str] | None = None
        try:
            first_price, first_fees, first_cost = Decimal(0), Decimal(0), Decimal(0)
            first_quantity = Decimal(0)
            for index, leg in enumerate(legs):
                if target < 1:
                    break
                if widen and index == 1:
                    revised = second_limit(leg, int(target), edge, first_price, first_fees / first_quantity,
                                           lambda p: fundable(leg, p, int(target))
                                           and first_cost + cost(leg, p, int(target)) <= capital_limit)
                    if revised is None:
                        outcomes.append({"status": "profit_floor", "filled": "0", "ticker": leg["ticker"]})
                        break
                    leg = {**leg, "limit": str(revised)}
                    self.kalshi_broker.observe("slippage_reprice", {"trade_id": trade_id, "leg": leg,
                        "first_price_bound": str(first_price), "first_fees": str(first_fees), "sets": int(target)})
                if self.kalshi_stream is not None:
                    for ticker, _ in self.pair.contracts("kalshi"):
                        quote = self.kalshi_execution_book(ticker)["execution_quote"]
                        self.kalshi_broker.observe("execution_quote", {"trade_id": trade_id,
                            "ticker": ticker, **quote})
                if self.stream_book is not None:
                    quote = self.us_execution_book()["execution_quote"]
                    self.kalshi_broker.observe("execution_quote", {"trade_id": trade_id,
                        "ticker": "PMUS:" + self.pair.polymarket_market,
                        "submitted_ticker": leg["ticker"], **quote})
                identifier = str(uuid.uuid4())
                attempted = (leg, identifier)
                if index == 0:
                    self.cooldown_until = time.time() + self.cfg.cooldown
                result = self.brokers[venue_of(leg["ticker"])].submit({"client_order_id": identifier, "ticker": leg["ticker"], **buy(leg), "count": str(int(target)),
                                             "time_in_force": "immediate_or_cancel",
                                             "self_trade_prevention_type": "taker_at_cross"}, context)
                got = decimal(result.get("filled") or "0")
                outcomes.append({"ticker": leg["ticker"], "venue": leg["venue"], "asked": str(int(target)),
                                 "status": result["status"], "filled": str(got)})
                if result.get("status") not in TERMINAL or "filled" not in result:
                    # Never send the next leg (or an unwind) on a guess about this one.
                    self.halt(f"order on {leg['ticker']} has an unknown outcome", recoverable=True)
                    sent.append((leg, got))
                    break
                sent.append((leg, got))
                target = min(target, got)
                if widen and got > 0:
                    bound = self.fill_bound(leg, identifier, got)
                    if bound is None:
                        self.halt(f"missing fill evidence for {leg['ticker']}; reconcile before repair", recoverable=True)
                        break
                    price_bound, fees, spent, exact = bound
                    if index == 0:
                        first_quantity, first_price, first_fees, first_cost = got, price_bound, fees, spent
                    elif exact and 1 - first_price - first_fees / first_quantity - price_bound - fees / got < edge:
                        # A limit-bounded second leg already met the floor when its limit was set.
                        failure = ValueError("actual matched fills fall below the profit floor; repairing both legs")
                        break
        except Exception as exc:
            failure = exc
            outcomes.append({"status": "error", "error": f"{type(exc).__name__}: {exc}"[:200]})
            if attempted is not None:
                failed_leg, identifier = attempted
                rows = [o for o in self.brokers[venue_of(failed_leg["ticker"])].orders()
                        if o["client_order_id"] == identifier]
                if rows and rows[0]["status"] in ("pending", "unknown", "acknowledged"):
                    self.halt(f"order {identifier} may have executed; reconcile before repairing this experiment", recoverable=True)
                elif rows and not any(leg is failed_leg for leg, _ in sent):
                    sent.append((failed_leg, decimal(rows[0]["filled"])))
        # A known failure still needs repair of fills already obtained. Unknown writes
        # retain every holding and reservation until authoritative recovery is available.
        keep = target if failure is None and len(sent) == len(legs) else Decimal(0)
        for leg, got in ([] if self.halted else sent):
            if got <= keep:
                continue
            try:
                outcome = self.unwind(leg, got - keep, context)
                outcomes.append(outcome)
                if outcome["status"] not in TERMINAL or decimal(outcome["sold"]) < got - keep:
                    self.halt("incomplete basket repair; waiting for automatic sell recovery", recoverable=True)
                    break
            except Exception as exc:
                outcomes.append({"status": "repair_error", "ticker": leg["ticker"], "error": str(exc)[:200]})
                self.halt("basket repair failed; waiting for automatic sell recovery", recoverable=True)
                failure = failure or exc
                break
        cooldown_record: dict[str, Any] = {}
        if (failure is None and not self.halted and len(outcomes) == 1
                and outcomes[0].get("status") == "canceled"
                and decimal(outcomes[0].get("filled", "-1")) == 0):
            # A confirmed empty IOC has no inventory to protect. Retry only after
            # a short backoff and the next scan's fresh prices and preflight.
            self.cooldown_until = time.time() + 1.0
            cooldown_record["zero_fill_cooldown_until"] = self.cooldown_until
        self.kalshi_broker.observe("basket_execution", {"context": context, "outcomes": outcomes, **cooldown_record})
        self.resume()
        if failure is not None:
            # Keep the recovery loop alive after a transport failure. Durable
            # broker intent, acknowledgement and fills determine the next action.
            log.error("basket execution failed: %s", failure)
        if any(decimal(o.get("filled") or "0") > 0 for o in outcomes):
            self.counters["baskets"] += 1
        log.info("BASKET %s %s sets, expected net %s; actual %s", event, current.sets, current.net, outcomes)

    def unwind(self, leg: dict[str, Any], count: Decimal, context: dict[str, Any]) -> dict[str, Any]:
        """Sell excess at visible bids, without an entry-price loss floor."""
        ticker, venue = leg["ticker"], venue_of(leg["ticker"])
        if venue == "kalshi":
            market = self.kalshi.market(ticker)
            trading = market["status"] in ("open", "active")
            book = self.kalshi_execution_book(ticker) if trading else {}
        elif self.us:
            slug = ticker.removeprefix("PMUS:")
            trading = self.poly.market_raw(slug)["status"] == "MARKET_STATUS_OPEN"
            book = self.us_execution_book() if trading else {}
        else:
            token = ticker.removeprefix("PM:")
            market = self.poly.market_by_slug(self.pair.polymarket_event)
            trading = normalize_market(market, token)["status"] == "active"
            book = kalshi_layout(self.poly.books([token])[token]) if trading else {}
        quantity, floor = Decimal(0), Decimal(0)
        for price, depth in book_levels(book)[leg["contract"].lower()]:
            if not 0 < price < 1:
                continue
            quantity += min(depth, count - quantity)
            floor = price
            if quantity >= count:
                break
        if quantity == 0:
            return {"ticker": ticker, "venue": venue, "unwind": str(count), "sold": "0",
                    "status": "no_liquidity" if trading else "market_closed"}
        no = leg["contract"] == "NO"  # orders are on the YES book: selling NO is buying YES at 1 - price
        result = self.brokers[venue].submit({"ticker": ticker, "side": "bid" if no else "ask",
                                     "count": str(quantity), "price": str(1 - floor if no else floor),
                                     "time_in_force": "immediate_or_cancel",
                                     "self_trade_prevention_type": "taker_at_cross", "reduce_only": True},
                                    {**context, "repair": "unwind"})
        self.counters["unwinds"] += 1
        sold = decimal(result.get("filled") or "0")
        return {"ticker": leg["ticker"], "venue": leg["venue"], "unwind": str(count), "sold": str(sold),
                "status": result["status"], "floor": str(floor)}

    def recover(self) -> None:
        """One recovery pass; outstanding intents fence retries across restarts."""
        if any(not h["recoverable"] for h in self._halts.values()) or self.live_block():
            return
        self.validate_inventory()
        if self.repairs:
            # Recompute after each reconciled sale, since FIFO can change lots.
            # Submit at most one repair per scan and never enter a new basket here.
            leg, count, context = self.repairs[0]
            try:
                outcome = self.unwind(leg, count, context)
                self.kalshi_broker.observe("recovery_attempt", {"context": context, "outcome": outcome})
            except Exception as exc:
                self.kalshi_broker.observe("recovery_attempt", {"context": context,
                    "outcome": {"ticker": leg["ticker"], "status": "error", "error": str(exc)[:200]}})
                log.error("automatic sell recovery failed: %s", exc)
            self.resume()
        if not self.repairs and not self.live_block() and all(h["recoverable"] for h in self._halts.values()):
            for h in self._halts.values():
                self.kalshi_broker.observe("halt_cleared", {"halt_id": h["id"],
                    "reason": "Automatic recovery: reconciled holdings have no unmatched contracts"})
            self.resume()

    # -- run -------------------------------------------------------------------

    def step(self) -> None:
        if os.path.exists(os.path.join(self.diagnostics, "KILL")):
            self.halt("kill switch file present")
        self.resume()
        if self.halted:
            self.recover()
            return
        pause = self.live_block()
        if pause:
            if not self._throttled(("live_block", pause), 60):
                self.kalshi_broker.observe("live_paused", {"reason": pause})
            return
        basket = self.scan()
        if basket is None:
            return
        block = self.risk_block()
        if block:
            self.reject(self.pair.kalshi_event, block)
            return
        self.execute(basket)

    def run(self, once: bool = False) -> None:
        deadline = time.monotonic() + self.cfg.duration_minutes * 60 if self.cfg.duration_minutes else None
        try:
            while True:
                started = time.monotonic()
                self.step()
                self.kalshi_broker.observe("heartbeat", {"cash": str(self.cash), "exposure": str(self.exposure()),
                                                  "book_stream": self.stream_status(),
                                                  "open_trades": len(self.positions), "halted": self.halted,
                                                  "counters": dict(self.counters)})
                if once or (deadline and time.monotonic() >= deadline):
                    break
                wait = max(1.0, self.cfg.interval_seconds - (time.monotonic() - started))
                time.sleep(min(wait, deadline - time.monotonic()) if deadline else wait)
        finally:
            if self.kalshi_stream is not None:
                self.kalshi_stream.close()
            stream = getattr(self.poly, "stream", None) if self.us else None
            if stream is not None:
                stream.stop()
            self.kalshi_broker.observe("run_stop", {"cash": str(self.cash), "counters": dict(self.counters)})


def buy(leg: dict[str, Any]) -> dict[str, str]:
    """Orders are on the YES book: buying NO at p is an ask (selling YES) at 1 - p."""
    if leg["contract"] == "NO":
        return {"side": "ask", "price": str(1 - decimal(leg["limit"]))}
    return {"side": "bid", "price": leg["limit"]}


def load_experiment(path: str) -> Config:
    with open(path, encoding="utf-8") as stream:
        data = json.load(stream)
    selected = dict(data.pop("pair"))
    selected["outcomes"] = tuple(Outcome(**o) for o in selected["outcomes"])
    parameters = data.pop("parameters", {})
    experiment = Experiment(pair=SelectedPair(**selected), **data)
    return Config(experiment=experiment, **parameters)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--experiment", required=True, metavar="FILE.json",
                   help="one pair, mode, parameters and two pre-created venue allocations; together these form the experiment portfolio")
    p.add_argument("--once", action="store_true", help="one scan, then stop; funding and holdings are retained")
    a = p.parse_args(argv)
    if not os.environ.get("STRATEGY_RUNS_DIR"):
        p.error("export STRATEGY_RUNS_DIR to the persistent shared logging directory before starting")
    try:
        cfg = load_experiment(a.experiment)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        p.error(f"invalid experiment: {exc}")
    engine = Engine(cfg)
    run_dir = os.path.join(engine.diagnostics, engine.run_id)
    os.makedirs(run_dir, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(os.path.join(run_dir, "run.log"))])
    if cfg.experiment.mode == "live":
        log.warning("LIVE%s: portfolio %s, caps $%s per basket, $%s total, %d sets; Kalshi $%s and Polymarket US $%s free",
                    " DRY RUN (nothing sent)" if cfg.dry_run else "", list(b.pool_id for b in engine.brokers.values()), cfg.max_basket_dollars,
                    cfg.max_total_dollars, cfg.max_sets, engine.kalshi_broker.available,
                    engine.poly_broker.available)
    try:
        engine.run(once=a.once)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
