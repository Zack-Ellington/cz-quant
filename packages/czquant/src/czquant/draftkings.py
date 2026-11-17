"""DraftKings sportsbook odds as binary contracts. DraftKings publishes no API.

A moneyline selection at decimal odds d returns d per $1 staked if it wins. That is
a YES contract on "this selection wins" bought at 1/d, with the bookmaker's margin
already in the price. The broker therefore records a bet like a Kalshi buy: count
contracts (the payout) at price 1/d (count * price is the stake) and no separate
fee. A bet cannot be sold back, so its book has an ask and no bid.

Tickers are DK:<source>:<league>:<event>:<kind>:<role>. Kind ML2 is a two-way
moneyline (a tie pushes: the stake is returned), ML3 a three-way moneyline with a
draw selection. Role is home, away or draw. Kalshi tickers never contain ':'.

Two sources normalize to the same Selection:
  oddsapi  The Odds API (the-odds-api.com), bookmaker key "draftkings". Keyed
           (ODDS_API_KEY) and documented; its /scores endpoint supplies results.
  nash     DraftKings' own undocumented sportscontent feed, the JSON its website
           reads. Field names follow community reverse-engineering and can change
           without notice. DraftKings' CDN refuses many non-browser clients (HTTP
           403). This client sends an ordinary request and reports the refusal; it
           does not imitate a browser to get around it. The feed carries no results,
           so its bets are settled by the operator (`czquant settle-sportsbook`).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from decimal import Decimal, ROUND_CEILING
from typing import Any

import httpx

from .store import canonical_time, decimal, utc

PREFIX = "DK:"


class _RedactKey(logging.Filter):
    """httpx logs every request URL, and The Odds API takes its key in the URL."""
    pattern = re.compile(r"(apiKey=)[^&\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(self.pattern.sub(r"\g<1>REDACTED", str(a)) if isinstance(a, (str, httpx.URL)) else a
                                for a in (record.args if isinstance(record.args, tuple) else (record.args,)))
        record.msg = self.pattern.sub(r"\g<1>REDACTED", str(record.msg))
        return True


logging.getLogger("httpx").addFilter(_RedactKey())
ROLES = ("home", "away", "draw")


def is_sportsbook(ticker: str) -> bool:
    return ticker.startswith(PREFIX)


def make_ticker(source: str, league: str, event: str, kind: str, role: str) -> str:
    parts = (source, league, event, kind, role)
    if any(":" in p or not p for p in parts):
        raise ValueError(f"invalid sportsbook ticker parts: {parts}")
    return PREFIX + ":".join(parts)


def parse_ticker(ticker: str) -> dict[str, str]:
    if not is_sportsbook(ticker):
        raise ValueError(f"not a sportsbook ticker: {ticker}")
    parts = ticker[len(PREFIX):].split(":")
    if len(parts) != 5 or parts[3] not in ("ML2", "ML3") or parts[4] not in ROLES:
        raise ValueError(f"malformed sportsbook ticker: {ticker}")
    return dict(zip(("source", "league", "event", "kind", "role"), parts))


def implied_price(odds: Decimal) -> Decimal:
    """YES price of a bet at decimal odds, rounded up: the stake per $1 of payout."""
    if odds <= 1:
        raise ValueError(f"decimal odds must exceed 1: {odds}")
    return (1 / odds).quantize(Decimal("0.000001"), rounding=ROUND_CEILING)


@dataclass(frozen=True)
class Selection:
    ticker: str
    source: str
    league: str
    event: str
    kind: str
    role: str
    name: str
    odds: Decimal
    start: str  # canonical UTC
    home: str
    away: str
    suspended: bool
    updated_at: str  # when the source last updated this price (canonical UTC)
    raw: dict[str, Any] = field(default_factory=dict, compare=False)
    fetched_at: str = ""  # when this client read it (canonical UTC)

    @property
    def price(self) -> Decimal:
        return implied_price(self.odds)

    def started(self, now: float | None = None) -> bool:
        start = datetime.fromisoformat(self.start).timestamp()
        return (time.time() if now is None else now) >= start


@dataclass(frozen=True)
class Result:
    """A finished game. winner is home, away or draw; void cancels every bet."""
    winner: str | None
    void: bool
    evidence: dict[str, Any]


def payout(kind: str, role: str, result: Result) -> Decimal | None:
    """YES value of a selection, or None when the bet is void (stake returned)."""
    if result.void or result.winner is None:
        return None
    if result.winner == "draw" and kind == "ML2":
        return None  # a two-way moneyline pushes on a tie
    return Decimal(1) if result.winner == role else Decimal(0)


class Blocked(RuntimeError):
    """The source refused this client. Not retried and not worked around."""


class OutOfCredits(RuntimeError):
    """A paid read would spend credits kept in reserve, or the quota is exhausted."""


# --------------------------------------------------------------------------- The Odds API

class OddsApi:
    name = "oddsapi"
    base = "https://api.the-odds-api.com/v4"

    def __init__(self, key: str | None = None, http: httpx.Client | None = None,
                 bookmaker: str = "draftkings", region: str = "us"):
        self.key = key or os.environ.get("ODDS_API_KEY")
        if not self.key:
            raise ValueError("export ODDS_API_KEY for the oddsapi DraftKings source")
        self.http = http or httpx.Client(timeout=30)
        self.bookmaker, self.region = bookmaker, region
        # Credits left after the last response (x-requests-remaining), and when it was read.
        self.remaining: int | None = None
        self.remaining_at: float = 0.0

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        response = self.http.get(self.base + path, params={**params, "apiKey": self.key})
        if response.status_code in (401, 403):
            raise Blocked(f"The Odds API refused the key (HTTP {response.status_code})")
        remaining = response.headers.get("x-requests-remaining")
        if remaining not in (None, ""):
            self.remaining, self.remaining_at = int(float(remaining)), time.time()
        if response.status_code == 429:
            raise OutOfCredits("The Odds API quota is exhausted (HTTP 429)")
        response.raise_for_status()
        return response.json()

    def fetch(self, league: str) -> Any:
        """One odds read: one credit (one market, one region)."""
        return self._get(f"/sports/{league}/odds", {"regions": self.region, "markets": "h2h",
                         "oddsFormat": "decimal", "bookmakers": self.bookmaker, "dateFormat": "iso"})

    def parse(self, raw: Any, league: str, fetched_at: str) -> list[Selection]:
        return parse_oddsapi(raw, league, self.bookmaker, fetched_at)

    def selections(self, league: str) -> list[Selection]:
        return self.parse(self.fetch(league), league, utc())

    def results(self, league: str, events: list[str]) -> dict[str, Result]:
        """Finished games among `events`: two credits per call (daysFrom), whatever the count."""
        if not events:
            return {}
        data = self._get(f"/sports/{league}/scores", {"daysFrom": 3, "eventIds": ",".join(events),
                                                       "dateFormat": "iso"})
        out = {}
        for ev in data:
            if not ev.get("completed"):
                continue
            scores = {s.get("name"): s.get("score") for s in ev.get("scores") or []}
            home, away = scores.get(ev.get("home_team")), scores.get(ev.get("away_team"))
            if home in (None, "") or away in (None, ""):
                continue  # completed without both scores: left to the operator
            h, a = decimal(home), decimal(away)
            winner = "home" if h > a else "away" if a > h else "draw"
            out[ev["id"]] = Result(winner, False, {"source": self.name, "scores": ev})
        return out


def parse_oddsapi(data: list[dict[str, Any]], league: str, bookmaker: str = "draftkings",
                  fetched_at: str | None = None) -> list[Selection]:
    fetched = fetched_at or utc()
    out = []
    for ev in data:
        book = next((b for b in ev.get("bookmakers") or [] if b.get("key") == bookmaker), None)
        market = next((m for m in (book or {}).get("markets") or [] if m.get("key") == "h2h"), None)
        if book is None or market is None:
            continue
        outcomes = market.get("outcomes") or []
        kind = "ML3" if any(o.get("name", "").lower() == "draw" for o in outcomes) else "ML2"
        home, away = ev["home_team"], ev["away_team"]
        updated = canonical_time(market.get("last_update") or book.get("last_update") or ev["commence_time"])
        for o in outcomes:
            name = o.get("name", "")
            role = "home" if name == home else "away" if name == away else "draw" if name.lower() == "draw" else None
            if role is None or decimal(o["price"]) <= 1:
                continue
            out.append(Selection(make_ticker("oddsapi", league, ev["id"], kind, role), "oddsapi", league,
                                 ev["id"], kind, role, name, decimal(o["price"]), canonical_time(ev["commence_time"]),
                                 home, away, False, updated, {"event": ev["id"], "bookmaker": book}, fetched))
    return out


# --------------------------------------------------------------------------- DraftKings sportscontent

class Nash:
    name = "nash"

    def __init__(self, site: str | None = None, http: httpx.Client | None = None):
        self.site = site or os.environ.get("DRAFTKINGS_SITE", "dkusnj")
        self.http = http or httpx.Client(timeout=30, headers={"Accept": "application/json"})

    def url(self, league: str) -> str:
        return f"https://sportsbook-nash.draftkings.com/api/sportscontent/{self.site}/v1/leagues/{league}"

    remaining = None  # no quota
    remaining_at = 0.0

    def fetch(self, league: str) -> Any:
        response = self.http.get(self.url(league))
        if response.status_code == 403:
            raise Blocked("DraftKings refused this client (HTTP 403 from its CDN). Use "
                          "DRAFTKINGS_SOURCE=oddsapi, or a host DraftKings serves. This client "
                          "does not work around the block.")
        response.raise_for_status()
        return response.json()

    def parse(self, raw: Any, league: str, fetched_at: str) -> list[Selection]:
        return parse_nash(raw, league, fetched_at)

    def selections(self, league: str) -> list[Selection]:
        return self.parse(self.fetch(league), league, utc())

    def results(self, league: str, events: list[str]) -> dict[str, Result]:
        return {}  # the feed drops finished games and publishes no scores


def _role(value: Any) -> str | None:
    value = str(value or "").lower()
    return {"home": "home", "away": "away", "draw": "draw", "tie": "draw"}.get(value)


def parse_nash(data: dict[str, Any], league: str, observed: str) -> list[Selection]:
    """Moneyline selections from a sportscontent league payload.

    The payload is relational: events[], markets[] (eventId, marketType.name) and
    selections[] (marketId, outcomeType Home/Away/Draw, trueOdds decimal).
    """
    events = {str(e.get("id", e.get("eventId"))): e for e in data.get("events") or []}
    markets = {}
    for m in data.get("markets") or []:
        name = ((m.get("marketType") or {}).get("name") or m.get("name") or "").strip().lower()
        if name == "moneyline":
            markets[str(m.get("id", m.get("marketId")))] = m
    grouped: dict[str, list[dict[str, Any]]] = {}
    for s in data.get("selections") or []:
        if str(s.get("marketId")) in markets:
            grouped.setdefault(str(s["marketId"]), []).append(s)
    out = []
    for market_id, rows in grouped.items():
        market = markets[market_id]
        event = events.get(str(market.get("eventId")))
        if event is None:
            continue
        event_id = str(event.get("id", event.get("eventId")))
        teams: dict[str | None, str] = {_role(p.get("venueRole")): str(p.get("name") or "")
                                        for p in event.get("participants") or []}
        if not (teams.get("home") and teams.get("away")) and " @ " in (event.get("name") or ""):
            away_name, home_name = event["name"].split(" @ ", 1)
            teams = {"home": home_name, "away": away_name}
        roles = [r for r in (_role(s.get("outcomeType")) for s in rows) if r is not None]
        if not teams.get("home") or not teams.get("away") or len(roles) != len(rows) or len(set(roles)) != len(roles):
            continue
        kind = "ML3" if "draw" in roles else "ML2"
        start = event.get("startEventDate")
        if not start:
            continue
        for s, role in zip(rows, roles):
            odds = s.get("trueOdds")
            if odds in (None, "") or decimal(odds) <= 1:
                continue
            out.append(Selection(make_ticker("nash", league, event_id, kind, role), "nash", league, event_id,
                                 kind, role, str(s.get("label") or teams.get(role) or role), decimal(odds),
                                 canonical_time(start), teams["home"], teams["away"],
                                 bool(market.get("isSuspended") or s.get("isSuspended")), observed,
                                 {"event": event, "market": market, "selection": s}, observed))
    return out


# --------------------------------------------------------------------------- facade

class DraftKings:
    """Kalshi-shaped reads over one source, for the paper adapter, the monitor and the trader.

    Every paid odds read is written to `cache_dir` (the raw response and its fetch
    time), so other processes reuse it: the monitor marks held bets from the
    trader's reads and pays only for results. The remaining-credit count each
    response reports is written there too (credits.json). Odds reads stop at
    `reserve` credits, which are kept for results, so held bets can still settle.
    """

    def __init__(self, source: Any, ttl: float = 5.0, max_stake: str = "500",
                 cache_dir: str | os.PathLike | None = None, reserve: int = 0):
        self.source, self.ttl, self.max_stake = source, ttl, decimal(max_stake)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.reserve = reserve
        self._cache: dict[str, tuple[float, list[Selection]]] = {}
        # Monitor pacing: results are read per league at most every results_minutes,
        # for games that started settle_after_hours ago; marks only when they change.
        self.results_minutes = float(os.environ.get("DRAFTKINGS_RESULTS_MINUTES", "30"))
        self.settle_after_hours = float(os.environ.get("DRAFTKINGS_SETTLE_AFTER_HOURS", "2.5"))
        self.results_checked: dict[str, float] = {}
        self.last_marked: dict[str, tuple[Any, str]] = {}
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> "DraftKings":
        name = os.environ.get("DRAFTKINGS_SOURCE", "oddsapi")
        if name not in ("oddsapi", "nash"):
            raise ValueError("DRAFTKINGS_SOURCE must be oddsapi or nash")
        source = OddsApi() if name == "oddsapi" else Nash()
        root = os.environ.get("STRATEGY_RUNS_DIR", "runs")
        return cls(source, max_stake=os.environ.get("DRAFTKINGS_MAX_STAKE", "500"),
                   cache_dir=Path(root) / "odds_cache", reserve=int(os.environ.get("ODDS_API_RESERVE", "40")))

    # -- credits ---------------------------------------------------------------

    def credits(self) -> int | None:
        """The newest remaining-credit count seen by any process; None without a quota or before the first read."""
        best: tuple[float, int | None] = (getattr(self.source, "remaining_at", 0.0), getattr(self.source, "remaining", None))
        path = self.cache_dir / "credits.json" if self.cache_dir else None
        if path and path.exists():
            try:
                data = json.loads(path.read_text())
                if data.get("source") == self.source.name and float(data["at"]) > best[0]:
                    best = (float(data["at"]), int(data["remaining"]))
            except (ValueError, KeyError, OSError):
                pass
        return best[1]

    def spendable(self, cost: int = 1, reserve: int | None = None) -> bool:
        """May a read costing `cost` credits go ahead without dipping into the reserve?
        Unknown credits (the first read, or a source without a quota) are allowed."""
        remaining = self.credits()
        return remaining is None or remaining - cost >= (self.reserve if reserve is None else reserve)

    def _record_credits(self) -> None:
        remaining = getattr(self.source, "remaining", None)
        if self.cache_dir and remaining is not None:
            _write_json(self.cache_dir / "credits.json", {"source": self.source.name, "remaining": remaining,
                                                          "at": self.source.remaining_at, "observed_at": utc()})

    # -- odds ------------------------------------------------------------------

    def _cache_path(self, league: str) -> Path | None:
        return self.cache_dir / f"{self.source.name}-{league}.json" if self.cache_dir else None

    def _load(self, league: str) -> tuple[float, list[Selection]] | None:
        """The newest read of this league, from memory or another process's cache file."""
        memory = self._cache.get(league)
        path = self._cache_path(league)
        if path and path.exists():
            try:
                data = json.loads(path.read_text())
                if memory is None or float(data["fetched"]) > memory[0]:
                    memory = (float(data["fetched"]), self.source.parse(data["raw"], league, data["fetched_at"]))
                    self._cache[league] = memory
            except (ValueError, KeyError, OSError):
                pass
        return memory

    def cached_at(self, league: str) -> float | None:
        """Unix time of the newest read of this league by any process."""
        loaded = self._load(league)
        return loaded[0] if loaded else None

    def selections(self, league: str, fresh: bool = False, max_age: float | None = None,
                   fetch: bool = True) -> list[Selection]:
        """Selections read no longer than max_age seconds ago (ttl by default, 0 when fresh).
        With fetch=False the newest cached read of any age is returned and nothing is paid."""
        limit = 0.0 if fresh else (self.ttl if max_age is None else max_age)
        loaded = self._load(league)
        if loaded and (not fetch or time.time() - loaded[0] <= limit):
            return loaded[1]
        if not fetch:
            return []
        if not hasattr(self.source, "fetch"):  # a source without raw reads (fixtures): memory cache only
            rows = self.source.selections(league)
            self._cache[league] = (time.time(), rows)
            return rows
        if not self.spendable():
            raise OutOfCredits(f"{self.credits()} credits left; {self.reserve} are kept for settlement")
        raw = self.source.fetch(league)
        fetched, fetched_at = time.time(), utc()
        self._record_credits()
        rows = self.source.parse(raw, league, fetched_at)
        self._cache[league] = (fetched, rows)
        path = self._cache_path(league)
        if path:
            _write_json(path, {"fetched": fetched, "fetched_at": fetched_at, "raw": raw})
        return rows

    def selection(self, ticker: str, fresh: bool = False) -> Selection | None:
        parts = parse_ticker(ticker)
        if parts["source"] != self.source.name:
            raise ValueError(f"{ticker} belongs to source {parts['source']}, not {self.source.name}")
        return next((s for s in self.selections(parts["league"], fresh) if s.ticker == ticker), None)

    def results(self, league: str, events: list[str]) -> dict[str, Result]:
        """Results may spend the reserve: it exists so held bets can settle."""
        if events and not self.spendable(2, reserve=0):
            raise OutOfCredits(f"{self.credits()} credits left: too few for a results read")
        out = self.source.results(league, events)
        self._record_credits()
        return out

    def book(self, selection: Selection | None) -> dict[str, Any]:
        """A back-only book: the YES ask 1/d (a NO bid at 1 - 1/d) up to the stake limit."""
        if selection is None or selection.suspended:
            return {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}
        depth = (self.max_stake / selection.price).quantize(Decimal("0.01"))
        return {"orderbook_fp": {"yes_dollars": [], "no_dollars": [[str(1 - selection.price), str(depth)]]},
                "odds": str(selection.odds), "updated_at": selection.updated_at, "fetched_at": selection.fetched_at}

    def market(self, ticker: str, selection: Selection | None, result: Result | None = None) -> dict[str, Any]:
        parts = parse_ticker(ticker)
        data: dict[str, Any] = {"ticker": ticker, "venue": "draftkings", **parts, "void": False,
                                "settlement_value_dollars": None}
        if selection is not None:
            data.update(title=f"{selection.away} @ {selection.home}", name=selection.name, start=selection.start,
                        odds=str(selection.odds), updated_at=selection.updated_at, fetched_at=selection.fetched_at,
                        status="suspended" if selection.suspended else "in_play" if selection.started() else "open")
        else:
            data["status"] = "closed"  # not in the newest read and not yet resolved
        if result is not None:
            value = payout(parts["kind"], parts["role"], result)
            data.update(status="finalized", void=value is None, result=result.evidence,
                        settlement_value_dollars=None if value is None else str(value))
        return data

    def markets(self, tickers: list[str], results_for: set[str] | None = None,
                fetch: bool = True) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
        """(market, book) for many tickers. With fetch=False odds come from the newest
        cached read and cost nothing. Results are read once per league, for the events
        in `results_for` (default: every event no longer offered or already started)."""
        out: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        by_league: dict[str, list[str]] = {}
        for ticker in tickers:
            by_league.setdefault(parse_ticker(ticker)["league"], []).append(ticker)
        for league, group in by_league.items():
            offered = {s.ticker: s for s in self.selections(league, fresh=fetch, fetch=fetch)}
            if results_for is None:
                pending = {parse_ticker(t)["event"] for t in group if t not in offered or offered[t].started()}
            else:
                pending = {parse_ticker(t)["event"] for t in group} & results_for
            results = self.results(league, sorted(pending)) if pending else {}
            for ticker in group:
                selection = offered.get(ticker)
                out[ticker] = (self.market(ticker, selection, results.get(parse_ticker(ticker)["event"])),
                               self.book(selection))
        return out


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomic replace, so a reader in another process never sees half a file."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    os.replace(tmp, path)
