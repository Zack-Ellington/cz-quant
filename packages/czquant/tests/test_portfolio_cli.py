"""Portfolio CLI lifecycle with isolated paper state and no venue access."""
import json
import sys
from decimal import Decimal

import pytest

from czquant.cli import main
from czquant.portfolios import create, delete, details
from czquant.store import Store, utc


def config():
    return {'id': 'game', 'pools': [
        {'id': 'k', 'venue': 'kalshi', 'cash': 60},
        {'id': 'u', 'venue': 'polymarket_us', 'cash': 40}]}


def test_create_list_inspect_and_refuse_refunding(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'portfolio.yaml'
    path.write_text('id: game\npools:\n  - {id: k, venue: kalshi, cash: 60}\n  - {id: u, venue: polymarket_us, cash: 40}\n')

    def invoke(*args):
        monkeypatch.setattr(sys, 'argv', ['czquant', '--runs-dir', str(tmp_path / 'state'), *args])
        main()
        return json.loads(capsys.readouterr().out)

    result = invoke('portfolios', 'create', '--config', str(path))
    assert result['pnl']['cash'] == '100' and result['pnl']['net_pnl'] == '0'
    assert {p['id'] for p in invoke('portfolios', 'list')} == {'game', 'k', 'u'}
    result = invoke('portfolios', 'inspect', 'game', '--events', '1')
    assert len(result['events']) == 1 and result['event_count'] == 2
    with pytest.raises(SystemExit):
        invoke('portfolios', 'create', '--config', str(path))
    capsys.readouterr()
    assert invoke('portfolios', 'inspect', 'game')['pnl']['cash'] == '100'
    assert invoke('portfolios', 'delete', 'game')['metadata']['retired_at']
    assert invoke('portfolios', 'list') == []
    assert len(invoke('portfolios', 'list', '--all')) == 3
    assert invoke('portfolios', 'inspect', 'game')['pnl']['net_pnl'] == '0'


@pytest.mark.parametrize('second', [
    {'id': 'u', 'venue': 'missing', 'cash': 40},
    {'id': 'k', 'venue': 'polymarket_us', 'cash': 40},
    {'id': 'u', 'venue': 'polymarket_us', 'cash': '.nan'},
    'nonexistent',
])
def test_invalid_creation_rolls_back_all_funding(tmp_path, second):
    store = Store(tmp_path)
    data = config()
    data['pools'][1] = second
    with pytest.raises((ValueError, ArithmeticError)):
        create(store, data)
    assert store.pools() == [] and store.events() == [] and store.reporting_portfolios() == []
    store.close()


def test_existing_live_pools_are_grouped_without_funding(tmp_path):
    store = Store(tmp_path)
    store.create_pool('live', 'kalshi', mode='live')
    create(store, {'id': 'group', 'pools': ['live']})
    assert details(store, 'group')['mode'] == 'live'
    assert store.events() == []
    with pytest.raises(ValueError, match='unknown'):
        details(store, 'missing')
    store.close()


def test_positions_pnl_and_events_exclude_other_portfolios(tmp_path):
    store = Store(tmp_path)
    create(store, config())
    store.create_pool('unrelated', 'kalshi', '999')
    store.append('transaction', {'reason': 'fill', 'cash_delta': '-4.1', 'external_delta': '0',
        'fee': '.1', 'legs': [{'market': 'GAME-A', 'side': 'yes', 'quantity_delta': '10', 'price': '.4'}]}, 'k')
    unmarked = details(store, 'game')
    assert unmarked['pnl']['net_pnl'] is None
    assert unmarked['pnl']['unmarked_cost'] == Decimal('4.1')
    store.append('market', {'ticker': 'GAME-A', 'book': {
        'yes_dollars': [['0.5', '100']], 'no_dollars': [['0.4', '100']]}}, at=utc())
    result = details(store, 'game', 100)
    assert result['pnl']['net_pnl'] == Decimal('1.4')
    assert result['pnl']['fees'] == Decimal('.1')
    assert result['positions'][0]['pool_id'] == 'k'
    assert {e['portfolio_id'] for e in result['events']} == {'k', 'u'}
    store.close()


def test_retirement_preserves_losses_and_blocks_open_broker(tmp_path):
    from czquant import Broker
    from test_virtual_subaccounts import Fills, order

    store = Store(tmp_path)
    create(store, config())
    broker = Broker(store, 'k', Fills())
    store.append('transaction', {'reason': 'fee', 'cash_delta': '-1', 'external_delta': '0',
                                'fee': '1', 'legs': []}, 'k')
    before = details(store, 'game')['pnl']['net_pnl']
    delete(store, 'game')
    result = details(store, 'game')
    assert result['metadata']['retired_at']
    assert result['pnl']['net_pnl'] == before == -1
    assert result['pnl']['cash'] == 0 and result['pnl']['contributed'] == 1
    with pytest.raises(ValueError, match='retired'):
        broker.submit(order())
    count = len(store.events())
    delete(store, 'game')
    assert len(store.events()) == count
    with pytest.raises(ValueError):
        create(store, config())
    store.close()


@pytest.mark.parametrize('obstruction', ['position', 'pending', 'unknown', 'acknowledged', 'reservation', 'shared'])
def test_retirement_refuses_exposure_or_shared_pools_without_changes(tmp_path, obstruction):
    store = Store(tmp_path)
    create(store, config())
    if obstruction == 'position':
        store.append('transaction', {'reason': 'fill', 'cash_delta': '-1', 'external_delta': '0',
            'fee': '0', 'legs': [{'market': 'PMUS:game', 'side': 'yes', 'quantity_delta': '2', 'price': '.5'}]}, 'u')
    elif obstruction == 'shared':
        store.create_portfolio('other', ['u'])
    else:
        store.db.execute("INSERT INTO orders(id,portfolio_id,request,context,status,reserved) VALUES(?,?,?,?,?,?)",
                         ('order', 'u', '{}', '{}', 'filled' if obstruction == 'reservation' else obstruction,
                          '1' if obstruction == 'reservation' else '0'))
    before = store.events()
    with pytest.raises(ValueError):
        delete(store, 'game')
    assert store.events() == before
    assert all(not p['metadata'].get('retired_at') for p in store.pools())
    store.close()


def test_registered_allocations_return_to_reserves_atomically(tmp_path, monkeypatch):
    from czquant import accounts
    from czquant.accounting import portfolio_state

    store = Store(tmp_path)
    accounts.register_paper(store, 'acct', 'kalshi', '100')
    accounts.open_sub(store, 'a', 'acct', '30')
    accounts.open_sub(store, 'b', 'acct', '20')
    store.create_portfolio('game', ['a', 'b'])
    close = accounts.close_sub

    def fail_second(store, identifier, **kwargs):
        if identifier == 'b':
            raise ValueError('backing mismatch')
        return close(store, identifier, **kwargs)

    monkeypatch.setattr(accounts, 'close_sub', fail_second)
    with pytest.raises(ValueError, match='backing'):
        delete(store, 'game')
    assert portfolio_state(store, 'acct:reserve').cash == 50
    assert portfolio_state(store, 'a').cash == 30
    assert not store.reporting_portfolios()[0].get('retired_at')
    monkeypatch.setattr(accounts, 'close_sub', close)
    delete(store, 'game')
    assert portfolio_state(store, 'acct:reserve').cash == 100
    assert details(store, 'game')['pnl']['net_pnl'] == 0
    store.close()


def test_live_registered_allocations_retire_through_existing_backing_checks(tmp_path, monkeypatch):
    from czquant import accounts, kalshi
    from czquant.accounting import portfolio_state
    from test_virtual_subaccounts import Kalshi

    api = Kalshi('100')
    monkeypatch.setattr(kalshi, 'Kalshi', lambda: api)
    store = Store(tmp_path)
    accounts.register_kalshi(store, api, 'acct', 0, primary=True)
    accounts.open_sub(store, 'a', 'acct', '30', api=api)
    store.create_portfolio('game', ['a'])
    delete(store, 'game')
    assert portfolio_state(store, 'acct:reserve').cash == 100
    assert details(store, 'game')['pnl']['cash'] == 0
    store.close()


def test_store_expands_literal_home_path(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    store = Store('~/.czquant/runs')
    assert store.root == tmp_path / '.czquant' / 'runs'
    store.close()
