"""The YAML runner selects existing allocations and cannot create or fund them."""
import json
from dataclasses import asdict

import pytest

import trader
from czquant import Store
from czquant.portfolios import details
from tests.test_trader import experiment


def prepare(tmp_path, monkeypatch):
    monkeypatch.setenv('STRATEGY_RUNS_DIR', str(tmp_path / 'state'))
    store = Store()
    store.create_pool('k', 'kalshi', '60')
    store.create_pool('u', 'polymarket_us', '40')
    store.create_portfolio('game', ['k', 'u'])
    pair = asdict(experiment(venue="polymarket_us").pair)
    (tmp_path / 'candidates.json').write_text(json.dumps({'pairs': [pair]}))
    path = tmp_path / 'config.yaml'
    path.write_text('id: trial\nportfolio: game\nmode: paper\npair_file: candidates.json\npair_index: 0\nonce: true\nparameters:\n  max_sets: 3\n')
    return store, path


def test_yaml_resolves_relative_pair_and_existing_portfolio(tmp_path, monkeypatch):
    store, path = prepare(tmp_path, monkeypatch)
    before = store.events()
    monkeypatch.chdir(tmp_path.parent)
    cfg = trader.load_experiment(str(path))
    assert cfg.portfolio_id == 'game' and cfg.experiment.portfolio == {'kalshi': 'k', 'polymarket_us': 'u'}
    assert cfg.once is True and cfg.max_sets == 3
    assert store.events() == before
    store.close()


@pytest.mark.parametrize('old,new', [
    ('portfolio: game', 'portfolio: missing'),
    ('pair_index: 0', 'pair_index: -1'),
    ('pair_index: 0', 'pair_index: 10'),
    ('mode: paper', 'mode: live'),
    ('once: true', 'once: "false"'),
])
def test_invalid_config_does_not_create_engine_or_change_funding(tmp_path, monkeypatch, old, new):
    store, path = prepare(tmp_path, monkeypatch)
    before = store.events()
    path.write_text(path.read_text().replace(old, new))
    monkeypatch.setattr(trader, 'Engine', lambda *a: pytest.fail('engine must not start'))
    with pytest.raises(SystemExit):
        trader.main(['--config', str(path)])
    assert store.events() == before and len(store.reporting_portfolios()) == 1
    store.close()


def test_config_alone_controls_one_scan(tmp_path, monkeypatch):
    store, path = prepare(tmp_path, monkeypatch)
    calls = []

    class FakeEngine:
        diagnostics = str(tmp_path / 'diagnostics')
        run_id = 'test'

        def __init__(self, cfg):
            calls.append(cfg.experiment.portfolio)

        def run(self, once):
            calls.append(once)

    monkeypatch.setattr(trader, 'Engine', FakeEngine)
    trader.main(['--config', str(path)])
    assert calls == [{'kalshi': 'k', 'polymarket_us': 'u'}, True]
    store.close()


def test_configured_paper_run_reports_fills_and_preserves_state_on_restart(tmp_path, monkeypatch):
    from tests.test_trader import FakeKalshi
    from tests.test_trader_us import FakeUSVenue

    store, path = prepare(tmp_path, monkeypatch)
    path.write_text(path.read_text() + '  kalshi_latency: 0\n  polymarket_latency: 0\n')
    cfg = trader.load_experiment(str(path))
    engine = trader.Engine(cfg, FakeKalshi(), revision='fixture', us=FakeUSVenue())
    engine.run(once=cfg.once)
    result = details(store, 'game', 100)
    assert result['pnl']['fills'] == 2
    assert {lot['pool_id'] for lot in result['positions']} == {'k', 'u'}
    assert result['pnl']['contributed'] == 100
    cash = result['pnl']['cash']
    restarted = trader.Engine(trader.load_experiment(str(path)), FakeKalshi(), revision='fixture', us=FakeUSVenue())
    restarted.run(once=True)
    after = details(store, 'game')
    assert after['pnl']['cash'] == cash and after['pnl']['fills'] == 2
    assert after['pnl']['contributed'] == 100
    engine.store.close()
    restarted.store.close()
    store.close()
