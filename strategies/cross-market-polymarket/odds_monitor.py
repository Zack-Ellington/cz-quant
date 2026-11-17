"""Interactive odds monitor: one NFL game at a time, Kalshi against Polymarket.

    uv run python odds_monitor.py                   # http://127.0.0.1:8765
    uv run python odds_monitor.py --port 8800 --league nba

Search an open Kalshi game and load it. The page charts each team's price on both
venues, shades the times a cross-venue basket cost less than $1, marks the
portfolio's trades and the trader's opportunities, and lists the most recent
arbitrage windows. Every read is public and free.

Data
  history  Kalshi's candlesticks (bid, ask, last; 1-minute for the last day, hourly
           before) and Polymarket's price history (1-minute, then hourly). Polymarket
           publishes no ask history, only a price, so edges before the monitor started
           watching are estimates and are labelled that way.
  live     both venues' books every --interval seconds while a game is loaded: real
           asks on both sides. Live samples are kept in
           $STRATEGY_RUNS_DIR/diagnostics/<portfolio>/odds_monitor/<event>.jsonl.

Arbitrage is the trader's sum check at the top of the book:

    cost = Kalshi YES ask + taker fee (team A) + Polymarket ask + taker fee (team B)

for either assignment of the two teams, and edge = 1 - cost per $1 set. Depth isn't
checked: a window is a lead to look at, not a basket the trader could fill. Reads
the shared store only; never trades.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from czquant import Store
from czquant.book import levels
from czquant.kalshi import Kalshi
from czquant.polymarket import Polymarket, kalshi_layout

from matching import KalshiGame, kalshi_game, load_pairs, match, poly_game

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "odds_monitor.html")
LEAGUES = os.path.join(HERE, "leagues.json")
TAKER = 0.07
EMPTY = [None, None, None]  # bid, ask, last
log = logging.getLogger("odds_monitor")
KEEP_FINISHED = 3 * 86400  # a closed game's windows stay in the table this long after its last sample


def closed_game(event: dict[str, Any]) -> KalshiGame | None:
    """A game from a Kalshi event whose markets have closed or settled (kalshi_game
    reads only open markets)."""
    return kalshi_game({**event, "markets": [{**m, "status": "open"} for m in event.get("markets") or []]})


def _ts(value: str | None) -> float | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None


def _f(value: Any) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def kalshi_fee(price: float, multiplier: float, sets: int) -> float:
    """Kalshi taker fee per contract on one order of `sets` contracts, rounded up to the cent per order."""
    return math.ceil(round(TAKER * multiplier * sets * price * (1 - price) * 100, 9)) / 100 / sets


def poly_fee(price: float, schedule: dict[str, Any]) -> float:
    """Polymarket taker fee per share: rate x (p(1-p)) ** exponent."""
    if not schedule.get("enabled", True):
        return 0.0
    return float(schedule.get("rate", 0)) * (price * (1 - price)) ** int(schedule.get("exponent", 1))


def edges(kalshi_asks: list[float | None], poly_asks: list[float | None], multiplier: float,
          schedule: dict[str, Any], sets: int) -> dict[str, Any]:
    """Basket costs at the top of the book, per $1 set, after both venues' taker fees.

    Returns the best cross-venue edge and which team goes where (`kalshi` is the
    index of the team bought on Kalshi; the other is bought on Polymarket), plus the
    one-venue edges for reference."""
    k = [None if a is None or not 0 < a < 1 else a + kalshi_fee(a, multiplier, sets) for a in kalshi_asks]
    p = [None if a is None or not 0 < a < 1 else a + poly_fee(a, schedule) for a in poly_asks]
    out: dict[str, Any] = {"edge": None, "kalshi": None, "cost": None,
                           "kalshi_only": None if None in k else 1 - (k[0] + k[1]),  # type: ignore[operator]
                           "poly_only": None if None in p else 1 - (p[0] + p[1])}  # type: ignore[operator]
    for i in (0, 1):
        j = 1 - i
        if k[i] is not None and p[j] is not None:
            cost = k[i] + p[j]  # type: ignore[operator]
            if out["cost"] is None or cost < out["cost"]:
                out.update(edge=1 - cost, kalshi=i, cost=cost)
    return out


def windows(points: list[dict[str, Any]], min_edge: float, gap: float) -> list[dict[str, Any]]:
    """Runs of consecutive points whose cross-venue edge is at least min_edge.

    A run ends at the first point below the threshold, or where samples are more
    than `gap` seconds apart. A run with any history point (Polymarket price, not its
    ask) is an estimate."""
    out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    last_t = None
    for p in points:
        e = p.get("arb") or {}
        hit = e.get("edge") is not None and e["edge"] >= min_edge
        if current and (not hit or (last_t is not None and p["t"] - last_t > gap)):
            current["end"] = p["t"] if not hit else last_t
            out.append(current)
            current = None
        if hit:
            i = e["kalshi"]
            if current is None:
                current = {"start": p["t"], "end": None, "best": -1.0, "points": 0, "estimate": False}
            current["points"] += 1
            current["estimate"] = current["estimate"] or not p.get("live")
            if e["edge"] > current["best"]:
                current.update(best=e["edge"], at=p["t"], kalshi=i, kalshi_ask=p["k"][i][1], poly_ask=p["p"][1 - i][1])
        last_t = p["t"]
    if current:
        out.append(current)  # still open: end None
    return out


class Watch:
    """One loaded game: both venues' samples and the pairing."""

    def __init__(self, game: KalshiGame, path: Path):
        self.game, self.path, self.title = game, path, game.event
        self.tickers = [t["market"]["ticker"] for t in game.teams]
        self.tokens: list[str] | None = None  # Polymarket tokens in Kalshi's team order
        self.schedule: dict[str, Any] = {}
        self.poly: dict[str, Any] | None = None
        self.pair_status = "not paired yet"
        self.history: dict[float, dict[str, Any]] = {}  # t -> {"k": [...], "p": [...]}: from the venues' histories
        self.live: dict[float, dict[str, Any]] = {}  # t -> {"k": [...], "p": [...]}: books read by the monitor
        self.loaded = False  # opened on the page: history backfilled
        self.cache: list[dict[str, Any]] = []  # Monitor.points, kept up to date incrementally
        self.cache_key: tuple[Any, ...] | None = None
        self.cache_live = 0  # live samples already in the cache
        self.cache_held: dict[str, Any] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                    self.live[row["t"]] = {"k": row["k"], "p": row["p"]}
                except (ValueError, KeyError):
                    continue  # a line cut short by a crash

    def add_live(self, t: float, k: list[list[float | None]], p: list[list[float | None]]) -> None:
        self.live[t] = {"k": k, "p": p}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": t, "k": k, "p": p}, separators=(",", ":")) + "\n")


class Monitor:
    def __init__(self, portfolio: str, league: str, interval: float, sets: int):
        self._local = threading.local()  # one SQLite connection per thread: sqlite3 refuses to share them
        self.portfolio, self.league = portfolio, league
        self.interval, self.sets = interval, sets
        with open(LEAGUES, encoding="utf-8") as f:
            self.spec = json.load(f)[league]
        self.aliases = self.spec.get("abbr_aliases") or {}
        self.kalshi = Kalshi(authenticated=False)
        self.poly = Polymarket.from_env()
        self.pairs = load_pairs()
        self.dir = Path(self.store.root) / "diagnostics" / portfolio / "odds_monitor"
        self.lock = threading.Lock()
        self.games: list[dict[str, Any]] = []
        self.polys: list[Any] = []
        self.games_at = 0.0
        self.fee_multiplier = 1.0
        self.watches: dict[str, Watch] = {}  # open games, polled
        self.finished: dict[str, Watch] = {}  # closed games: no longer polled, still in the table
        self._restored = False
        self._ledger: dict[str, Any] = {}  # Kalshi event -> {"trades", "opportunities"}
        self._ledger_seq = 0  # the newest store sequence read into it
        self._ledger_lock = threading.Lock()

    @property
    def store(self) -> Store:
        if not hasattr(self._local, "store"):
            self._local.store = Store()
        return self._local.store

    # -- discovery ----------------------------------------------------------------

    def refresh_games(self, force: bool = False) -> None:
        """Open Kalshi games and Polymarket games of the league, every 5 minutes."""
        if not force and time.time() - self.games_at < 300 and self.games:
            return
        series = self.spec["kalshi_series"]
        schedule = self.kalshi.get(f"/series/{series}").get("series", {})
        events = list(self.kalshi.pages("/events", "events", series_ticker=series, status="open",
                                        with_nested_markets="true", limit=200))
        polys = [g for g in (poly_game(e) for e in self.poly.events(tag_slug=self.spec["polymarket_tag"], closed="false"))
                 if g is not None]
        with self.lock:
            self.fee_multiplier = float(schedule.get("fee_multiplier") or 1)
            self.games, self.polys, self.games_at = events, polys, time.time()
        # Every paired open game is polled live, loaded or not, so the arbitrage table
        # covers the whole league. Games no longer open stop being polled.
        fresh = {}
        for raw in events:
            game = kalshi_game(raw)
            if game is None:
                continue
            with self.lock:
                w = self.watches.get(game.event)
            if w is None:
                w = self.make_watch(raw, game)
                if w.tokens is None:
                    continue  # unpaired: polled only once opened on the page
            fresh[game.event] = w
        with self.lock:
            for event, w in self.watches.items():
                if w.loaded and event not in fresh:
                    fresh[event] = w  # keep an opened game until it closes on Kalshi too
            open_events = {r["event_ticker"] for r in events}
            for event, w in self.watches.items():
                if event not in open_events and w.live:
                    self.finished[event] = w  # closed on Kalshi: stop polling, keep its windows
            self.watches = {e: w for e, w in fresh.items() if e in open_events}
        if not self._restored:
            self._restored = True
            self.restore_finished()
        cutoff = time.time() - KEEP_FINISHED
        with self.lock:
            self.finished = {e: w for e, w in self.finished.items()
                             if e not in self.watches and w.live and next(reversed(w.live)) >= cutoff}

    def restore_finished(self) -> None:
        """Closed games sampled in the last few days, from their files, so a restart keeps
        their windows. The pairing comes from the game's .meta.json; files written before
        those existed get the game from Kalshi and the league's Polymarket fee schedule
        from an open game (one schedule per league)."""
        if not self.dir.exists():
            return
        with self.lock:
            known = set(self.watches)
            schedule = next((w.schedule for w in self.watches.values() if w.schedule), None)
        cutoff = time.time() - KEEP_FINISHED
        for path in sorted(self.dir.glob("*.jsonl")):
            event = path.stem
            if event in known or path.stat().st_mtime < cutoff:
                continue
            meta_path = path.with_suffix(".meta.json")
            try:
                if meta_path.exists():
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    game = closed_game(meta["event"])
                    w_schedule, poly = meta.get("schedule"), meta.get("poly")
                else:
                    raw = self.kalshi.get(f"/events/{event}", {"with_nested_markets": "true"})
                    raw = {**(raw.get("event") or {}), "markets": raw.get("markets") or (raw.get("event") or {}).get("markets")}
                    game, w_schedule, poly = closed_game(raw), schedule, None
            except Exception:
                log.exception("couldn't restore %s", event)
                continue
            if game is None or w_schedule is None:
                continue
            w = Watch(game, path)
            if not w.live or next(reversed(w.live)) < cutoff:
                continue
            w.title = game.raw.get("title") or event
            w.schedule, w.poly, w.pair_status = w_schedule, poly, "paired (closed)"
            with self.lock:
                self.finished.setdefault(event, w)

    def pair(self, game: KalshiGame) -> tuple[Any, str]:
        return match(game, self.polys, self.aliases, self.pairs["pairs"].get(game.event))

    # -- history ------------------------------------------------------------------------

    def kalshi_candles(self, ticker: str, start: int, end: int, period: int) -> dict[float, list[float | None]]:
        data = self.kalshi.get(f"/series/{self.spec['kalshi_series']}/markets/{ticker}/candlesticks",
                               {"start_ts": start, "end_ts": end, "period_interval": period})
        out = {}
        for c in data.get("candlesticks") or []:
            ask = _f((c.get("yes_ask") or {}).get("close_dollars"))
            out[float(c["end_period_ts"])] = [_f((c.get("yes_bid") or {}).get("close_dollars")),
                                              ask if ask is not None and ask < 1 else None,
                                              _f((c.get("price") or {}).get("close_dollars"))]
        return out

    def poly_history(self, token: str, start: int, end: int, fidelity: int) -> dict[float, float]:
        data = self.poly._request("GET", self.poly.clob + "/prices-history",
                                  params={"market": token, "startTs": start, "endTs": end, "fidelity": fidelity})
        return {float(h["t"]): float(h["p"]) for h in data.get("history") or []}

    def backfill(self, w: Watch) -> None:
        """Both venues' history: hourly since the Kalshi market opened, 1-minute for the last day."""
        now = int(time.time())
        opened = int(min((_ts(t["market"].get("open_time")) or now - 14 * 86400) for t in w.game.teams))
        spans = ([(opened, now - 86400, 60)] if now - 86400 > opened else []) + [(max(opened, now - 86400), now, 1)]
        rows: dict[float, dict[str, Any]] = {}
        for i, ticker in enumerate(w.tickers):
            for start, end, period in spans:
                for t, q in self.kalshi_candles(ticker, start, end, period).items():
                    rows.setdefault(t, {"k": [None, None], "p": [None, None]})["k"][i] = q
        for i, token in enumerate(w.tokens or []):
            for start, end, period in spans:
                for t, price in self.poly_history(token, start, end, period).items():
                    rows.setdefault(t, {"k": [None, None], "p": [None, None]})["p"][i] = [None, price, price]
        with self.lock:
            w.history = rows

    # -- live -------------------------------------------------------------------------

    def poll(self) -> None:
        with self.lock:
            watches = list(self.watches.values())
        if not watches:
            return
        tickers = [t for w in watches for t in w.tickers]
        tokens = [t for w in watches for t in (w.tokens or [])]
        kbooks = {}
        for i in range(0, len(tickers), 100):
            for row in self.kalshi.get("/markets/orderbooks", [("tickers", t) for t in tickers[i:i + 100]]).get("orderbooks") or []:
                kbooks[row["ticker"]] = levels(row)
        pbooks = {t: levels(kalshi_layout(b)) for t, b in self.poly.books(tokens).items()} if tokens else {}

        def quote(b: dict[str, Any] | None) -> list[float | None]:
            bid = float(b["yes"][0][0]) if b and b["yes"] else None
            ask = float(1 - b["no"][0][0]) if b and b["no"] else None
            return [bid, ask, None]

        now = time.time()
        with self.lock:
            for w in watches:
                k = [quote(kbooks.get(t)) for t in w.tickers]
                p = [quote(pbooks.get(t)) for t in w.tokens] if w.tokens else [list(EMPTY), list(EMPTY)]
                w.add_live(now, k, p)

    # -- the store --------------------------------------------------------------------

    def leg(self, leg: dict[str, Any]) -> dict[str, Any]:
        """A basket leg with a readable label: the team abbreviation on either venue."""
        ticker = str(leg.get("ticker") or "")
        if leg.get("venue") == "polymarket":
            token = ticker.removeprefix("PM:")
            abbr = next((a for g in self.polys for a, t in g.tokens.items() if t == token), None)
            label = abbr.upper() if abbr else "token " + token[-6:]
        else:
            label = ticker.rsplit("-", 1)[-1]
        return {"venue": leg.get("venue"), "ticker": ticker, "label": label, "limit": leg.get("limit")}

    def ledger(self) -> dict[str, Any]:
        """The portfolio's baskets and the trader's opportunities in this league's games, by
        Kalshi event. Non-sports pairs and other leagues are left out in SQL: the trader logs
        an opportunity per scan for every auto pair (tens of thousands of rows), so reading
        them all each refresh took a minute. After the first read only new rows are read."""
        prefix = self.spec["kalshi_series"] + "-%"
        with self._ledger_lock:
            newest = self.store.db.execute("SELECT max(sequence) FROM events").fetchone()[0] or 0
            rows = self.store.db.execute(
                "SELECT sequence, effective_at, data FROM events INDEXED BY events_portfolio "
                "WHERE portfolio_id = ? AND sequence > ? AND sequence <= ? AND type = 'observation' "
                "AND json_extract(data, '$.kind') IN ('basket_execution', 'opportunity') "
                "AND (json_extract(data, '$.kalshi_event') LIKE ? OR json_extract(data, '$.context.kalshi_event') LIKE ?) "
                "ORDER BY sequence", (self.portfolio, self._ledger_seq, newest, prefix, prefix)).fetchall()
            self._ledger_seq = newest  # scanned through here, matched or not
            for sequence, effective_at, raw in rows:
                d = json.loads(raw)
                t = _ts(effective_at)
                if d.get("kind") == "basket_execution":
                    c = d.get("context") or {}
                    self._ledger.setdefault(c.get("kalshi_event"), {"trades": [], "opportunities": []})["trades"].append({
                        "t": t, "trade_id": c.get("trade_id"), "sets": c.get("sets"), "edge": _f(c.get("edge")),
                        "expected_net": _f(c.get("expected_net")),
                        "legs": [self.leg(l) for l in c.get("intended_legs") or []],
                        "outcomes": [{"venue": o.get("venue"), "status": o.get("status"), "filled": o.get("filled")}
                                     for o in d.get("outcomes") or [] if "unwind" not in o]})
                else:
                    self._ledger.setdefault(d.get("kalshi_event"), {"trades": [], "opportunities": []})["opportunities"].append({
                        "t": t, "edge": _f(d.get("edge")), "net": _f(d.get("net")), "sets": d.get("sets"),
                        "legs": [self.leg(l) for l in d.get("legs") or []]})
            return self._ledger

    # -- views ----------------------------------------------------------------------

    def make_watch(self, raw: dict[str, Any], game: KalshiGame) -> Watch:
        """A game paired with Polymarket (when it pairs), not yet backfilled."""
        w = Watch(game, self.dir / f"{game.event}.jsonl")
        w.title = raw.get("title") or game.event
        pair, why = self.pair(game)
        if pair is None:
            w.pair_status = f"not paired with a Polymarket game ({why})"
            return w
        w.tokens = [pair.legs[t] for t in w.tickers]
        market = pair.poly.market
        sched = market.get("feeSchedule") or {}
        w.schedule = {"enabled": bool(market.get("feesEnabled", True)), "rate": float(sched.get("rate", 0)),
                      "exponent": int(sched.get("exponent", 1))}
        by_token = {tok: abbr for abbr, tok in pair.poly.tokens.items()}
        w.poly = {"slug": pair.poly.slug, "names": [pair.poly.names[by_token[t]] for t in w.tokens],
                  "start": pair.poly.start.isoformat() if pair.poly.start else None, "question": market.get("question")}
        w.pair_status = "paired"
        try:  # lets a restart restore the game after it closes
            w.path.parent.mkdir(parents=True, exist_ok=True)
            w.path.with_suffix(".meta.json").write_text(json.dumps({
                "event": {k: v for k, v in raw.items() if k in ("event_ticker", "series_ticker", "title", "sub_title")}
                | {"markets": [{k: m.get(k) for k in ("ticker", "yes_sub_title", "status", "open_time")}
                               for m in raw.get("markets") or []]},
                "schedule": w.schedule, "poly": w.poly}), encoding="utf-8")
        except OSError:
            log.warning("couldn't save the pairing for %s", game.event)
        return w

    def watch(self, event: str) -> Watch:
        """The game opened on the page: its history is backfilled the first time."""
        self.refresh_games()
        with self.lock:
            w = self.watches.get(event) or self.finished.get(event)
        if w is not None and event in self.finished:
            if not w.loaded:
                try:
                    self.backfill(w)
                except Exception:
                    log.exception("couldn't backfill closed game %s", event)
                w.loaded = True
            return w
        if w is None:
            raw = next((e for e in self.games if e["event_ticker"] == event), None)
            game = kalshi_game(raw) if raw else None
            if game is None:
                raise KeyError(f"{event} isn't an open Kalshi {self.league.upper()} game")
            w = self.make_watch(raw, game)  # type: ignore[arg-type]
        if not w.loaded:
            self.backfill(w)
            w.loaded = True
            with self.lock:
                self.watches[event] = w
        return w

    def points(self, w: Watch) -> list[dict[str, Any]]:
        """History then live samples in time order, each venue's quote carried forward to
        samples where it has none, with the basket edges at each.

        Cached on the watch: a game polled for a day holds ~10,000 live samples, and every
        game is recomputed on each table refresh, so only samples added since the last call
        are processed. History (replaced on backfill) or the fee schedule changing starts over.
        Live samples arrive in time order (appended by poll, read back from the file in order).
        Call with self.lock held."""
        first = next(iter(w.live), None)
        key = (id(w.history), len(w.history), first, self.fee_multiplier, id(w.schedule))
        if w.cache_key != key:
            w.cache_key, w.cache, w.cache_live = key, [], 0
            w.cache_held = {"k": [list(EMPTY), list(EMPTY)], "p": [list(EMPTY), list(EMPTY)]}
            for t in sorted(t for t in w.history if first is None or t < first):
                self._add_point(w, t, w.history[t], False)
        if len(w.live) > w.cache_live:
            for t, r in list(w.live.items())[w.cache_live:]:
                self._add_point(w, t, r, True)
            w.cache_live = len(w.live)
        return w.cache

    def _add_point(self, w: Watch, t: float, r: dict[str, Any], live: bool) -> None:
        held = w.cache_held
        for venue in ("k", "p"):
            held[venue] = [q if q and any(v is not None for v in q) else held[venue][i]
                           for i, q in enumerate(r[venue])]
        k, p = held["k"], held["p"]
        w.cache.append({"t": t, "live": live, "k": k, "p": p,
                        "arb": edges([k[0][1], k[1][1]], [p[0][1], p[1][1]], self.fee_multiplier, w.schedule, self.sets)})

    def game_view(self, event: str) -> dict[str, Any]:
        w = self.watch(event)
        with self.lock:
            pts = list(self.points(w))
        ledger = self.ledger().get(event, {"trades": [], "opportunities": []})
        return {"event": event, "title": w.title, "league": self.league,
                "teams": [{"ticker": t["market"]["ticker"], "name": t["name"], "abbr": t["abbr"]} for t in w.game.teams],
                "fee_multiplier": self.fee_multiplier, "poly_fee": w.schedule, "sets": self.sets,
                "interval": self.interval, "poly": w.poly, "pair_status": w.pair_status,
                "live_since": min(w.live) if w.live else None, "points": pts, **ledger}

    def games_view(self) -> list[dict[str, Any]]:
        self.refresh_games()
        led = self.ledger()
        out = []
        for e in self.games:
            game = kalshi_game(e)
            if game is None:
                continue
            pair, _ = self.pair(game)
            out.append({"event": game.event, "title": e.get("title") or game.event, "sub_title": e.get("sub_title"),
                        "day": game.day.isoformat(), "teams": [t["name"] for t in game.teams],
                        "abbrs": [t["abbr"] for t in game.teams], "paired": pair is not None,
                        "polymarket": pair.poly.slug if pair else None, "watched": bool(self.watches.get(game.event) and self.watches[game.event].loaded),
                        "trades": len((led.get(game.event) or {}).get("trades") or [])})
        return sorted(out, key=lambda g: (g["day"], g["event"]))

    def arbs_view(self, min_edge: float, limit: int = 50) -> list[dict[str, Any]]:
        """Recent windows in every polled game (every paired open game in the league, and
        games that closed in the last few days), plus the trader's own opportunities."""
        rows = []
        gap = max(3 * self.interval, 180.0)
        with self.lock:
            watches = list(self.watches.values()) + list(self.finished.values())
        for w in watches:
            with self.lock:
                pts = list(self.points(w))
            names = [t["abbr"] for t in w.game.teams]
            for win in windows(pts, min_edge, gap):
                i = win["kalshi"]
                rows.append({"source": "estimate" if win["estimate"] else "live", "event": w.game.event,
                             "game": " vs ".join(names), "start": win["start"], "end": win["end"], "best": win["best"],
                             "at": win["at"], "kalshi_team": names[i], "kalshi_ask": win["kalshi_ask"],
                             "poly_team": names[1 - i], "poly_ask": win["poly_ask"]})
        for event, d in self.ledger().items():
            for o in d.get("opportunities") or []:
                abbrs = sorted({l["label"] for l in o["legs"]})
                rows.append({"source": "trader", "event": event, "game": " vs ".join(abbrs),
                             "start": o["t"], "end": None, "best": o["edge"], "at": o["t"], "legs": o["legs"],
                             "net": o["net"], "sets": o["sets"]})
        rows.sort(key=lambda r: r["start"] or 0, reverse=True)
        return rows[:limit]

    # -- loop -------------------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        try:  # read the ledger and every game's samples now, not on the page's first request
            self.arbs_view(0.0)
        except Exception:
            log.exception("warm-up failed")
        while not stop.is_set():
            started = time.time()
            try:
                self.poll()
            except Exception:
                log.exception("poll failed")
            stop.wait(max(1.0, self.interval - (time.time() - started)))


def handler(monitor: Monitor) -> type[BaseHTTPRequestHandler]:
    from plotly.offline import get_plotlyjs
    plotly_js = get_plotlyjs().encode()  # the copy bundled with plotly, as czquant's dashboard serves it

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug(fmt, *args)

        def send(self, code: int, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def json(self, data: Any, code: int = 200) -> None:
            self.send(code, json.dumps(data, separators=(",", ":"), default=str).encode(), "application/json")

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path in ("/", "/index.html"):
                    with open(PAGE, "rb") as f:
                        self.send(200, f.read(), "text/html; charset=utf-8")
                elif url.path == "/plotly.min.js":
                    self.send(200, plotly_js, "application/javascript")
                elif url.path == "/api/games":
                    self.json({"games": monitor.games_view(), "portfolio": monitor.portfolio, "league": monitor.league})
                elif url.path == "/api/game":
                    self.json(monitor.game_view(q["event"]))
                elif url.path == "/api/arbs":
                    self.json({"arbs": monitor.arbs_view(float(q.get("min_edge", 0.01)), int(q.get("limit", 50)))})
                else:
                    self.json({"error": "not found"}, 404)
            except KeyError as exc:
                self.json({"error": str(exc).strip("'\"")}, 404)
            except Exception as exc:
                log.exception("request failed: %s", self.path)
                self.json({"error": f"{type(exc).__name__}: {exc}"[:300]}, 500)

    return Handler


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--league", default="nfl", help="a leagues.json key")
    p.add_argument("--interval", type=float, default=10.0, help="seconds between live book reads")
    p.add_argument("--sets", type=int, default=100, help="order size for the Kalshi fee's rounding")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # a line per read every few seconds
    if not os.environ.get("STRATEGY_RUNS_DIR"):
        raise SystemExit("Set STRATEGY_RUNS_DIR to the shared runs directory (the one the trader uses).")
    portfolio = os.environ.get("CZQUANT_PORTFOLIO") or "cross-market-polymarket"
    monitor = Monitor(portfolio, a.league, a.interval, a.sets)
    monitor.refresh_games(force=True)
    stop = threading.Event()
    threading.Thread(target=monitor.run, args=(stop,), daemon=True, name="poll").start()
    server = ThreadingHTTPServer(("127.0.0.1", a.port), handler(monitor))
    log.info("odds monitor for %s (%s) on http://127.0.0.1:%d", portfolio, a.league, a.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
