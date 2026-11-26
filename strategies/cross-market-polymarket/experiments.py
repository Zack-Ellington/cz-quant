"""One prepared pair and the funded venue portfolios used by a strategy run."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from czquant.accounting import venue_of


@dataclass(frozen=True)
class Outcome:
    kalshi: str
    kalshi_side: str
    polymarket: str
    polymarket_side: str


@dataclass(frozen=True)
class SelectedPair:
    venue: str
    kalshi_event: str
    kalshi_series: str
    polymarket_event: str
    polymarket_market: str  # US slug or international market ID
    resolves_at: str  # timezone-aware estimate prepared before the run
    outcomes: tuple[Outcome, ...]
    evidence: dict[str, Any] = field(default_factory=dict)

    def contracts(self, venue: str) -> set[tuple[str, str]]:
        return {(o.kalshi, o.kalshi_side) if venue == "kalshi" else
                (o.polymarket, o.polymarket_side) for o in self.outcomes}

    def validate(self) -> None:
        if self.venue not in ("polymarket", "polymarket_us"):
            raise ValueError("pair venue must be polymarket or polymarket_us")
        if datetime.fromisoformat(self.resolves_at).tzinfo is None:
            raise ValueError("resolves_at must include a timezone")
        if len(self.outcomes) != 2:
            raise ValueError("a pair needs exactly two complementary outcomes")
        for outcome in self.outcomes:
            if (venue_of(outcome.kalshi) != "kalshi" or venue_of(outcome.polymarket) != self.venue
                    or outcome.kalshi_side not in ("YES", "NO")
                    or outcome.polymarket_side not in ("YES", "NO")):
                raise ValueError("outcome venue or contract side does not match the pair")
        kalshi = self.contracts("kalshi")
        # A sports pair has two team YES markets; a question has YES/NO on one market.
        if not (len(kalshi) == 2 and (len({m for m, _ in kalshi}) == 1 or {s for _, s in kalshi} == {"YES"})):
            raise ValueError("Kalshi outcomes must be two team YES markets or one binary question")
        if self.venue == "polymarket_us":
            if self.contracts(self.venue) != {("PMUS:" + self.polymarket_market, "YES"),
                                               ("PMUS:" + self.polymarket_market, "NO")}:
                raise ValueError("US outcomes must be YES and NO on the selected market")
        elif (len(self.contracts(self.venue)) != 2
              or {o.polymarket_side for o in self.outcomes} != {"YES"}):
            raise ValueError("international outcomes must be two distinct YES tokens")


@dataclass(frozen=True)
class Experiment:
    id: str
    portfolio: dict[str, str]  # venue -> pre-created cash pool ID
    mode: str
    pair: SelectedPair

    def validate(self) -> None:
        self.pair.validate()
        if self.mode not in ("paper", "live") or (self.mode == "live" and self.pair.venue != "polymarket_us"):
            raise ValueError("live execution requires Polymarket US; otherwise select paper")
        if set(self.portfolio) != {"kalshi", self.pair.venue} or len(set(self.portfolio.values())) != 2:
            raise ValueError("portfolio must select one distinct cash pool for each venue")
        if not self.id or not all(self.portfolio.values()):
            raise ValueError("experiment and cash pool IDs are required")
