"""Venue ownership and accounting survive separate brokers and process restarts."""
import json
from decimal import Decimal as D

import pytest

from czquant import Broker, Store
from czquant.monitor import market_pass
from czquant.report import compare, generate
from czquant.store import utc


class PendingPaper:
    def __init__(self, venue):
        self.venue, self.sent = venue, []

    def create_order(self, order, broker):
        self.sent.append(order)
        return {"status": "pending", "filled": "0"}


def order(ticker, identifier):
    return {"ticker": ticker, "client_order_id": identifier, "side": "bid", "count": "2", "price": "0.5"}


@pytest.mark.parametrize("venue,ticker", [("polymarket", "PM:token"), ("polymarket_us", "PMUS:slug")])
def test_wrong_venue_rejected_before_intent_or_adapter_call(tmp_path, venue, ticker):
    store = Store(tmp_path)
    store.create("p", "10")
    kalshi, other = PendingPaper("kalshi"), PendingPaper(venue)
    a, b = Broker(store, "p", kalshi), Broker(store, "p", other)
    before = store.events()
    for broker, wrong in ((a, ticker), (b, "M")):
        with pytest.raises(ValueError, match="broker cannot"):
            broker.submit(order(wrong, "wrong"))
    assert not kalshi.sent and not other.sent
    assert store.events() == before and store.reserved("p") == 0
    with pytest.raises(ValueError, match="different venue"):
        Broker(store, "p", other, venue="kalshi")


def test_two_brokers_share_paper_reservations_context_and_restart(tmp_path):
    store = Store(tmp_path)
    store.create("p", "2")
    a = Broker(store, "p", PendingPaper("kalshi"), "run")
    b = Broker(store, "p", PendingPaper("polymarket_us"), "run")
    context = {"arbitrary": {"features": [1, None, "x"]}, "portfolio": "cannot-change-ownership"}
    a.submit(order("M", "k"), context)
    assert a.available == b.available == D("0.96")
    assert b.submit(order("PMUS:slug", "too-large"))["status"] == "rejected"
    a.fill("k", "2", "0.5", "0", "fill-k", {"source": "fixture"})
    a.finish("k", "filled")
    a.settle(next(iter(a.state().lots)), "1", {"result": "fixture"})
    b.submit(order("PMUS:slug", "p"), context)
    b.fill("p", "2", "0.5", "0", "fill-p", {"source": "fixture"})
    b.finish("p", "filled")
    cash = b.state().cash
    store.close()
    reopened = Store(tmp_path)
    for venue in ("kalshi", "polymarket_us"):
        broker = Broker(reopened, "p", venue=venue)
        assert broker.state().cash == cash == 2
        assert broker.available == 2
        assert len(broker.state().lots) == 1
    fills = [e for e in reopened.events(kinds=("transaction",)) if e["data"]["reason"] == "fill"]
    assert {e["data"]["venue"] for e in fills} == {"kalshi", "polymarket_us"}
    assert all(e["data"]["context"] == context and e["portfolio_id"] == "p" and e["run_id"] == "run" for e in fills)


def test_wrong_broker_cannot_mutate_an_order_lot_or_receipt(tmp_path):
    store = Store(tmp_path)
    store.create("p", "10")
    a = Broker(store, "p", PendingPaper("kalshi"))
    b = Broker(store, "p", PendingPaper("polymarket_us"))
    b.submit(order("PMUS:slug", "p"))
    b.fill("p", "2", "0.5", "0", "fill-p", {})
    lot_id = next(iter(b.state().lots))
    target = next(e["record_id"] for e in store.events(kinds=("transaction",)) if e["data"]["reason"] == "fill")
    before, reserved = store.events(), store.reserved("p")
    operations = [lambda: a.cancel("p"), lambda: a.finish("p", "filled"),
                  lambda: a.fill("p", "2", "0.5", "0", "fill-p", {}),  # even a duplicate receipt
                  lambda: a.settle(lot_id, "1", {}), lambda: a.correct([target], "wrong venue")]
    for operation in operations:
        with pytest.raises(ValueError):
            operation()
        assert store.events() == before and store.reserved("p") == reserved
    b.correct([target], "fixture correction")
    assert not b.state().lots and b.state().cash == 10


@pytest.mark.parametrize("final", [False, True])
def test_market_collector_follows_live_holdings_without_paper_settlement(tmp_path, final):
    store = Store(tmp_path)
    store.create("live", mode="live", subaccount=3)
    store.append("transaction", {"reason": "fill", "cash_delta": "-1", "fee": "0", "legs": [
        {"market": "M", "side": "yes", "quantity_delta": "2", "price": "0.5"}]}, "live")
    seen = []

    class Market:
        def market(self, ticker):
            seen.append(ticker)
            return {"status": "finalized" if final else "active", "settlement_value_dollars": "1"}

        def book(self, ticker):
            return {"orderbook_fp": {"yes_dollars": [["0.4", "10"]], "no_dollars": [["0.4", "10"]]}}

    status = market_pass(store, Market())
    assert status["status"] == "healthy" and seen == ["M"]
    assert len(Broker(store, "live").state().lots) == 1
    assert not store.events("live", kinds=("snapshot",))
    assert store.events(kinds=("market",))


def test_cross_venue_report_requires_both_accounts_and_records_mode_and_code(tmp_path):
    store = Store(tmp_path)
    store.create("live", mode="live", venues=["kalshi", "polymarket_us"])
    store.append("health", {"component": "account", "status": "healthy"})
    at = utc()
    assert not compare(store, at, at)[0][0]["complete"]
    store.append("health", {"component": "polymarket_us", "status": "healthy"})
    at = utc()
    assert compare(store, at, at)[0][0]["complete"]
    store.append("health", {"component": "polymarket_us", "status": "degraded"})
    at = utc()
    output = tmp_path / "reports" / "fixture"
    manifest = generate(str(tmp_path), at, at, str(output))
    assert not manifest["metrics"][0]["complete"]
    assert manifest["metrics"][0]["mode"] == "live"
    assert "one-sided midpoint" in manifest["valuation"]
    assert all(len(digest) == 64 for digest in manifest["implementation_sha256"].values())
    assert "| live | live |" in (output / "comparison.md").read_text()
    assert json.loads((output / "manifest.json").read_text())["implementation_sha256"] == manifest["implementation_sha256"]


def test_historical_transaction_keeps_its_namespace_for_replay_and_corrections(tmp_path):
    store = Store(tmp_path)
    store.create("p", "10")
    # Historical instruments remain readable without an active venue adapter.
    target = store.append("transaction", {"reason": "fill", "venue": "kalshi", "cash_delta": "-1",
                          "fee": "0", "legs": [{"market": "RETIRED:selection", "side": "yes",
                          "quantity_delta": "2", "price": "0.5"}]}, "p")
    with pytest.raises(ValueError, match="this venue"):
        Broker(store, "p").correct([target], "legacy attribution")
    Broker(store, "p", venue="retired").correct([target], "fixture correction")
    assert Broker(store, "p").state().cash == 10


def test_retired_instruments_remain_in_history_without_reaching_kalshi(tmp_path):
    store = Store(tmp_path)
    store.create("p", "10")
    store.append("transaction", {"reason": "fill", "cash_delta": "-1", "fee": "0", "legs": [
        {"market": "RETIRED:selection", "side": "yes", "quantity_delta": "2", "price": "0.5"}]}, "p")
    adapter = PendingPaper("kalshi")
    broker = Broker(store, "p", adapter)
    with pytest.raises(ValueError, match="kalshi broker"):
        broker.submit(order("RETIRED:selection", "unsupported"))
    assert not adapter.sent

    class PublicKalshi:
        def get(self, path, params=None):
            assert path == "/markets"  # only a no-exposure public probe
            return {"markets": []}

        def market(self, ticker):
            pytest.fail("retired instrument must not reach the Kalshi API")

    assert market_pass(store, PublicKalshi())["markets"] == 0
    assert broker.state().cash == 9 and len(broker.state().lots) == 1
    at = utc()
    metrics, _ = compare(store, at, at)
    assert metrics[0]["open_lots"] == 1 and not metrics[0]["complete"]
