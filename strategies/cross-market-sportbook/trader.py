"""Paper trader for the cross-market sum check: Kalshi game markets against DraftKings
moneylines. Paper only: nothing here can place a real order or a real bet.

Each scan
  discover  every --discover minutes, the open Kalshi game events of each enabled
            league in leagues.json, and each series' fee schedule
  odds      DraftKings moneylines, one paid read per league. Reads are paced so the
            source's credits (above a reserve kept for settlement) last until the
            end of the trial, and only leagues with a game starting within
            --horizon hours are read: one league per read, the longest unread first
  pair      each Kalshi game with exactly one DraftKings game (matching.py, pairs.json)
  price     Kalshi books for the paired games; the best basket with legs on both
            venues that beats the hurdle (sum_check.py)
  execute   highest APR first, within limits: re-check on fresh Kalshi books, back
            the DraftKings legs (fill or kill), then buy the Kalshi legs (IOC) for the
            sets DraftKings accepted. A declined bet costs nothing; the visible Kalshi
            book is the leg more likely to fill, so it goes second.
Settlement belongs to the monitors: `czquant monitor market` settles Kalshi legs and
`czquant monitor draftkings` settles bets. A fully filled basket that settles at a
loss halts new trades, also after a restart; partial baskets are recorded only.

Usage
  uv run python trader.py --duration 1:00
  uv run python trader.py --once --leagues nba
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from czquant import Broker, Store
from czquant.accounting import active_events
from czquant.draftkings import Blocked, DraftKings, OutOfCredits
from czquant.kalshi import Kalshi
from czquant.paper import Paper
from czquant.sportsbook import SportsbookPaper
from czquant.store import decimal

from matching import KalshiGame, Pair, kalshi_game, load_pairs, match
from sum_check import Basket, best, kalshi_offer, sportsbook_offer

STRATEGY = "cross-market-sportbook"
HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
LEAGUES = os.path.join(HERE, "leagues.json")
FEE_TYPES = ("quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees")
log = logging.getLogger("cross_market")


@dataclass
class Config:
    leagues: tuple[str, ...] = ()  # empty: every league enabled in leagues.json
    interval_seconds: float = 60.0  # loop tick, and the shortest gap between odds reads
    trial_days: float | None = None  # odds budget horizon; None: keep the saved trial end (default 7 days)
    odds_reserve: int = 40  # credits never spent on odds: they pay for results (2 each)
    horizon_hours: float = 12.0  # read a league only when one of its games starts this soon
    discover_minutes: float = 10.0
    max_event_frac: float = 0.10
    max_total_frac: float = 1.00
    max_open_events: int = 20
    max_sets: int = 500
    risk_free: float = 0.05
    min_net: float = 0.25
    min_edge: float = 0.01  # per $1 set: margin for rule and settlement risk between venues
    max_apr: float | None = None
    max_stake: float = 500.0  # per DraftKings selection
    max_odds_age: float = 1800.0  # since DraftKings last updated the price, per the source
    min_lead_minutes: float = 10.0  # pre-game only: skip games starting sooner
    kalshi_latency: float = 0.30
    book_latency: float = 1.00
    cooldown: float = 300.0
    duration_minutes: float | None = None


def load_leagues(names: tuple[str, ...] = (), path: str = LEAGUES) -> dict[str, dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        specs = json.load(f)
    unknown = set(names) - set(specs)
    if unknown:
        raise ValueError(f"unknown leagues: {sorted(unknown)}")
    return {k: v for k, v in specs.items() if (k in names if names else v.get("enabled"))}


class Engine:
    def __init__(self, cfg: Config, kalshi: Any = None, book: DraftKings | None = None,
                 leagues: dict[str, dict[str, Any]] | None = None, pairs: dict[str, Any] | None = None,
                 revision: str | None = None):
        self.cfg = cfg
        portfolio = os.environ.get("CZQUANT_PORTFOLIO")
        if not portfolio:
            raise ValueError("Set CZQUANT_PORTFOLIO to an explicitly created paper portfolio")
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        self.store = Store()
        self.diagnostics = os.path.join(str(self.store.root), "diagnostics", portfolio)
        self.kalshi = kalshi or Kalshi(authenticated=False)
        self.book = book or DraftKings.from_env()
        self.book.max_stake = decimal(cfg.max_stake)
        self.book.reserve = cfg.odds_reserve
        self.pacer = OddsPacer(self.book, cfg)
        self.broker = Broker(self.store, portfolio, Paper(self.kalshi, latency=cfg.kalshi_latency), self.run_id)
        self.book_broker = Broker(self.store, portfolio,
                                  SportsbookPaper(self.book, latency=cfg.book_latency, max_stake=str(cfg.max_stake),
                                                  max_age=cfg.max_odds_age), self.run_id)
        if self.broker.mode != "paper":
            raise ValueError("This strategy entry point is paper only")
        self.leagues = leagues if leagues is not None else load_leagues(cfg.leagues)
        self.pairs = pairs if pairs is not None else load_pairs()
        source = self.book.source.name
        missing = [k for k, v in self.leagues.items() if not v.get(source)]
        if missing:
            raise ValueError(f"leagues without a {source} id: {missing}")
        revision = revision or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()
        self.broker.start(STRATEGY, revision, {**asdict(self.cfg), "source": source, "leagues": self.leagues,
                                               "pairs": {"pinned": sorted(self.pairs["pairs"]),
                                                         "blocked": sorted(self.pairs["blocked"])}})
        self.games: dict[str, list[KalshiGame]] = {}
        self.fees: dict[str, Decimal | None] = {}
        self._discovered = -1e18
        self.cooldown_until: dict[str, float] = {}
        self.halted: str | None = None
        self.positions: dict[str, dict[str, Any]] = {}
        self.counters = {"scans": 0, "baskets": 0, "rejects": 0}
        self._last_logged: dict[tuple, float] = {}
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="leg")
        self._local = threading.local()
        self.resume()

    # -- state ---------------------------------------------------------------

    def resume(self) -> None:
        """Recover actual holdings and the halt state from the shared store."""
        state = self.broker.state()
        self.cash, self.bankroll = self.broker.available, state.contributed
        self.positions = {}
        for lot in state.lots.values():
            context = lot["context"]
            if context.get("trade_id"):
                position = self.positions.setdefault(context["trade_id"], {**context, "capital": Decimal(0)})
                position["capital"] += lot["cost"]
        events = active_events(self.store.events(self.broker.portfolio, kinds=("transaction", "observation", "correction"),
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
                if d["reason"] == "fill" and decimal(leg["quantity_delta"]) > 0:
                    key = (leg["market"], leg["side"])
                    filled.setdefault(tid, {})[key] = filled.get(tid, {}).get(key, Decimal(0)) + decimal(leg["quantity_delta"])
        for tid, pnl in totals.items():
            if tid in self.positions:
                continue
            intended = {(leg["ticker"], leg["contract"].lower()): decimal(leg["qty"])
                        for leg in contexts[tid].get("intended_legs", [])}
            status = ("complete" if filled.get(tid) == intended else "partial") if intended else "unknown"
            if tid not in reported:
                self.broker.observe("basket_settled", {"trade_id": tid, "net_pnl": str(pnl), "fill_status": status,
                    "context": contexts[tid], "filled_legs": [{"ticker": t, "side": s, "quantity": str(q)}
                                                              for (t, s), q in filled.get(tid, {}).items()]})
            # Only a complete basket's loss contradicts the arbitrage; a partial one is directional.
            if pnl < Decimal("-0.005") and status != "partial":
                self.halt(f"settled trade {tid} lost ${-pnl:.2f}")

    def halt(self, reason: str) -> None:
        if not self.halted:
            self.halted = reason
            self.broker.observe("halt", {"reason": reason})
            log.error("HALT: %s", reason)

    def exposure(self, event: str | None = None) -> Decimal:
        return sum((p["capital"] for p in self.positions.values() if event is None or p.get("kalshi_event") == event),
                   Decimal(0))

    def limits(self, event: str) -> Decimal:
        bankroll = self.bankroll
        return min(self.cash, bankroll * Decimal(str(self.cfg.max_total_frac)) - self.exposure(),
                   bankroll * Decimal(str(self.cfg.max_event_frac)) - self.exposure(event))

    def staked(self, ticker: str) -> Decimal:
        return sum((lot["cost"] for lot in self.broker.state().lots.values() if lot["market"] == ticker), Decimal(0))

    def _throttled(self, key: tuple, every: float = 300.0) -> bool:
        now = time.monotonic()
        if now - self._last_logged.get(key, -1e18) < every:
            return True
        self._last_logged[key] = now
        return False

    def reject(self, event: str, reason: str, **extra: Any) -> None:
        if not self._throttled(("reject", event, reason)):
            self.counters["rejects"] += 1
            self.broker.observe("reject", {"event": event, "reason": reason, **extra})

    # -- data ----------------------------------------------------------------

    def discover(self) -> None:
        for name, spec in self.leagues.items():
            try:
                series = spec["kalshi_series"]
                schedule = self.kalshi.get(f"/series/{series}").get("series", {})
                fee_type = schedule.get("fee_type", "quadratic")
                self.fees[name] = decimal(schedule.get("fee_multiplier", "1")) if fee_type in FEE_TYPES else None
                events = list(self.kalshi.pages("/events", "events", series_ticker=series, status="open",
                                                with_nested_markets="true", limit=200))
                self.games[name] = [g for g in (kalshi_game(e) for e in events) if g is not None]
            except Exception as exc:
                log.exception("discovery failed for %s", name)
                self.broker.observe("error", {"where": "discover", "league": name, "error": str(exc)[:300]})
        self._discovered = time.monotonic()
        self.broker.observe("universe", {"games": {k: len(v) for k, v in self.games.items()},
                                         "fee_multipliers": {k: None if v is None else str(v) for k, v in self.fees.items()}})

    def kalshi_books(self, tickers: list[str]) -> dict[str, dict[str, Any]]:
        out = {}
        for i in range(0, len(tickers), 100):
            data = self.kalshi.get("/markets/orderbooks", [("tickers", t) for t in tickers[i:i + 100]])
            for row in data.get("orderbooks") or []:
                out[row["ticker"]] = row
        return out

    def league_id(self, league: str) -> str:
        return str(self.leagues[league][self.book.source.name])

    # -- evaluation ------------------------------------------------------------

    def evaluate(self, league: str, pair: Pair, books: dict[str, Any],
                 max_capital: Decimal | None = None) -> tuple[Basket | None, str]:
        now = time.time()
        selections = pair.selections.values()
        start = min(datetime.fromisoformat(s.start).timestamp() for s in selections)
        if start - now < self.cfg.min_lead_minutes * 60:
            return None, "too_close_to_start"
        if any(s.suspended for s in selections):
            return None, "suspended"
        if any(now - datetime.fromisoformat(s.updated_at).timestamp() > self.cfg.max_odds_age for s in selections):
            return None, "stale_odds"
        multiplier = self.fees.get(league)
        if multiplier is None:
            return None, "unsupported_kalshi_fees"
        offers: dict[str, list] = {role: [] for role in pair.selections}
        for ticker, role in pair.roles.items():
            if ticker not in books:
                return None, "no_kalshi_book"
            offers[role].append(kalshi_offer(role, ticker, books[ticker], multiplier))
        for role, selection in pair.selections.items():
            room = Decimal(str(self.cfg.max_stake)) - self.staked(selection.ticker)
            if room > 0:
                offers[role].append(sportsbook_offer(selection, room))
        settle = [m["market"].get("expected_expiration_time") for m in pair.game.teams]
        end = max((datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() for t in settle if t), default=0)
        days = (max(end, start + 4 * 3600) - now) / 86400
        cap = max_capital if max_capital is not None else self.limits(pair.game.event)
        return best(offers, days, self.cfg.risk_free, self.cfg.max_sets, max(cap, Decimal(0)),
                    Decimal(str(self.cfg.min_net)), Decimal(str(self.cfg.min_edge)), self.cfg.max_apr)

    def upcoming(self, league: str) -> bool:
        """Does the league have a game starting between min_lead and horizon from now?
        Kalshi gives the expected end; a game is taken to start three hours before it."""
        now = time.time()
        for game in self.games.get(league, []):
            ends = [m["market"].get("expected_expiration_time") for m in game.teams]
            end = max((datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() for t in ends if t), default=None)
            if end is None:
                continue
            start = end - 3 * 3600
            if now + self.cfg.min_lead_minutes * 60 <= start <= now + self.cfg.horizon_hours * 3600:
                return True
        return False

    def scan(self) -> list[tuple[Basket, Pair, str]]:
        counts: dict[str, int] = {}
        candidates = []
        eligible = [league for league in self.leagues if self.upcoming(league)]
        reading = self.pacer.pick(eligible, list(self.leagues), self.league_id)
        if not reading:
            return []
        self.counters["scans"] += 1
        for league in reading:
            try:
                selections = self.book.selections(self.league_id(league), fresh=True)
            except OutOfCredits as exc:
                if not self._throttled(("credits",), 3600):
                    self.broker.observe("error", {"where": "odds", "league": league, "out_of_credits": True,
                                                  "error": str(exc)})
                counts["odds_reserve_reached"] = counts.get("odds_reserve_reached", 0) + 1
                continue
            except Blocked as exc:
                if not self._throttled(("blocked", league), 3600):
                    self.broker.observe("error", {"where": "odds", "league": league, "blocked": True, "error": str(exc)})
                counts["odds_blocked"] = counts.get("odds_blocked", 0) + 1
                continue
            except Exception as exc:
                log.exception("odds read failed for %s", league)
                self.broker.observe("error", {"where": "odds", "league": league, "error": str(exc)[:300]})
                continue
            paired = []
            for game in self.games.get(league, []):
                if game.event in self.pairs["blocked"]:
                    counts["blocked"] = counts.get("blocked", 0) + 1
                    continue
                pair, why = match(game, selections, bool(self.leagues[league].get("draw")),
                                  self.pairs["pairs"].get(game.event))
                counts[why] = counts.get(why, 0) + 1
                if pair:
                    paired.append(pair)
            if not paired:
                continue
            books = self.kalshi_books([t for p in paired for t in p.roles])
            for pair in paired:
                basket, why = self.evaluate(league, pair, books, self.bankroll * Decimal(str(self.cfg.max_event_frac)))
                counts["priced_" + why] = counts.get("priced_" + why, 0) + 1
                if basket:
                    candidates.append((basket, pair, league))
                    if not self._throttled(("opportunity", pair.game.event), 60):
                        self.broker.observe("opportunity", {"league": league, "kalshi_event": pair.game.event,
                                                            "book_event": pair.book_event, **basket.summary()})
        top = max((b for b, _, _ in candidates), key=lambda b: b.apr, default=None)
        self.broker.observe("scan_complete", {"counts": counts, "candidates": len(candidates), "read": reading,
                                              "eligible": eligible, "next_read_in_s": round(self.pacer.spacing()),
                                              "best_apr": top.apr if top else None,
                                              "best_edge": str(top.edge) if top else None,
                                              "odds_credits_remaining": self.book.credits()})
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
        if not self.book.spendable():
            return "odds_reserve_reached"  # the bet's own odds read would spend the settlement reserve
        return None

    def execute(self, basket: Basket, pair: Pair, league: str) -> None:
        event = pair.game.event
        self.cooldown_until[event] = time.time() + self.cfg.cooldown
        intended = {leg["ticker"] for leg in basket.legs}
        # Re-check on fresh Kalshi books, within the current limits. The odds are the read
        # this scan just paid for; the bet itself re-reads them once (the adapter's fill check).
        fresh = {s.ticker: s for s in self.book.selections(self.league_id(league), fetch=False)}
        if any(s.ticker not in fresh for s in pair.selections.values()):
            self.reject(event, "recheck_failed", why="selection_withdrawn")
            return
        pair = Pair(pair.game, pair.book_event, pair.roles,
                    {role: fresh[s.ticker] for role, s in pair.selections.items()}, pair.evidence)
        current, why = self.evaluate(league, pair, self.kalshi_books(list(pair.roles)))
        if current is None or {leg["ticker"] for leg in current.legs} != intended:
            self.reject(event, "recheck_failed", why=why if current is None else "legs_changed")
            return
        trade_id = uuid.uuid4().hex[:12]
        context = {"trade_id": trade_id, "strategy": STRATEGY, "league": league, "kalshi_event": event,
                   "book_event": pair.book_event, "book_source": self.book.source.name, "match": pair.evidence,
                   "rules": self.leagues[league].get("rules"), "side": "long", "sets": current.sets,
                   "intended_legs": current.legs, "expected_net": str(current.net), "edge": str(current.edge),
                   "apr": current.apr, "days": current.days}
        outcomes: list[dict[str, Any]] = []
        failure: BaseException | None = None
        sportsbook = [leg for leg in current.legs if leg["venue"] == "draftkings"]
        kalshi = [leg for leg in current.legs if leg["venue"] == "kalshi"]
        try:
            for leg in sportsbook:
                result = self.book_broker.submit({"ticker": leg["ticker"], "side": "bid", "count": str(current.sets),
                                             "price": leg["limit"], "time_in_force": "fill_or_kill"}, context)
                outcomes.append({"ticker": leg["ticker"], "venue": "draftkings", "status": result["status"],
                                 "filled": result.get("filled", "0"), "reason": result.get("reason")})
                if result["status"] != "filled":
                    break
            if all(o["status"] == "filled" for o in outcomes):
                orders = [{"ticker": leg["ticker"], "side": "bid", "count": str(current.sets), "price": leg["limit"],
                           "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross"}
                          for leg in kalshi]
                futures = [self._pool.submit(self._submit_leg, order, context) for order in orders]
                for order, future in zip(orders, futures):
                    try:
                        result = future.result()
                        outcomes.append({"ticker": order["ticker"], "venue": "kalshi", "status": result["status"],
                                         "filled": result.get("filled", "0")})
                    except Exception as exc:
                        failure = failure or exc
                        outcomes.append({"ticker": order["ticker"], "venue": "kalshi", "status": "error",
                                         "filled": "0", "error": f"{type(exc).__name__}: {exc}"[:200]})
        except Exception as exc:
            failure = exc
            outcomes.append({"status": "error", "error": f"{type(exc).__name__}: {exc}"[:200]})
        self.broker.observe("basket_execution", {"context": context, "outcomes": outcomes})
        self.resume()
        if failure is not None:
            raise failure
        if any(decimal(o.get("filled") or "0") > 0 for o in outcomes):
            self.counters["baskets"] += 1
        log.info("BASKET %s %s sets, expected net %s; actual %s", event, current.sets, current.net, outcomes)

    def _submit_leg(self, order: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """Submit one Kalshi leg on a pool thread through that thread's own store connection."""
        broker = getattr(self._local, "broker", None)
        if broker is None:
            broker = Broker(Store(self.store.root), self.broker.portfolio, self.broker.adapter, self.run_id)
            self._local.broker = broker
        return broker.submit(order, context)

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
        for basket, pair, league in sorted(candidates, key=lambda c: c[0].apr, reverse=True):
            block = self.risk_block(pair.game.event)
            if block:
                self.reject(pair.game.event, block, apr=basket.apr)
                continue
            self.execute(basket, pair, league)

    def run(self, once: bool = False) -> None:
        deadline = time.monotonic() + self.cfg.duration_minutes * 60 if self.cfg.duration_minutes else None
        try:
            while True:
                started = time.monotonic()
                self.step()
                self.broker.observe("heartbeat", {"cash": str(self.cash), "exposure": str(self.exposure()),
                                                  "open_trades": len(self.positions), "halted": self.halted,
                                                  "odds_credits_remaining": self.book.credits(),
                                                  "trial_end": self.pacer.end_iso(), "counters": dict(self.counters)})
                if once or (deadline and time.monotonic() >= deadline):
                    break
                wait = max(1.0, self.cfg.interval_seconds - (time.monotonic() - started))
                time.sleep(min(wait, deadline - time.monotonic()) if deadline else wait)
        finally:
            self.broker.observe("run_stop", {"cash": str(self.cash), "counters": dict(self.counters)})
            self._pool.shutdown(wait=True)


class OddsPacer:
    """Spreads paid odds reads so the credits last until the trial ends.

    The next read waits (trial end - now) / (credits above the reserve), never less
    than the loop interval. The gap is recomputed from what is left, so hours with no
    upcoming games leave more credits for later ones. Reads by any process count (the
    shared cache records them), and the trial end is saved beside the cache, so a
    restart neither resets the budget nor reads early. A source without a quota is
    read every interval.
    """

    def __init__(self, book: DraftKings, cfg: Config):
        self.book, self.floor = book, cfg.interval_seconds
        path = book.cache_dir / "trial.json" if book.cache_dir else None
        saved = None
        if path and path.exists():
            try:
                saved = float(json.loads(path.read_text())["end"])
            except (ValueError, KeyError, OSError):
                saved = None
        if cfg.trial_days is not None or saved is None or saved <= time.time():
            self.end = time.time() + 86400 * (7 if cfg.trial_days is None else cfg.trial_days)
            if path:
                path.write_text(json.dumps({"end": self.end, "end_iso": self.end_iso()}))
        else:
            self.end = saved

    def end_iso(self) -> str:
        return datetime.fromtimestamp(self.end, timezone.utc).isoformat(timespec="seconds")

    def spacing(self) -> float:
        credits = self.book.credits()
        if credits is None:
            return self.floor  # no quota, or not yet known: the first read learns it
        spendable = credits - self.book.reserve
        if spendable < 1:
            return float("inf")
        return max(self.floor, (self.end - time.time()) / spendable)

    def pick(self, eligible: list[str], every: list[str], league_id) -> list[str]:
        """The leagues to read now. Nothing until the next read is due (the newest read of
        any league is older than the spacing); then, on a metered source, the eligible
        league read longest ago, and on a source without a quota every eligible league."""
        if not eligible:
            return []
        reads = {league: self.book.cached_at(league_id(league)) or 0.0 for league in every}
        if time.time() - max(reads.values()) < self.spacing():
            return []
        if self.book.credits() is None:
            return eligible
        return [min(eligible, key=lambda league: reads[league])]


def _duration(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def main(argv: list[str] | None = None) -> None:
    d = Config()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--leagues", default="", help="comma-separated leagues.json keys (default: every enabled league)")
    p.add_argument("--interval", type=float, default=d.interval_seconds,
                   help="loop tick and shortest gap between odds reads, seconds (60)")
    p.add_argument("--trial-days", type=float, default=None,
                   help="spread the odds credits over this many days from now (default: keep the saved trial end, else 7)")
    p.add_argument("--odds-reserve", type=int, default=d.odds_reserve,
                   help="credits kept for results reads, never spent on odds (40)")
    p.add_argument("--horizon", type=float, default=d.horizon_hours,
                   help="read a league's odds only when one of its games starts within this many hours (12)")
    p.add_argument("--discover", type=float, default=d.discover_minutes, help="minutes between Kalshi refreshes (10)")
    p.add_argument("--max-event-frac", type=float, default=d.max_event_frac, help="cap per game, share of bankroll (0.10)")
    p.add_argument("--max-total-frac", type=float, default=d.max_total_frac)
    p.add_argument("--max-open-events", type=int, default=d.max_open_events)
    p.add_argument("--max-sets", type=int, default=d.max_sets, help="contract sets ($1 payouts) per basket (500)")
    p.add_argument("--risk-free", type=float, default=d.risk_free, help="hurdle APR, compounded to payout (0.05)")
    p.add_argument("--min-net", type=float, default=d.min_net, help="minimum profit after fees per basket, $ (0.25)")
    p.add_argument("--min-edge", type=float, default=d.min_edge, help="minimum profit per $1 set (0.01)")
    p.add_argument("--max-apr", type=lambda v: None if v == "none" else float(v), default=d.max_apr)
    p.add_argument("--max-stake", type=float, default=d.max_stake, help="paper stake limit per DraftKings selection, $ (500)")
    p.add_argument("--max-odds-age", type=float, default=d.max_odds_age,
                   help="seconds since DraftKings last updated a price, per the source (1800)")
    p.add_argument("--min-lead", type=float, default=d.min_lead_minutes, help="skip games starting within this many minutes (10)")
    p.add_argument("--kalshi-latency", type=float, default=d.kalshi_latency)
    p.add_argument("--book-latency", type=float, default=d.book_latency, help="seconds before a bet is accepted (1.0)")
    p.add_argument("--cooldown", type=float, default=d.cooldown, help="seconds before trading a game again (300)")
    p.add_argument("--duration", type=_duration, default=None, help="run for H:MM, then stop")
    p.add_argument("--once", action="store_true", help="one scan, then stop")
    a = p.parse_args(argv)
    cfg = Config(leagues=tuple(x for x in a.leagues.split(",") if x), interval_seconds=a.interval,
                 trial_days=a.trial_days, odds_reserve=a.odds_reserve, horizon_hours=a.horizon,
                 discover_minutes=a.discover, max_event_frac=a.max_event_frac, max_total_frac=a.max_total_frac,
                 max_open_events=a.max_open_events, max_sets=a.max_sets, risk_free=a.risk_free, min_net=a.min_net,
                 min_edge=a.min_edge, max_apr=a.max_apr, max_stake=a.max_stake, max_odds_age=a.max_odds_age,
                 min_lead_minutes=a.min_lead, kalshi_latency=a.kalshi_latency, book_latency=a.book_latency,
                 cooldown=a.cooldown, duration_minutes=a.duration)
    engine = Engine(cfg)
    run_dir = os.path.join(engine.diagnostics, engine.run_id)
    os.makedirs(run_dir, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(os.path.join(run_dir, "run.log"))])
    try:
        engine.run(once=a.once)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
