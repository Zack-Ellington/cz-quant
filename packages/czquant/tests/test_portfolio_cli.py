"""Portfolio CLI lifecycle with isolated paper state and no venue access."""
import json
import sys
from decimal import Decimal

import pytest

from czquant.cli import main
from czquant.portfolios import create, details
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

    result = invoke('portfolio-create', '--config', str(path))
    assert result['pnl']['cash'] == '100' and result['pnl']['net_pnl'] == '0'
    assert {p['id'] for p in invoke('portfolios')} == {'game', 'k', 'u'}
    result = invoke('portfolios', 'game', '--events', '1')
    assert len(result['events']) == 1 and result['event_count'] == 2
    with pytest.raises(SystemExit):
        invoke('portfolio-create', '--config', str(path))
    capsys.readouterr()
    assert invoke('portfolios', 'game')['pnl']['cash'] == '100'


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
