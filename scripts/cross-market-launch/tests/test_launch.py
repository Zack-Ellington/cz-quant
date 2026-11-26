"""Offline launcher checks: these tests never construct venue clients."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location("launch", Path(__file__).parents[1] / "launch.py")
assert spec and spec.loader
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)


def pair(event="KXNFLGAME-26NOV26PHIDAL"):
    return {"venue": "polymarket_us", "kalshi_event": event,
            "outcomes": [{"kalshi": event + "-A", "polymarket": "PMUS:" + event},
                         {"kalshi": event + "-B", "polymarket": "PMUS:" + event}]}


def test_two_games_have_four_distinct_pools_and_explicit_live_caps():
    configs = launch.experiments([pair(), pair("KXNFLGAME-26NOV26KCBUF")])
    assert len({p for c in configs for p in c["portfolio"].values()}) == 4
    for config in configs:
        assert config["mode"] == "live"
        assert config["parameters"]["dry_run"] is False
        assert config["parameters"]["max_total_dollars"] == 20
        assert config["parameters"]["max_capital_frac"] == 1


def test_duplicate_or_international_pairs_are_refused():
    with pytest.raises(ValueError):
        launch.experiments([pair(), pair()])
    with pytest.raises(ValueError):
        launch.experiments([{**pair(), "venue": "polymarket"}])


def test_restart_preserves_pool_cash_and_scopes(monkeypatch):
    configs = launch.experiments([pair(), pair("KXNFLGAME-26NOV26KCBUF")])
    selected = {"kalshi": "main", "polymarket_us": "pmus"}
    pools, groups, opened = [], [], []
    store = Mock()
    store.pools.side_effect = lambda: pools
    store.reporting_portfolios.side_effect = lambda: groups
    store.create_portfolio.side_effect = lambda identifier, ids: groups.append({"id": identifier, "pools": ids})
    monkeypatch.setattr(launch.accounts, "pool", lambda *_: {})
    monkeypatch.setattr(launch.accounts, "free_cash", lambda *_: 20)

    def open_sub(_store, identifier, account_id, cash, markets, *, api):
        opened.append((identifier, cash))
        venue = next(v for v, a in selected.items() if a == account_id)
        pools.append({"id": identifier, "mode": "live", "venue": venue,
                      "metadata": {"account": account_id, "markets": markets}})

    monkeypatch.setattr(launch.accounts, "open_sub", open_sub)
    launch.prepare_pools(store, configs, selected, {v: None for v in selected})
    launch.prepare_pools(store, configs, selected, {v: None for v in selected})
    assert len(opened) == 4
    assert all(cash == "10" for _, cash in opened)
    assert len(groups) == 2
    pools[0]["metadata"]["markets"] = ["*"]
    with pytest.raises(ValueError, match="incompatible"):
        launch.prepare_pools(store, configs, selected, {v: None for v in selected})


def test_insufficient_reserve_prevents_any_allocations(monkeypatch):
    store = Mock()
    store.pools.return_value = []
    monkeypatch.setattr(launch.accounts, "pool", lambda *_: {})
    monkeypatch.setattr(launch.accounts, "free_cash", lambda *_: 9)
    opened = Mock()
    monkeypatch.setattr(launch.accounts, "open_sub", opened)
    with pytest.raises(ValueError, match="unallocated cash"):
        launch.prepare_pools(store, launch.experiments([pair()]),
                             {"kalshi": "main", "polymarket_us": "pmus"}, {})
    opened.assert_not_called()


def test_reconciliation_failure_blocks_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(launch, "account_pass", lambda *_: {"status": "healthy"})
    monkeypatch.setattr(launch, "polymarket_us_pass",
                        lambda *_: {"status": "degraded", "discrepancies": [{"kind": "cash_mismatch"}]})
    log = tmp_path / "reconciliation.json"
    with pytest.raises(ValueError, match="reconciliation failed"):
        launch.reconcile(Mock(), Mock(), Mock(), log)
    assert "cash_mismatch" in log.read_text()


def test_monitor_exit_stops_children(monkeypatch, tmp_path):
    children = []

    def popen(*_args, **_kwargs):
        proc = Mock(pid=100 + len(children))
        proc.poll.return_value = 1 if not children else None
        children.append(proc)
        return proc

    monkeypatch.setattr(launch.subprocess, "Popen", popen)
    with pytest.raises(RuntimeError, match="monitor exited"):
        launch.supervise([tmp_path / "game.json"], tmp_path)
    children[1].send_signal.assert_called_once_with(launch.signal.SIGINT)
    children[2].send_signal.assert_called_once_with(launch.signal.SIGINT)
