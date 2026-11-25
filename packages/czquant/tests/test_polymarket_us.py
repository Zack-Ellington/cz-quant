"""Polymarket US: signing, books, fees, orders, the two-venue live portfolio and its monitor."""
import base64
from decimal import Decimal as D

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from czquant import Broker, Store
from czquant.live import KalshiLive
from czquant.monitor import polymarket_us_pass
from czquant.operator import map_live_cross
from czquant.polymarket_us import (PolymarketUS, PolymarketUSLive, fee, kalshi_layout, load_key,
                                   normalize_market, order_body)

SLUG = "aec-nba-bos-sac-2026-11-21"
TICKER = "PMUS:" + SLUG


def seed_file(tmp_path, size=32):
    key = ed25519.Ed25519PrivateKey.generate()
    from cryptography.hazmat.primitives import serialization
    seed = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    raw = seed if size == 32 else seed + key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    path = tmp_path / "pmus.secret"
    path.write_text(base64.b64encode(raw).decode())
    return path, key.public_key()


@pytest.mark.parametrize("size", [32, 64])
def test_requests_are_signed_over_timestamp_method_and_path(tmp_path, size):
    path, public = seed_file(tmp_path, size)
    client = PolymarketUS("key-1", load_key(path))
    headers = client.headers("GET", "/v1/portfolio/positions")
    assert headers["X-PM-Access-Key"] == "key-1"
    public.verify(base64.b64decode(headers["X-PM-Signature"]),
                  (headers["X-PM-Timestamp"] + "GET" + "/v1/portfolio/positions").encode())


def test_a_secret_of_the_wrong_length_is_refused(tmp_path):
    path = tmp_path / "bad"
    path.write_text(base64.b64encode(b"x" * 20).decode())
    with pytest.raises(ValueError, match="20 bytes"):
        load_key(path)


def test_the_book_maps_onto_kalshis_yes_layout():
    raw = {"marketData": {"marketSlug": SLUG, "bids": [{"px": {"value": "0.86"}, "qty": "10"}],
                          "offers": [{"px": {"value": "0.87"}, "qty": "5"}]}}
    book = kalshi_layout(raw)["orderbook_fp"]
    assert book["yes_dollars"] == [["0.86", "10"]]  # YES bids
    assert book["no_dollars"] == [["0.13", "5"]]    # an offer at 0.87 is a NO bid at 0.13


def test_fee_is_the_rounded_exact_total():
    # 0.095 x 10 x 0.5 x 0.5 = 0.2375 -> 0.24 (half-even only matters at exact halves)
    assert fee([(D("0.5"), D("10"))], D("0.095")) == D("0.24")
    assert fee([(D("0.5"), D("2"))], D("0.1")) == D("0.05")    # exactly 0.05
    assert fee([(D("0.5"), D("1"))], D("0.1")) == D("0.02")    # 0.025 rounds half to even


def test_market_status():
    market = {"slug": SLUG, "status": "MARKET_STATUS_OPEN", "active": True, "closed": False, "marketSides": []}
    assert normalize_market(market)["status"] == "active"
    resolved = {**market, "status": "MARKET_STATUS_RESOLVED"}
    final = {"marketData": {"stats": {"settlementPx": {"value": "1"}, "settlementPreliminaryFlag": False}}}
    prelim = {"marketData": {"stats": {"settlementPx": {"value": "1"}, "settlementPreliminaryFlag": True}}}
    assert normalize_market(resolved, final)["settlement_value_dollars"] == "1"
    assert normalize_market(resolved, prelim)["status"] == "closed"


@pytest.mark.parametrize("side,closing,intent", [
    ("bid", D(0), "ORDER_INTENT_BUY_LONG"), ("ask", D(0), "ORDER_INTENT_BUY_SHORT"),
    ("ask", D(5), "ORDER_INTENT_SELL_LONG"), ("bid", D(5), "ORDER_INTENT_SELL_SHORT")])
def test_order_intents_keep_the_yes_price(side, closing, intent):
    body = order_body({"ticker": TICKER, "side": side, "count": "5", "price": "0.40",
                       "time_in_force": "immediate_or_cancel"}, closing)
    assert body["intent"] == intent and body["price"]["value"] == "0.40" and body["marketSlug"] == SLUG
    assert body["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL" and body["synchronousExecution"] is True


def test_an_order_that_would_close_and_open_is_refused():
    with pytest.raises(ValueError, match="split"):
        order_body({"ticker": TICKER, "side": "ask", "count": "5", "price": "0.4"}, D(2))


class FakeUS(PolymarketUS):
    """Records what would be sent; replies with canned executions."""

    def __init__(self, executions=None, state="ORDER_STATE_FILLED", dry_run=False):
        super().__init__("key", ed25519.Ed25519PrivateKey.generate(), min_interval=0, dry_run=dry_run)
        self.sent, self.executions, self.state = [], executions, state
        self.cash, self.held, self.orders, self.market_status = D("500"), {}, {}, "MARKET_STATUS_OPEN"
        self.settlement = None

    def private(self, method, path, params=None, body=None):
        if method == "POST" and path == "/v1/orders":
            self.sent.append(body)
            execs = self.executions if self.executions is not None else [
                {"id": f"e{len(self.sent)}", "lastShares": str(body["quantity"]), "lastPx": {"value": body["price"]["value"]},
                 "commissionNotionalCollected": {"value": "0.05"}, "order": {"state": self.state}}]
            return {"id": f"o{len(self.sent)}", "executions": execs}
        if path.startswith("/v1/order/"):
            return {"order": self.orders[path.rsplit("/", 1)[-1]]}
        if path == "/v1/account/balances":
            return {"balances": [{"currency": "USD", "currentBalance": str(self.cash)}]}
        if path == "/v1/portfolio/positions":
            return {"positions": {k: {"netPositionDecimal": str(v)} for k, v in self.held.items()}, "eof": True}
        if path == "/v1/orders/open":
            return {"orders": []}
        if path == "/v1/portfolio/activities":
            return {"activities": [], "eof": True}
        raise AssertionError(path)

    def public(self, path, params=None):
        if path.startswith("/v1/market/slug/"):
            return {"market": {"slug": SLUG, "status": self.market_status, "marketSides": []}}
        if path.endswith("/book"):
            return {"marketData": {"bids": [], "offers": [], "stats": {
                "settlementPx": {"value": self.settlement}, "settlementPreliminaryFlag": False}}}
        raise AssertionError(path)


class FakeKalshi:
    key_id = "fixture"

    def __init__(self):
        self.sent = []

    def get(self, path, params=None):
        return {"balance_dollars": "300"} if path == "/portfolio/balance" else {}

    def pages(self, path, key, **params):
        return iter([])

    def create_order(self, order):
        self.sent.append(order)
        return {"order": {"order_id": "k1", "status": "executed", "fill_count_fp": order["count"]}}


@pytest.fixture
def live(tmp_path):
    store = Store(tmp_path)
    us, kalshi = FakeUS(), FakeKalshi()
    map_live_cross(store, kalshi, us, "x", subaccount=3)
    return store, us, kalshi


def test_mapping_opens_each_venue_at_its_own_cash(live):
    store, _, _ = live
    state = Broker(store, "x").state()
    assert state.venue_cash == {"kalshi": D("300"), "polymarket_us": D("500")} and state.cash == D("800")
    assert Broker(store, "x").metadata["venues"] == ["kalshi", "polymarket_us"]


def test_a_deposit_still_clearing_is_not_a_cash_mismatch(live):
    store, us, _ = live

    class Clearing(type(us)):
        def private(self, method, path, params=None, body=None):
            if path == "/v1/account/balances":  # $51 deposited, still pending
                return {"balances": [{"currency": "USD", "currentBalance": "551", "depositReservation": "51"}]}
            return super().private(method, path, params, body)

    clearing = Clearing()
    clearing.__dict__.update(us.__dict__)
    status = polymarket_us_pass(store, clearing)
    assert status["status"] == "healthy", status["discrepancies"]
    assert status["balances"] == [{"portfolio": "x", "cash": "500", "recorded": "500",
                                   "clearing_deposits": "51", "advanced": "0", "buying_power": "0"}]


def deposit(status, advanced="51"):
    change = {"transactionId": "DW1", "status": f"ACCOUNT_BALANCE_CHANGE_STATUS_{status}",
              "amount": {"value": "51", "currency": "USD"},
              "createTime": "2099-01-01T00:00:00Z", "updateTime": "2099-01-02T00:00:00Z",
              "transactions": [{"advancedAmount": {"value": advanced, "currency": "USD"}}]}
    return {"type": "ACTIVITY_TYPE_ACCOUNT_DEPOSIT", "accountBalanceChange": change}


def advancing(us, activity, balance):
    class Advancing(type(us)):
        def private(self, method, path, params=None, body=None):
            if path == "/v1/account/balances":
                return {"balances": [{"currency": "USD", **balance}]}
            if path == "/v1/portfolio/activities":
                return {"activities": [activity], "eof": True}
            return super().private(method, path, params, body)

    venue = Advancing()
    venue.__dict__.update(us.__dict__)
    return venue


def test_an_advanced_deposit_is_cash_at_once_and_counted_once(live):
    store, us, _ = live
    pending = advancing(us, deposit("PENDING"), {"currentBalance": "551", "depositReservation": "51"})
    for _ in range(2):
        status = polymarket_us_pass(store, pending)
        assert status["status"] == "healthy", status["discrepancies"]
    assert Broker(store, "x").state().venue_cash["polymarket_us"] == D("551")
    assert status["balances"][0]["advanced"] == "51"
    done = advancing(us, deposit("COMPLETED"), {"currentBalance": "551"})
    for _ in range(2):
        assert polymarket_us_pass(store, done)["status"] == "healthy"
    assert Broker(store, "x").state().venue_cash["polymarket_us"] == D("551")


def test_a_failed_advanced_deposit_is_taken_back_and_reported(live):
    store, us, _ = live
    polymarket_us_pass(store, advancing(us, deposit("PENDING"), {"currentBalance": "551", "depositReservation": "51"}))
    status = polymarket_us_pass(store, advancing(us, deposit("FAILED"), {"currentBalance": "500"}))
    assert [d["kind"] for d in status["discrepancies"]] == ["deposit_failed"]
    assert Broker(store, "x").state().venue_cash["polymarket_us"] == D("500")


def test_mapping_refuses_a_polymarket_us_account_with_positions(tmp_path):
    us = FakeUS()
    us.held = {SLUG: D(3)}
    with pytest.raises(ValueError, match="flat Polymarket US"):
        map_live_cross(Store(tmp_path), FakeKalshi(), us, "x", subaccount=3)


def test_two_live_brokers_share_a_portfolio_and_only_kalshi_gets_the_subaccount(live):
    store, us, kalshi = live
    broker = Broker(store, "x", KalshiLive(kalshi))
    poly_broker = Broker(store, "x", PolymarketUSLive(us))
    k = broker.submit({"ticker": "KXNBAGAME-26NOV21BOSSAC-BOS", "side": "bid", "count": "4", "price": "0.55",
                       "time_in_force": "immediate_or_cancel"})
    p = poly_broker.submit({"ticker": TICKER, "side": "ask", "count": "4", "price": "0.86",
                       "time_in_force": "immediate_or_cancel"})
    assert kalshi.sent[0]["subaccount"] == 3 and "subaccount" not in us.sent[0]
    assert k["status"] == "filled" and k["filled"] == "4"
    assert p["status"] == "filled" and D(p["filled"]) == 4
    assert us.sent[0]["intent"] == "ORDER_INTENT_BUY_SHORT" and us.sent[0]["price"]["value"] == "0.86"
    state = broker.state()
    lot = [lot for lot in state.lots.values() if lot["market"] == TICKER][0]
    assert lot["side"] == "no" and lot["quantity"] == 4  # Sacramento (NO) at 1 - 0.86
    # 4 x 0.14 + 0.05 fee came out of Polymarket US cash only; Kalshi's waits for its monitor.
    assert state.venue_cash["polymarket_us"] == D("500") - D("0.56") - D("0.05")
    assert state.venue_cash["kalshi"] == D("300")
    assert broker.available < D("300")  # its reservation stands until the monitor books the fill


def test_a_reply_without_a_final_state_is_unknown_and_keeps_its_reservation(live):
    store, _, _ = live
    us = FakeUS(executions=[{"id": "e1", "lastShares": "0", "order": {"state": "ORDER_STATE_NEW"}}])
    broker = Broker(store, "x", PolymarketUSLive(us))
    result = broker.submit({"ticker": TICKER, "side": "bid", "count": "4", "price": "0.5",
                            "time_in_force": "immediate_or_cancel"})
    assert result["status"] == "unknown"
    row = store.db.execute("SELECT status,reserved,venue_id FROM orders").fetchone()
    assert row["status"] == "unknown" and D(row["reserved"]) > 0 and row["venue_id"] == "o1"


def test_dry_run_signs_but_sends_nothing(live):
    store, _, _ = live
    us, kalshi = FakeUS(dry_run=True), FakeKalshi()
    broker = Broker(store, "x", KalshiLive(kalshi, dry_run=True))
    poly_broker = Broker(store, "x", PolymarketUSLive(us))
    for broker, ticker in ((poly_broker, TICKER), (broker, "KXNBAGAME-26NOV21BOSSAC-BOS")):
        result = broker.submit({"ticker": ticker, "side": "bid", "count": "4", "price": "0.5",
                                "time_in_force": "immediate_or_cancel"})
        assert result["status"] == "dry_run" and result["filled"] == "0"
    assert us.sent == [] and kalshi.sent == []
    assert broker.store.reserved("x") == 0 and not broker.state().lots


def test_the_monitor_recovers_fills_settles_and_reconciles(live):
    store, us, _ = live
    broker = Broker(store, "x", PolymarketUSLive(
        FakeUS(executions=[{"id": "e1", "lastShares": "0", "order": {"state": "ORDER_STATE_NEW"}}])))
    broker.submit({"ticker": TICKER, "side": "bid", "count": "10", "price": "0.80", "time_in_force": "immediate_or_cancel"})
    # The venue says it filled 10 at 0.80 with a 0.15 fee.
    us.orders["o1"] = {"state": "ORDER_STATE_FILLED", "cumQuantity": 10, "avgPx": {"value": "0.80"},
                       "commissionNotionalTotalCollected": {"value": "0.15"}}
    us.cash, us.held = D("500") - D("8.15"), {SLUG: D(10)}
    status = polymarket_us_pass(store, us)
    assert status["status"] == "healthy", status["discrepancies"]
    assert status["recovered_orders"] == 1
    assert store.db.execute("SELECT status FROM orders").fetchone()["status"] == "filled"
    # The game resolves for Boston (YES): 10 x $1 lands in the account.
    us.market_status, us.settlement, us.cash, us.held = "MARKET_STATUS_RESOLVED", "1", us.cash + 10, {}
    status = polymarket_us_pass(store, us)
    assert status["status"] == "healthy" and status["settled_lots"] == 1
    assert Broker(store, "x").state().venue_cash["polymarket_us"] == D("501.85")
    # Money the ledger can't explain is a discrepancy, never profit.
    us.cash += 1
    status = polymarket_us_pass(store, us)
    assert [d["kind"] for d in status["discrepancies"]] == ["cash_mismatch"]


def test_the_monitor_settles_paper_lots_without_credentials(tmp_path):
    from czquant.polymarket_us import PolymarketUSPaper
    store = Store(tmp_path)
    store.create("paper", "100")
    us = FakeUS()
    us.public = lambda path, params=None: (  # an open market with Celtics bid at 0.62
        {"market": {"slug": SLUG, "status": us.market_status, "marketSides": [], "feeCoefficient": 0.095}}
        if path.startswith("/v1/market/slug/") else
        {"marketData": {"bids": [{"px": {"value": "0.62"}, "qty": "50"}], "offers": [],
                        "stats": {"settlementPx": {"value": us.settlement}, "settlementPreliminaryFlag": False}}})
    broker = Broker(store, "paper", PolymarketUSPaper(us, latency=0))
    broker.submit({"ticker": TICKER, "side": "ask", "count": "10", "price": "0.62", "time_in_force": "immediate_or_cancel"})
    assert [lot["side"] for lot in broker.state().lots.values()] == ["no"]  # Kings at 0.38
    unauthenticated = PolymarketUS(min_interval=0)
    unauthenticated.public = us.public  # type: ignore[method-assign]
    us.market_status, us.settlement = "MARKET_STATUS_RESOLVED", "0"  # Kings win: YES settles at 0
    status = polymarket_us_pass(store, unauthenticated)
    assert status["settled_lots"] == 1 and not broker.state().lots
    # 100 - 3.80 cost - 0.22 fee + 10 x $1
    assert broker.state().cash == D("100") - D("3.80") - D("0.22") + 10


def test_kalshi_unknown_reply_persists_unknown_and_keeps_only_its_venue_reserved(live):
    store, us, kalshi = live
    kalshi.create_order = lambda order: {"order": {"order_id": "unresolved"}}
    broker = Broker(store, "x", KalshiLive(kalshi))
    request = {"ticker": "M", "client_order_id": "unknown", "side": "bid", "count": "4", "price": "0.5",
               "time_in_force": "immediate_or_cancel"}
    assert broker.submit(request)["status"] == "unknown"
    assert broker.submit(request)["status"] == "unknown"  # durable retry result
    assert broker.available == D("297.93")
    assert Broker(store, "x", PolymarketUSLive(us)).available == D("500")
    assert not broker.state().lots


@pytest.mark.parametrize("account_still_holds", [False, True])
def test_live_resolution_needs_account_payout_and_retired_position(live, account_still_holds):
    store, us, _ = live
    broker = Broker(store, "x", PolymarketUSLive(us))
    broker.submit({"ticker": TICKER, "side": "bid", "count": "4", "price": "0.5",
                   "time_in_force": "immediate_or_cancel"})
    cash = broker.state().venue_cash["polymarket_us"]
    us.market_status, us.settlement = "MARKET_STATUS_RESOLVED", "1"
    us.cash = cash + 4 if account_still_holds else cash
    us.held = {SLUG: D(4)} if account_still_holds else {}
    assert polymarket_us_pass(store, us)["settled_lots"] == 0
    assert len(broker.state().lots) == 1
    us.cash, us.held = cash + 4, {}
    status = polymarket_us_pass(store, us)
    assert status["settled_lots"] == 1 and status["status"] == "healthy"
    settlement = [e for e in store.events(kinds=("transaction",)) if e["data"]["reason"] == "settlement"][-1]
    assert settlement["data"]["evidence"]["account"]["positions"] == {}
    assert settlement["data"]["evidence"]["account"]["balance"]["currentBalance"] == str(us.cash)
    assert polymarket_us_pass(store, us)["settled_lots"] == 0


def test_live_polymarket_holdings_keep_public_books_for_reporting(live):
    store, us, _ = live
    broker = Broker(store, "x", PolymarketUSLive(us))
    broker.submit({"ticker": TICKER, "side": "bid", "count": "4", "price": "0.5",
                   "time_in_force": "immediate_or_cancel"})
    us.cash, us.held = broker.state().venue_cash["polymarket_us"], {SLUG: D(4)}
    status = polymarket_us_pass(store, us)
    assert status["status"] == "healthy"
    mark = [e for e in store.events(kinds=("market",)) if e["data"]["ticker"] == TICKER][-1]
    assert mark["data"]["book"] == us.book(TICKER) and mark["data"]["book"]


def test_settlement_defers_when_ledger_changes_during_reference_reads(live):
    store, us, _ = live
    broker = Broker(store, "x", PolymarketUSLive(us))
    broker.submit({"ticker": TICKER, "side": "bid", "count": "4", "price": "0.5",
                   "time_in_force": "immediate_or_cancel"})
    us.cash = broker.state().venue_cash["polymarket_us"] + 4
    us.market_status, us.settlement, us.held = "MARKET_STATUS_RESOLVED", "1", {}
    read = us.market

    def changing_market(ticker):
        # Another writer books a cash-neutral fill while public data is being read.
        # A shared replay-cache object must not hide that change from reconciliation.
        store.append("transaction", {"reason": "fill", "venue": "kalshi", "cash_delta": "0", "fee": "0",
                     "legs": [{"market": "OTHER", "side": "yes", "quantity_delta": "1", "price": "0"}]}, "x")
        return read(ticker)

    us.market = changing_market
    assert polymarket_us_pass(store, us)["settled_lots"] == 0
    assert any(lot["market"] == TICKER for lot in broker.state().lots.values())


@pytest.mark.parametrize("operation", ["cancel", "amend"])
def test_live_mutations_cannot_cross_venue_or_exceed_adapter_capabilities(live, operation):
    store, us, kalshi = live
    broker = Broker(store, "x", PolymarketUSLive(us))
    broker.submit({"ticker": TICKER, "client_order_id": "us-order", "side": "bid", "count": "4", "price": "0.5",
                   "time_in_force": "immediate_or_cancel"})
    args = ("us-order", {}) if operation == "amend" else ("us-order",)
    before = store.events()
    with pytest.raises(ValueError, match="kalshi broker"):
        getattr(Broker(store, "x", KalshiLive(kalshi)), operation)(*args)
    with pytest.raises(NotImplementedError, match="does not support"):
        getattr(broker, operation)(*args)
    assert store.events() == before
