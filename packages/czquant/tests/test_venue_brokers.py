"""Venue allocations obey the same ownership and cash rules in paper and live mode."""
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D

import pytest

from czquant import Broker, Store
from czquant.accounting import portfolio_state
from czquant.monitor import market_pass
from czquant.report import compare, generate
from czquant.store import utc


class Pending:
    takes_broker = True

    def __init__(self, venue):
        self.venue, self.sent = venue, []

    def create_order(self, order, broker):
        self.sent.append(order)
        return {"status": "pending", "filled": "0"}


def pool(store, identifier, venue, cash, mode):
    store.create_pool(identifier, venue, cash, mode, **({'subaccount': 3} if venue == 'kalshi' else {}))
    if mode == 'live':
        store.append('transaction', {'reason': 'funding', 'cash_delta': cash, 'external_delta': cash, 'legs': []}, identifier)


def order(ticker, identifier):
    return {"ticker": ticker, "client_order_id": identifier, "side": "bid", "count": "2", "price": "0.5"}


@pytest.mark.parametrize('mode', ['paper', 'live'])
def test_venue_cash_is_separate_and_restarts_do_not_refund(tmp_path, mode):
    store = Store(tmp_path)
    pool(store, 'k', 'kalshi', '1000000000', mode)
    pool(store, 'p', 'polymarket_us', '1', mode)
    store.create_portfolio('experiment', ['k', 'p'])
    a, b = Broker(store, 'k', Pending('kalshi'), 'run'), Broker(store, 'p', Pending('polymarket_us'), 'run')
    assert b.submit(order('PMUS:slug', 'too-large'))['status'] == 'rejected'
    assert not b.adapter.sent and a.available == D('1000000000')
    context = {'arbitrary': {'features': [1, None, 'x']}, 'pool': 'cannot-change-ownership'}
    a.submit(order('M', 'k'), context)
    assert b.available == 1
    a.fill('k', '2', '0.5', '0.01', 'fill-k', {})
    a.finish('k', 'filled')
    expected = a.state().cash
    store.close()
    reopened = Store(tmp_path)
    assert Broker(reopened, 'k').available == expected == D('999999998.99')
    assert len(Broker(reopened, 'k').state().lots) == 1
    assert Broker(reopened, 'p').state().cash == 1 and not Broker(reopened, 'p').state().lots
    assert Broker(reopened, 'k').state().contributed == D('1000000000')
    fill = next(e for e in reopened.events('k') if e['data'].get('reason') == 'fill')
    assert fill['data']['context'] == context and fill['run_id'] == 'run'
    with pytest.raises(ValueError):
        Broker(reopened, 'experiment')
    with pytest.raises(ValueError):
        reopened.create_pool('k', 'kalshi', '1000000000')


@pytest.mark.parametrize('mode', ['paper', 'live'])
def test_concurrent_writers_cannot_overspend_the_same_pool(tmp_path, mode):
    store = Store(tmp_path)
    pool(store, 'k', 'kalshi', '1.5', mode)
    store.close()

    def submit(i):
        connection = Store(tmp_path)
        result = Broker(connection, 'k', Pending('kalshi')).submit(order('M', str(i)))
        connection.close()
        return result['status']

    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(submit, range(4)))
    assert results.count('pending') == 1 and results.count('rejected') == 3


@pytest.mark.parametrize('venue,ticker', [('polymarket', 'PM:token'), ('polymarket_us', 'PMUS:slug')])
def test_wrong_venue_rejected_before_intent_or_adapter_call(tmp_path, venue, ticker):
    store = Store(tmp_path)
    store.create_pool('k', 'kalshi', '10')
    store.create_pool('p', venue, '10')
    a, b = Broker(store, 'k', Pending('kalshi')), Broker(store, 'p', Pending(venue))
    before = store.events()
    for broker, wrong in ((a, ticker), (b, 'M')):
        with pytest.raises(ValueError, match='broker cannot'):
            broker.submit(order(wrong, 'wrong'))
    assert not a.adapter.sent and not b.adapter.sent and store.events() == before
    with pytest.raises(ValueError, match='different cash-pool venue'):
        Broker(store, 'k', b.adapter)


def test_wrong_pool_cannot_mutate_order_lot_or_receipt(tmp_path):
    store = Store(tmp_path)
    for name in ('a', 'b'):
        store.create_pool(name, 'kalshi', '10')
    a, b = Broker(store, 'a', Pending('kalshi')), Broker(store, 'b', Pending('kalshi'))
    b.submit(order('M', 'b-order'))
    b.fill('b-order', '2', '0.5', '0', 'fill-b', {})
    target = next(iter(b.state().lots))
    before = store.events()
    operations = [lambda: a.cancel('b-order'), lambda: a.finish('b-order', 'filled'),
                  lambda: a.fill('b-order', '2', '0.5', '0', 'fill-b', {}),
                  lambda: a.settle(target, '1', {}), lambda: a.correct([target], 'wrong owner')]
    for operation in operations:
        with pytest.raises(ValueError):
            operation()
        assert store.events() == before
    b.correct([target], 'fixture correction')
    assert not b.state().lots and b.state().cash == 10


@pytest.mark.parametrize('final', [False, True])
def test_market_collector_follows_live_holdings_without_paper_settlement(tmp_path, final):
    store = Store(tmp_path)
    pool(store, 'live', 'kalshi', '100', 'live')
    store.append('transaction', {'reason': 'fill', 'cash_delta': '-1', 'fee': '0', 'legs': [
        {'market': 'M', 'side': 'yes', 'quantity_delta': '2', 'price': '0.5'}]}, 'live')
    seen = []

    class Market:
        def market(self, ticker):
            seen.append(ticker)
            return {'status': 'finalized' if final else 'active', 'settlement_value_dollars': '1'}

        def book(self, ticker):
            return {'orderbook_fp': {'yes_dollars': [['0.4', '10']], 'no_dollars': [['0.4', '10']]}}

    status = market_pass(store, Market())
    assert status['status'] == 'healthy' and seen == ['M']
    assert len(Broker(store, 'live').state().lots) == 1
    assert not store.events('live', kinds=('snapshot',))


def test_report_group_requires_both_accounts_and_records_membership(tmp_path):
    store = Store(tmp_path)
    pool(store, 'k', 'kalshi', '600', 'live')
    pool(store, 'p', 'polymarket_us', '400', 'live')
    store.create_portfolio('experiment', ['k', 'p'])
    store.append('health', {'component': 'account', 'status': 'healthy'})
    at = utc()
    metrics = {m['portfolio']: m for m in compare(store, at, at)[0]}
    assert metrics['k']['complete'] and not metrics['experiment']['complete']
    store.append('health', {'component': 'polymarket_us', 'status': 'healthy'})
    at = utc()
    assert all(m['complete'] for m in compare(store, at, at)[0])
    output = tmp_path / 'reports' / 'fixture'
    manifest = generate(str(tmp_path), at, at, str(output))
    assert next(u for u in manifest['reporting_units'] if u['id'] == 'experiment')['pools'] == ['k', 'p']
    assert all(len(digest) == 64 for digest in manifest['implementation_sha256'].values())
    assert '| experiment | portfolio | live |' in (output / 'comparison.md').read_text()
    assert json.loads((output / 'manifest.json').read_text())['snapshot_sha256'] == manifest['snapshot_sha256']


def test_historical_pooled_capital_is_report_only_and_deprecated(tmp_path):
    store = Store(tmp_path)
    # A pre-pool database has ownership rows, but no cash_pools entries.
    store.db.execute('INSERT INTO portfolios VALUES(?,?,?,?)', ('old', 'paper', utc(), '{}'))
    store.append('transaction', {'reason': 'funding', 'cash_delta': '10', 'external_delta': '10', 'legs': []}, 'old')
    store.append('transaction', {'reason': 'fill', 'cash_delta': '-1', 'fee': '0', 'legs': [
        {'market': 'RETIRED:selection', 'side': 'yes', 'quantity_delta': '2', 'price': '0.5'}]}, 'old')
    original = store.events('old')
    store.db.execute('DROP TABLE cash_pools')
    store.db.execute('DROP TABLE reporting_portfolios')
    store.close()
    store = Store(tmp_path)  # open a database with the original schema
    assert store.events('old') == original
    assert portfolio_state(store, 'old').cash == 9
    with pytest.raises(ValueError, match='legacy portfolios are report-only'):
        Broker(store, 'old')
    at = utc()
    manifest = generate(str(tmp_path), at, at, str(tmp_path / 'reports' / 'old'))
    assert manifest['deprecations']
    assert manifest['metrics'][0]['deprecated'] and manifest['metrics'][0]['open_lots'] == 1
    assert not manifest['metrics'][0]['complete']


def test_reports_sum_dollar_results_and_compute_returns_from_total_capital(tmp_path):
    store = Store(tmp_path)
    store.create_pool('k', 'kalshi', '600')
    store.create_pool('p', 'polymarket_us', '400')
    store.create_portfolio('experiment', ['k', 'p'])
    start = utc()
    store.append('transaction', {'reason': 'reward', 'cash_delta': '60', 'legs': []}, 'k')
    store.append('transaction', {'reason': 'fee', 'cash_delta': '-40', 'fee': '40', 'legs': []}, 'p')
    metrics = {m['portfolio']: m for m in compare(store, start, utc())[0]}
    assert metrics['k']['return_on_opening_equity'] == D('.1')
    assert metrics['p']['return_on_opening_equity'] == D('-.1')
    total = metrics['experiment']
    assert total['net_pnl'] == 20 and total['return_on_opening_equity'] == D('.02')
    assert total['time_weighted_return'] == D('.02') and total['ending_equity'] == 1020
    assert total['available'] is None  # total cash is not venue buying power


def test_shared_account_allocation_is_not_portfolio_profit_or_external_funding(tmp_path):
    from czquant.operator import allocate
    store = Store(tmp_path)
    pool(store, 'a', 'kalshi', '600', 'live')
    pool(store, 'b', 'kalshi', '400', 'live')
    store.create_portfolio('account-experiment', ['a', 'b'])
    start = utc()
    allocate(store, 'a', 'b', D(100))
    store.append('health', {'component': 'account', 'status': 'healthy'})
    metrics = {m['portfolio']: m for m in compare(store, start, utc())[0]}
    assert metrics['a']['external_flows'] == -100 and metrics['b']['external_flows'] == 100
    total = metrics['account-experiment']
    assert total['external_flows'] == total['net_pnl'] == total['time_weighted_return'] == 0
    assert total['return_on_opening_equity'] == total['max_drawdown'] == 0
    assert (Broker(store, 'a').available, Broker(store, 'b').available) == (500, 500)


@pytest.mark.parametrize('mode', ['paper', 'live'])
def test_unknown_order_keeps_its_pool_reservation_in_reports(tmp_path, mode):
    store = Store(tmp_path)
    pool(store, 'p', 'kalshi', '10', mode)
    broker = Broker(store, 'p', Pending('kalshi'))
    broker.submit(order('M', 'o'))
    broker.finish('o', 'unknown', release=False)
    at = utc()
    row = compare(store, at, at)[0][0]
    assert row['reserved'] == store.reserved('p') == D('1.04')
    assert row['available'] == broker.available == D('8.96')
    broker.finish('o', 'canceled')
    at = utc()
    assert compare(store, at, at)[0][0]['available'] == 10


@pytest.mark.parametrize('mode', ['paper', 'live'])
def test_closing_owned_positions_needs_no_other_venue_cash(tmp_path, mode):
    store = Store(tmp_path)
    pool(store, 'p', 'kalshi', '1.04', mode)
    broker = Broker(store, 'p', Pending('kalshi'))
    broker.submit(order('M', 'buy'))
    broker.fill('buy', '2', '0.5', '0.04', 'fill-buy', {})
    broker.finish('buy', 'filled')
    assert broker.available == 0
    closing = {**order('M', 'close'), 'side': 'ask', 'reduce_only': True}
    assert broker.submit(closing)['status'] == 'pending'
    with pytest.raises(ValueError, match='unreserved holdings'):
        broker.submit({**closing, 'client_order_id': 'duplicate-close'})
    broker.fill('close', '2', '0.5', '0.04', 'fill-close', {})
    broker.finish('close', 'filled')
    assert broker.available == D('.96') and not broker.state().lots


def test_portfolio_membership_is_explicit_unique_and_same_mode(tmp_path):
    store = Store(tmp_path)
    pool(store, 'paper', 'kalshi', '100', 'paper')
    pool(store, 'live', 'kalshi', '100', 'live')
    for members in ([], ['paper', 'paper'], ['unknown'], ['paper', 'live']):
        with pytest.raises(ValueError):
            store.create_portfolio('invalid', members)
    store.create_portfolio('valid', ['paper'])
    assert Broker(store, 'paper').available == 100


def test_concurrent_shared_account_allocations_cannot_spend_the_same_cash(tmp_path):
    from czquant.operator import allocate
    store = Store(tmp_path)
    for identifier, cash in [('a', '100'), ('b', '0'), ('c', '0')]:
        pool(store, identifier, 'kalshi', cash, 'live')
    store.close()

    def move(target):
        connection = Store(tmp_path)
        try:
            allocate(connection, 'a', target, D(80))
            return True
        except ValueError:
            return False
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(move, ['b', 'c'])) == [False, True]
    reopened = Store(tmp_path)
    assert Broker(reopened, 'a').available == 20
    assert sum(Broker(reopened, p).state().cash for p in ('a', 'b', 'c')) == 100
