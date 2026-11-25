"""Independent cross-market sum check using one broker per venue.

Paper by default; --live uses Kalshi and Polymarket US. The strategy owns matching,
sizing, sequential IOC execution, partial-fill repairs, and monitor-health gates.
Brokers retain execution evidence in the shared portfolio log. See README.md for
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
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from czquant import Broker, Store
from czquant.accounting import active_events, venue_of
from czquant.kalshi import Kalshi
from czquant.live import KalshiLive
from czquant.paper import Paper
from czquant.polymarket import Polymarket, PolymarketPaper, kalshi_layout, normalize_market, ticker
from czquant.polymarket_us import DEFAULT_THETA, PolymarketUS, PolymarketUSLive, PolymarketUSPaper
from czquant.polymarket_us import ticker as us_ticker
from czquant.store import decimal

from markets import Listing, MarketPair, load_markets, resolve
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
log = logging.getLogger("cross_market_polymarket")


@dataclass
class Config:
    leagues: tuple[str, ...] = ()  # empty: every league enabled in leagues.json
    interval_seconds: float = 15.0
    discover_minutes: float = 10.0
    max_event_frac: float = 0.10
    max_total_frac: float = 1.00
    max_open_events: int = 20
    max_sets: int = 500
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
    markets: bool = True  # also scan the reviewed non-sports pairs in markets.json
    venue: str = "polymarket"  # polymarket (international, paper) or polymarket_us
    live: bool = False  # real orders: needs a live portfolio mapped with map-live-cross
    dry_run: bool = False  # live, but build and sign orders without sending them
    pmus_theta: float | None = None  # Polymarket US taker coefficient; None: each market's own
    max_basket_dollars: float | None = None  # hard cap on one basket's capital
    max_total_dollars: float | None = None  # hard cap on capital at risk across open trades
    monitor_max_age: float = 300.0  # live: seconds a monitor's last health check stays valid


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
                 revision: str | None = None, listings: list[Listing] | None = None,
                 us: PolymarketUS | None = None, kalshi_live: Any = None):
        self.cfg = cfg
        self.theta_override = None if cfg.pmus_theta is None else Decimal(str(cfg.pmus_theta))
        portfolio = os.environ.get("CZQUANT_PORTFOLIO")
        if not portfolio:
            raise ValueError("Set CZQUANT_PORTFOLIO to an explicitly created paper portfolio")
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        self.store = Store()
        self.diagnostics = os.path.join(str(self.store.root), "diagnostics", portfolio)
        self.kalshi = kalshi or Kalshi(authenticated=False)
        self.us = cfg.venue == "polymarket_us"
        if cfg.live and not self.us:
            raise ValueError("live trading is on Polymarket US only (--venue polymarket-us)")
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
        self.kalshi_broker = Broker(self.store, portfolio, kalshi_adapter, self.run_id)
        self.poly_broker = Broker(self.store, portfolio, poly_adapter, self.run_id)
        self.brokers = {broker.venue: broker for broker in (self.kalshi_broker, self.poly_broker)}
        if cfg.live:
            if self.kalshi_broker.mode != "live" or not {"kalshi", "polymarket_us"}.issubset(self.kalshi_broker.metadata.get("venues", [])):
                raise ValueError(f"{portfolio} is not a live portfolio mapped to Kalshi and Polymarket US "
                                 "(uv run czquant map-live-cross)")
            if not self.poly.authenticated:
                raise ValueError("live trading needs POLYMARKET_US_KEY_ID and POLYMARKET_US_SECRET_KEY_PATH")
        elif self.kalshi_broker.mode != "paper":
            raise ValueError(f"{portfolio} is a live portfolio: pass --live (and --dry-run first)")
        self.leagues = leagues if leagues is not None else load_leagues(cfg.leagues)
        self.pairs = pairs if pairs is not None else load_pairs(PAIRS_US if self.us else PAIRS)
        # Non-sports pairs name international Polymarket markets: scanned on that venue only.
        markets_on = cfg.markets and not self.us
        self.reload_markets = listings is None and markets_on  # re-read markets.json at each discovery
        self.listings = listings if listings is not None else (load_markets() if markets_on else [])
        revision = revision or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()
        self.kalshi_broker.start(STRATEGY, revision, {**asdict(self.cfg), "leagues": self.leagues,
                                               "pairs": {"pinned": sorted(self.pairs["pairs"]),
                                                         "blocked": sorted(self.pairs["blocked"])},
                                               "markets": [asdict(x) for x in self.listings]})
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

    # -- state ---------------------------------------------------------------

    def resume(self) -> None:
        """Recover actual holdings and the halt state from the shared store."""
        state = self.kalshi_broker.state()
        self.cash = state.cash - self.store.reserved(self.kalshi_broker.portfolio)
        self.bankroll = state.contributed
        self.positions = {}
        for lot in state.lots.values():
            context = lot["context"]
            if context.get("trade_id"):
                position = self.positions.setdefault(context["trade_id"], {**context, "capital": Decimal(0)})
                position["capital"] += lot["cost"]
        events = active_events(self.store.events(self.kalshi_broker.portfolio, kinds=("transaction", "observation", "correction"),
                                                 observations=("halt", "halt_cleared", "basket_settled")))
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
                      - self.store.reserved(self.kalshi_broker.portfolio))
        if self.cfg.max_basket_dollars is not None:
            cap = min(cap, Decimal(str(self.cfg.max_basket_dollars)))
        return cap

    def venue_shortfall(self, basket: Basket) -> str | None:
        """Live: the first venue whose free cash can't pay its legs (cost plus fee)."""
        if not self.cfg.live:
            return None
        need: dict[str, Decimal] = {}
        for leg in basket.legs:
            venue = venue_of(leg["ticker"])
            need[venue] = need.get(venue, Decimal(0)) + decimal(leg["vwap"]) * leg["qty"] + decimal(leg["fee"])
        return next((v for v, amount in need.items() if self.brokers[v].available < amount), None)

    def live_block(self) -> str | None:
        """Live: a reason not to trade now. Unresolved orders and monitor discrepancies
        halt; a missing or stale monitor check only pauses."""
        if not self.cfg.live:
            return None
        unresolved = self.store.db.execute(
            "SELECT COUNT(*) FROM orders WHERE portfolio_id=? AND status IN ('unknown','pending')",
            (self.kalshi_broker.portfolio,)).fetchone()[0]
        if unresolved:
            self.halt(f"{unresolved} order(s) with unknown outcome; check both venues, then clear the halt")
            return "halted"
        if self.cfg.dry_run:
            return None
        now = datetime.now(timezone.utc)
        for component in ("account", "polymarket_us"):
            rows = [e for e in self.store.events(kinds=("health",)) if e["data"].get("component") == component]
            if not rows:
                return f"no_{component}_monitor"
            last = rows[-1]
            age = (now - datetime.fromisoformat(last["effective_at"])).total_seconds()
            if age > self.cfg.monitor_max_age or last["data"].get("status") == "failed":
                return f"stale_{component}_monitor"
            mine = [d for d in last["data"].get("discrepancies") or [] if d.get("portfolio") == self.kalshi_broker.portfolio]
            if mine:
                self.halt(f"{component} monitor reports {mine[0].get('kind')}: reconcile before trading")
                return "halted"
        return None

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
        if self.reload_markets:
            self.reread_markets()
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

    def reread_markets(self) -> None:
        """Pick up pairs added to, changed in or disabled in markets.json since the last
        discovery. A file that doesn't parse (say, mid-save) keeps the previous list.
        Positions in a removed pair stay held; the monitors still settle them."""
        try:
            listings = load_markets()
        except (ValueError, KeyError) as exc:
            self.kalshi_broker.observe("error", {"where": "markets.json", "error": str(exc)[:300]})
            log.error("markets.json unreadable, keeping %d listings: %s", len(self.listings), exc)
            return
        if listings != self.listings:
            before, after = {x.kalshi for x in self.listings}, {x.kalshi for x in listings}
            self.kalshi_broker.observe("markets_reloaded", {"added": sorted(after - before), "removed": sorted(before - after),
                                                     "listed": len(listings)})
            log.info("markets.json: %d listed (+%d, -%d)", len(listings), len(after - before), len(before - after))
            self.listings = listings

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
            if game.event in self.pairs["blocked"]:
                counts["blocked"] = counts.get("blocked", 0) + 1
                continue
            pair, why = match(game, self.polys.get(league, []), aliases, self.pairs["pairs"].get(game.event))
            counts[why] = counts.get(why, 0) + 1
            if pair:
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
        event = event_of(pair)
        self.cooldown_until[event] = time.time() + self.cfg.cooldown
        intended = {(leg["ticker"], leg["contract"]) for leg in basket.legs}
        current, why = self.recheck(pair, league)
        if current is None or {(leg["ticker"], leg["contract"]) for leg in current.legs} != intended:
            self.reject(event, "recheck_failed", why=why if current is None else "legs_changed")
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
        context = {"trade_id": trade_id, "strategy": STRATEGY, "league": league, "kalshi_event": event,
                   **where, "side": "long", "sets": current.sets,
                   "intended_legs": current.legs, "expected_net": str(current.net), "edge": str(current.edge),
                   "apr": current.apr, "days": current.days}
        # The thinnest book first: it is the leg most likely to come up short.
        legs = sorted(current.legs, key=lambda leg: decimal(leg["depth"]))
        target, outcomes, sent = Decimal(current.sets), [], []
        failure: BaseException | None = None
        try:
            for leg in legs:
                if target < 1:
                    break
                result = self.brokers[venue_of(leg["ticker"])].submit({"ticker": leg["ticker"], **buy(leg), "count": str(int(target)),
                                             "time_in_force": "immediate_or_cancel",
                                             "self_trade_prevention_type": "taker_at_cross"}, context)
                got = decimal(result.get("filled") or "0")
                outcomes.append({"ticker": leg["ticker"], "venue": leg["venue"], "asked": str(int(target)),
                                 "status": result["status"], "filled": str(got)})
                if result["status"] == "unknown":
                    # Never send the next leg (or an unwind) on a guess about this one.
                    self.halt(f"order on {leg['ticker']} has an unknown outcome")
                    sent.append((leg, got))
                    break
                sent.append((leg, got))
                target = min(target, got)
            keep = target if len(sent) == len(legs) else Decimal(0)
            for leg, got in ([] if self.halted else sent):
                if got > keep:
                    outcomes.append(self.unwind(leg, got - keep, context))
        except Exception as exc:
            failure = exc
            outcomes.append({"status": "error", "error": f"{type(exc).__name__}: {exc}"[:200]})
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


def _duration(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def main(argv: list[str] | None = None) -> None:
    d = Config()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--leagues", default="", help="comma-separated leagues.json keys (default: every enabled league)")
    p.add_argument("--interval", type=float, default=d.interval_seconds, help="seconds between scans (15)")
    p.add_argument("--discover", type=float, default=d.discover_minutes, help="minutes between event refreshes (10)")
    p.add_argument("--max-event-frac", type=float, default=d.max_event_frac, help="cap per game, share of bankroll (0.10)")
    p.add_argument("--max-total-frac", type=float, default=d.max_total_frac)
    p.add_argument("--max-open-events", type=int, default=d.max_open_events)
    p.add_argument("--max-sets", type=int, default=d.max_sets, help="contract sets ($1 payouts) per basket (500)")
    p.add_argument("--risk-free", type=float, default=d.risk_free, help="hurdle APR, compounded to payout (0.05)")
    p.add_argument("--min-net", type=float, default=d.min_net, help="minimum profit after fees per basket, $ (0.25)")
    p.add_argument("--min-edge", type=float, default=d.min_edge, help="minimum profit per $1 set (0.01)")
    p.add_argument("--max-apr", type=lambda v: None if v == "none" else float(v), default=d.max_apr)
    p.add_argument("--any-venue", action="store_true",
                   help="also trade baskets with every leg on one venue (default: a leg on each)")
    p.add_argument("--resolution-hours", type=float, default=d.resolution_hours,
                   help="hours from the start to payout, for the hurdle (6)")
    p.add_argument("--unwind-slippage", type=float, default=d.unwind_slippage,
                   help="worst price per contract below cost when selling back a partial basket (0.05)")
    p.add_argument("--kalshi-latency", type=float, default=d.kalshi_latency)
    p.add_argument("--polymarket-latency", type=float, default=d.polymarket_latency)
    p.add_argument("--cooldown", type=float, default=d.cooldown, help="seconds before trading a game again (300)")
    p.add_argument("--duration", type=_duration, default=None, help="run for H:MM, then stop")
    p.add_argument("--no-markets", action="store_true", help="skip the reviewed non-sports pairs in markets.json")
    p.add_argument("--venue", choices=["polymarket-us", "polymarket"], default="polymarket-us",
                   help="Polymarket US (default) or the international exchange (paper only)")
    p.add_argument("--pmus-theta", type=float, default=None,
                   help="Polymarket US taker coefficient for pricing and paper fees (default: each market's feeCoefficient, 0.095 at tier 1)")
    p.add_argument("--live", action="store_true",
                   help="send real orders to Kalshi and Polymarket US (a live portfolio from map-live-cross)")
    p.add_argument("--dry-run", action="store_true", help="with --live: build and sign every order, send none")
    p.add_argument("--max-basket-dollars", type=float, default=None, help="cap on one basket's capital, $ (live default 25)")
    p.add_argument("--max-total-dollars", type=float, default=None, help="cap on capital at risk, $ (live default 100)")
    p.add_argument("--once", action="store_true", help="one scan, then stop")
    a = p.parse_args(argv)
    if a.dry_run and not a.live:
        p.error("--dry-run goes with --live (paper runs never send orders anyway)")
    if a.live:
        # Small by default: the first live runs measure fills and fees, not profit.
        a.max_basket_dollars = 25.0 if a.max_basket_dollars is None else a.max_basket_dollars
        a.max_total_dollars = 100.0 if a.max_total_dollars is None else a.max_total_dollars
        if "--max-sets" not in (argv if argv is not None else __import__("sys").argv):
            a.max_sets = 20
    if not os.environ.get("STRATEGY_RUNS_DIR"):
        # Without it the store falls back to ./runs, which the monitors and dashboard never read.
        p.error("STRATEGY_RUNS_DIR is not set; load .env first (set -a; source .env; set +a)")
    cfg = Config(leagues=tuple(x for x in a.leagues.split(",") if x), interval_seconds=a.interval,
                 discover_minutes=a.discover, max_event_frac=a.max_event_frac, max_total_frac=a.max_total_frac,
                 max_open_events=a.max_open_events, max_sets=a.max_sets, risk_free=a.risk_free, min_net=a.min_net,
                 min_edge=a.min_edge, max_apr=a.max_apr, require_cross=not a.any_venue,
                 resolution_hours=a.resolution_hours, unwind_slippage=a.unwind_slippage,
                 kalshi_latency=a.kalshi_latency, polymarket_latency=a.polymarket_latency, cooldown=a.cooldown,
                 duration_minutes=a.duration, markets=not a.no_markets,
                 venue=a.venue.replace("-", "_"), live=a.live, dry_run=a.dry_run, pmus_theta=a.pmus_theta,
                 max_basket_dollars=a.max_basket_dollars, max_total_dollars=a.max_total_dollars)
    engine = Engine(cfg)
    run_dir = os.path.join(engine.diagnostics, engine.run_id)
    os.makedirs(run_dir, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(os.path.join(run_dir, "run.log"))])
    if cfg.live:
        log.warning("LIVE%s: portfolio %s, caps $%s per basket, $%s total, %d sets; Kalshi $%s and Polymarket US $%s free",
                    " DRY RUN (nothing sent)" if cfg.dry_run else "", engine.kalshi_broker.portfolio, cfg.max_basket_dollars,
                    cfg.max_total_dollars, cfg.max_sets, engine.kalshi_broker.available,
                    engine.poly_broker.available)
    try:
        engine.run(once=a.once)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
