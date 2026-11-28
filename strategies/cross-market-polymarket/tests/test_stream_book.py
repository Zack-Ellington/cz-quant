import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from stream_book import StreamBook, QuoteUnavailable
from tests.test_experiments import setup


def message(age=0, state="MARKET_STATE_OPEN", slug="game"):
    return {"marketData": {"marketSlug": slug, "state": state,
        "transactTime": datetime.fromtimestamp(time.time() - age, timezone.utc).isoformat(),
        "bids": [{"px": {"value": ".60"}, "qty": "100"}],
        "offers": [{"px": {"value": ".62"}, "qty": "100"}]}}


def feed():
    return StreamBook(SimpleNamespace(), "game", 2)


def test_snapshot_freshness_and_disconnect():
    f = feed()
    with pytest.raises(QuoteUnavailable, match="unavailable"):
        f.snapshot()
    f.accept(message())
    assert f.snapshot()["execution_quote"]["source"] == "polymarket_us_websocket"
    f.received -= 3
    with pytest.raises(QuoteUnavailable, match="stale"):
        f.snapshot()
    f.clear()
    with pytest.raises(QuoteUnavailable, match="unavailable"):
        f.snapshot()


@pytest.mark.parametrize("age", [3, -5])
def test_old_or_future_exchange_timestamp_is_rejected(age):
    f = feed()
    f.accept(message(age))
    with pytest.raises(QuoteUnavailable, match="stale"):
        f.snapshot()


def test_suspended_market_and_wrong_slug():
    f = feed()
    f.accept(message(slug="other"))
    with pytest.raises(QuoteUnavailable, match="unavailable"):
        f.snapshot()
    f.accept(message(state="MARKET_STATE_SUSPENDED"))
    with pytest.raises(QuoteUnavailable, match="not_open"):
        f.snapshot()


def test_duplicate_or_old_messages_do_not_refresh_age():
    f = feed()
    m = message()
    f.accept(m)
    received = f.received
    f.accept(m)
    f.accept(message(10))
    assert f.received == received


def test_reconnect_clears_cache_and_signs_again(monkeypatch):
    from contextlib import contextmanager
    signatures = []
    client = SimpleNamespace(api="https://api.polymarket.us", headers=lambda *a: signatures.append(a) or {})
    f = StreamBook(client, "game", 2)
    f.accept(message())
    calls = []
    @contextmanager
    def connection(*a, **k):
        calls.append(a)
        assert f.data is None
        if len(calls) == 1:
            raise ConnectionError("disconnected")
        f.stop.set()
        yield SimpleNamespace(send=lambda s: None)
    monkeypatch.setattr("stream_book.connect", connection)
    monkeypatch.setattr(f.stop, "wait", lambda *a: None)
    f._run()
    assert len(calls) == 2
    assert signatures == [("GET", "/v1/ws/markets")] * 2
    assert f.data is None


def test_execution_pricing_never_falls_back_to_rest(setup, monkeypatch):
    store, create, build = setup
    engine = build(create())
    f = StreamBook(engine.poly, engine.pair.polymarket_market, 2)
    engine.stream_book = f
    monkeypatch.setattr(engine.poly, "books", lambda *a: pytest.fail("REST book used"))
    assert engine.price_pair()[1] == "polymarket_websocket_unavailable"
    f.accept(message(3, slug=f.slug))
    assert engine.price_pair()[1] == "polymarket_websocket_stale"
    f.accept(message(slug=f.slug))
    assert engine.price_pair()[1] not in ("polymarket_websocket_stale", "polymarket_websocket_unavailable")


def test_stale_before_submission_sends_no_order_or_cooldown(setup, monkeypatch):
    store, create, build = setup
    engine = build(create(), profit_slippage=True, max_basket_dollars=50)
    basket = engine.scan()
    assert basket is not None
    monkeypatch.setattr(engine, "price_pair", lambda **_: (basket, "ok"))
    engine.stream_book = StreamBook(engine.poly, engine.pair.polymarket_market, 2)
    engine.execute(basket)
    assert not store.events(kinds=("order", "execution"))


def test_disconnect_after_first_fill_blocks_second_and_retains_repair(setup, monkeypatch):
    store, create, build = setup
    engine = build(create(), profit_slippage=True, max_basket_dollars=50)
    basket = engine.scan()
    assert basket is not None
    monkeypatch.setattr(engine, "price_pair", lambda **_: (basket, "ok"))
    f = StreamBook(engine.poly, engine.pair.polymarket_market, 2)
    snapshot = message(slug=f.slug)
    snapshot['marketData']['bids'][0]['px']['value'] = '.62'
    snapshot['marketData']['offers'][0]['px']['value'] = '.66'
    f.accept(snapshot)
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
    assert len([e for e in store.events(kinds=("order",)) if e['data']['status']=='intent']) == 1
    assert engine.halted
    assert broker.state().lots
