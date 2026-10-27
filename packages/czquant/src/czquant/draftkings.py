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

import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
from typing import Any

import httpx

from .store import canonical_time, decimal, utc

PREFIX = "DK:"
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
    updated_at: str  # when the source last changed this price (canonical UTC)
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

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
        self.remaining: str | None = None  # request credits left, from the last response

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        response = self.http.get(self.base + path, params={**params, "apiKey": self.key})
        if response.status_code in (401, 403):
            raise Blocked(f"The Odds API refused the key (HTTP {response.status_code})")
        response.raise_for_status()
        self.remaining = response.headers.get("x-requests-remaining", self.remaining)
        return response.json()

    def selections(self, league: str) -> list[Selection]:
        data = self._get(f"/sports/{league}/odds", {"regions": self.region, "markets": "h2h",
                         "oddsFormat": "decimal", "bookmakers": self.bookmaker, "dateFormat": "iso"})
        return parse_oddsapi(data, league, self.bookmaker)

    def results(self, league: str, events: list[str]) -> dict[str, Result]:
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


def parse_oddsapi(data: list[dict[str, Any]], league: str, bookmaker: str = "draftkings") -> list[Selection]:
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
                                 home, away, False, updated, {"event": ev["id"], "bookmaker": book}))
    return out


# --------------------------------------------------------------------------- DraftKings sportscontent

class Nash:
    name = "nash"

    def __init__(self, site: str | None = None, http: httpx.Client | None = None):
        self.site = site or os.environ.get("DRAFTKINGS_SITE", "dkusnj")
        self.http = http or httpx.Client(timeout=30, headers={"Accept": "application/json"})

    def url(self, league: str) -> str:
        return f"https://sportsbook-nash.draftkings.com/api/sportscontent/{self.site}/v1/leagues/{league}"

    def selections(self, league: str) -> list[Selection]:
        response = self.http.get(self.url(league))
        if response.status_code == 403:
            raise Blocked("DraftKings refused this client (HTTP 403 from its CDN). Use "
                          "DRAFTKINGS_SOURCE=oddsapi, or a host DraftKings serves. This client "
                          "does not work around the block.")
        response.raise_for_status()
        return parse_nash(response.json(), league, utc())

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
                                 {"event": event, "market": market, "selection": s}))
    return out


# --------------------------------------------------------------------------- facade

class DraftKings:
    """Kalshi-shaped reads over one source, for the paper adapter and the monitor.

    League reads are cached for `ttl` seconds so a monitor pass or a basket of legs
    makes one request per league; fills always read fresh.
    """

    def __init__(self, source: Any, ttl: float = 5.0, max_stake: str = "500"):
        self.source, self.ttl, self.max_stake = source, ttl, decimal(max_stake)
        self._cache: dict[str, tuple[float, list[Selection]]] = {}

    @classmethod
    def from_env(cls) -> "DraftKings":
        name = os.environ.get("DRAFTKINGS_SOURCE", "oddsapi")
        if name not in ("oddsapi", "nash"):
            raise ValueError("DRAFTKINGS_SOURCE must be oddsapi or nash")
        source = OddsApi() if name == "oddsapi" else Nash()
        return cls(source, max_stake=os.environ.get("DRAFTKINGS_MAX_STAKE", "500"))

    def selections(self, league: str, fresh: bool = False) -> list[Selection]:
        cached = self._cache.get(league)
        if cached and not fresh and time.monotonic() - cached[0] < self.ttl:
            return cached[1]
        rows = self.source.selections(league)
        self._cache[league] = (time.monotonic(), rows)
        return rows

    def selection(self, ticker: str, fresh: bool = False) -> Selection | None:
        parts = parse_ticker(ticker)
        if parts["source"] != self.source.name:
            raise ValueError(f"{ticker} belongs to source {parts['source']}, not {self.source.name}")
        return next((s for s in self.selections(parts["league"], fresh) if s.ticker == ticker), None)

    def book(self, selection: Selection | None) -> dict[str, Any]:
        """A back-only book: the YES ask 1/d (a NO bid at 1 - 1/d) up to the stake limit."""
        if selection is None or selection.suspended:
            return {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}
        depth = (self.max_stake / selection.price).quantize(Decimal("0.01"))
        return {"orderbook_fp": {"yes_dollars": [], "no_dollars": [[str(1 - selection.price), str(depth)]]},
                "odds": str(selection.odds), "updated_at": selection.updated_at}

    def market(self, ticker: str, selection: Selection | None, result: Result | None = None) -> dict[str, Any]:
        parts = parse_ticker(ticker)
        data: dict[str, Any] = {"ticker": ticker, "venue": "draftkings", **parts, "void": False,
                                "settlement_value_dollars": None}
        if selection is not None:
            data.update(title=f"{selection.away} @ {selection.home}", name=selection.name, start=selection.start,
                        odds=str(selection.odds), updated_at=selection.updated_at,
                        status="suspended" if selection.suspended else "in_play" if selection.started() else "open")
        else:
            data["status"] = "closed"  # no longer offered and not yet resolved
        if result is not None:
            value = payout(parts["kind"], parts["role"], result)
            data.update(status="finalized", void=value is None, result=result.evidence,
                        settlement_value_dollars=None if value is None else str(value))
        return data

    def markets(self, tickers: list[str]) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
        """(market, book) for many tickers: one odds read per league, and one results
        read per league for games that are no longer offered or have started."""
        out: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        by_league: dict[str, list[str]] = {}
        for ticker in tickers:
            by_league.setdefault(parse_ticker(ticker)["league"], []).append(ticker)
        for league, group in by_league.items():
            offered = {s.ticker: s for s in self.selections(league, fresh=True)}
            pending = sorted({parse_ticker(t)["event"] for t in group
                              if t not in offered or offered[t].started()})
            results = self.source.results(league, pending)
            for ticker in group:
                selection = offered.get(ticker)
                out[ticker] = (self.market(ticker, selection, results.get(parse_ticker(ticker)["event"])),
                               self.book(selection))
        return out
