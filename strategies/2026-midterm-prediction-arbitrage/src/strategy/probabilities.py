"""Turn Kalshi race markets into a probability for every race.

Each race is a mutually-exclusive Kalshi event with a Democratic leg, a
Republican leg, and sometimes independent candidates. ``load_race_quotes``
reads every leg -- best bid and ask from the order book, last trade and volume
from the event -- and ``apply_estimator`` turns them into a ``RaceProb`` (P(D
nominee wins), P(independent wins)) with the chosen estimator. Loading and
estimating are separate so several estimators can be compared on one set of
quotes without refetching.

``caucus_probs`` then collapses each race to the single number the simulations
use: P(the seat caucuses with Democrats) = P(D) + share * P(independent), where
``share`` is the fraction of independent winners assumed to caucus with
Democrats (fitted in ``calibration``; 0 when not calibrated).

Races with no market (Louisiana, the safe House seats) and legs the event does
not list get no quote; every estimator returns the prior when nothing is quoted.
"""

from __future__ import annotations

from strategy.api import Client
from strategy.estimators import ESTIMATORS, RaceProb
from strategy.markets import Quote, load_quote
from strategy.races import Race

# race_id -> (Democratic leg, Republican leg, independent legs)
RaceQuotes = dict[str, tuple[Quote | None, Quote | None, tuple[Quote, ...]]]


def load_race_quotes(races: list[Race], client: Client) -> RaceQuotes:
    """Every leg's quote for every race.

    One event fetch per race gives the list of legs and their summaries; one
    order-book fetch per listed leg gives the bid and ask.
    """
    quotes: RaceQuotes = {}
    for race in races:
        if not race.has_market:
            quotes[race.race_id] = (None, None, ())
            continue
        event = client.fetch_event(race.event)
        tickers = [m["ticker"] for m in event["markets"]] if event else []
        d = load_quote(client, race.d_ticker, event) if race.d_ticker in tickers else None
        r = load_quote(client, race.r_ticker, event) if race.r_ticker in tickers else None
        others = tuple(
            load_quote(client, t, event) for t in tickers if t not in (race.d_ticker, race.r_ticker)
        )
        quotes[race.race_id] = (d, r, others)
    return quotes


def apply_estimator(
    races: list[Race], quotes: RaceQuotes, estimator: str = "midpoint"
) -> dict[str, RaceProb]:
    """``{race_id: RaceProb}`` using the named estimator."""
    fn = ESTIMATORS[estimator]
    return {r.race_id: fn(*quotes[r.race_id], r.prior) for r in races}


def caucus_probs(race_probs: dict[str, RaceProb], share: float = 0.0) -> dict[str, float]:
    """``{race_id: P(seat caucuses with Democrats)}`` for a caucus ``share``."""
    return {rid: rp.caucus(share) for rid, rp in race_probs.items()}


def extract_probabilities(
    races: list[Race], client: Client, estimator: str = "midpoint", share: float = 0.0
) -> dict[str, float]:
    """Load quotes, apply ``estimator``, and collapse with caucus ``share``."""
    race_probs = apply_estimator(races, load_race_quotes(races, client), estimator)
    return caucus_probs(race_probs, share)
