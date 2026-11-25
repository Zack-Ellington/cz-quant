"""Read-only DraftKings odds and results for research.

Sources normalize moneyline selections while retaining source timestamps and raw
responses. No broker, cash pool, execution simulation, or settlement is provided.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx


def utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def canonical_time(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires a UTC offset")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def decimal(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("nonfinite numeric value")
    return result


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


def is_selection_id(selection_id: str) -> bool:
    return selection_id.startswith(PREFIX)


def make_selection_id(source: str, league: str, event: str, kind: str, role: str) -> str:
    parts = (source, league, event, kind, role)
    if any(":" in p or not p for p in parts):
        raise ValueError(f"invalid selection ID parts: {parts}")
    return PREFIX + ":".join(parts)


def parse_selection_id(selection_id: str) -> dict[str, str]:
    if not is_selection_id(selection_id):
        raise ValueError(f"not a selection ID: {selection_id}")
    parts = selection_id[len(PREFIX):].split(":")
    if len(parts) != 5 or parts[3] not in ("ML2", "ML3") or parts[4] not in ROLES:
        raise ValueError(f"malformed selection ID: {selection_id}")
    return dict(zip(("source", "league", "event", "kind", "role"), parts))


def implied_probability(odds: Decimal) -> Decimal:
    """Reciprocal decimal odds, including the bookmaker margin."""
    if odds <= 1:
        raise ValueError(f"decimal odds must exceed 1: {odds}")
    return 1 / odds


@dataclass(frozen=True)
class Selection:
    selection_id: str
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
    def implied_probability(self) -> Decimal:
        return implied_probability(self.odds)

    def started(self, now: float | None = None) -> bool:
        start = datetime.fromisoformat(self.start).timestamp()
        return (time.time() if now is None else now) >= start


@dataclass(frozen=True)
class Result:
    """A reported game result, independent of any venue settlement rules."""
    winner: str | None
    evidence: dict[str, Any]


class Blocked(RuntimeError):
    """The source refused this client. Not retried and not worked around."""


class OutOfCredits(RuntimeError):
    """The data source reports an exhausted quota."""


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
        """Fetch moneyline odds for one league and configured bookmaker."""
        return self._get(f"/sports/{league}/odds", {"regions": self.region, "markets": "h2h",
                         "oddsFormat": "decimal", "bookmakers": self.bookmaker, "dateFormat": "iso"})

    def parse(self, raw: Any, league: str, fetched_at: str) -> list[Selection]:
        return parse_oddsapi(raw, league, self.bookmaker, fetched_at)

    def selections(self, league: str) -> list[Selection]:
        return self.parse(self.fetch(league), league, utc())

    def results(self, league: str, events: list[str]) -> dict[str, Result]:
        """Read reported scores for the requested games."""
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
                continue  # a complete result needs both scores
            h, a = decimal(home), decimal(away)
            winner = "home" if h > a else "away" if a > h else "draw"
            out[ev["id"]] = Result(winner, {"source": self.name, "scores": ev})
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
            out.append(Selection(make_selection_id("oddsapi", league, ev["id"], kind, role), "oddsapi", league,
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
            out.append(Selection(make_selection_id("nash", league, event_id, kind, role), "nash", league, event_id,
                                 kind, role, str(s.get("label") or teams.get(role) or role), decimal(odds),
                                 canonical_time(start), teams["home"], teams["away"],
                                 bool(market.get("isSuspended") or s.get("isSuspended")), observed,
                                 {"event": event, "market": market, "selection": s}, observed))
    return out
