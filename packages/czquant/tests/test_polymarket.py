"""Polymarket as a paper venue: fees, market status, book layout, fills, routing, settlement."""
import json
from decimal import Decimal as D

import pytest

from czquant import Broker, Store
from czquant.book import levels
from czquant.monitor import market_pass, polymarket_pass
from czquant.polymarket import Polymarket, PolymarketPaper, fee, kalshi_layout, normalize_market, ticker

CELTICS, KINGS = "1152", "4341"


def gamma_market(closed=False, resolved=False, prices=("0.8", "0.2"), fees=True, accepting=True):
    """Celtics vs. Kings as Gamma lists it: list fields are JSON text."""
    return {"id": "m1", "question": "Celtics vs. Kings", "slug": "nba-bos-sac-2026-11-21", "conditionId": "0xc",
            "outcomes": json.dumps(["Celtics", "Kings"]), "outcomePrices": json.dumps(list(prices)),
            "clobTokenIds": json.dumps([CELTICS, KINGS]), "closed": closed, "acceptingOrders": accepting,
            "umaResolutionStatus": "resolved" if resolved else None, "feesEnabled": fees,
            "feeType": "sports_fees_v3", "feeSchedule": {"exponent": 1, "rate": 0.05, "takerOnly": True},
            "negRisk": False, "gameStartTime": "2026-11-22 03:00:00+00"}


def clob_book(token, bids, asks):
    """CLOB order: bids ascending and asks descending, best last."""
    return {"asset_id": token, "bids": [{"price": p, "size": s} for p, s in bids],
            "asks": [{"price": p, "size": s} for p, s in asks], "tick_size": "0.01", "min_order_size": "5"}


class Client(Polymarket):
    """The real client over canned Gamma and CLOB responses."""

    def __init__(self, market=None, books=None):
        super().__init__(min_interval=0)
        self.canned_market = market or gamma_market()
        self.canned_books = books or {CELTICS: clob_book(CELTICS, [("0.78", "100")], [("0.82", "30"), ("0.81", "20")])}

    def _request(self, method, url, **kwargs):
        if url.endswith("/markets"):
            return [self.canned_market]
        if url.endswith("/book"):
            return self.canned_books[kwargs["params"]["token_id"]]
        if url.endswith("/books"):
            return [self.canned_books[row["token_id"]] for row in kwargs["json"]]
        raise AssertionError(url)


def test_fee_is_the_documented_formula_rounded_to_five_decimals():
    crypto = {"fees_enabled": True, "rate": "0.07", "exponent": 1}
    assert fee([(D("0.50"), D("100"))], crypto) == D("1.75")  # the docs' example
    assert fee([(D("0.30"), D("100"))], crypto) == D("1.47")
    assert fee([(D("0.81"), D("20")), (D("0.82"), D("10"))], {"fees_enabled": True, "rate": "0.05"}) == D("0.2277")
    assert fee([(D("0.5"), D("100"))], {"fees_enabled": False, "rate": "0.07"}) == 0


def test_market_status_follows_trading_and_resolution():
    assert normalize_market(gamma_market(), CELTICS)["status"] == "active"
    assert normalize_market(gamma_market(accepting=False), CELTICS)["status"] == "closed"
    assert normalize_market(gamma_market(closed=True), CELTICS)["status"] == "closed"  # awaiting the result
    won = normalize_market(gamma_market(closed=True, resolved=True, prices=("1", "0")), CELTICS)
    lost = normalize_market(gamma_market(closed=True, resolved=True, prices=("1", "0")), KINGS)
    assert (won["status"], won["settlement_value_dollars"], won["outcome"]) == ("finalized", "1", "Celtics")
    assert (lost["settlement_value_dollars"], lost["outcome"]) == ("0", "Kings")
    with pytest.raises(ValueError):
        normalize_market(gamma_market(), "999")


def test_books_use_the_kalshi_layout_so_shared_tools_read_them():
    book = levels(kalshi_layout(clob_book(CELTICS, [("0.77", "5"), ("0.78", "100")], [("0.82", "30"), ("0.81", "20")])))
    assert book["yes"][0] == (D("0.78"), D("100"))  # best bid
    assert book["no"][0] == (D("0.19"), D("20"))  # best ask 0.81, as a NO bid at 0.19


def test_paper_fills_walk_the_asks_and_charge_the_taker_fee(tmp_path):
    store = Store(tmp_path)
    store.create("p", "100")
    broker = Broker(store, "p", PolymarketPaper(Client(), latency=0))
    result = broker.submit({"ticker": ticker(CELTICS), "side": "bid", "count": "30", "price": "0.82",
                            "time_in_force": "immediate_or_cancel"}, {"trade_id": "t"})
    assert result["status"] == "filled" and D(result["fee"]) == D("0.2277")
    state = broker.state()
    assert state.cash == D("100") - (D("0.81") * 20 + D("0.82") * 10) - D("0.2277")
    # The 30 contracts taken are gone from this portfolio's view of the book.
    again = broker.submit({"ticker": ticker(CELTICS), "side": "bid", "count": "30", "price": "0.82",
                           "time_in_force": "immediate_or_cancel"}, {})
    assert again["filled"] == "20"  # 0.81 is used up; 20 of the 30 at 0.82 remain


def test_closed_markets_do_not_fill(tmp_path):
    store = Store(tmp_path)
    store.create("p", "100")
    broker = Broker(store, "p", PolymarketPaper(Client(gamma_market(accepting=False)), latency=0))
    result = broker.submit({"ticker": ticker(CELTICS), "side": "bid", "count": "5", "price": "0.9",
                            "time_in_force": "immediate_or_cancel"}, {})
    assert result["status"] == "canceled" and result["filled"] == "0"


def test_separate_brokers_send_each_ticker_to_its_venue(tmp_path):
    seen = []

    class Recorder:
        policy = "kalshi-fixture"

        def create_order(self, order, broker):
            seen.append(order["ticker"])
            return {"status": "canceled", "filled": "0"}

    store = Store(tmp_path)
    store.create("p", "100")
    broker = Broker(store, "p", Recorder())
    poly_broker = Broker(store, "p", PolymarketPaper(Client(), latency=0))
    broker.submit({"ticker": "KXNBAGAME-26NOV21BOSSAC-BOS", "side": "bid", "count": "1", "price": "0.5"}, {})
    filled = poly_broker.submit({"ticker": ticker(CELTICS), "side": "bid", "count": "1", "price": "0.9"}, {})
    assert seen == ["KXNBAGAME-26NOV21BOSSAC-BOS"] and filled["status"] == "filled"
    with pytest.raises(ValueError, match="kalshi broker"):
        broker.submit({"ticker": ticker(CELTICS), "side": "bid", "count": "1", "price": "0.9"})


def test_the_monitor_marks_tokens_and_settles_them_once_final(tmp_path):
    store = Store(tmp_path)
    store.create("p", "100")
    client = Client()
    Broker(store, "p", PolymarketPaper(client, latency=0)).submit(
        {"ticker": ticker(CELTICS), "side": "bid", "count": "10", "price": "0.82"}, {})
    status = polymarket_pass(store, client)
    assert status["status"] == "healthy" and status["markets"] == 1
    mark = [e for e in store.events() if e["type"] == "market"][-1]["data"]
    assert mark["market"]["status"] == "active" and levels(mark["book"])["yes"][0][0] == D("0.78")
    client.canned_market = gamma_market(closed=True, resolved=True, prices=("1", "0"))
    status = polymarket_pass(store, client)
    state = Broker(store, "p").state()
    assert status["settled_lots"] == 1 and not state.lots and state.cash > 100
    assert polymarket_pass(store, client)["markets"] == 0  # nothing left to watch


def test_the_kalshi_monitor_leaves_polymarket_tokens_alone(tmp_path):
    store = Store(tmp_path)
    store.create("p", "100")
    Broker(store, "p", PolymarketPaper(Client(), latency=0)).submit(
        {"ticker": ticker(CELTICS), "side": "bid", "count": "10", "price": "0.82"}, {})

    class Kalshi:
        def get(self, path, params=None):
            return {"markets": []}

        def market(self, ticker_):
            raise AssertionError("a Polymarket token reached the Kalshi monitor")

    assert market_pass(store, Kalshi())["markets"] == 0
