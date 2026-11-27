"""Automatic exits use reconciled exposure, fresh bids and durable order intents."""
from decimal import Decimal as D

import pytest
from czquant.live import KalshiLive

from tests.test_experiments import setup


def seed(engine, role, quantity, trade="fixture"):
    """Synthetic account-monitor inventory, not a venue request."""
    outcome = engine.pair.outcomes[role]
    broker = engine.kalshi_broker if role == 0 else engine.poly_broker
    ticker = outcome.kalshi if role == 0 else outcome.polymarket
    side = outcome.kalshi_side if role == 0 else outcome.polymarket_side
    context = {"trade_id": trade, "experiment_id": engine.experiment.id,
               "kalshi_event": engine.pair.kalshi_event,
               "intended_legs": [{"ticker": o.kalshi if i == 0 else o.polymarket,
                   "contract": o.kalshi_side if i == 0 else o.polymarket_side, "qty": 10}
                   for i, o in enumerate(engine.pair.outcomes)]}
    engine.store.append("transaction", {"reason": "fill", "cash_delta": str(-D(quantity) * D(".5")),
        "fee": "0", "context": context, "legs": [{"market": ticker, "side": side.lower(),
        "quantity_delta": str(quantity), "price": "0.5"}]}, broker.pool_id)


def held(engine, role):
    broker = engine.kalshi_broker if role == 0 else engine.poly_broker
    return sum((lot["quantity"] for lot in broker.state().lots.values()), D(0))


def kalshi_bids(engine, monkeypatch, bids):
    original = engine.kalshi.book
    monkeypatch.setattr(engine.kalshi, "book", lambda ticker: {"orderbook_fp": {
        **original(ticker)["orderbook_fp"], "yes_dollars": bids}})


def test_recovery_walks_bids_below_entry_and_preserves_matched_pairs(setup, monkeypatch):
    store, create, build = setup
    engine = build(create(mode="live"))
    seed(engine, 0, 10)
    seed(engine, 1, 4)
    kalshi_bids(engine, monkeypatch, [["0.20", "2"], ["0.10", "10"]])
    engine.step()
    assert held(engine, 0) == held(engine, 1) == 4
    assert not engine.halted and engine.counters["scans"] == 0
    intent = [e["data"] for e in store.events(kinds=("order",)) if e["data"]["status"] == "intent"][-1]
    assert intent["order"]["reduce_only"] and D(intent["order"]["count"]) == 6
    assert intent["order"]["price"] == "0.10"
    assert engine.kalshi_broker.state().realized < -2  # No 5-cent entry loss floor.


def test_fifo_closes_do_not_cause_repeated_sales_of_matched_pairs(setup, monkeypatch):
    store, create, build = setup
    engine = build(create(mode="live"))
    seed(engine, 0, 4, "matched")
    seed(engine, 1, 4, "matched")
    seed(engine, 0, 6, "unmatched")
    kalshi_bids(engine, monkeypatch, [["0.20", "100"]])
    engine.step()
    engine.resume()
    assert held(engine, 0) == held(engine, 1) == 4
    assert not engine.repairs and not engine.halted
    # The older Kalshi lot was consumed by FIFO, but total outcome quantities match.
    assert {l["context"]["trade_id"] for l in engine.kalshi_broker.state().lots.values()} == {"unmatched"}


def test_empty_book_and_partial_exit_retry_after_restart(setup, monkeypatch):
    store, create, build = setup
    spec = create(mode="live")
    engine = build(spec)
    seed(engine, 0, "3.5")
    kalshi_bids(engine, monkeypatch, [])
    engine.step()
    assert held(engine, 0) == D("3.5") and not engine.kalshi_broker.orders()
    restarted = build(spec)
    kalshi_bids(restarted, monkeypatch, [["0.20", "1.5"]])
    restarted.step()
    assert held(restarted, 0) == 2 and restarted.halted
    # A new process reads the booked partial exit and sells only its remainder.
    restarted = build(spec)
    kalshi_bids(restarted, monkeypatch, [["0.10", "100"]])
    restarted.step()
    assert held(restarted, 0) == 0 and not restarted.halted
    assert [o["filled"] for o in restarted.kalshi_broker.orders()] == [D("1.5"), D(2)]


def test_lost_repair_ack_waits_for_monitor_across_restart(setup, monkeypatch):
    store, create, build = setup
    spec = create(mode="live")
    engine = build(spec)
    seed(engine, 0, 5)
    kalshi_bids(engine, monkeypatch, [["0.20", "100"]])

    class LostReply:
        def create_order(self, order):
            raise TimeoutError("venue may have sold")

    engine.kalshi_broker.adapter = KalshiLive(LostReply())
    engine.step()
    orders = engine.kalshi_broker.orders()
    assert len(orders) == 1 and orders[0]["status"] == "unknown"
    restarted = build(spec)
    restarted.step()
    assert restarted.kalshi_broker.orders() == orders and held(restarted, 0) == 5
    broker = restarted.kalshi_broker
    broker.fill(orders[0]["client_order_id"], "5", "0.20", "0.01", "recovered-exit", {})
    broker.finish(orders[0]["client_order_id"], "executed")
    restarted.step()
    assert held(restarted, 0) == 0 and not restarted.halted
    assert len(broker.orders()) == 1


@pytest.mark.parametrize("filled", ["0", "4", "10"])
def test_unknown_entry_recovers_only_confirmed_imbalance(setup, monkeypatch, filled):
    store, create, build = setup
    spec = create(mode="live")
    engine = build(spec)

    def timeout(*args):
        raise TimeoutError("entry reply lost")

    monkeypatch.setattr(engine.kalshi_broker.adapter, "create_order", timeout)
    engine.step()  # Polymarket fills 10, Kalshi's reply is lost.
    assert held(engine, 1) == 10
    order = engine.kalshi_broker.orders()[0]
    restarted = build(spec)
    restarted.step()
    assert held(restarted, 1) == 10 and restarted.counters["unwinds"] == 0
    if D(filled):
        restarted.kalshi_broker.fill(order["client_order_id"], filled, "0.55", "0", "entry-recovered", {})
    restarted.kalshi_broker.finish(order["client_order_id"], "canceled")
    restarted.step()
    assert held(restarted, 0) == held(restarted, 1) == D(filled)
    assert not restarted.halted and restarted.counters["scans"] == 0


@pytest.mark.parametrize("block", ["kill", "manual", "discrepancy", "stale"])
def test_hard_halts_and_unhealthy_monitors_block_recovery(setup, monkeypatch, block):
    store, create, build = setup
    engine = build(create(mode="live"))
    seed(engine, 0, 5)
    kalshi_bids(engine, monkeypatch, [["0.20", "100"]])
    if block == "kill":
        monkeypatch.setattr("trader.os.path.exists", lambda path: True)
    elif block == "manual":
        engine.halt("operator review required")
    else:
        store.append("health", {"component": "account", "status": "healthy" if block == "discrepancy" else "failed",
            "discrepancies": [{"portfolio": engine.kalshi_broker.pool_id, "kind": "cash_mismatch"}]
            if block == "discrepancy" else []})
    engine.step()
    assert held(engine, 0) == 5 and not engine.kalshi_broker.orders()
    assert engine.halted


def test_legacy_unknown_fill_halt_can_recover_without_manual_clear(setup, monkeypatch):
    store, create, build = setup
    engine = build(create(mode="live"))
    seed(engine, 0, 5)
    ticker = engine.pair.outcomes[0].kalshi
    engine.kalshi_broker.observe("halt", {"reason": f"order on {ticker} has an unknown outcome"})
    # Historical order ownership is required before treating the old halt as recoverable.
    store.append("order", {"status": "intent", "order": {"ticker": ticker, "client_order_id": "old"},
                 "context": {}}, engine.kalshi_broker.pool_id)
    kalshi_bids(engine, monkeypatch, [["0.20", "100"]])
    engine.step()
    assert not engine.halted and held(engine, 0) == 0


def test_settlement_of_one_leg_does_not_sell_the_other(setup):
    store, create, build = setup
    engine = build(create())
    seed(engine, 0, 5)
    seed(engine, 1, 5)
    lot = next(iter(engine.kalshi_broker.state().lots))
    engine.kalshi_broker.settle(lot, "1", {"fixture": "resolved"})
    engine.resume()
    assert not engine.repairs and not engine.halted and held(engine, 1) == 5
