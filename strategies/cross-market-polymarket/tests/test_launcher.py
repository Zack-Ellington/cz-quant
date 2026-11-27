"""Replacing cash pools is atomic, repeatable, and refuses outstanding exposure."""
from dataclasses import asdict
from decimal import Decimal as D
import importlib.util
from pathlib import Path

import pytest
from czquant import Broker, Store, accounts

from tests.test_trader import experiment
from tests.test_trader_us import LiveKalshi, MappingUS

spec = importlib.util.spec_from_file_location("launch", Path(__file__).resolve().parents[3] / "scripts/cross-market-launch/launch.py")
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)


@pytest.fixture
def allocation(tmp_path):
    store = Store(tmp_path)
    k, p = LiveKalshi(), MappingUS(authenticated=True)
    k.get = lambda path, params=None: {"balance_dollars": "50"}
    p.balance = lambda: {"currentBalance": "50", "depositReservation": "25"}
    selected = launch.register(store, k, p)
    clients = {"kalshi": k, "polymarket_us": p}
    old = []
    for name in ("old-a", "old-b"):
        for venue in clients:
            identifier = name + "-" + venue
            accounts.open_sub(store, identifier, selected[venue], "10", api=clients[venue])
            old.append(identifier)
    config = asdict(experiment("polymarket_us", "live", "new-k", "new-p"))
    return store, selected, clients, old, config


def test_replace_four_flat_pools_funds_twenty_per_venue_without_history_loss(allocation):
    store, selected, clients, old, config = allocation
    before = store.events()
    launch.replace_pools(store, config, selected, clients, D(20), old)
    assert store.events()[:len(before)] == before
    assert all(p["metadata"].get("retired_at") for p in store.pools() if p["id"] in old)
    assert all(Broker(store, p).state().cash == 0 for p in old)
    assert [Broker(store, p).available for p in config["portfolio"].values()] == [20, 20]
    assert [Broker(store, accounts.reserve_id(selected[v])).available for v in clients] == [30, 5]
    # Relaunch preserves balances rather than restoring either allocation to $20.
    accounts.allocate(store, "new-k", accounts.reserve_id(selected["kalshi"]), "3", api=clients["kalshi"])
    events = store.events()
    launch.replace_pools(store, config, selected, clients, D(20), old)
    assert Broker(store, "new-k").available == 17 and store.events() == events


@pytest.mark.parametrize("block", ["holding", "unknown", "insufficient_cash", "wrong_scope"])
def test_replacement_rolls_back_all_retirements_on_failure(allocation, block):
    store, selected, clients, old, config = allocation
    if block == "holding":
        store.append("transaction", {"reason": "fill", "cash_delta": "-1", "legs": [
            {"market": "M", "side": "yes", "quantity_delta": "1", "price": "1"}]}, old[2])
    elif block == "unknown":
        store.db.execute("INSERT INTO orders(id,portfolio_id,request,context,status,reserved) VALUES(?,?,'{}','{}','unknown','1')",
                         ("pending", old[2]))
    elif block == "wrong_scope":
        accounts.open_sub(store, "new-p", selected["polymarket_us"], "0", ["OTHER"], api=clients["polymarket_us"])
    before = store.events()
    with pytest.raises(ValueError):
        launch.replace_pools(store, config, selected, clients, D(60) if block == "insufficient_cash" else D(20), old)
    assert store.events() == before
    assert all(not accounts.pool(store, p)["metadata"].get("retired_at") for p in old)
    assert not any(p["id"] == "new-k" for p in store.pools())


def test_stop_launcher_refuses_a_reused_pid(allocation, monkeypatch, tmp_path):
    def busy(*args):
        raise BlockingIOError
    monkeypatch.setattr(launch.fcntl, "flock", busy)
    monkeypatch.setattr(launch.subprocess, "run", lambda *a, **k: type("Result", (), {"stdout": "123"})())
    monkeypatch.setattr(launch.subprocess, "check_output", lambda *a, **k: "python /another/service.py")
    monkeypatch.setattr(launch.os, "kill", lambda *a: pytest.fail("must not signal an unrelated process"))
    with (tmp_path / "lock").open("a") as lock, pytest.raises(ValueError, match="does not match"):
        launch.acquire_lock(lock, Path("/intended/launch.py"))
