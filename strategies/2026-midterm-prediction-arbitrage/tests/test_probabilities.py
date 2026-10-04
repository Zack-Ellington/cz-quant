"""Race quotes: every leg is read, unlisted legs and missing events fall back."""

from __future__ import annotations

import pytest

from strategy.probabilities import (
    apply_estimator,
    caucus_probs,
    extract_probabilities,
    load_race_quotes,
)
from strategy.races import Race, discover_races
from strategy.snapshot import SnapshotClient
from tests.conftest import committed_snapshot


class StubClient:
    def __init__(self, events=None, books=None):
        self.events, self.books, self.calls = events or {}, books or {}, []

    def fetch_event(self, ticker):
        self.calls.append(ticker)
        return self.events.get(ticker)

    def fetch_orderbook(self, ticker):
        self.calls.append(ticker)
        return self.books.get(ticker)

    def fetch_series(self, ticker):
        return None


def _race(event="SENATENE-26", prior=0.1):
    return Race("senate:NE", "senate", "NE Senate", event,
                f"{event}-D" if event else None, f"{event}-R" if event else None, prior)


def _event(*suffixes, ticker="SENATENE-26"):
    return {"event_ticker": ticker, "markets": [
        {"ticker": f"{ticker}-{s}", "last_price": None, "volume": 0.0} for s in suffixes
    ]}


def _book(bid, ask):
    return {"yes": [[bid, 1.0]], "no": [[1 - ask, 1.0]]}


def test_every_leg_is_read_including_independents():
    client = StubClient(
        {"SENATENE-26": _event("R", "D", "DOSB")},
        {"SENATENE-26-D": _book(0.00, 0.01), "SENATENE-26-R": _book(0.68, 0.69),
         "SENATENE-26-DOSB": _book(0.31, 0.32)},
    )
    quotes = load_race_quotes([_race()], client)
    d, r, others = quotes["senate:NE"]
    assert d.ticker.endswith("-D") and r.ticker.endswith("-R")
    assert [o.ticker for o in others] == ["SENATENE-26-DOSB"]
    p = apply_estimator([_race()], quotes)["senate:NE"]
    assert p.ind == pytest.approx(0.315 / (0.005 + 0.685 + 0.315))


def test_leg_the_event_does_not_list_is_none():
    client = StubClient({"SENATENE-26": _event("R")}, {"SENATENE-26-R": _book(0.9, 0.92)})
    d, r, _ = load_race_quotes([_race()], client)["senate:NE"]
    assert d is None and r is not None
    assert "SENATENE-26-D" not in client.calls  # no book fetch for an unlisted leg


def test_missing_event_falls_back_to_the_prior():
    probs = extract_probabilities([_race(prior=0.2)], StubClient())
    assert probs["senate:NE"] == pytest.approx(0.2)


def test_race_without_a_market_never_calls_the_client():
    client = StubClient()
    probs = extract_probabilities([_race(event=None, prior=0.03)], client)
    assert probs["senate:NE"] == pytest.approx(0.03)
    assert client.calls == []


def test_caucus_share_moves_only_races_with_independents():
    from strategy.estimators import RaceProb

    race_probs = {"senate:NE": RaceProb(0.0, 0.3), "senate:GA": RaceProb(0.9, 0.0)}
    assert caucus_probs(race_probs, 0.0) == {"senate:NE": 0.0, "senate:GA": 0.9}
    assert caucus_probs(race_probs, 1.0) == {"senate:NE": pytest.approx(0.3), "senate:GA": 0.9}


def test_kentucky_priced_from_the_la_ticker_louisiana_from_its_prior():
    probs = extract_probabilities(discover_races(), SnapshotClient(committed_snapshot()))
    assert probs["senate:LA"] == pytest.approx(0.03)  # no market: prior
    assert probs["senate:KY"] != pytest.approx(0.08)  # SENATELA-26 is priced
    assert all(0.0 <= p <= 1.0 for p in probs.values())
