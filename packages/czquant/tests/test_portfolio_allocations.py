"""Account-backed portfolio configs allocate atomically, using fake live venues."""
import json
import sys
from decimal import Decimal

import pytest

from czquant import Broker, Store, accounts, kalshi
from czquant.accounting import portfolio_state
from czquant.cli import main
from czquant.polymarket_us import PolymarketUS
from czquant.portfolios import create
from test_polymarket_us import FakeUS
from test_virtual_subaccounts import Fills, Kalshi, order


@pytest.fixture
def allocation(tmp_path, monkeypatch):
    store = Store(tmp_path)
    k, u = Kalshi('30'), FakeUS()
    u.cash = Decimal('30')
    accounts.register_kalshi(store, k, 'kalshi-account', 0, primary=True)
    accounts.register_polymarket_us(store, u, 'us-account')
    monkeypatch.setattr(kalshi, 'Kalshi', lambda: k)
    monkeypatch.setattr(PolymarketUS, 'from_env', lambda **kwargs: u)
    config = {'id': 'game', 'pools': [
        {'id': 'game-k', 'account': 'kalshi-account', 'cash': 10},
        {'id': 'game-u', 'account': 'us-account', 'cash': 10}]}
    yield store, config, u
    store.close()


def test_cli_allocates_ten_per_venue_and_preserves_reserves(allocation, tmp_path, monkeypatch, capsys):
    store, config, venue = allocation
    path = tmp_path / 'portfolio.json'
    path.write_text(json.dumps(config))
    monkeypatch.setattr(sys, 'argv', ['czquant', '--runs-dir', str(tmp_path),
                                    'portfolios', 'create', '--config', str(path)])
    main()
    result = json.loads(capsys.readouterr().out)
    assert result['mode'] == 'live' and result['pnl']['cash'] == '20'
    assert portfolio_state(store, 'game-k').cash == portfolio_state(store, 'game-u').cash == 10
    for account in ('kalshi-account', 'us-account'):
        assert portfolio_state(store, account + ':reserve').cash == 20
    pools = {p['id']: p for p in store.pools()}
    assert pools['game-k']['venue'] == 'kalshi' and pools['game-u']['venue'] == 'polymarket_us'
    assert 'markets' not in pools['game-k']['metadata']
    assert 'markets' not in pools['game-u']['metadata']
    broker = Broker(store, 'game-k', Fills(mode='live'))
    for market in ('KXGAME-ONE', 'KXOTHER-TWO'):
        assert broker.submit(order(market, count='1'))['status'] == 'executed'
    assert venue.sent == []
    before = store.events()
    with pytest.raises(SystemExit):
        main()
    assert store.events() == before


@pytest.mark.parametrize('failure', ['insufficient', 'backing', 'unknown', 'duplicate', 'mixed-mode', 'markets', 'venue'])
def test_second_allocation_failure_rolls_back_first(allocation, failure):
    store, config, venue = allocation
    second = config['pools'][1]
    if failure == 'insufficient':
        second['cash'] = 31
    elif failure == 'backing':
        venue.cash = Decimal('5')
    elif failure == 'unknown':
        second['account'] = 'missing'
    elif failure == 'duplicate':
        second['id'] = 'game-k'
    elif failure == 'mixed-mode':
        accounts.register_paper(store, 'paper', 'polymarket_us', '30')
        second['account'] = 'paper'
    elif failure == 'markets':
        second['markets'] = ['PMUS:game']
    else:
        second['venue'] = 'kalshi'
    before, pools = store.events(), store.pools()
    with pytest.raises(ValueError):
        create(store, config)
    assert store.events() == before and store.pools() == pools
    assert store.reporting_portfolios() == []
    assert portfolio_state(store, 'kalshi-account:reserve').cash == 30
    assert portfolio_state(store, 'us-account:reserve').cash == 30
    assert venue.sent == []
