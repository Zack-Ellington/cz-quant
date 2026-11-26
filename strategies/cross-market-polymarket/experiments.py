"""Explicit, strategy-local ownership and outcome mapping for one experiment."""

from __future__ import annotations

from dataclasses import dataclass

from czquant.accounting import venue_of


@dataclass(frozen=True)
class Outcome:
    kalshi: str
    kalshi_side: str
    polymarket: str
    polymarket_side: str


@dataclass(frozen=True)
class SelectedPair:
    league: str  # leagues.json key, or "markets" for a reviewed binary question
    kalshi_event: str
    polymarket_event: str
    polymarket_market: str  # international market ID or US market slug
    outcomes: tuple[Outcome, ...]
    invert: bool = False  # reviewed international binary questions only


@dataclass(frozen=True)
class Experiment:
    id: str
    kalshi_pool: str
    polymarket_pool: str
    venue: str
    mode: str
    pair: SelectedPair

    def validate(self) -> None:
        if not all(isinstance(x, str) and x.strip() for x in (
            self.id, self.kalshi_pool, self.polymarket_pool, self.pair.league,
            self.pair.kalshi_event, self.pair.polymarket_event, self.pair.polymarket_market,
        )):
            raise ValueError("experiment, pool and pair identifiers must be nonempty strings")
        if self.kalshi_pool == self.polymarket_pool:
            raise ValueError("an experiment needs two distinct venue pools")
        if self.venue not in ("polymarket", "polymarket_us") or self.mode not in ("paper", "live"):
            raise ValueError("invalid experiment venue or mode")
        if self.mode == "live" and self.venue != "polymarket_us":
            raise ValueError("live execution requires Polymarket US")
        if len(self.pair.outcomes) != 2:
            raise ValueError("select exactly two complementary outcomes of one pair")
        for outcome in self.pair.outcomes:
            if (not outcome.kalshi.startswith(self.pair.kalshi_event + "-")
                    or venue_of(outcome.kalshi) != "kalshi"
                    or venue_of(outcome.polymarket) != self.venue
                    or outcome.kalshi_side not in ("YES", "NO")
                    or outcome.polymarket_side not in ("YES", "NO")):
                raise ValueError("outcome identifiers, venues or sides do not match the pair")
        if self.venue == "polymarket_us":
            if (self.pair.league == "markets"
                    or {o.polymarket for o in self.pair.outcomes} != {"PMUS:" + self.pair.polymarket_market}
                    or {o.polymarket_side for o in self.pair.outcomes} != {"YES", "NO"}):
                raise ValueError("US games need YES and NO on the selected market slug")
        elif (len({o.polymarket for o in self.pair.outcomes}) != 2
              or {o.polymarket_side for o in self.pair.outcomes} != {"YES"}):
            raise ValueError("international outcomes need two distinct YES token IDs")
        if self.pair.league == "markets":
            if (len({o.kalshi for o in self.pair.outcomes}) != 1
                    or {o.kalshi_side for o in self.pair.outcomes} != {"YES", "NO"}):
                raise ValueError("binary questions need YES and NO on one Kalshi market")
        elif (len({o.kalshi for o in self.pair.outcomes}) != 2
              or {o.kalshi_side for o in self.pair.outcomes} != {"YES"}):
            raise ValueError("sports games need two distinct Kalshi team YES markets")

    def contracts(self, venue: str) -> set[tuple[str, str]]:
        return {(o.kalshi, o.kalshi_side) if venue == "kalshi" else
                (o.polymarket, o.polymarket_side) for o in self.pair.outcomes}


def in_scope(markets: list[str] | None, ticker: str) -> bool:
    """Pool scopes use exact identifiers or a trailing prefix wildcard."""
    return not markets or any(ticker.startswith(m[:-1]) if m.endswith("*") else ticker == m for m in markets)
