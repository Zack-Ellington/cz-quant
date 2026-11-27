import threading
import time
from types import SimpleNamespace
import pytest
from kalshi_stream import KalshiStreamBook
from czquant.polymarket_us import QuoteUnavailable
from tests.test_experiments import setup


def snapshot(ticker='A', seq=1):
    return {'type': 'orderbook_snapshot', 'sid': 7, 'seq': seq, 'sending_ts_ms': time.time()*1000,
            'msg': {'market_ticker': ticker, 'yes_dollars_fp': [['.4', '10']], 'no_dollars_fp': [['.5', '20']]}}


def ready():
    f = KalshiStreamBook(SimpleNamespace(), ['A', 'B'], 2)
    f.accept(snapshot()); f.accept(snapshot('B', 2)); f.confirmed_pong()
    return f


def delta(seq=3, quantity='-3'):
    return {'type': 'orderbook_delta', 'sid': 7, 'seq': seq, 'sending_ts_ms': time.time()*1000,
            'msg': {'market_ticker': 'A', 'side': 'yes', 'price_dollars': '.4', 'delta_fp': quantity}}


def test_quiet_book_and_dead_connection():
    f = ready(); f.received['A'] -= 60
    assert f.snapshot('A')['execution_quote']['receive_age_seconds'] >= 60
    f.healthy_at -= 3
    with pytest.raises(QuoteUnavailable, match='connection_stale'): f.snapshot('A')
    f.confirmed_pong()
    assert f.snapshot('A')['orderbook_fp']['yes_dollars'] == [['0.4', '10']]
    f.clear()
    with pytest.raises(QuoteUnavailable): f.snapshot('A')


def test_deltas_and_replacement():
    f = ready(); f.accept(delta())
    assert f.snapshot('A')['orderbook_fp']['yes_dollars'] == [['0.4', '7']]
    f.accept(delta(4, '-7'))
    assert f.snapshot('A')['orderbook_fp']['yes_dollars'] == []
    f.accept(snapshot('A', 5))
    assert f.snapshot('A')['orderbook_fp']['yes_dollars'] == [['0.4', '10']]


@pytest.mark.parametrize('seq', [2, 4])
def test_gap_or_duplicate_clears_all_books(seq):
    f = ready()
    with pytest.raises(QuoteUnavailable, match='sequence_gap'): f.accept(delta(seq))
    with pytest.raises(QuoteUnavailable): f.snapshot('B')
    f.accept(snapshot()); f.confirmed_pong()
    with pytest.raises(QuoteUnavailable): f.snapshot('A')
    f.accept(snapshot('B', 2))
    assert f.snapshot('A')


def test_delayed_invalid_and_missing_snapshot():
    f = ready(); f.clear()
    with pytest.raises(QuoteUnavailable, match='missing_snapshot'): f.accept(delta())
    f = ready(); m = delta(); m['sending_ts_ms'] -= 3000
    with pytest.raises(QuoteUnavailable, match='delayed_message'): f.accept(m)
    assert not f.books
    f = ready()
    with pytest.raises(ValueError, match='negative'): f.accept(delta(quantity='-11'))
    assert not f.books


def test_gap_reconnects_and_resigns(monkeypatch):
    import json
    from contextlib import contextmanager
    signatures, subscriptions = [], []
    client = SimpleNamespace(base='https://external-api.kalshi.com/trade-api/v2', headers=lambda *a: signatures.append(a) or {})
    f = KalshiStreamBook(client, ['A', 'B'], 2)
    class Socket:
        def send(self, data): subscriptions.append(data)
        def ping(self):
            pong = threading.Event(); pong.set(); return pong
        def recv(self, **kwargs):
            if len(signatures) == 1: return json.dumps(messages.pop(0))
            assert not f.books
            f.stop.set(); raise TimeoutError
    messages = [snapshot(), snapshot('B', 2), delta(4)]
    @contextmanager
    def connect(*a, **k): yield Socket()
    monkeypatch.setattr('kalshi_stream.connect', connect)
    monkeypatch.setattr(f.stop, 'wait', lambda *a: None)
    f._run()
    assert signatures == [('GET', '/trade-api/ws/v2')] * 2
    assert len(subscriptions) == 2 and not f.books


def test_scan_and_submission_have_no_rest_fallback(setup, monkeypatch):
    store, create, build = setup
    engine = build(create()); basket = engine.scan()
    assert basket is not None
    tickers = sorted({t for t, _ in engine.pair.contracts('kalshi')})
    f = KalshiStreamBook(SimpleNamespace(), tickers, 2)
    for seq, ticker in enumerate(tickers, 1): f.accept(snapshot(ticker, seq))
    f.confirmed_pong(); engine.kalshi_stream = f
    monkeypatch.setattr(engine.kalshi, 'book', lambda *a: pytest.fail('REST fallback'))
    engine.scan()
    q = store.events(kinds=('observation',))[-1]['data']['quote']
    assert all(b['execution_quote']['source']=='kalshi_websocket' for b in q['books']['kalshi'].values())
    f.clear()
    assert engine.scan() is None
    monkeypatch.setattr(engine, 'price_pair', lambda *_, **__: (basket, 'ok'))
    engine.execute(basket)
    assert store.db.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 0
    assert engine.cooldown_until == 0
    leg = next(leg for leg in basket.legs if leg['venue'] == 'kalshi')
    with pytest.raises(QuoteUnavailable): engine.unwind(leg, 1, {})


def test_missing_pong_invalidates_quiet_connection(monkeypatch):
    from contextlib import contextmanager
    clock = [100.0]
    monkeypatch.setattr('kalshi_stream.time.monotonic', lambda: clock[0])
    client = SimpleNamespace(base='https://external-api.kalshi.com/trade-api/v2', headers=lambda *a: {})
    f = KalshiStreamBook(client, ['A', 'B'], 2)
    class Socket:
        def send(self, data): pass
        def ping(self): return threading.Event()
        def recv(self, **kwargs):
            clock[0] += 1.1
            raise TimeoutError
    @contextmanager
    def connect(*a, **k): yield Socket()
    monkeypatch.setattr('kalshi_stream.connect', connect)
    monkeypatch.setattr(f.stop, 'wait', lambda *a: f.stop.set())
    f._run()
    assert not f.books and f.healthy_at == 0


def test_connection_loss_after_fill_blocks_hedge_and_preserves_repair(setup, monkeypatch):
    store, create, build = setup
    engine = build(create(), profit_slippage=True, max_basket_dollars=50)
    basket = engine.scan()
    assert basket is not None
    for leg in basket.legs:
        leg['depth'] = '1' if leg['venue'] == 'kalshi' else '100'
    monkeypatch.setattr(engine, 'price_pair', lambda *_, **__: (basket, 'ok'))
    tickers = sorted({t for t, _ in engine.pair.contracts('kalshi')})
    f = KalshiStreamBook(SimpleNamespace(), tickers, 2)
    for seq, ticker in enumerate(tickers, 1): f.accept(snapshot(ticker, seq))
    f.confirmed_pong(); engine.kalshi_stream = f
    first = min(basket.legs, key=lambda leg: float(leg['depth']))
    broker = engine.brokers[first['venue']]
    original = broker.adapter.create_order
    def disconnect(order, bound):
        result = original(order, bound); f.clear(); return result
    monkeypatch.setattr(broker.adapter, 'create_order', disconnect)
    engine.execute(basket)
    assert len([e for e in store.events(kinds=('order',)) if e['data']['status']=='intent']) == 1
    assert engine.halted and broker.state().lots
