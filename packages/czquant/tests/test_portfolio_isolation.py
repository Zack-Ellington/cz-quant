"""Offline reproductions from PR #31's design-principles review.

Run: uv run pytest -q tests/test_portfolio_isolation.py
Every venue is fake; stores are temporary. No credentials or network are used.
"""
from decimal import Decimal as D

import pytest

from czquant import Broker, Store
from czquant.accounting import portfolio_state
from czquant.accounts import (allocate, close_sub, move_lots, open_sub, register_kalshi,
                              register_paper, register_polymarket_us)
from czquant.monitor import account_pass, polymarket_us_pass
from czquant.report import compare
from czquant.store import utc
from test_virtual_subaccounts import Fills, Kalshi, cash, order
from test_polymarket_us import FakeUS


class Pending:
    venue, mode = 'kalshi', 'live'

    def __init__(self, confirmed='0', terminal=False):
        self.confirmed, self.terminal = confirmed, terminal
        self.sent = []

    def create_order(self, request):
        self.sent.append(request)
        return {'order': {'order_id': request['client_order_id'],
                         'status': 'executed' if self.terminal else 'resting',
                         'fill_count_fp': self.confirmed}}

    def amend_order(self, identifier, request):
        self.sent.append(request)
        return {'order_id': identifier}


def account_with_pools(tmp_path, mode='live', amounts=('20', '20')):
    store, api = Store(tmp_path), Kalshi('50')
    if mode == 'live':
        register_kalshi(store, api, 'account', 0, primary=True)
    else:
        register_paper(store, 'account', 'kalshi', '50')
    for name, amount in zip(('a', 'b'), amounts):
        open_sub(store, name, 'account', amount, api=api)
    return store, api


@pytest.mark.parametrize('mode', ['paper', 'live'])
def test_five_ten_dollar_pools_keep_the_winners_twelve(tmp_path, mode):
    store, api = Store(tmp_path), Kalshi('50')
    if mode == 'live':
        register_kalshi(store, api, 'account', 0, primary=True)
    else:
        register_paper(store, 'account', 'kalshi', '50')
    for i in range(5):
        open_sub(store, f'game{i}', 'account', '10', api=api)
    winner = Broker(store, 'game0', Fills(mode=mode))
    winner.submit(order(count='10', price='0.4'))
    winner.submit(order(side='ask', count='10', price='0.6'))
    api.dollars = D('52')
    assert cash(store, 'game0') == 12 and cash(store, 'account:reserve') == 0
    with pytest.raises(ValueError, match='free'):
        open_sub(store, 'sixth', 'account', '2', api=api)
    assert not any(p['id'] == 'sixth' for p in store.pools())
    assert close_sub(store, 'game0', api=api) == 12
    assert cash(store, 'account:reserve') == 12
    assert Broker(Store(tmp_path), 'game1').available == 10


def test_live_allocation_rechecks_cash_without_monitor_dependency(tmp_path):
    store, api = account_with_pools(tmp_path, amounts=('10', '0'))
    api.dollars = D('20')
    # Even without a running monitor, current backing must be checked.
    with pytest.raises(ValueError, match='reconcile'):
        open_sub(store, 'excess', 'account', '40', api=api)
    assert not any(p['id'] == 'excess' for p in store.pools())
    assert account_pass(store, api)['status'] == 'degraded'
    with pytest.raises(ValueError, match='reconcile'):
        allocate(store, 'account:reserve', 'b', '40', api=api)
    assert cash(store, 'b') == 0
    api.dollars = D('50')
    allocate(store, 'account:reserve', 'b', '40', api=api)
    assert cash(store, 'b') == 40


class RebateUS(FakeUS):
    def activities(self):
        yield {'type': 'ACTIVITY_TYPE_TAKER_FEE_REBATE', 'accountBalanceChange': {
            'transactionId': 'rebate1', 'amount': {'value': '2'},
            'status': 'ACCOUNT_BALANCE_CHANGE_STATUS_COMPLETED',
            'updateTime': '2100-01-01T00:00:00Z'}}


def test_shared_rebate_with_unknown_owner_is_not_spendable(tmp_path):
    store, api = Store(tmp_path), RebateUS()
    api.cash = D('50')
    register_polymarket_us(store, api, 'us')
    open_sub(store, 'game', 'us', '50', api=api)
    api.cash = D('52')
    for _ in range(2):
        health = polymarket_us_pass(store, api)
        assert health['status'] == 'degraded'
        assert any(d['kind'] == 'unattributed_rebate' for d in health['discrepancies'])
    assert cash(store, 'us:reserve') == 0 and cash(store, 'game') == 50
    with pytest.raises(ValueError, match='free'):
        open_sub(store, 'another-game', 'us', '2', api=api)
    assert len(store.events(kinds=('quarantine',))) == 1


def test_polymarket_allocation_rechecks_backing(tmp_path):
    store, api = Store(tmp_path), FakeUS()
    api.cash = D('50')
    register_polymarket_us(store, api, 'us')
    api.cash = D('20')
    with pytest.raises(ValueError, match='reconcile'):
        open_sub(store, 'game', 'us', '50', api=api)


def test_confirmed_unbooked_fill_can_be_closed_beside_same_side_sibling(tmp_path):
    store, _ = account_with_pools(tmp_path)
    Broker(store, 'b', Fills(mode='live')).submit(order(count='1'))
    a = Broker(store, 'a', Pending(confirmed='10', terminal=True))
    a.submit(order(count='10'))
    result = a.submit({**order(side='ask', count='10'), 'reduce_only': True})
    assert result.get('status') != 'rejected'
    assert len(a.adapter.sent) == 2


@pytest.mark.parametrize('mode', ['paper', 'live'])
def test_shared_accounts_block_opposite_ownership_in_both_modes(tmp_path, mode):
    store, _ = account_with_pools(tmp_path, mode)
    Broker(store, 'a', Fills(mode=mode)).submit(order(count='10'))
    result = Broker(store, 'b', Fills(mode=mode)).submit(order(side='ask'))
    assert result['status'] == 'rejected'


def test_pending_order_that_flips_sides_blocks_sibling(tmp_path):
    store, _ = account_with_pools(tmp_path)
    Broker(store, 'a', Fills(mode='live')).submit(order(count='10'))
    Broker(store, 'a', Pending()).submit(order(side='ask', count='20', price='0.5'))
    result = Broker(store, 'b', Fills(mode='live')).submit(order(count='1'))
    assert result['status'] == 'rejected'


def test_pending_close_cannot_be_reused_to_open_opposite_sibling_position(tmp_path):
    store, _ = account_with_pools(tmp_path)
    for pid in ('a', 'b'):
        Broker(store, pid, Fills(mode='live')).submit(order(count='10'))
    a = Broker(store, 'a', Pending())
    a.submit(order(side='ask', count='10'))
    result = a.submit(order(side='ask', count='10'))
    assert result['status'] == 'rejected'


def test_amend_cannot_turn_a_close_into_cross_pool_netting(tmp_path):
    store, _ = account_with_pools(tmp_path)
    for pid in ('a', 'b'):
        Broker(store, pid, Fills(mode='live')).submit(order(count='10'))
    adapter = Pending()
    a = Broker(store, 'a', adapter)
    a.submit({**order(side='ask', count='10'), 'client_order_id': 'close'})
    with pytest.raises(ValueError, match='opposite side'):
        a.amend('close', {'count': '20'})
    assert len(adapter.sent) == 1


def test_move_lots_and_close_leave_no_phantom_equity(tmp_path):
    store, _ = account_with_pools(tmp_path, 'paper', amounts=('10', '0'))
    Broker(store, 'a', Fills()).submit(order(count='10', price='0.4'))
    store.create_portfolio('combined', ['a', 'b'])
    start = utc()
    move_lots(store, 'a', 'b', 'M', 'yes')
    assert close_sub(store, 'a') == 6
    a, b = portfolio_state(store, 'a'), portfolio_state(store, 'b')
    assert a.prepaid == b.prepaid == 0
    assert a.cash == a.cost == a.realized == 0
    assert b.cost == 4 and b.contributed == 4 and b.realized == 0
    at = utc()
    metrics = {m['portfolio']: m for m in compare(store, start, at)[0]}
    assert metrics['a']['ending_equity'] == 0
    # Transfer basis cancels inside the group; only returned cash is external.
    assert metrics['combined']['external_flows'] == -6
    buyer = Broker(store, 'b', Fills())
    buyer.submit(order(side='ask', count='10', price='0.6'))
    assert buyer.state().realized == 2 and buyer.state().prepaid == 0


class PartialClose(Pending):
    takes_broker = True

    def create_order(self, request, broker):
        broker.fill(request['client_order_id'], '10', '1', '0', 'partial-close', {})
        broker.acknowledge(request['client_order_id'], 'partial-close', {}, '10', False)
        return {'status': 'acknowledged', 'filled': '10'}


def test_close_refuses_zero_reservation_unresolved_order(tmp_path):
    store, api = account_with_pools(tmp_path, amounts=('10', '0'))
    Broker(store, 'a', Fills(mode='live')).submit(order(count='10'))
    Broker(store, 'a', PartialClose()).submit(order(side='ask', count='20', price='1'))
    assert store.reserved('a') == 0 and not portfolio_state(store, 'a').lots
    with pytest.raises(ValueError, match='orders open'):
        close_sub(store, 'a', api=api)
    assert cash(store, 'a') == 16


def test_running_broker_observes_retirement(tmp_path):
    store, _ = account_with_pools(tmp_path, 'paper', amounts=('10', '0'))
    broker = Broker(Store(tmp_path), 'a', Fills())
    close_sub(store, 'a')
    with pytest.raises(ValueError, match='retired|active'):
        broker.submit(order(count='1', price='0'))


def test_amendment_exposure_is_visible_before_the_venue_replies(tmp_path):
    store, _ = account_with_pools(tmp_path)
    Broker(store, 'a', Fills(mode='live')).submit(order(count='10'))
    sibling = Broker(Store(tmp_path), 'b', Fills(mode='live'))

    class LostAmendment(Pending):
        def amend_order(self, identifier, request):
            assert sibling.submit(order(count='1'))['status'] == 'rejected'
            raise TimeoutError('unknown amendment outcome')

    a = Broker(store, 'a', LostAmendment())
    a.submit({**order(side='ask', count='10'), 'client_order_id': 'close'})
    with pytest.raises(TimeoutError):
        a.amend('close', {'count': '20'})
    assert sibling.submit(order(count='1'))['status'] == 'rejected'


def test_independent_paper_accounts_can_hold_opposite_positions(tmp_path):
    store = Store(tmp_path)
    for pid in ('a', 'b'):
        register_paper(store, pid, 'kalshi', '10')
        open_sub(store, pid + '-game', pid, '10')
    assert Broker(store, 'a-game', Fills()).submit(order())['status'] == 'executed'
    assert Broker(store, 'b-game', Fills()).submit(order(side='ask'))['status'] == 'executed'
