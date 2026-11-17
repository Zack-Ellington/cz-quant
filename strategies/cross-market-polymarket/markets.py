"""Reviewed pairs of non-sports questions: one Kalshi market against one Polymarket market.

Sports games pair themselves (matching.py), because both venues name the teams.
Elections, economics, policy and the rest don't: the same question is worded
differently on each venue, and the fine print (deadlines, sources, what counts) can
differ. So these pairs are listed by hand in markets.json, usually from candidates
that find_pairs.py suggests, after reading both venues' rules:

    {"markets": [{
        "name": "Fed cuts in December",
        "kalshi": "KXFEDDECISION-26DEC-C25",
        "polymarket": "fed-decreases-interest-rates-by-25-bps-after-december-2026-meeting",
        "invert": false,
        "enabled": true,
        "rules": "Both settle on the FOMC statement of Dec 9; reviewed 2026-11-08"}]}

`invert` is true when Polymarket's Yes is Kalshi's No (the questions are complements).
Each pair is one question with two answers, so a basket is one answer on each venue:

    Kalshi YES + Polymarket No     (Polymarket Yes when inverted)
    Kalshi NO  + Polymarket Yes    (Polymarket No when inverted)

and pays $1 however the question resolves, if both venues resolve it the same way.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

MARKETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "markets.json")
# Written by `find_pairs.py --auto-approve`: unreviewed pairs, for paper benchmarking.
AUTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "markets.auto.json")


@dataclass(frozen=True)
class Listing:
    name: str
    kalshi: str
    polymarket: str  # Polymarket market slug
    invert: bool = False
    rules: str = ""
    source: str = "reviewed"  # reviewed (markets.json) or auto (markets.auto.json)
    score: float | None = None  # find_pairs' word score, for auto pairs


def _rows(path: str) -> list[dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("markets") or []
    except FileNotFoundError:
        return []


def _listing(row: dict[str, Any], source: str) -> Listing:
    return Listing(row.get("name") or row["kalshi"], row["kalshi"], row["polymarket"], bool(row.get("invert")),
                   row.get("rules", ""), source, row.get("score"))


def load_markets(path: str = MARKETS, auto: str | None = AUTO) -> list[Listing]:
    """Reviewed pairs from markets.json, then auto-approved ones from markets.auto.json.

    A reviewed entry wins over an auto one for the same Kalshi market, and a reviewed
    entry with "enabled": false also keeps that market out of the auto list."""
    reviewed = _rows(path)
    claimed = {row["kalshi"] for row in reviewed}
    out = [_listing(row, "reviewed") for row in reviewed if row.get("enabled", True)]
    if auto:
        out += [_listing(row, "auto") for row in _rows(auto)
                if row.get("enabled", True) and row["kalshi"] not in claimed]
    return out


def _list(value: Any) -> list[Any]:
    return json.loads(value) if isinstance(value, str) else list(value or [])


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


@dataclass
class MarketPair:
    """A listing resolved against both venues: which token answers which way, and when."""
    listing: Listing
    event: str  # the Kalshi event ticker, for the per-event limits
    series: str
    tokens: dict[str, str]  # "yes"/"no" (in Kalshi's terms) -> the Polymarket token with the same answer
    resolves: datetime | None  # the later of the two venues' expected resolution
    poly: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def key(self) -> str:
        return self.listing.kalshi

    def roles(self) -> dict[str, list[tuple[str, str, str]]]:
        """Each answer's ways to buy it: (venue, ticker or token, contract)."""
        return {"yes": [("kalshi", self.listing.kalshi, "YES"), ("polymarket", self.tokens["yes"], "YES")],
                "no": [("kalshi", self.listing.kalshi, "NO"), ("polymarket", self.tokens["no"], "YES")]}


def resolve(listing: Listing, kalshi_market: dict[str, Any], poly_market: dict[str, Any]) -> tuple[MarketPair | None, str]:
    """Check both sides of a listing still describe one open yes/no question."""
    if kalshi_market.get("status") not in ("active", "open"):
        return None, "kalshi_not_trading"
    outcomes = [str(o).lower() for o in _list(poly_market.get("outcomes"))]
    tokens = [str(t) for t in _list(poly_market.get("clobTokenIds"))]
    if sorted(outcomes) != ["no", "yes"] or len(tokens) != 2:
        return None, "polymarket_not_yes_no"
    yes, no = tokens[outcomes.index("yes")], tokens[outcomes.index("no")]
    mapping = {"yes": no, "no": yes} if listing.invert else {"yes": yes, "no": no}
    ends = [t for t in (_time(kalshi_market.get("expected_expiration_time") or kalshi_market.get("close_time")),
                        _time(poly_market.get("endDate"))) if t is not None]
    return MarketPair(listing, kalshi_market.get("event_ticker", listing.kalshi), "", mapping,
                      max(ends) if ends else None, poly_market), "ok"
