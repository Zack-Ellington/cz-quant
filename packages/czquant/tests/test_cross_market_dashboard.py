from decimal import Decimal as D

import pytest

from czquant import Store
from czquant.accounting import State
from czquant.cross_market_dashboard import conditional_profit, seconds, snapshot, trade_rows, windows
from czquant.cross_market_monitor import asks, measure_quotes, quote_route, sample


def config(event="GAME1"):
    return {"max_sets": 20, "max_capital_frac": 1,
            "experiment": {"id": event, "mode": "live", "portfolio": {"kalshi": event + '-k', "polymarket_us": event + '-p'},
                           "pair": {"venue": "polymarket_us", "kalshi_event": event,
                                    "kalshi_series": "GAMES", "polymarket_market": event,
                                    "outcomes": [{"kalshi": event+'-A', "kalshi_side": "YES",
                                                  "polymarket": 'PMUS:'+event, "polymarket_side": "YES"},
                                                 {"kalshi": event+'-B', "kalshi_side": "YES",
                                                  "polymarket": 'PMUS:'+event, "polymarket_side": "NO"}]}}}


def book(yes="0.30", no="0.60", qty="20"):
    return {"orderbook_fp": {"yes_dollars": [[yes, qty]], "no_dollars": [[no, qty]]}}


def test_fees_round_per_order_and_depth_is_walked():
    one = quote_route([(D('.4'), D('20'))], [(D('.4'), D('20'))], 1, D(1), D('.095'))
    ten = quote_route([(D('.4'), D('20'))], [(D('.4'), D('20'))], 10, D(1), D('.095'))
    assert one['fees'] == D('.04')
    assert ten['fees'] == D('.40')
    assert one['net'] == D('.16')
    assert quote_route([(D('.4'), D('.5'))], [(D('.4'), D('20'))], 1, D(1), D('.095')) is None
    walked = quote_route([(D('.4'), D(1)), (D('.5'), D(1))], [(D('.4'), D(2))], 2, D(1), D('.095'))
    assert walked['kalshi_price'] == D('.45')


def test_positive_window_has_a_negative_control_and_cash_constraint():
    cfg = config()
    k = {'GAME1-A': book(), 'GAME1-B': book()}
    available = {'kalshi': D(10), 'polymarket_us': D(10)}
    measured = measure_quotes(cfg, k, book('.5', '.5'), D(1), D('.095'), available, D(20))
    assert all(r['opportunity'] for r in measured['routes'])
    negative = measure_quotes(cfg, {t: book('.5', '.5') for t in k}, book('.5', '.5'),
                              D(1), D('.095'), available, D(20))
    assert not any(r['opportunity'] for r in negative['routes'])
    no_cash = measure_quotes(cfg, k, book('.5', '.5'), D(1), D('.095'), {**available, 'kalshi': D('.1')}, D(20))
    assert not any(r['opportunity'] for r in no_cash['routes'])
    assert no_cash['routes'][0]['one_set']['net'] > 0


def test_no_side_uses_complement_of_yes_bid():
    cfg = config()
    result = measure_quotes(cfg, {'GAME1-A': book(), 'GAME1-B': book()}, book('.7', '.2'),
                            D(1), D('.095'), {'kalshi': D(10), 'polymarket_us': D(10)}, D(20))
    assert result['legs'][0]['polymarket_ask'] == D('.8')
    assert result['legs'][1]['polymarket_ask'] == D('.3')


def test_crossed_book_is_not_an_opportunity():
    with pytest.raises(ValueError, match='crossed'):
        asks(book('.6', '.6'), 'YES')


def sample_at(second, positive=True, status='ok'):
    return {'effective_at': f'2026-11-26T22:00:{second:02d}+00:00',
            'data': {'interval': 5, 'status': status, 'routes': [
                {'id': '0', 'name': 'A + B', 'opportunity': positive, 'best': {'net': '.16'}}]}}


def test_windows_break_on_negative_quotes_errors_and_sampling_outages():
    samples = [sample_at(0), sample_at(5), sample_at(10, False), sample_at(15), sample_at(40)]
    result = windows(samples, seconds('2026-11-26T22:00:45+00:00'))
    assert len(result) == 3
    assert result[-1]['observed_seconds'] == 5
    assert result[-1]['status'] == 'closed'
    assert result[1]['status'] == 'sampling gap'
    assert result[0]['status'] == 'open'
    assert windows([sample_at(0), sample_at(5, status='error')], seconds('2026-11-26T22:00:05+00:00'))[0]['status'] == 'sampling gap'


def test_partial_inventory_has_no_expected_profit():
    state = State(realized=D('.1'), lots={'a': {'market': 'GAME1-A', 'side': 'yes', 'quantity': D(2), 'cost': D('.84')}})
    assert conditional_profit(state, config()['experiment']['pair'])['expected_profit'] is None
    state.lots['b'] = {'market': 'PMUS:GAME1', 'side': 'no', 'quantity': D(2), 'cost': D('.84')}
    assert conditional_profit(state, config()['experiment']['pair'])['expected_profit'] == D('.42')


def test_snapshot_deduplicates_brokers_pools_and_fills(tmp_path):
    store = Store(tmp_path)
    for event in ('GAME1', 'GAME2'):
        cfg = config(event)
        for venue, pid in cfg['experiment']['portfolio'].items():
            store.create_pool(pid, venue, '0', mode='live')
            store.append('transaction', {'reason': 'funding', 'cash_delta': '10', 'external_delta': '10', 'fee': '0', 'legs': []}, pid)
            store.append('run', {'strategy': 'cross-market-polymarket', 'config': cfg}, pid)
    for venue, cash in [('kalshi', '30'), ('polymarket_us', '5')]:
        pid = venue + '-reserve'
        store.create_pool(pid, venue, '0', mode='live', reserve=True)
        store.append('transaction', {'reason': 'funding', 'cash_delta': cash, 'external_delta': cash, 'fee': '0', 'legs': []}, pid)
    context = {'trade_id': 'trade1', 'expected_net': '.16'}
    for pid, market, side in [('GAME1-k', 'GAME1-A', 'yes'), ('GAME1-p', 'PMUS:GAME1', 'no')]:
        request = {'ticker': market, 'count': '1', 'price': '.4', 'side': 'bid', 'client_order_id': pid}
        import json
        store.db.execute('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?)',
                         (pid, pid, None, json.dumps(request), json.dumps(context), 'filled', '0', '1', pid))
        store.append('order', {'status': 'intent', 'order': request}, pid)
        store.append('transaction', {'reason': 'fill', 'cash_delta': '-.42', 'fee': '.02',
                     'order_id': pid, 'context': context,
                     'legs': [{'market': market, 'side': side, 'quantity_delta': '1', 'price': '.4'}]}, pid)
    store.append('health', {'component': 'account', 'status': 'healthy', 'balances': [{'cash': '49.58'}]})
    store.append('account_evidence', {'kind': 'polymarket_us_snapshot', 'balance': {
        'currentBalance': '49.58', 'buyingPower': '49.58', 'depositReservation': '25',
        'displayedBonus': '25', 'availableToWithdraw': '0'}})
    store.close()
    data = snapshot(tmp_path)
    assert len(data['games']) == 2
    assert data['overall']['cash'] == D('39.16')
    assert data['overall']['account_cash'] == D('74.16')
    assert data['overall']['venue_balance'] == D('99.16')
    assert data['accounts'][1]['pending_deposit'] == '25'
    assert data['accounts'][1]['bonus'] == '25'
    assert data['overall']['attempts'] == 1
    assert data['overall']['executed_trades'] == 1
    assert data['overall']['expected_profit'] == D('.16')
    assert data['games'][0]['fills'] == 2
    assert data['games'][0]['trades'][0]['fees'] == D('.04')


def test_acknowledgement_without_booked_fill_is_not_execution():
    orders = [{'id': 'x', 'portfolio_id': 'pool', 'context': {'trade_id': 'basket'},
               'request': {'ticker': 'GAME1-A', 'side': 'bid', 'count': '1', 'price': '.4'},
               'filled': '0', 'status': 'acknowledged'}]
    result = trade_rows(orders, [], {'pool'})
    assert len(result) == 1
    assert result[0]['filled'] is False


def test_preflight_refusal_is_an_attempt_but_not_an_execution():
    event = {'record_id': 'refusal', 'portfolio_id': 'pool', 'type': 'observation',
             'effective_at': '2026-11-26T22:00:00+00:00',
             'data': {'kind': 'reject', 'reason': 'basket_preflight', 'detail': 'insufficient_cash'}}
    result = trade_rows([], [event], {'pool'})
    assert len(result) == 1
    assert result[0]['orders'] == []
    assert result[0]['filled'] is False
    assert 'insufficient_cash' in result[0]['reason']


def test_missing_ledger_is_not_created(tmp_path):
    with pytest.raises(Exception):
        snapshot(tmp_path)
    assert not (tmp_path/'accounting.sqlite3').exists()


def test_public_sampler_does_not_change_cash_or_orders(tmp_path):
    from types import SimpleNamespace
    store = Store(tmp_path)
    cfg = config()
    for venue, pid in cfg['experiment']['portfolio'].items():
        store.create_pool(pid, venue, '10')
    before = store.events(kinds=('transaction', 'order', 'execution'))
    kalshi = SimpleNamespace(market=lambda ticker: {'ticker': ticker, 'event_ticker': 'GAME1', 'status': 'active'},
                             book=lambda _: book(),
                             get=lambda _: {'series': {'fee_type': 'quadratic', 'fee_multiplier': '1'}})
    pmus = SimpleNamespace(market_raw=lambda slug: {'slug': slug, 'status': 'MARKET_STATUS_OPEN', 'feeCoefficient': '.095'},
                           books=lambda slugs: {s: book('.5', '.5') for s in slugs})
    result = sample(store, cfg, kalshi, pmus)
    assert result['status'] == 'ok'
    assert store.events(kinds=('transaction', 'order', 'execution')) == before
    assert store.db.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 0
    store.close()
