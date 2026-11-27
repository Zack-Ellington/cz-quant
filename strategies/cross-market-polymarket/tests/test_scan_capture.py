from decimal import Decimal as D
import pytest
from scan_capture import measure
from sum_check import kalshi_offer, polymarket_us_offer, price
from tests.test_experiments import setup
from tests.test_stream_book import Clock, feed, message


def test_scan_records_stream_input_and_blocked_gap_without_rest(setup, monkeypatch):
    store, create, build = setup
    engine = build(create())
    clock = Clock()
    f = feed(engine.pair.polymarket_market, clock)
    engine.stream_book = f
    monkeypatch.setattr(engine.poly, "books", lambda *a: pytest.fail("extra REST book request"))
    f.handle(message(slug=engine.pair.polymarket_market))
    engine.scan()
    scan = store.events(kinds=("observation",))[-1]["data"]
    q = scan["quote"]
    assert q["source"]["source"] == "polymarket_us_websocket"
    assert q["legs"][0]["polymarket_ask"] in ("0.62", "0.40")
    assert len(q["routes"]) == 2
    assert q["books"]["polymarket"]["execution_quote"] == q["source"]
    clock.now += 3
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
    for route in result["routes"]:
        assert route["max_gap"]["sets"] == 1
        assert route["best"]["sets"] == 3
        expected = price((offers["0"][0], offers["1"][1]), 1, 1, 0)
        assert route["max_gap"]["net_per_set"] == expected.edge
    assert all(r["max_gap"] is None for r in measure(offers, ["A", "B"], 20, D(20), lambda b: False)["routes"])
    expensive = {k: [kalshi_offer(k, k, book([[".01", "20"]]), D(1)), v[1]] for k, v in offers.items()}
    assert not any(r["opportunity"] for r in measure(expensive, ["A", "B"], 20, D(40), lambda b: True)["routes"])
