"""Execution boundaries and scheduling, with no venue connections."""
import copy
import threading
import time
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from czquant.live import KalshiLive
from metadata import Metadata
from stream_book import StreamBook, QuoteUnavailable
from tests.test_experiments import setup
from tests.test_stream_book import message


@pytest.mark.parametrize('position', ['first', 'second'])
@pytest.mark.parametrize('filled', ['4', '10'])
def test_slippage_uses_kalshi_response_before_monitor_receipts(setup, monkeypatch, position, filled):
    store, create, build = setup
    engine = build(create(mode='live'), profit_slippage=True, max_basket_dollars=50)
    sent = []
    def create_order(order):
        sent.append(order)
        return {'order_id': 'kalshi-test', 'fill_count': filled, 'remaining_count': '0',
                'average_fill_price': order['price'], 'average_fee_paid': '0.01'}
    engine.kalshi_broker.adapter = KalshiLive(SimpleNamespace(create_order=create_order))
    basket = engine.scan()
    for leg in basket.legs:
        leg['depth'] = '1' if (leg['venue'] == 'kalshi') == (position == 'first') else '1000'
    monkeypatch.setattr(engine, 'price_pair', lambda **_: (basket, 'ok'))
    engine.execute(basket)
    assert len(sent) == 1
    assert not engine.halted
    assert engine.poly_broker.state().lots
    assert not store.events(engine.kalshi_broker.pool_id, kinds=('execution',))
    assert engine.live_block() == 'awaiting_reconciliation'
    # Only exact monitor receipts affect cash/lots. Repeated ingestion is idempotent.
    order = sent[0]
    for _ in range(2):
        engine.kalshi_broker.fill(order['client_order_id'], filled, order['price'],
                                 str(D(filled)*D('.01')), 'kalshi-fill:test', {'source': 'fixture'})
    engine.kalshi_broker.finish(order['client_order_id'], 'filled')
    engine.resume()
    assert len(store.events(engine.kalshi_broker.pool_id, kinds=('execution',))) == 1
    assert not engine.repairs


def test_absent_price_evidence_remains_recoverable(setup, monkeypatch):
    store, create, build = setup
    engine = build(create(mode='live'), profit_slippage=True, max_basket_dollars=50)
    engine.kalshi_broker.adapter = KalshiLive(SimpleNamespace(create_order=lambda order: {
        'order_id': 'kalshi-test', 'fill_count': order['count'], 'remaining_count': '0'}))
    basket = engine.scan()
    for leg in basket.legs:
        leg['depth'] = '1' if leg['venue'] == 'kalshi' else '1000'
    monkeypatch.setattr(engine, 'price_pair', lambda **_: (basket, 'ok'))
    engine.execute(basket)
    assert engine.halted == 'missing execution evidence; reconcile before repair'
    assert all(h['recoverable'] for h in engine._halts.values())
    assert not engine.poly_broker.orders()
    assert store.reserved(engine.kalshi_broker.pool_id) > 0


def test_recheck_keeps_entry_floor_and_selected_contracts(setup):
    _, create, build = setup
    engine = build(create(), min_edge=.01)
    basket = engine.scan()
    assert basket is not None
    selected = {(leg['ticker'], leg['contract']) for leg in basket.legs}
    assert engine.price_pair(recheck=selected)[0] is not None
    engine.cfg.min_edge = .9
    assert engine.price_pair(recheck=selected)[0] is None
    # No alternative route may replace an unavailable selected contract.
    assert engine.price_pair(recheck={('missing', 'YES')})[0] is None


def test_stale_rest_pauses_and_new_snapshot_resumes(setup, monkeypatch, caplog):
    store, create, build = setup
    engine = build(create())
    original = engine.poly.book_raw
    stale = copy.deepcopy(original(engine.pair.polymarket_market))
    stale['marketData']['transactTime'] = '2000-01-01T00:00:00Z'
    monkeypatch.setattr(engine.poly, 'book_raw', lambda _: stale)
    caplog.set_level('INFO')
    engine.step()
    assert not engine.halted and not engine.poly_broker.orders()
    assert 'polymarket_rest_freshness_unavailable' in caplog.text
    monkeypatch.setattr(engine.poly, 'book_raw', original)
    engine.step()
    assert engine.poly_broker.state().lots and not engine.halted
    scans = store.events(kinds=('observation',), observations=('scan_complete',))
    assert [e['data']['reason'] for e in scans] == ['polymarket_rest_freshness_unavailable', 'ok']


def test_paper_cannot_fill_stale_book_after_pricing(setup, monkeypatch):
    _, create, build = setup
    engine = build(create())
    basket = engine.scan()
    monkeypatch.setattr(engine, 'price_pair', lambda **_: (basket, 'ok'))
    feed = StreamBook(engine.poly, engine.pair.polymarket_market, 2)
    feed.accept(message(slug=feed.slug))
    engine.stream_book = feed
    adapter = engine.poly_broker.adapter
    original = adapter.create_order
    def stale_before_fill(order, broker):
        feed.received -= 3
        return original(order, broker)
    monkeypatch.setattr(adapter, 'create_order', stale_before_fill)
    engine.execute(basket)
    assert not engine.poly_broker.state().lots
    assert not engine.kalshi_broker.state().lots


def test_quiet_polymarket_book_is_unproven_until_new_exchange_snapshot():
    feed = StreamBook(SimpleNamespace(), 'game', 2)
    old = message()
    feed.accept(old)
    feed.received -= 3
    feed.accept(old)
    with pytest.raises(QuoteUnavailable, match='stale'):
        feed.snapshot()
    feed.accept(message())
    assert feed.snapshot()
    feed.clear()
    with pytest.raises(QuoteUnavailable, match='unavailable'):
        feed.snapshot()


def test_metadata_failure_invalidates_then_recovers_without_stopping():
    changed = threading.Event()
    metadata = Metadata(lambda: {'fee': '.07'}, changed)
    metadata.refresh()
    assert metadata.snapshot() == {'fee': '.07'}
    metadata.fetch = lambda: (_ for _ in ()).throw(ConnectionError())
    with pytest.raises(ConnectionError):
        metadata.refresh()
    with pytest.raises(ConnectionError):
        metadata.snapshot()
    metadata.fetch = lambda: {'fee': '.08'}
    metadata.refresh()
    assert metadata.snapshot() == {'fee': '.08'}
    metadata.started_at -= 16
    with pytest.raises(QuoteUnavailable):
        metadata.snapshot()


@pytest.mark.parametrize('error', [KeyError('market'), TypeError('invalid payload'), ValueError('invalid market')])
def test_metadata_worker_propagates_unexpected_errors(error, caplog):
    metadata = Metadata(lambda: (_ for _ in ()).throw(error), threading.Event())
    metadata._run()
    with pytest.raises(type(error)) as caught:
        metadata.snapshot()
    assert caught.value is error
    assert metadata.changed.is_set()
    assert 'Metadata worker failed' in caplog.text


def test_streaming_pricing_and_paper_execution_do_not_fetch_metadata(setup, monkeypatch):
    _, create, build = setup
    engine = build(create())
    metadata = Metadata(engine._read_metadata, engine.changed)
    metadata.refresh()
    engine.metadata = metadata
    for client, method in [(engine.kalshi, 'market'), (engine.kalshi, 'get'), (engine.poly, 'market_raw')]:
        monkeypatch.setattr(client, method, lambda *a: pytest.fail('metadata REST in price path'))
    basket = engine.scan()
    assert basket is not None
    engine.execute(basket)
    assert engine.kalshi_broker.state().lots and engine.poly_broker.state().lots


def test_stream_update_wakes_loop_before_watchdog(setup, monkeypatch):
    _, create, build = setup
    engine = build(create(), interval_seconds=5)
    engine.cfg.execution_websocket = True
    waiting = threading.Event()
    times = []
    def step(**kwargs):
        times.append(time.monotonic())
        if len(times) == 2:
            raise KeyboardInterrupt()
        waiting.set()
    monkeypatch.setattr(engine, 'step', step)
    def publish():
        assert waiting.wait(1)
        time.sleep(.02)
        engine.changed.set()
    publisher = threading.Thread(target=publish)
    publisher.start()
    with pytest.raises(KeyboardInterrupt):
        engine.run()
    publisher.join()
    assert times[1] - times[0] < 1  # A five-second polling loop would fail this.


def test_diagnostics_do_not_write_between_quote_checks_and_submit(setup, monkeypatch):
    store, create, build = setup
    engine = build(create(), profit_slippage=True, max_basket_dollars=50)
    kinds = []
    original = engine.kalshi_broker.observe
    def observe(kind, data):
        kinds.append(kind)
        return original(kind, data)
    monkeypatch.setattr(engine.kalshi_broker, 'observe', observe)
    engine.step()
    assert 'execution_quote' not in kinds
    assert kinds.index('slippage_reprice') < kinds.index('scan_complete')
    intents = [e for e in store.events(kinds=('order',)) if e['data']['status'] == 'intent']
    assert len(intents) == 2
    assert all(e['data']['context']['execution_quotes'] for e in intents)


def test_watchdog_does_not_duplicate_unchanged_scan_capture(setup):
    store, create, build = setup
    engine = build(create(), min_edge=.9)
    engine.step()
    before = len(store.events(kinds=('observation',), observations=('scan_complete',)))
    engine.step(scan_quotes=False)
    assert len(store.events(kinds=('observation',), observations=('scan_complete',))) == before


def test_terminal_empty_kalshi_order_can_retry_without_monitor_or_cooldown(setup):
    _, create, build = setup
    engine = build(create(mode='live'), profit_slippage=True)
    engine.kalshi_broker.adapter = KalshiLive(SimpleNamespace(create_order=lambda order: {
        'order_id': order['client_order_id'], 'fill_count': '0', 'remaining_count': '0'}))
    basket = engine.scan()
    for leg in basket.legs:
        leg['depth'] = '1' if leg['venue'] == 'kalshi' else '1000'
    engine.price_pair = lambda **_: (basket, 'ok')
    engine.execute(basket)
    assert not engine.live_block() and not engine.risk_block()
    engine.execute(basket)
    assert len(engine.kalshi_broker.orders()) == 2
    assert not engine.poly_broker.orders()


def test_conflicting_same_timestamp_snapshot_cannot_leave_old_open_book():
    feed = StreamBook(SimpleNamespace(), 'game', 2)
    data = message()
    feed.accept(data)
    data['marketData']['state'] = 'MARKET_STATE_SUSPENDED'
    with pytest.raises(QuoteUnavailable, match='conflicting_snapshot'):
        feed.accept(data)
    with pytest.raises(QuoteUnavailable, match='unavailable'):
        feed.snapshot()
