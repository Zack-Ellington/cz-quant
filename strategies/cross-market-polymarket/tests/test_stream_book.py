"""Execution reads of the shared Polymarket US market stream (czquant.polymarket_us.BookStream)."""
import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from czquant.polymarket_us import BookStream, QuoteUnavailable
from tests.test_experiments import setup


def message(age=0, state="MARKET_STATE_OPEN", slug="game"):
    return json.dumps({"marketData": {"marketSlug": slug, "state": state,
        "transactTime": datetime.fromtimestamp(time.time() - age, timezone.utc).isoformat(),
        "bids": [{"px": {"value": ".60"}, "qty": "100"}],
        "offers": [{"px": {"value": ".62"}, "qty": "100"}]}})


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def feed(slug="game", clock=None):
    return BookStream(SimpleNamespace(), [slug], max_age=2, clock=clock or Clock())


def test_snapshot_freshness_and_disconnect():
    clock = Clock()
    f = feed(clock=clock)
    with pytest.raises(QuoteUnavailable, match="unavailable"):
        f.snapshot("game")
    f.handle(message())
    quote = f.snapshot("game")
    assert quote["execution_quote"]["source"] == "polymarket_us_websocket"
    assert quote["orderbook_fp"]["yes_dollars"] == [["0.60", "100"]]
    clock.now += 3
    with pytest.raises(QuoteUnavailable, match="stale"):
        f.snapshot("game")
    f.clear()
    with pytest.raises(QuoteUnavailable, match="unavailable"):
        f.snapshot("game")


def test_a_quiet_book_stays_valid_while_snapshots_keep_arriving():
    # The exchange timestamp of an unchanged book does not move; receipt does.
    clock = Clock()
    f = feed(clock=clock)
    f.handle(message(age=30))
    clock.now += 1.5
    f.handle(message(age=31.5))  # older exchange time than held: ignored, no refresh
    clock.now += 1
    with pytest.raises(QuoteUnavailable, match="stale"):
        f.snapshot("game")
    f.handle(message(age=30))  # same book again: the connection is alive
    assert f.snapshot("game")["execution_quote"]["exchange_age_seconds"] >= 30


def test_future_exchange_time_is_rejected():
    f = feed()
    f.handle(message(age=-5))
    with pytest.raises(QuoteUnavailable, match="stale"):
        f.snapshot("game")


def test_suspended_incomplete_and_other_markets():
    f = feed()
    f.handle(message(slug="other"))
    with pytest.raises(QuoteUnavailable, match="unavailable"):
        f.snapshot("game")
    f.handle(message(state="MARKET_STATE_SUSPENDED"))
    with pytest.raises(QuoteUnavailable, match="not_open"):
        f.snapshot("game")
    partial = json.loads(message())
    del partial["marketData"]["offers"]
    f.handle(json.dumps(partial))
    with pytest.raises(QuoteUnavailable, match="incomplete"):
        f.snapshot("game")


def test_reconnect_clears_books_and_signs_again():
    signatures = []
    client = SimpleNamespace(headers=lambda *a: signatures.append(a) or {})
    f = BookStream(client, ["game"], max_age=2)
    f.handle(message())
    calls = []

    @contextmanager
    def connection():
        calls.append(f.book("game"))
        if len(calls) == 1:
            raise ConnectionError("disconnected")
        f.stop()
        yield SimpleNamespace(send=lambda s: None, recv=lambda timeout: message())

    f._connect = connection
    f._stop.wait = lambda *a: None  # type: ignore[method-assign]
    f._run()
    assert len(calls) == 2 and calls[1] is None  # the old book did not survive the drop
    with pytest.raises(QuoteUnavailable, match="unavailable"):
        f.snapshot("game")


def test_execution_pricing_never_falls_back_to_rest(setup, monkeypatch):
    store, create, build = setup
    engine = build(create())
    clock = Clock()
    f = feed(engine.pair.polymarket_market, clock)
    engine.stream_book = f
    monkeypatch.setattr(engine.poly, "books", lambda *a: pytest.fail("REST book used"))
    assert engine.price_pair()[1] == "polymarket_websocket_unavailable"
    f.handle(message(slug=engine.pair.polymarket_market))
    clock.now += 3
    assert engine.price_pair()[1] == "polymarket_websocket_stale"
    f.handle(message(slug=engine.pair.polymarket_market))
    assert engine.price_pair()[1] not in ("polymarket_websocket_stale", "polymarket_websocket_unavailable")


def test_stale_before_submission_sends_no_order_or_cooldown(setup, monkeypatch):
    store, create, build = setup
    engine = build(create(), profit_slippage=True, max_basket_dollars=50)
    basket = engine.scan()
    assert basket is not None
    monkeypatch.setattr(engine, "price_pair", lambda *_, **__: (basket, "ok"))
    engine.stream_book = feed(engine.pair.polymarket_market)
    engine.execute(basket)
    assert not store.events(kinds=("order", "execution"))
    assert engine.cooldown_until == 0


def test_disconnect_after_first_fill_blocks_second_and_retains_repair(setup, monkeypatch):
    store, create, build = setup
    engine = build(create(), profit_slippage=True, max_basket_dollars=50)
    basket = engine.scan()
    assert basket is not None
    monkeypatch.setattr(engine, "price_pair", lambda *_, **__: (basket, "ok"))
    f = feed(engine.pair.polymarket_market)
    f.handle(message(slug=engine.pair.polymarket_market))
    engine.stream_book = f
    first = min(basket.legs, key=lambda l: float(l['depth']))
    broker = engine.brokers[first['venue']]
    original = broker.adapter.create_order

    def fill_then_disconnect(order, bound):
        result = original(order, bound)
        f.clear()
        return result
    monkeypatch.setattr(broker.adapter, "create_order", fill_then_disconnect)
    engine.execute(basket)
    assert len([e for e in store.events(kinds=("order",)) if e['data']['status'] == 'intent']) == 1
    assert engine.halted
    assert broker.state().lots
