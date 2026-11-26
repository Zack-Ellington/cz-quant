"""Independent cross-market sum check using one broker per venue.

Select one funded experiment with --experiment FILE; its mode selects paper or live. The strategy owns matching,
sizing, sequential IOC execution, partial-fill repairs, and monitor-health gates.
Brokers retain execution evidence in the shared portfolio log. See README.md for
configuration, collectors, recovery, and the supported venue modes.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from typing import Any

from czquant import Broker, Store
from czquant.accounting import active_events, combine_states, venue_of
from czquant.kalshi import Kalshi
from czquant.live import KalshiLive
from czquant.paper import Paper
from czquant.polymarket import Polymarket, PolymarketPaper, kalshi_layout, normalize_market, ticker
from czquant.polymarket_us import DEFAULT_THETA, PolymarketUS, PolymarketUSLive, PolymarketUSPaper
from czquant.polymarket_us import ticker as us_ticker
from czquant.store import decimal

from experiments import Experiment, Outcome, SelectedPair, in_scope
from markets import Listing, MarketPair, resolve
from matching import KalshiGame, Pair, PolyGame, kalshi_game, load_pairs, match, poly_game, us_game
from sum_check import Basket, best, kalshi_offer, polymarket_offer, polymarket_us_offer

STRATEGY = "cross-market-polymarket"
HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
LEAGUES = os.path.join(HERE, "leagues.json")
PAIRS = os.path.join(HERE, "pairs.json")
PAIRS_US = os.path.join(HERE, "pairs.us.json")  # pins and blocks for Polymarket US, whose slugs differ
US_LOOKAHEAD_DAYS = 8  # Polymarket US game events read at each discovery: starting within this many days
FEE_TYPES = ("quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees")
TICK = Decimal("0.01")
TERMINAL = {"filled", "canceled", "rejected", "failed", "dry_run"}
log = logging.getLogger("cross_market_polymarket")


@dataclass
class Config:
    experiment: Experiment | None = None
    interval_seconds: float = 15.0
    discover_minutes: float = 10.0
    max_event_frac: float = 0.10
    max_total_frac: float = 1.00
    max_open_events: int = 20
    max_sets: int = 20
    risk_free: float = 0.05
    min_net: float = 0.25
    min_edge: float = 0.01  # per $1 set: margin for rule and settlement risk between venues
    max_apr: float | None = None
    require_cross: bool = True  # baskets need a leg on each venue
    resolution_hours: float = 6.0  # game length plus Polymarket's resolution delay, for the hurdle
    unwind_slippage: float = 0.05
    kalshi_latency: float = 0.30
    polymarket_latency: float = 0.50
    cooldown: float = 300.0
    duration_minutes: float | None = None
    venue: str = "polymarket"  # polymarket (international, paper) or polymarket_us
    live: bool = False  # real orders: needs two mapped live venue cash pools
    dry_run: bool = False  # live, but build and sign orders without sending them
    pmus_theta: float | None = None  # Polymarket US taker coefficient; None: each market's own
    max_basket_dollars: float | None = 25.0  # hard cap on one basket's capital
    max_total_dollars: float | None = 100.0  # hard cap on capital at risk across open trades
    monitor_max_age: float = 300.0  # live: seconds a monitor's last health check stays valid

    def validate(self) -> None:
        for name, value in asdict(self).items():
            if name in ("experiment", "venue"):
                continue
            if name in ("live", "dry_run", "require_cross"):
                if type(value) is not bool:
                    raise ValueError(f"{name} must be boolean")
                continue
            if value is None and name in ("max_apr", "pmus_theta", "duration_minutes", "max_basket_dollars", "max_total_dollars"):
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite nonnegative number")
            if name in ("max_sets", "max_open_events") and (type(value) is not int or value < 1):
                raise ValueError(f"{name} must be a positive integer")
        if self.unwind_slippage > 1 or self.max_event_frac > 1 or self.max_total_frac > 1:
            raise ValueError("slippage and capital fractions must be at most one")


def load_leagues(names: tuple[str, ...] = (), path: str = LEAGUES) -> dict[str, dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        specs = json.load(f)
    unknown = set(names) - set(specs)
    if unknown:
        raise ValueError(f"unknown leagues: {sorted(unknown)}")
    return {k: v for k, v in specs.items() if (k in names if names else v.get("enabled"))}


class Engine:
    def __init__(self, cfg: Config, kalshi: Any = None, poly: Polymarket | None = None,
                 leagues: dict[str, dict[str, Any]] | None = None, pairs: dict[str, Any] | None = None,
                 revision: str | None = None,
                 us: PolymarketUS | None = None, kalshi_live: Any = None):
        self.cfg = cfg
        cfg.validate()
        if cfg.experiment is None:
            raise ValueError("select an experiment configuration with pre-created venue pools")
        self.experiment = cfg.experiment
        self.experiment.validate()
        selected = self.experiment.pair
        if cfg.venue != self.experiment.venue or cfg.live != (self.experiment.mode == "live"):
            raise ValueError("configuration venue and mode must match the experiment")
        if cfg.dry_run and not cfg.live:
            raise ValueError("dry_run requires live mode")
        self.theta_override = None if cfg.pmus_theta is None else Decimal(str(cfg.pmus_theta))
        kalshi_pool, poly_pool = self.experiment.kalshi_pool, self.experiment.polymarket_pool
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        self.store = Store()
        self.diagnostics = os.path.join(str(self.store.root), "diagnostics", STRATEGY, kalshi_pool, poly_pool)
        self.kalshi = kalshi or Kalshi(authenticated=False)
        self.us = cfg.venue == "polymarket_us"
        if cfg.live and not self.us:
            raise ValueError("live trading requires venue polymarket_us in the experiment file")
        if self.us:
            self.poly: Any = us or PolymarketUS.from_env(authenticated=cfg.live, dry_run=cfg.dry_run)
            poly_adapter: Any = (PolymarketUSLive(self.poly) if cfg.live else
                                 PolymarketUSPaper(self.poly, latency=cfg.polymarket_latency,
                                                   theta=self.theta_override))
        else:
            self.poly = poly or Polymarket.from_env()
            poly_adapter = PolymarketPaper(self.poly, latency=cfg.polymarket_latency)
        kalshi_adapter = (KalshiLive(kalshi_live or Kalshi(authenticated=True), dry_run=cfg.dry_run)
                          if cfg.live else Paper(self.kalshi, latency=cfg.kalshi_latency))
        self.kalshi_broker = Broker(self.store, kalshi_pool, kalshi_adapter, self.run_id)
        self.poly_broker = Broker(self.store, poly_pool, poly_adapter, self.run_id)
        self.brokers = {broker.venue: broker for broker in (self.kalshi_broker, self.poly_broker)}
        expected_mode = "live" if cfg.live else "paper"
        if any(b.mode != expected_mode for b in self.brokers.values()):
            raise ValueError(f"both cash pools must be {expected_mode}")
        if cfg.live and not self.poly.authenticated:
            raise ValueError("live trading needs POLYMARKET_US_KEY_ID and POLYMARKET_US_SECRET_KEY_PATH")
        self.leagues = ({} if selected.league == "markets" else
                        {selected.league: (leagues if leagues is not None else load_leagues((selected.league,)))[selected.league]})
        self.pairs = pairs if pairs is not None else load_pairs(PAIRS_US if self.us else PAIRS)
        if selected.league == "markets":
            self.listings = [Listing(self.experiment.id, selected.outcomes[0].kalshi,
                                    selected.polymarket_event, invert=selected.invert)]
        else:
            self.listings = []
        revision = revision or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()
        self.validate_allocations()
        run_config = {**asdict(self.cfg), "leagues": self.leagues,
                      "pairs": {"pinned": self.pairs["pairs"], "blocked": sorted(self.pairs["blocked"])},
                      "markets": [asdict(x) for x in self.listings]}
        # Serialize ownership checks and run records across processes sharing this store.
        with self.store.transaction():
            identity = json.loads(json.dumps(asdict(self.experiment)))
            for broker in self.brokers.values():
                for event in self.store.events(broker.pool_id, kinds=("run",)):
                    prior = event["data"].get("config", {}).get("experiment")
                    if prior != identity:
                        raise ValueError(f"pool {broker.pool_id} already belongs to another experiment; reuse its configuration")
            for broker in self.brokers.values():
                broker.start(STRATEGY, revision, run_config)
        self.games: dict[str, list[KalshiGame]] = {}
        self.polys: dict[str, list[PolyGame]] = {}
        self.fees: dict[str, Decimal | None] = {}
        self.market_pairs: list[MarketPair] = []
        self.series_fees: dict[str, Decimal | None] = {}
        self.market_counts: dict[str, int] = {}
        self._discovered = -1e18
        self.cooldown_until: dict[str, float] = {}
        self.halted: str | None = None
        self.positions: dict[str, dict[str, Any]] = {}
        self.counters = {"scans": 0, "baskets": 0, "rejects": 0, "unwinds": 0}
        self._last_logged: dict[tuple, float] = {}
        self.resume()

    def validate_allocations(self) -> None:
        """Check both allocations before any entry or repair can be requested."""
        pools = {p["id"]: p for p in self.store.pools()}
        for broker in self.brokers.values():
            pool = pools[broker.pool_id]
            metadata = pool["metadata"]
            if pool["mode"] != self.experiment.mode or pool["venue"] != broker.venue:
                raise ValueError("pool venue or mode no longer matches the experiment")
            if metadata.get("retired_at") or metadata.get("reserve"):
                raise ValueError(f"pool {broker.pool_id} is retired or an account reserve")
            contracts = self.experiment.contracts(broker.venue)
            if any(not in_scope(metadata.get("markets"), market) for market, _ in contracts):
                raise ValueError(f"experiment pair is outside cash pool {broker.pool_id}'s markets")
            if self.cfg.live and broker.venue == "kalshi" and metadata.get("subaccount") is None:
                raise ValueError("live Kalshi pool needs an existing account mapping")
            for lot in broker.state().lots.values():
                if ((lot["market"], lot["side"].upper()) not in contracts
                        or lot["context"].get("experiment_id") != self.experiment.id
                        or not lot["context"].get("trade_id") or not lot["context"].get("intended_legs")):
                    raise ValueError(f"incompatible existing holdings in {broker.pool_id}; reconcile before restart")
            for order in broker.orders(("pending", "unknown", "acknowledged")):
                if order["context"].get("experiment_id") != self.experiment.id:
                    raise ValueError(f"incompatible pending order in {broker.pool_id}; reconcile before restart")

    def validate_pair(self, pair: Pair | MarketPair) -> None:
        selected = self.experiment.pair
        if isinstance(pair, MarketPair):
            actual = {(pair.key, side.upper(), ticker(token), "YES") for side, token in pair.tokens.items()}
            identity = (pair.event, pair.listing.polymarket, str(pair.poly.get("id")))
        else:
            actual = {(k, "YES", us_ticker(pair.poly.market["slug"]) if self.us else ticker(p),
                       p if self.us else "YES") for k, p in pair.legs.items()}
            identity = (pair.game.event, pair.poly.slug,
                        str(pair.poly.market.get("slug") if self.us else pair.poly.market.get("id")))
        expected = {(o.kalshi, o.kalshi_side, o.polymarket, o.polymarket_side) for o in selected.outcomes}
        if identity != (selected.kalshi_event, selected.polymarket_event, selected.polymarket_market) or actual != expected:
            raise ValueError("venue pair or outcome mapping differs from the experiment; review configuration")

    def validate_basket(self, basket: Basket, pair: Pair | MarketPair) -> str | None:
        """Raise for invalid configuration; return a reason for a runtime refusal."""
        self.validate_pair(pair)
        self.validate_allocations()
        roles = []
        need: dict[str, Decimal] = {}
        for leg in basket.legs:
            venue = venue_of(leg["ticker"])
            contract = (leg["ticker"], leg["contract"])
            if venue not in self.brokers or contract not in self.experiment.contracts(venue):
                raise ValueError("basket contains a contract outside the selected pair")
            if leg["venue"] != venue or decimal(leg["qty"]) != basket.sets or basket.sets < 1:
                raise ValueError("basket venue or quantity is inconsistent")
            role = next(i for i, o in enumerate(self.experiment.pair.outcomes)
                        if contract == ((o.kalshi, o.kalshi_side) if venue == "kalshi" else
                                        (o.polymarket, o.polymarket_side)))
            roles.append(role)
            price = decimal(leg["limit"])
            if not price.is_finite() or not 0 <= price <= 1:
                raise ValueError("invalid basket limit price")
            # Mirrors czquant.broker.FEE_ALLOWANCE, including its Kalshi fallback.
            allowance = Decimal("0.025") if venue == "polymarket_us" else Decimal("0.0175")
            fees = max(decimal(leg["fee"]), (basket.sets * allowance).quantize(Decimal("0.01"), rounding=ROUND_CEILING))
            need[venue] = need.get(venue, Decimal(0)) + basket.sets * price + fees
        if sorted(roles) != [0, 1] or (self.cfg.require_cross and len(need) != 2):
            raise ValueError("basket must cover both outcomes and the configured venues")
        block = "halted" if self.halted else self.live_block()
        if block:
            return block
        if len({leg["ticker"] for leg in basket.legs}) != len(basket.legs):
            return "basket contains opposite sides of the same market; fills would net"
        if any(self.brokers[venue].available < amount for venue, amount in need.items()):
            return "selected allocation cannot fund the full basket at its limit prices"
        # Opposite inventory would close an existing position instead of buying a set.
        for leg in basket.legs:
            if any(lot["market"] == leg["ticker"] and lot["side"].upper() != leg["contract"]
                   for lot in self.brokers[venue_of(leg["ticker"])].state().lots.values()):
                return "basket conflicts with existing inventory; reconcile before adding a set"
        return None

    # -- state ---------------------------------------------------------------

    def resume(self) -> None:
        """Recover actual holdings and the halt state from the shared store."""
        # Read orders first: if the monitor finishes while state is read, defer
        # completeness until the next scan rather than judging older lots.
        awaiting = {o["context"].get("trade_id") for b in self.brokers.values()
                    for o in b.orders(("pending", "unknown", "acknowledged"))}
        state = combine_states([b.state() for b in self.brokers.values()])
        self.cash = state.cash - sum((self.store.reserved(b.pool_id) for b in self.brokers.values()), Decimal(0))
        self.bankroll = state.contributed
        self.positions = {}
        for lot in state.lots.values():
            context = lot["context"]
            if context.get("trade_id"):
                position = self.positions.setdefault(context["trade_id"], {**context, "capital": Decimal(0)})
                position["capital"] += lot["cost"]
        events = active_events(sorted(
            [e for b in self.brokers.values() for e in self.store.events(b.pool_id,
             kinds=("transaction", "observation", "correction", "order"), observations=("halt", "halt_cleared", "basket_settled"))],
            key=lambda e: (e["effective_at"], e["sequence"])))
        for event in events:
            data = event["data"]
            if event["type"] == "order" and data.get("status") == "intent":
                game = data.get("context", {}).get("kalshi_event")
                if game:
                    until = datetime.fromisoformat(event["effective_at"]).timestamp() + self.cfg.cooldown
                    self.cooldown_until[game] = max(self.cooldown_until.get(game, 0), until)
        observations = [e for e in events if e["type"] == "observation"]
        cleared = {e["data"]["halt_id"] for e in observations if e["data"].get("kind") == "halt_cleared"}
        reported = {e["data"]["trade_id"] for e in observations if e["data"].get("kind") == "basket_settled"}
        for e in observations:
            if e["data"].get("kind") == "halt" and e["record_id"] not in cleared:
                self.halted = e["data"]["reason"]
        totals: dict[str, Decimal] = {}
        contexts: dict[str, dict[str, Any]] = {}
        filled: dict[str, dict[tuple[str, str], Decimal]] = {}
        for e in events:
            d = e["data"]
            tid = (d.get("context") or {}).get("trade_id")
            if e["type"] != "transaction" or not tid:
                continue
            totals[tid] = totals.get(tid, Decimal(0)) + decimal(d["cash_delta"])
            contexts[tid] = d["context"]
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
            if tid not in reported:
                self.kalshi_broker.observe("basket_settled", {"trade_id": tid, "net_pnl": str(pnl), "fill_status": status,
                    "context": contexts[tid], "filled_legs": [{"ticker": t, "side": s, "quantity": str(q)}
                                                              for (t, s), q in filled.get(tid, {}).items()]})
            # Only a complete basket's loss contradicts the arbitrage; a partial one is directional.
            if pnl < Decimal("-0.005") and status != "partial":
                self.halt(f"settled trade {tid} lost ${-pnl:.2f}")

        for tid, position in self.positions.items():
            # Live fills can be confirmed before their lots are booked. The order
            # gate prevents new entries until the monitor finishes reconciliation.
            if tid in awaiting:
                continue
            intended = {(leg["ticker"], leg["contract"].lower()) for leg in position.get("intended_legs", [])}
            quantities: dict[tuple[str, str], Decimal] = {}
            for lot in state.lots.values():
                if lot["context"].get("trade_id") == tid:
                    key = (lot["market"], lot["side"])
                    quantities[key] = quantities.get(key, Decimal(0)) + lot["quantity"]
            # A settled leg can leave the other leg open legitimately. Recovery halts
            # only incomplete executions, not a basket whose settlement is in progress.
            settled = any(e["type"] == "transaction" and e["data"].get("reason") == "settlement"
                          and e["data"].get("context", {}).get("trade_id") == tid for e in events)
            if not settled and (set(quantities) != intended or len(set(quantities.values())) != 1):
                self.halt(f"trade {tid} has incomplete inventory; reconcile this experiment before restarting orders")

    def halt(self, reason: str) -> None:
        if not self.halted:
            self.halted = reason
            self.kalshi_broker.observe("halt", {"reason": reason})
            log.error("HALT: %s", reason)

    def exposure(self, event: str | None = None) -> Decimal:
        return sum((p["capital"] for p in self.positions.values() if event is None or p.get("kalshi_event") == event),
                   Decimal(0))

    def limits(self, event: str) -> Decimal:
        bankroll = self.bankroll
        cap = min(self.cash, bankroll * Decimal(str(self.cfg.max_total_frac)) - self.exposure(),
                  bankroll * Decimal(str(self.cfg.max_event_frac)) - self.exposure(event))
        if self.cfg.max_total_dollars is not None:
            # Open reservations count too: live Kalshi fills reach the ledger only with the monitor.
            cap = min(cap, Decimal(str(self.cfg.max_total_dollars)) - self.exposure()
                      - sum((self.store.reserved(b.pool_id) for b in self.brokers.values()), Decimal(0)))
        if self.cfg.max_basket_dollars is not None:
            cap = min(cap, Decimal(str(self.cfg.max_basket_dollars)))
        return cap

    def venue_shortfall(self, basket: Basket) -> str | None:
        """The first pool whose free cash cannot pay its legs, in paper or live mode."""
        need: dict[str, Decimal] = {}
        for leg in basket.legs:
            venue = venue_of(leg["ticker"])
            need[venue] = need.get(venue, Decimal(0)) + decimal(leg["vwap"]) * leg["qty"] + decimal(leg["fee"])
        return next((v for v, amount in need.items() if self.brokers[v].available < amount), None)

    def live_block(self) -> str | None:
        """Unknown writes and monitor discrepancies halt; acknowledged orders and
        missing or stale monitor checks pause until reconciliation catches up."""
        unresolved = sum(len(b.orders(("pending", "unknown"))) for b in self.brokers.values())
        if unresolved:
            self.halt(f"{unresolved} order(s) awaiting reconciliation; keep the same experiment and pools, reconcile both venues, then clear the halt")
            return "halted"
        busy = any(b.orders(("acknowledged",)) for b in self.brokers.values())
        if not self.cfg.live or self.cfg.dry_run:
            return "awaiting_reconciliation" if busy else None
        now = datetime.now(timezone.utc)
        for component in ("account", "polymarket_us"):
            rows = [e for e in self.store.events(kinds=("health",)) if e["data"].get("component") == component]
            if not rows:
                return f"no_{component}_monitor"
            last = rows[-1]
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

    # -- data ----------------------------------------------------------------

    def discover_us(self) -> dict[str, list[PolyGame]]:
        """Polymarket US game events starting within US_LOOKAHEAD_DAYS, by league, each
        with its moneyline. A game event lists its moneyline as the market group
        PRIMARY's market; the markets are read in batches by ID."""
        now = datetime.now(timezone.utc)
        prefixes = {spec.get("polymarket_us_prefix", name) + "-": name for name, spec in self.leagues.items()}
        events: dict[str, tuple[str, dict[str, Any], str]] = {}  # market id -> (league, event, ...)
        for event in self.poly.events(active="true", closed="false",
                                      startTimeMin=(now - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                      startTimeMax=(now + timedelta(days=US_LOOKAHEAD_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")):
            league = next((v for k, v in prefixes.items() if str(event.get("slug", "")).startswith(k)), None)
            primary = [g for g in event.get("marketGroups") or [] if g.get("id") == "PRIMARY"]
            if league is None or len(primary) != 1 or len(primary[0].get("marketIds") or []) != 1:
                continue
            events[str(primary[0]["marketIds"][0])] = (league, event, "")
        out: dict[str, list[PolyGame]] = {name: [] for name in self.leagues}
        for market in self.poly.markets_by_id(sorted(events)):
            league, event, _ = events[str(market.get("id"))]
            game = us_game(event, market)
            if game is not None:
                out[league].append(game)
        return out

    def discover(self) -> None:
        us_games: dict[str, list[PolyGame]] = {}
        if self.us:
            try:
                us_games = self.discover_us()
            except Exception as exc:
                log.exception("Polymarket US discovery failed")
                self.kalshi_broker.observe("error", {"where": "discover", "venue": "polymarket_us", "error": str(exc)[:300]})
        for name, spec in self.leagues.items():
            try:
                series = spec["kalshi_series"]
                schedule = self.kalshi.get(f"/series/{series}").get("series", {})
                fee_type = schedule.get("fee_type", "quadratic")
                self.fees[name] = decimal(schedule.get("fee_multiplier", "1")) if fee_type in FEE_TYPES else None
                events = list(self.kalshi.pages("/events", "events", series_ticker=series, status="open",
                                                with_nested_markets="true", limit=200))
                self.games[name] = [g for g in (kalshi_game(e) for e in events) if g is not None]
                if self.us:
                    self.polys[name] = us_games.get(name, [])
                    continue
                polys = []
                for event in self.poly.events(tag_slug=spec["polymarket_tag"], closed="false"):
                    game = poly_game(event)
                    if game is not None:
                        self.poly.remember(game.market)
                        polys.append(game)
                self.polys[name] = polys
            except Exception as exc:
                log.exception("discovery failed for %s", name)
                self.kalshi_broker.observe("error", {"where": "discover", "league": name, "error": str(exc)[:300]})
        if self.listings:
            self.discover_markets()
        else:
            self.market_pairs, self.market_counts = [], {}
        self._discovered = time.monotonic()
        self.kalshi_broker.observe("universe", {"markets": {"listed": len(self.listings), "paired": len(self.market_pairs),
                                                     "counts": self.market_counts},"kalshi_games": {k: len(v) for k, v in self.games.items()},
                                         "polymarket_games": {k: len(v) for k, v in self.polys.items()},
                                         "fee_multipliers": {k: None if v is None else str(v) for k, v in self.fees.items()}})

    def series_fee(self, series: str) -> Decimal | None:
        if series not in self.series_fees:
            schedule = self.kalshi.get(f"/series/{series}").get("series", {})
            fee_type = schedule.get("fee_type", "quadratic")
            self.series_fees[series] = (decimal(schedule.get("fee_multiplier", "1"))
                                        if fee_type in FEE_TYPES else None)
        return self.series_fees[series]

    def discover_markets(self) -> None:
        """Resolve each reviewed listing against both venues; a listing that no longer
        checks out (closed, not yes/no, renamed) is counted and skipped."""
        pairs, counts = [], {}
        for listing in self.listings:
            try:
                kmarket = self.kalshi.get(f"/markets/{listing.kalshi}").get("market", {})
                pmarket = self.poly.market_by_slug(listing.polymarket)
                pair, why = resolve(listing, kmarket, pmarket)
                if pair is not None:
                    event = self.kalshi.get(f"/events/{pair.event}").get("event", {})
                    pair.series = event.get("series_ticker", "")
                    if self.series_fee(pair.series) is None:
                        pair, why = None, "unsupported_kalshi_fees"
            except Exception as exc:
                pair, why = None, "lookup_failed"
                self.kalshi_broker.observe("error", {"where": "discover_markets", "listing": listing.kalshi,
                                              "error": str(exc)[:300]})
            counts[why] = counts.get(why, 0) + 1
            if pair is not None:
                self.validate_pair(pair)
                pairs.append(pair)
        self.market_pairs, self.market_counts = pairs, counts

    def kalshi_books(self, tickers: list[str]) -> dict[str, dict[str, Any]]:
        out = {}
        for i in range(0, len(tickers), 100):
            data = self.kalshi.get("/markets/orderbooks", [("tickers", t) for t in tickers[i:i + 100]])
            for row in data.get("orderbooks") or []:
                out[row["ticker"]] = row
        return out

    def poly_books(self, tokens: list[str]) -> dict[str, dict[str, Any]]:
        """Polymarket books in the Kalshi layout: by token, or on Polymarket US by market slug."""
        if self.us:
            return self.poly.books(sorted(set(tokens)))
        return {token: kalshi_layout(raw) for token, raw in self.poly.books(tokens).items()}

    def book_keys(self, pair: Pair) -> list[str]:
        return [pair.poly.market["slug"]] if self.us else list(pair.legs.values())

    def theta(self, market: dict[str, Any]) -> Decimal:
        if self.theta_override is not None:
            return self.theta_override
        coefficient = market.get("feeCoefficient")
        return Decimal(str(coefficient)) if coefficient is not None else DEFAULT_THETA

    # -- evaluation ------------------------------------------------------------

    def evaluate(self, league: str, pair: Pair, kbooks: dict[str, Any], pbooks: dict[str, Any],
                 max_capital: Decimal | None = None) -> tuple[Basket | None, str]:
        now = time.time()
        start = pair.poly.start.timestamp() if pair.poly.start else None
        if start is None:
            return None, "no_start_time"
        multiplier = self.fees.get(league)
        if multiplier is None:
            return None, "unsupported_kalshi_fees"
        offers: dict[str, list] = {}
        if self.us:
            market, slug = pair.poly.market, pair.poly.market["slug"]
            if market.get("status") != "MARKET_STATUS_OPEN" or slug not in pbooks:
                return None, "polymarket_not_trading" if slug in pbooks else "no_book"
            for kticker, contract in pair.legs.items():
                if kticker not in kbooks:
                    return None, "no_book"
                offers[kticker] = [kalshi_offer(kticker, kticker, kbooks[kticker], multiplier),
                                   polymarket_us_offer(kticker, us_ticker(slug), pbooks[slug], self.theta(market), contract)]
        for kticker, token in ([] if self.us else pair.legs.items()):
            market = normalize_market(pair.poly.market, token)
            if market["status"] != "active":
                return None, "polymarket_not_trading"
            if kticker not in kbooks or token not in pbooks:
                return None, "no_book"
            offers[kticker] = [kalshi_offer(kticker, kticker, kbooks[kticker], multiplier),
                               polymarket_offer(kticker, ticker(token), pbooks[token], market["fee_schedule"])]
        # Games in progress trade too; past the expected payout, hold for at least an hour.
        days = max(start + self.cfg.resolution_hours * 3600 - now, 3600) / 86400
        cap = max_capital if max_capital is not None else self.limits(pair.game.event)
        return best(offers, days, self.cfg.risk_free, self.cfg.max_sets, max(cap, Decimal(0)),
                    Decimal(str(self.cfg.min_net)), Decimal(str(self.cfg.min_edge)), self.cfg.max_apr,
                    self.cfg.require_cross)

    def evaluate_market(self, pair: MarketPair, kbooks: dict[str, Any], pbooks: dict[str, Any],
                        max_capital: Decimal | None = None) -> tuple[Basket | None, str]:
        if pair.resolves is None:
            return None, "no_resolution_date"
        multiplier = self.series_fees.get(pair.series) or Decimal(1)
        offers: dict[str, list] = {}
        for role, ways in pair.roles().items():
            offers[role] = []
            for venue, name, contract in ways:
                if venue == "kalshi":
                    if name not in kbooks:
                        return None, "no_book"
                    offers[role].append(kalshi_offer(role, name, kbooks[name], multiplier, contract))
                else:
                    market = normalize_market(pair.poly, name)
                    if market["status"] != "active":
                        return None, "polymarket_not_trading"
                    if name not in pbooks:
                        return None, "no_book"
                    offers[role].append(polymarket_offer(role, ticker(name), pbooks[name], market["fee_schedule"]))
        days = (pair.resolves.timestamp() + self.cfg.resolution_hours * 3600 - time.time()) / 86400
        cap = max_capital if max_capital is not None else self.limits(pair.event)
        return best(offers, days, self.cfg.risk_free, self.cfg.max_sets, max(cap, Decimal(0)),
                    Decimal(str(self.cfg.min_net)), Decimal(str(self.cfg.min_edge)), self.cfg.max_apr, True)

    def scan_markets(self, counts: dict[str, int], candidates: list) -> None:
        if not self.market_pairs:
            return
        try:
            kbooks = self.kalshi_books([p.listing.kalshi for p in self.market_pairs])
            pbooks = self.poly_books([t for p in self.market_pairs for t in p.tokens.values()])
        except Exception as exc:
            log.exception("book read failed for markets")
            self.kalshi_broker.observe("error", {"where": "books", "league": "markets", "error": str(exc)[:300]})
            return
        for pair in self.market_pairs:
            basket, why = self.evaluate_market(pair, kbooks, pbooks,
                                               self.bankroll * Decimal(str(self.cfg.max_event_frac)))
            counts["market_" + why] = counts.get("market_" + why, 0) + 1
            if basket:
                candidates.append((basket, pair, "markets"))
                if not self._throttled(("opportunity", pair.key), 60):
                    self.kalshi_broker.observe("opportunity", {"league": "markets", "kalshi_event": pair.event,
                                                        "kalshi_market": pair.key,
                                                        "polymarket_event": pair.listing.polymarket,
                                                        **basket.summary()})

    def pair_league(self, league: str, counts: dict[str, int]) -> list[Pair]:
        paired = []
        spec = self.leagues[league]
        aliases = ((spec.get("polymarket_us_abbr_aliases") if self.us else None)
                   or spec.get("abbr_aliases") or {})
        for game in self.games.get(league, []):
            if game.event != self.experiment.pair.kalshi_event:
                continue
            if game.event in self.pairs["blocked"]:
                counts["blocked"] = counts.get("blocked", 0) + 1
                continue
            pair, why = match(game, [p for p in self.polys.get(league, [])
                                     if p.slug == self.experiment.pair.polymarket_event], aliases,
                              self.pairs["pairs"].get(game.event))
            counts[why] = counts.get(why, 0) + 1
            if pair:
                self.validate_pair(pair)
                paired.append(pair)
        return paired

    def scan(self) -> list[tuple[Basket, Pair, str]]:
        counts: dict[str, int] = {}
        candidates = []
        self.counters["scans"] += 1
        for league in self.leagues:
            paired = self.pair_league(league, counts)
            if not paired:
                continue
            try:
                kbooks = self.kalshi_books([t for p in paired for t in p.legs])
                pbooks = self.poly_books([t for p in paired for t in self.book_keys(p)])
            except Exception as exc:
                log.exception("book read failed for %s", league)
                self.kalshi_broker.observe("error", {"where": "books", "league": league, "error": str(exc)[:300]})
                continue
            for pair in paired:
                basket, why = self.evaluate(league, pair, kbooks, pbooks,
                                            self.bankroll * Decimal(str(self.cfg.max_event_frac)))
                counts["priced_" + why] = counts.get("priced_" + why, 0) + 1
                if basket:
                    candidates.append((basket, pair, league))
                    if not self._throttled(("opportunity", pair.game.event), 60):
                        self.kalshi_broker.observe("opportunity", {"league": league, "kalshi_event": pair.game.event,
                                                            "polymarket_event": pair.poly.slug, **basket.summary()})
        self.scan_markets(counts, candidates)
        top = max((b for b, _, _ in candidates), key=lambda b: b.apr, default=None)
        self.kalshi_broker.observe("scan_complete", {"counts": counts, "candidates": len(candidates),
                                              "best_apr": top.apr if top else None,
                                              "best_edge": str(top.edge) if top else None})
        return candidates

    # -- execution -------------------------------------------------------------

    def risk_block(self, event: str) -> str | None:
        if time.time() < self.cooldown_until.get(event, 0):
            return "cooldown"
        held = {p.get("kalshi_event") for p in self.positions.values()}
        if event not in held and len(held) >= self.cfg.max_open_events:
            return "max_open_events"
        if self.limits(event) < 1:
            return "no_cash" if self.cash < 1 else "capital_limit"
        return None

    def recheck(self, pair: Pair | MarketPair, league: str) -> tuple[Basket | None, str]:
        if isinstance(pair, MarketPair):
            return self.evaluate_market(pair, self.kalshi_books([pair.listing.kalshi]),
                                        self.poly_books(list(pair.tokens.values())))
        return self.evaluate(league, pair, self.kalshi_books(list(pair.legs)),
                             self.poly_books(self.book_keys(pair)))

    def execute(self, basket: Basket, pair: Pair | MarketPair, league: str) -> None:
        self.validate_pair(pair)
        event = event_of(pair)
        self.cooldown_until[event] = time.time() + self.cfg.cooldown
        intended = {(leg["ticker"], leg["contract"]) for leg in basket.legs}
        current, why = self.recheck(pair, league)
        if current is None or {(leg["ticker"], leg["contract"]) for leg in current.legs} != intended:
            self.reject(event, "recheck_failed", why=why if current is None else "legs_changed")
            return
        block = self.validate_basket(current, pair)
        if block:
            self.reject(event, block)
            return
        trade_id = uuid.uuid4().hex[:12]
        if isinstance(pair, MarketPair):
            where = {"kalshi_market": pair.key, "polymarket_event": pair.listing.polymarket,
                     "match": {"method": pair.listing.source, "name": pair.listing.name,
                               "invert": pair.listing.invert, "score": pair.listing.score},
                     "rules": pair.listing.rules}
        else:
            where = {"polymarket_event": pair.poly.slug, "match": pair.evidence,
                     "rules": self.leagues[league].get("rules")}
        context = {"trade_id": trade_id, "experiment_id": self.experiment.id, "strategy": STRATEGY, "league": league, "kalshi_event": event,
                   **where, "side": "long", "sets": current.sets,
                   "intended_legs": current.legs, "expected_net": str(current.net), "edge": str(current.edge),
                   "apr": current.apr, "days": current.days}
        # The thinnest book first: it is the leg most likely to come up short.
        legs = sorted(current.legs, key=lambda leg: decimal(leg["depth"]))
        target, outcomes, sent = Decimal(current.sets), [], []
        failure: BaseException | None = None
        attempted: tuple[dict[str, Any], str] | None = None
        try:
            for leg in legs:
                if target < 1:
                    break
                identifier = str(uuid.uuid4())
                attempted = (leg, identifier)
                result = self.brokers[venue_of(leg["ticker"])].submit({"client_order_id": identifier, "ticker": leg["ticker"], **buy(leg), "count": str(int(target)),
                                             "time_in_force": "immediate_or_cancel",
                                             "self_trade_prevention_type": "taker_at_cross"}, context)
                got = decimal(result.get("filled") or "0")
                outcomes.append({"ticker": leg["ticker"], "venue": leg["venue"], "asked": str(int(target)),
                                 "status": result["status"], "filled": str(got)})
                if result.get("status") not in TERMINAL or "filled" not in result:
                    # Never send the next leg (or an unwind) on a guess about this one.
                    self.halt(f"order on {leg['ticker']} has an unknown outcome")
                    sent.append((leg, got))
                    break
                sent.append((leg, got))
                target = min(target, got)
        except Exception as exc:
            failure = exc
            outcomes.append({"status": "error", "error": f"{type(exc).__name__}: {exc}"[:200]})
            if attempted is not None:
                failed_leg, identifier = attempted
                rows = [o for o in self.brokers[venue_of(failed_leg["ticker"])].orders()
                        if o["client_order_id"] == identifier]
                if rows and rows[0]["status"] in ("pending", "unknown", "acknowledged"):
                    self.halt(f"order {identifier} may have executed; reconcile before repairing this experiment")
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
                    self.halt("incomplete basket repair; reconcile holdings and orders before clearing the halt")
                    break
            except Exception as exc:
                outcomes.append({"status": "repair_error", "ticker": leg["ticker"], "error": str(exc)[:200]})
                self.halt("basket repair failed; reconcile holdings and orders before clearing the halt")
                failure = failure or exc
                break
        self.kalshi_broker.observe("basket_execution", {"context": context, "outcomes": outcomes})
        self.resume()
        if failure is not None:
            raise failure
        if any(decimal(o.get("filled") or "0") > 0 for o in outcomes):
            self.counters["baskets"] += 1
        log.info("BASKET %s %s sets, expected net %s; actual %s", event, current.sets, current.net, outcomes)

    def unwind(self, leg: dict[str, Any], count: Decimal, context: dict[str, Any]) -> dict[str, Any]:
        """Sell back contracts beyond the complete sets, no lower than cost less the slippage."""
        floor = max(TICK, decimal(leg["vwap"]) - Decimal(str(self.cfg.unwind_slippage)))
        no = leg["contract"] == "NO"  # orders are on the YES book: selling NO is buying YES at 1 - price
        result = self.brokers[venue_of(leg["ticker"])].submit({"ticker": leg["ticker"], "side": "bid" if no else "ask",
                                     "count": str(int(count)), "price": str(1 - floor if no else floor),
                                     "time_in_force": "immediate_or_cancel",
                                     "self_trade_prevention_type": "taker_at_cross", "reduce_only": True},
                                    {**context, "repair": "unwind"})
        self.counters["unwinds"] += 1
        sold = decimal(result.get("filled") or "0")
        return {"ticker": leg["ticker"], "venue": leg["venue"], "unwind": str(count), "sold": str(sold),
                "status": result["status"], "floor": str(floor)}

    # -- run -------------------------------------------------------------------

    def step(self) -> None:
        if os.path.exists(os.path.join(self.diagnostics, "KILL")):
            self.halt("kill switch file present")
        if time.monotonic() - self._discovered >= self.cfg.discover_minutes * 60:
            self.discover()
        self.resume()
        candidates = self.scan()
        if self.halted:
            return
        pause = self.live_block()
        if pause:
            if not self._throttled(("live_block", pause), 60):
                self.kalshi_broker.observe("live_paused", {"reason": pause})
                log.warning("not trading: %s", pause)
            return
        for basket, pair, league in sorted(candidates, key=lambda c: c[0].apr, reverse=True):
            if self.halted:
                return
            block = self.risk_block(event_of(pair)) or (
                f"venue_cash_{self.venue_shortfall(basket)}" if self.venue_shortfall(basket) else None)
            if block:
                self.reject(event_of(pair), block, apr=basket.apr)
                continue
            self.execute(basket, pair, league)

    def run(self, once: bool = False) -> None:
        deadline = time.monotonic() + self.cfg.duration_minutes * 60 if self.cfg.duration_minutes else None
        try:
            while True:
                started = time.monotonic()
                self.step()
                self.kalshi_broker.observe("heartbeat", {"cash": str(self.cash), "exposure": str(self.exposure()),
                                                  "open_trades": len(self.positions), "halted": self.halted,
                                                  "counters": dict(self.counters)})
                if once or (deadline and time.monotonic() >= deadline):
                    break
                wait = max(1.0, self.cfg.interval_seconds - (time.monotonic() - started))
                time.sleep(min(wait, deadline - time.monotonic()) if deadline else wait)
        finally:
            self.kalshi_broker.observe("run_stop", {"cash": str(self.cash), "counters": dict(self.counters)})


def event_of(pair: Pair | MarketPair) -> str:
    return pair.event if isinstance(pair, MarketPair) else pair.game.event


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
    forbidden = {"experiment", "venue", "live", "leagues", "markets"} & parameters.keys()
    if forbidden:
        raise ValueError(f"pair, venue and mode come from the experiment, not parameters: {sorted(forbidden)}")
    experiment = Experiment(pair=SelectedPair(**selected), **data)
    experiment.validate()
    cfg = Config(experiment=experiment, venue=experiment.venue, live=experiment.mode == "live",
                 **parameters)
    if cfg.dry_run and not cfg.live:
        raise ValueError("dry_run requires live mode")
    cfg.validate()
    return cfg


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
    if cfg.live:
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
