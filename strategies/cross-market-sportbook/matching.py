"""Pairs a Kalshi game event with the DraftKings event for the same game.

Kalshi names each team market by city ("Boston", "Los Angeles L") with the team's
abbreviation as the ticker suffix (KXNBAGAME-26OCT27BKNBOS-BOS), and dates the
event by its local (Eastern) game date, plus the start time for baseball
(KXMLBGAME-26OCT272000MILTB). DraftKings names teams in full ("Boston Celtics") or
by abbreviation and nickname ("BOS Celtics"), with home and away roles and a UTC
start time.

A pair needs the same Eastern date (and start within 90 minutes when Kalshi gives
a time), and exactly one way to assign Kalshi's team markets to DraftKings' home and
away selections. Anything else is unmatched or ambiguous and is never traded.
pairs.json can pin a pair or block an event after reading both venues' rules.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP",
                                      "OCT", "NOV", "DEC"), 1)}
EVENT_DATE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})(\d{4})?[A-Z]")
ALIASES = {"la": ["los", "angeles"], "ny": ["new", "york"], "saint": ["st"], "sf": ["san", "francisco"],
           "tb": ["tampa", "bay"], "kc": ["kansas", "city"], "gs": ["golden", "state"], "sd": ["san", "diego"],
           "okc": ["oklahoma", "city"], "lv": ["las", "vegas"], "sj": ["san", "jose"]}
TIE_NAMES = {"tie", "draw"}
START_TOLERANCE = timedelta(minutes=90)
PAIRS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pairs.json")


def tokens(name: str) -> list[str]:
    out = []
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()  # "Montréal" -> "Montreal"
    for raw in re.findall(r"[a-z0-9]+", plain.lower().replace("&", " ")):
        out.extend(ALIASES.get(raw, [raw]))
    return out


def name_matches(kalshi_name: str, abbreviation: str, book_name: str) -> bool:
    """Does Kalshi's team (city name and ticker abbreviation) name this DraftKings team?

    The city's words must open the DraftKings name in order. Each word may equal a
    DraftKings word or be a prefix of it ("L" for "Lakers"), or be the initials of
    the following words ("WS" for "White Sox"). Failing that, the abbreviation must
    equal DraftKings' first word ("BOS" for "BOS Celtics"). "Columbus" never matches
    "COL": a mismatch leaves the game unpaired, which is safe; pairs.json can pin it.
    """
    city, book = tokens(kalshi_name), tokens(book_name)
    j = 0
    for word in city:
        if j < len(book) and book[j].startswith(word):
            j += 1
            continue
        span = book[j:j + len(word)]
        if len(word) >= 2 and len(span) == len(word) and "".join(w[0] for w in span) == word:
            j += len(word)
            continue
        break
    else:
        return bool(city)
    return bool(book) and abbreviation.lower() == book[0]


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
class Pair:
    game: KalshiGame
    book_event: str
    roles: dict[str, str]  # Kalshi market ticker -> home / away / draw
    selections: dict[str, Any]  # role -> Selection
    evidence: dict[str, Any]


def _assign(game: KalshiGame, home: str, away: str) -> dict[str, str] | None:
    a, b = game.teams
    straight = name_matches(a["name"], a["abbr"], home) and name_matches(b["name"], b["abbr"], away)
    swapped = name_matches(a["name"], a["abbr"], away) and name_matches(b["name"], b["abbr"], home)
    if straight == swapped:
        return None  # neither, or both: never guess
    first, second = ("home", "away") if straight else ("away", "home")
    return {a["market"]["ticker"]: first, b["market"]["ticker"]: second}


def match(game: KalshiGame, selections: list[Any], draw: bool,
          pinned: dict[str, Any] | None = None) -> tuple[Pair | None, str]:
    """The one DraftKings event for this Kalshi game, or (None, why)."""
    by_event: dict[str, dict[str, Any]] = {}
    for s in selections:
        by_event.setdefault(s.event, {})[s.role] = s
    if (game.tie is not None) != draw:
        return None, "outcome_set_mismatch"
    need = {"home", "away"} | ({"draw"} if draw else set())
    if pinned:
        roles = by_event.get(str(pinned["event"]))
        if not roles or set(roles) != need:
            return None, "pinned_event_not_offered"
        mapping = dict(pinned["roles"])
        return Pair(game, str(pinned["event"]), mapping, roles, {"method": "pinned"}), "ok"
    found = []
    for event, roles in by_event.items():
        if set(roles) != need:
            continue
        any_selection = next(iter(roles.values()))
        start = datetime.fromisoformat(any_selection.start).astimezone(EASTERN)
        if start.date() != game.day or (game.start and abs(start - game.start) > START_TOLERANCE):
            continue
        mapping = _assign(game, any_selection.home, any_selection.away)
        if mapping is None:
            continue
        if game.tie is not None:
            mapping[game.tie["ticker"]] = "draw"
        found.append(Pair(game, event, mapping, roles, {
            "method": "names", "kalshi": [(t["name"], t["abbr"]) for t in game.teams],
            "book": {"home": any_selection.home, "away": any_selection.away, "start": any_selection.start}}))
    if len(found) == 1:
        return found[0], "ok"
    return None, "ambiguous" if found else "unmatched"


def load_pairs(path: str = PAIRS) -> dict[str, Any]:
    """{"pairs": {kalshi_event: {"event": book_event, "roles": {ticker: role}}}, "blocked": [kalshi_event]}"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    return {"pairs": data.get("pairs") or {}, "blocked": set(data.get("blocked") or [])}
