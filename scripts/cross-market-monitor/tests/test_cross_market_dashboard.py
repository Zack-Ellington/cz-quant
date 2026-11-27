from decimal import Decimal as D

import pytest

from czquant import Store
from czquant.accounting import State
from cross_market_dashboard import conditional_profit, seconds, snapshot, trade_rows, windows


def config(event="GAME1"):
    return {"max_sets": 20, "max_capital_frac": 1,
            "experiment": {"id": event, "mode": "live", "portfolio": {"kalshi": event + '-k', "polymarket_us": event + '-p'},
                           "pair": {"venue": "polymarket_us", "kalshi_event": event,
                                    "kalshi_series": "GAMES", "polymarket_market": event,
                                    "outcomes": [{"kalshi": event+'-A', "kalshi_side": "YES",
                                                  "polymarket": 'PMUS:'+event, "polymarket_side": "YES"},
                                                 {"kalshi": event+'-B', "kalshi_side": "YES",
                                                  "polymarket": 'PMUS:'+event, "polymarket_side": "NO"}]}}}


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
    assert data['overall']['positions'] == D(2)
    assert data['overall']['expected_return'] == D('.004')
    assert data['overall_history'][-1]['cash'] == D('39.16')
    assert data['overall_history'][-1]['expected_return'] == D('.004')
    assert data['overall_history'][-2]['positions'] == D(2)
    assert data['overall_history'][-3]['expected_return'] is None  # First leg alone is unhedged.
    assert data['venue_history'][-1]['cash'] == D('99.16')
    assert len(data['games'][0]['positions']) == 2
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


def test_only_scan_quotes_are_used_and_blocked_scans_break_history(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("venue connection"))
    store = Store(tmp_path)
    cfg = config()
    for venue, pid in cfg['experiment']['portfolio'].items():
        store.create_pool(pid, venue, '10')
    store.append('run', {'strategy': 'cross-market-polymarket', 'config': cfg})
    q = {'net': '.06', 'net_per_set': '.06', 'fee_per_set': '.04', 'sets': 1}
    measured = {'experiment_id': 'GAME1', 'status': 'ok',
        'legs': [{'name': 'A', 'kalshi_ask': '.4', 'polymarket_ask': '.5'},
                 {'name': 'B', 'kalshi_ask': '.4', 'polymarket_ask': '.5'}],
        'routes': [{'id': str(i), 'name': str(i), 'one_set': q, 'max_gap': q, 'best': q,
                    'opportunity': True} for i in range(2)]}
    store.append('observation', {'kind': 'cross_market_quote', **measured})
    for _ in range(3001):
        store.append('observation', {'kind': 'scan_complete', 'quote': measured}, 'GAME1-k')
    store.append('observation', {'kind': 'scan_complete', 'quote': {
        'experiment_id': 'GAME1', 'status': 'error', 'reason': 'polymarket_websocket_stale',
        'legs': [], 'routes': []}}, 'GAME1-k')
    store.close()
    before = (tmp_path/'accounting.sqlite3').read_bytes()
    game = snapshot(tmp_path)['games'][0]
    assert (tmp_path/'accounting.sqlite3').read_bytes() == before
    assert len(game['price_history']) == 3002
    assert game['price_history'][0]['routes'][0]['kalshi_price'] == '.4'
    assert game['price_history'][-1]['routes'] == []
    assert game['quote']['reason'] == 'polymarket_websocket_stale'
    assert len(game['route_labels']) == 2


def test_retired_allocations_are_not_selected_for_live_monitoring():
    from cross_market_monitor import selections
    old, new = config("old"), config("new")
    events = [{"sequence": i, "data": {"strategy": "cross-market-polymarket", "config": cfg}}
              for i, cfg in enumerate([old, new])]
    pools = [{"id": pid, "metadata": {"retired_at": "2026-11-27"} if cfg is old else {}}
             for cfg in [old, new] for pid in cfg["experiment"]["portfolio"].values()]
    assert selections(events, pools) == [new]
    pools[-1]["metadata"] = {"retired_at": "2026-11-27"}
    assert selections(events, pools) == []
