"""Reviewed non-sports pairs: one yes/no question on each venue, one answer bought on each."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from czquant import Broker, Store
from czquant.polymarket import Polymarket

from find_pairs import candidates, numbers, words
from markets import Listing, load_markets, resolve
from trader import Config, Engine

END = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
LISTING = Listing("Fed cut", "KXFED-26DEC-CUT", "fed-cut-december")


def poly_market(outcomes=("Yes", "No")):
    return {"id": "m9", "slug": "fed-cut-december", "question": "Fed cut in December?", "endDate": END,
            "outcomes": json.dumps(list(outcomes)), "clobTokenIds": json.dumps(["tok-yes", "tok-no"]),
            "outcomePrices": json.dumps(["0.5", "0.5"]), "closed": False, "acceptingOrders": True,
            "feesEnabled": False, "feeSchedule": {"rate": 0, "exponent": 1, "takerOnly": True}}


KALSHI_MARKET = {"ticker": "KXFED-26DEC-CUT", "event_ticker": "KXFED-26DEC", "status": "active",
                 "expected_expiration_time": END}


def test_resolve_maps_answers_and_inverts():
    pair, why = resolve(LISTING, KALSHI_MARKET, poly_market())
    assert why == "ok" and pair.tokens == {"yes": "tok-yes", "no": "tok-no"}
    inverted, _ = resolve(Listing("x", "KXFED-26DEC-CUT", "s", invert=True), KALSHI_MARKET, poly_market())
    assert inverted.tokens == {"yes": "tok-no", "no": "tok-yes"}
    assert resolve(LISTING, KALSHI_MARKET, poly_market(("A", "B")))[1] == "polymarket_not_yes_no"
    assert resolve(LISTING, {**KALSHI_MARKET, "status": "closed"}, poly_market())[1] == "kalshi_not_trading"


def test_disabled_listings_are_skipped(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"markets": [{"kalshi": "A", "polymarket": "a"},
                                            {"kalshi": "B", "polymarket": "b", "enabled": False}]}))
    assert [x.kalshi for x in load_markets(str(path), None)] == ["A"]


class FakeKalshi:
    """Kalshi: YES asked at 0.62, bid at 0.58 (so NO is asked at 0.42)."""

    def __init__(self):
        self.yes_bid, self.yes_ask = D("0.58"), D("0.62")

    def raw(self):
        return {"orderbook_fp": {"yes_dollars": [[str(self.yes_bid), "100"]],
                                 "no_dollars": [[str(1 - self.yes_ask), "100"]]}}

    def get(self, path, params=None):
        if path.startswith("/series/"):
            return {"series": {"fee_type": "quadratic", "fee_multiplier": 1}}
        if path.startswith("/markets/orderbooks"):
            return {"orderbooks": [{"ticker": t, **self.raw()} for _, t in params]}
        if path.startswith("/markets/"):
            return {"market": KALSHI_MARKET}
        if path.startswith("/events/"):
            return {"event": {"series_ticker": "KXFED"}}
        raise AssertionError(path)

    def market(self, ticker):
        return {"ticker": ticker, "status": "active", "series_ticker": "KXFED"}

    def book(self, ticker):
        return self.raw()


class FakePoly(Polymarket):
    """Polymarket: Yes asked at 0.50, No at 0.52."""

    def __init__(self):
        super().__init__(min_interval=0)
        self.asks = {"tok-yes": "0.50", "tok-no": "0.52"}

    def canned(self, token):
        ask = D(self.asks[token])
        return {"asset_id": token, "bids": [{"price": str(ask - D("0.02")), "size": "100"}],
                "asks": [{"price": str(ask), "size": "100"}]}

    def _request(self, method, url, **kwargs):
        if url.endswith("/markets"):
            return [poly_market()]
        if url.endswith("/books"):
            return [self.canned(row["token_id"]) for row in kwargs["json"]]
        if url.endswith("/book"):
            return self.canned(kwargs["params"]["token_id"])
        raise AssertionError(url)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("CZQUANT_PORTFOLIO", "pm")
    Store(tmp_path).create("pm", "1000")
    return Engine(Config(kalshi_latency=0, polymarket_latency=0, max_event_frac=1.0), FakeKalshi(), FakePoly(),
                  {}, {"pairs": {}, "blocked": set()}, "test", listings=[LISTING])


def test_a_reviewed_pair_buys_kalshi_no_against_polymarket_yes(engine):
    engine.step()  # Kalshi NO 0.42 + Polymarket Yes 0.50 = 0.92 a set
    held = {(lot["market"], lot["side"]): lot["quantity"] for lot in Broker(engine.store, "pm").state().lots.values()}
    assert held == {("KXFED-26DEC-CUT", "no"): 100, ("PM:tok-yes", "yes"): 100}
    record = [e["data"] for e in engine.store.events() if e["data"].get("kind") == "basket_execution"][-1]
    assert record["context"]["match"]["method"] == "reviewed"


def test_no_basket_when_the_venues_agree(engine):
    engine.poly.asks = {"tok-yes": "0.60", "tok-no": "0.41"}  # 0.42 + 0.60 and 0.62 + 0.41: both over $1
    engine.step()
    assert not Broker(engine.store, "pm").state().lots


def test_finder_requires_the_same_numbers():
    k = [{"ticker": "K1", "category": "Economics", "text": "Will CPI rise above 3% in December 2026?",
          "end": END, "yes_ask": "0.4", "no_ask": "0.62", "rules": ""}]
    p = [{"slug": "a", "text": "Will CPI rise above 3% in December 2026?", "end": END, "best_ask": "0.41",
          "best_bid": "0.39", "rules": ""},
         {"slug": "b", "text": "Will CPI rise above 4% in December 2026?", "end": END, "best_ask": "0.1",
          "best_bid": "0.09", "rules": ""}]
    found = candidates(k, p, 0.5, 7)
    assert [c["polymarket"] for c in found] == ["a"]
    assert numbers("above $3,000.50") == {"3000.5"} and words("December decrease") == {"12", "decrease"}


class FakeHttp:
    """Oddpool's documented shapes: bare JSON arrays, paged by offset."""

    SERIES = [{"series_id": "KXFEDDECISION", "exchange": "kalshi", "title": "Fed rate decision", "category": "Economics"},
              {"series_id": "fed-decision", "exchange": "polymarket", "title": "Fed decision", "category": "Economics"},
              {"series_id": "nba-games", "exchange": "polymarket", "title": "NBA games", "category": "Sports"}]
    MARKETS = {"KXFEDDECISION": [{"market_id": "KXFED-26DEC-C25", "exchange": "kalshi", "series_id": "KXFEDDECISION",
                                  "question": "Will the Fed cut rates by 25 bps in December 2026?",
                                  "event_id": "KXFED-26DEC", "event_title": "Fed December", "last_yes_price": "0.6"}],
               "fed-decision": [{"market_id": "0xabc", "exchange": "polymarket", "series_id": "fed-decision",
                                 "question": "Fed cuts rates by 25 bps in December 2026?", "slug": "fed-25-dec",
                                 "last_yes_price": "0.62"},
                                {"market_id": "0xdef", "exchange": "polymarket", "series_id": "fed-decision",
                                 "question": "Fed cuts rates by 50 bps in December 2026?", "slug": "fed-50-dec",
                                 "last_yes_price": "0.1"}]}

    def __init__(self):
        self.calls = []

    def get(self, url, params):
        self.calls.append((url, dict(params)))
        if params["offset"]:
            rows = []
        elif url.endswith("/search/series"):
            rows = self.SERIES
        else:
            rows = self.MARKETS[params["series_id"]]
        return type("R", (), {"status_code": 200, "headers": {}, "raise_for_status": lambda self: None,
                              "json": lambda self, rows=rows: rows})()


class EnrichKalshi:
    def get(self, path, params=None):
        return {"market": {"expected_expiration_time": END, "rules_primary": "If the Fed cuts by 25 bps...",
                           "yes_ask_dollars": "0.61", "no_ask_dollars": "0.41"}}


class EnrichPoly(Polymarket):
    def __init__(self):
        super().__init__(min_interval=0)

    def market_by_slug(self, slug):
        return {**poly_market(), "slug": slug, "bestAsk": "0.62", "bestBid": "0.60"}


def test_oddpool_pairs_series_then_markets_within_them():
    from find_pairs import oddpool_candidates
    from oddpool import Oddpool
    http = FakeHttp()
    found, stats = oddpool_candidates(Oddpool("k", http=http, min_interval=0), ["fed"], 0.5, 0.3, 7,
                                      EnrichKalshi(), EnrichPoly())
    assert stats == {"series": 2, "series_pairs": 1, "markets": 3}  # the sports series is left out
    assert [(c["kalshi"], c["polymarket"]) for c in found] == [("KXFED-26DEC-C25", "fed-25-dec")]  # not 50 bps
    c = found[0]
    assert c["series"]["kalshi"] == "KXFEDDECISION" and c["kalshi_end"] == END and c["rough_cost"] == 1.01
    assert ("https://api.oddpool.com/search/series", {"q": "fed", "status": "active", "limit": 100, "offset": 0}) in http.calls


def test_oddpool_needs_a_key(monkeypatch):
    from oddpool import Oddpool
    monkeypatch.delenv("ODDPOOL_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ODDPOOL_API_KEY"):
        Oddpool.from_env()


def test_discovery_rereads_markets_json(tmp_path, monkeypatch):
    import markets
    path = tmp_path / "markets.json"
    path.write_text(json.dumps({"markets": []}))
    monkeypatch.setattr(markets, "MARKETS", str(path))
    monkeypatch.setattr(markets.load_markets, "__defaults__", (str(path), None))
    monkeypatch.setenv("STRATEGY_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("CZQUANT_PORTFOLIO", "pm")
    Store(tmp_path).create("pm", "1000")
    e = Engine(Config(kalshi_latency=0, polymarket_latency=0), FakeKalshi(), FakePoly(), {},
               {"pairs": {}, "blocked": set()}, "test")
    e.discover()
    assert e.market_pairs == []
    path.write_text(json.dumps({"markets": [{"kalshi": LISTING.kalshi, "polymarket": LISTING.polymarket}]}))
    e.discover()
    assert [p.key for p in e.market_pairs] == [LISTING.kalshi]
    path.write_text('{"markets": [')  # caught mid-save: the last good list stays
    e.discover()
    assert [p.key for p in e.market_pairs] == [LISTING.kalshi]
    path.write_text(json.dumps({"markets": []}))
    e.discover()
    assert e.market_pairs == []
    kinds = [x["data"].get("kind") for x in e.store.events()]
    assert kinds.count("markets_reloaded") == 2 and "error" in kinds


def test_auto_approve_keeps_the_best_match_once_per_market():
    from find_pairs import auto_approve
    base = {"name": "n", "only_kalshi": [], "only_polymarket": []}
    found = [{**base, "score": 0.9, "kalshi": "K1", "polymarket": "a"},
             {**base, "score": 0.8, "kalshi": "K1", "polymarket": "b"},  # K1 already has a better match
             {**base, "score": 0.7, "kalshi": "K2", "polymarket": "a"},  # a already used
             {**base, "score": 0.65, "kalshi": "K3", "polymarket": "c"},
             {**base, "score": 0.5, "kalshi": "K4", "polymarket": "d"}]  # below the bar
    assert [(x["kalshi"], x["polymarket"]) for x in auto_approve(found, 0.6)] == [("K1", "a"), ("K3", "c")]


def test_reviewed_entries_override_auto_ones(tmp_path):
    reviewed, auto = tmp_path / "m.json", tmp_path / "a.json"
    reviewed.write_text(json.dumps({"markets": [{"kalshi": "K1", "polymarket": "x", "enabled": False},
                                                {"kalshi": "K2", "polymarket": "y"}]}))
    auto.write_text(json.dumps({"markets": [{"kalshi": "K1", "polymarket": "a", "score": 0.7},
                                            {"kalshi": "K2", "polymarket": "b", "score": 0.7},
                                            {"kalshi": "K3", "polymarket": "c", "score": 0.6}]}))
    got = [(x.kalshi, x.polymarket, x.source) for x in load_markets(str(reviewed), str(auto))]
    assert got == [("K2", "y", "reviewed"), ("K3", "c", "auto")]  # K1 is blocked by its disabled review
