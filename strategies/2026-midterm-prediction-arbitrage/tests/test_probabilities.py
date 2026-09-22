"""Price extraction from order books: leg normalization and the fallbacks."""

from __future__ import annotations

import pytest

from strategy.probabilities import extract_probabilities, read_combo_prices
from strategy.races import Race, discover_races


def book(yes=None, no=None):
    """A normalized order book with a single level per side (price, size)."""
    return {
        "yes": [[yes, 100.0]] if yes is not None else [],
        "no": [[no, 100.0]] if no is not None else [],
    }


class StubClient:
    """Serves canned order books by ticker; unknown tickers return ``None``."""

    def __init__(self, books: dict[str, dict]):
        self._books = books

    def fetch_event(self, ticker: str):
        return None

    def fetch_orderbook(self, ticker: str):
        return self._books.get(ticker)


def _race(event="SENATEXX-26"):
    return Race(
        race_id="senate:XX",
        chamber="senate",
        label="XX Senate",
        event=event,
        d_ticker=f"{event}-D" if event else None,
        r_ticker=f"{event}-R" if event else None,
        prior=0.30,
    )


def test_legs_are_normalized_against_each_other():
    # D book mid = (0.54 + (1-0.44))/2 = 0.55; R book mid = (0.40 + (1-0.58))/2
    # = 0.41. The two legs sum to 0.96, so P(D) = 0.55 / 0.96, not 0.55.
    client = StubClient(
        {
            "SENATEXX-26-D": book(yes=0.54, no=0.44),
            "SENATEXX-26-R": book(yes=0.40, no=0.58),
        }
    )
    probs = extract_probabilities([_race()], client)
    assert probs["senate:XX"] == pytest.approx(0.55 / (0.55 + 0.41), abs=1e-9)


def test_yes_ask_from_no_side():
    # Only the NO book has orders: yes_ask = 1 - best_no, and with no yes bids
    # that ask is the price. D no-best 0.30 -> 0.70; R no-best 0.72 -> 0.28.
    client = StubClient(
        {
            "SENATEXX-26-D": book(no=0.30),
            "SENATEXX-26-R": book(no=0.72),
        }
    )
    probs = extract_probabilities([_race()], client)
    assert probs["senate:XX"] == pytest.approx(0.70 / (0.70 + 0.28), abs=1e-9)


def test_empty_book_falls_back_to_prior():
    client = StubClient({"SENATEXX-26-D": book(), "SENATEXX-26-R": book()})
    assert extract_probabilities([_race()], client)["senate:XX"] == pytest.approx(0.30)


def test_absent_market_falls_back_to_prior():
    assert extract_probabilities([_race()], StubClient({}))["senate:XX"] == pytest.approx(0.30)


def test_no_market_race_uses_prior_without_fetching():
    # A race with event=None (Louisiana, safe seats) never calls the client.
    class Boom:
        def fetch_event(self, ticker):
            raise AssertionError("should not fetch")

        def fetch_orderbook(self, ticker):
            raise AssertionError("should not fetch")

    probs = extract_probabilities([_race(event=None)], Boom())
    assert probs["senate:XX"] == pytest.approx(0.30)


def test_single_sided_book_uses_that_side():
    # Only a YES bid exists on the D leg; R leg absent -> P(D) = that bid.
    client = StubClient({"SENATEXX-26-D": book(yes=0.63)})
    assert extract_probabilities([_race()], client)["senate:XX"] == pytest.approx(0.63)


def test_every_race_gets_a_probability(fixture_client):
    races = discover_races()
    probs = extract_probabilities(races, fixture_client)
    assert set(probs) == {r.race_id for r in races}
    assert all(0.0 <= p <= 1.0 for p in probs.values())


def test_prices_come_from_the_live_book_not_priors(fixture_client):
    # Georgia's book prices Ossoff far above the seat's 0.55 prior.
    probs = extract_probabilities(discover_races(), fixture_client)
    assert probs["senate:GA"] > 0.85


def test_louisiana_uses_its_prior(fixture_client):
    probs = extract_probabilities(discover_races(), fixture_client)
    assert probs["senate:LA"] == pytest.approx(0.03)


def test_combo_prices_read_from_snapshot(fixture_client):
    prices = read_combo_prices(fixture_client)
    assert set(prices) == {"DD", "DR", "RD", "RR"}
    assert all(0.0 <= p <= 1.0 for p in prices.values())
    # The four mutually-exclusive legs price close to a full book.
    assert sum(prices.values()) == pytest.approx(1.0, abs=0.05)
