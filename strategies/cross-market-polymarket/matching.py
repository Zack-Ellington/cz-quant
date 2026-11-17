"""Pairs a Kalshi game event with the Polymarket moneyline for the same game.

Kalshi dates each game event by its local (Eastern) date, plus the start time for
baseball (KXMLBGAME-26OCT272000MILTB), and ends each team market's ticker with the
team's abbreviation (KXNBAGAME-26NOV21BOSSAC-BOS). Polymarket's game event lists its
teams with their abbreviations and names ({"abbreviation": "bos", "name": "Celtics"}),
the local date (`eventDate`) and the UTC start, and its moneyline market has one
outcome token per team, named as in the team list.

A pair needs the same date (and a start within 90 minutes when Kalshi gives a time),
the same two abbreviations, and each team's name among the moneyline's outcomes.
Abbreviations a league spells differently on each venue go in leagues.json
(`abbr_aliases`, Kalshi's spelling to Polymarket's). Anything else stays unpaired.
pairs.json can pin a pair or block a Kalshi event after reading both venues' rules.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP",
                                      "OCT", "NOV", "DEC"), 1)}
EVENT_DATE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})(\d{4})?[A-Z]")
TIE_NAMES = {"tie", "draw"}
START_TOLERANCE = timedelta(minutes=90)
PAIRS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pairs.json")


@dataclass
class KalshiGame:
    event: str
    series: str
    day: date
    start: datetime | None  # Eastern start, when the ticker gives one
    teams: list[dict[str, Any]]  # [{"market": ..., "name": ..., "abbr": ...}]
    tie: dict[str, Any] | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


def kalshi_game(event: dict[str, Any]) -> KalshiGame | None:
    found = EVENT_DATE.search(event.get("event_ticker", ""))
    if not found or found.group(2) not in MONTHS:
        return None
    yy, mon, dd, hhmm = found.groups()
    day = date(2000 + int(yy), MONTHS[mon], int(dd))
    start = (datetime(day.year, day.month, day.day, int(hhmm[:2]), int(hhmm[2:]), tzinfo=EASTERN)
             if hhmm else None)
    teams, tie = [], None
    for market in event.get("markets") or []:
        if market.get("status") not in ("active", "open"):
            continue
        name = (market.get("yes_sub_title") or "").strip()
        if name.lower() in TIE_NAMES:
            tie = market
        else:
            teams.append({"market": market, "name": name, "abbr": market["ticker"].rsplit("-", 1)[-1]})
    if len(teams) != 2:
        return None
    return KalshiGame(event["event_ticker"], event.get("series_ticker", ""), day, start, teams, tie, event)


@dataclass
class PolyGame:
    slug: str
    day: date | None
    start: datetime | None  # UTC
    tokens: dict[str, str]  # lower-case team abbreviation -> outcome token id
    names: dict[str, str]  # lower-case team abbreviation -> outcome name
    market: dict[str, Any] = field(repr=False, default_factory=dict)


def _list(value: Any) -> list[Any]:
    return json.loads(value) if isinstance(value, str) else list(value or [])


def team_outcome(name: str, outcomes: list[str]) -> str | None:
    """The one moneyline outcome naming this team: its full name ("Boston Celtics", as
    NBA and NHL moneylines list them) or, failing that, the end of it ("Bills" for
    "Buffalo Bills", as NFL moneylines list them). None when it isn't exactly one."""
    if outcomes.count(name) == 1:
        return name
    tail = [o for o in outcomes if o and name.lower().endswith(" " + o.lower())]
    return tail[0] if len(tail) == 1 else None


def poly_game(event: dict[str, Any]) -> PolyGame | None:
    """The event's moneyline as abbreviation -> token, or None if it isn't a two-team game."""
    moneylines = [m for m in event.get("markets") or [] if m.get("sportsMarketType") == "moneyline"]
    teams = event.get("teams") or []
    if len(moneylines) != 1 or len(teams) != 2:
        return None
    market = moneylines[0]
    outcomes = [str(o) for o in _list(market.get("outcomes"))]
    tokens = [str(t) for t in _list(market.get("clobTokenIds"))]
    if len(outcomes) != 2 or len(tokens) != 2:
        return None
    by_abbr, names = {}, {}
    for team in teams:
        abbr = str(team.get("abbreviation") or "").lower()
        name = str(team.get("name") or "")
        outcome = team_outcome(name, outcomes)
        if not abbr or outcome is None:
            return None  # an outcome that isn't exactly one listed team is never guessed
        by_abbr[abbr], names[abbr] = tokens[outcomes.index(outcome)], name
    if len(by_abbr) != 2 or len(set(by_abbr.values())) != 2:
        return None
    day = date.fromisoformat(event["eventDate"]) if event.get("eventDate") else None
    raw_start = event.get("startTime") or market.get("gameStartTime")
    start = datetime.fromisoformat(raw_start.replace("Z", "+00:00").replace(" ", "T")) if raw_start else None
    if start is not None and start.tzinfo is None:
        start = start.replace(tzinfo=ZoneInfo("UTC"))
    if day is None and start is not None:
        day = start.astimezone(EASTERN).date()
    return PolyGame(event.get("slug", ""), day, start, by_abbr, names, market)


@dataclass
class Pair:
    game: KalshiGame
    poly: PolyGame
    legs: dict[str, str]  # Kalshi team market ticker -> Polymarket outcome token for the same team
    evidence: dict[str, Any]


def match(game: KalshiGame, polys: list[PolyGame], aliases: dict[str, str] | None = None,
          pinned: dict[str, Any] | None = None) -> tuple[Pair | None, str]:
    """The one Polymarket game for this Kalshi game, or (None, why)."""
    # A Kalshi event with a tie market still pairs on its two team markets. The tie
    # market is never traded, so a tie (or draw) can lose both legs: a known risk,
    # recorded in the pair's evidence for benchmarking.
    aliases = {k.lower(): v.lower() for k, v in (aliases or {}).items()}
    if pinned:
        poly = next((p for p in polys if p.slug == pinned["slug"]), None)
        if poly is None:
            return None, "pinned_event_not_offered"
        legs = {ticker: poly.tokens[abbr.lower()] for ticker, abbr in pinned["legs"].items()
                if abbr.lower() in poly.tokens}
        if len(legs) != 2:
            return None, "pinned_teams_not_offered"
        return Pair(game, poly, legs, {"method": "pinned", "slug": poly.slug}), "ok"
    wanted: dict[str, dict[str, Any]] = {}
    for team in game.teams:
        abbr = team["abbr"].lower()
        wanted[aliases.get(abbr) or abbr] = team
    found = []
    for poly in polys:
        if poly.day != game.day or set(poly.tokens) != set(wanted):
            continue
        if game.start and poly.start and abs(poly.start - game.start) > START_TOLERANCE:
            continue
        legs = {team["market"]["ticker"]: poly.tokens[abbr] for abbr, team in wanted.items()}
        found.append(Pair(game, poly, legs, {
            "method": "abbreviations", "slug": poly.slug, "day": str(poly.day),
            "kalshi": [(t["name"], t["abbr"]) for t in game.teams],
            "polymarket": [(poly.names[a], a) for a in sorted(poly.tokens)],
            "start": poly.start.isoformat() if poly.start else None,
            "kalshi_tie_market": game.tie is not None}))
    if len(found) == 1:
        return found[0], "ok"
    return None, "ambiguous" if found else "unmatched"


def load_pairs(path: str = PAIRS) -> dict[str, Any]:
    """{"pairs": {kalshi_event: {"slug": polymarket_slug, "legs": {kalshi_ticker: polymarket_abbr}}},
    "blocked": [kalshi_event]}"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    return {"pairs": data.get("pairs") or {}, "blocked": set(data.get("blocked") or [])}
