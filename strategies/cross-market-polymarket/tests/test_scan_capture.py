from decimal import Decimal as D
from datetime import datetime, timezone

import pytest

from scan_capture import measure
from sum_check import kalshi_offer, polymarket_us_offer, price
from tests.test_experiments import setup
from tests.test_stream_book import feed, message


@pytest.mark.parametrize("profitable", [True, False])
def test_scan_capture_preserves_decision_and_venue_reads(setup, monkeypatch, profitable):
    store, create, build = setup
    engine = build(create())
    if not profitable:
        engine.kalshi.asks = {"BOS": "0.70", "SAC": "0.70"}
    now = datetime.now(timezone.utc).timestamp()
    monkeypatch.setattr("trader.time.time", lambda: now)
    calls = []
    for client, method in ((engine.kalshi, "market"), (engine.kalshi, "get"),
                           (engine.kalshi, "book"), (engine.poly, "market_raw"),
                           (engine.poly, "books")):
        original = getattr(client, method)
        def record(*args, _method=method, _original=original, **kwargs):
            calls.append((_method, args))
            return _original(*args, **kwargs)
        monkeypatch.setattr(client, method, record)
    expected, reason = engine.price_pair()
    reads = list(calls)
    before = {b.pool_id: b.state() for b in engine.brokers.values()}
    calls.clear()
    actual = engine.scan()
    assert actual == expected
    assert (actual is not None) == profitable
    assert calls == reads
    assert {b.pool_id: b.state() for b in engine.brokers.values()} == before
    assert all(not b.orders() for b in engine.brokers.values())
    scan = store.events(kinds=("observation",))[-1]["data"]
    assert scan["reason"] == reason
    q = scan["quote"]
    assert q["status"] == "ok"
    assert q["source"]["source"] == "polymarket_us_rest"
    assert q["legs"][0]["polymarket_ask"] == "0.66"
    assert q["legs"][1]["polymarket_ask"] == "0.38"
    assert len(q["routes"]) == 2
    assert q["books"]["kalshi"] and q["books"]["polymarket"]


def test_blocked_and_failed_scans_do_not_reuse_prices(setup, monkeypatch):
    store, create, build = setup
    engine = build(create())
    engine.scan()
    original = engine.poly.market_raw
    monkeypatch.setattr(engine.poly, "market_raw", lambda slug: {**original(slug), "status": "closed"})
    assert engine.scan() is None
    blocked = store.events(kinds=("observation",))[-1]["data"]
    assert blocked["reason"] == "polymarket_not_trading"
    assert blocked["quote"]["status"] == "error"
    assert blocked["quote"]["legs"] == blocked["quote"]["routes"] == []
    def fail(*args):
        raise RuntimeError("offline")
    monkeypatch.setattr(engine.kalshi, "market", fail)
    with pytest.raises(RuntimeError, match="offline"):
        engine.scan()
    failed = store.events(kinds=("observation",))[-1]["data"]
    assert failed["reason"] == "scan_error"
    assert failed["quote"]["books"] == {}


def test_diagnostic_failure_does_not_change_trade_decision(setup, monkeypatch):
    store, create, build = setup
    engine = build(create())
    now = datetime.now(timezone.utc).timestamp()
    monkeypatch.setattr("trader.time.time", lambda: now)
    expected, _ = engine.price_pair()
    def fail(*args):
        raise RuntimeError("diagnostic failure")
    monkeypatch.setattr("trader.measure", fail)
    assert engine.scan() == expected
    assert store.events(kinds=("observation",))[-1]["data"]["quote"]["status"] == "error"


def test_scan_records_stream_input_and_blocked_gap_without_rest(setup, monkeypatch):
    store, create, build = setup
    engine = build(create())
    f = feed()
    engine.stream_book = f
    monkeypatch.setattr(engine.poly, "books", lambda *a: pytest.fail("extra REST book request"))
    f.accept(message())
    engine.scan()
    scan = store.events(kinds=("observation",))[-1]["data"]
    q = scan["quote"]
    assert q["source"]["source"] == "polymarket_us_websocket"
    assert q["legs"][0]["polymarket_ask"] in ("0.62", "0.40")
    assert len(q["routes"]) == 2
    assert q["books"]["polymarket"]["execution_quote"] == q["source"]
    f.received -= 3
    assert engine.scan() is None
    stale = store.events(kinds=("observation",))[-1]["data"]
    assert stale["reason"] == "polymarket_websocket_stale"
    assert stale["quote"]["routes"] == []
    assert stale["quote"]["status"] == "error"
    monkeypatch.setattr(engine.kalshi, "market", lambda *a: (_ for _ in ()).throw(RuntimeError("offline")))
    with pytest.raises(RuntimeError):
        engine.scan()
    assert store.events(kinds=("observation",))[-1]["data"]["reason"] == "scan_error"



def test_gap_uses_execution_fees_depth_and_fundable_sizes():
    def book(no):
        return {"orderbook_fp": {"yes_dollars": [[".5", "20"]], "no_dollars": no}}
    offers = {str(i): [kalshi_offer(str(i), str(i), book([[".6", "1"], [".55", "19"]]), D(1)),
                      polymarket_us_offer(str(i), "PMUS:x", book([[".6", "20"]]), D('.095'), "YES")]
              for i in range(2)}
    result = measure(offers, ["A", "B"], 20, D(20), lambda b: b.sets <= 3)
    expected = price((offers["0"][0], offers["1"][1]), 1, 1, 0)
    assert expected is not None
    for route in result["routes"]:
        assert route["max_gap"]["sets"] == 1
        assert route["best"]["sets"] == 3
        assert route["max_gap"]["net_per_set"] == expected.edge
    assert all(r["max_gap"] is None for r in measure(offers, ["A", "B"], 20, D(20), lambda b: False)["routes"])

    expensive = {k: [kalshi_offer(k, k, book([[".01", "20"]]), D(1)), v[1]] for k, v in offers.items()}
    assert not any(r["opportunity"] for r in measure(expensive, ["A", "B"], 20, D(40), lambda b: True)["routes"])
