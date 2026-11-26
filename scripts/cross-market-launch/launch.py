"""Launch independent live cross-market games with $10 per venue per game."""

from __future__ import annotations

import argparse
import fcntl
import importlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any

from czquant import Store
from czquant import accounts
from czquant.kalshi import Kalshi
from czquant.monitor import account_pass, polymarket_us_pass
from czquant.polymarket_us import PolymarketUS

REPO = Path(__file__).resolve().parents[2]
STRATEGY = REPO / "strategies/cross-market-polymarket"


def experiments(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for pair in pairs:
        if pair["venue"] != "polymarket_us":
            raise ValueError("live execution requires Polymarket US pairs")
        identifier = "cross-" + pair["kalshi_event"].lower()
        result.append({"id": identifier, "mode": "live", "pair": pair,
                       "portfolio": {"kalshi": identifier + "-kalshi",
                                     "polymarket_us": identifier + "-pmus"},
                       "parameters": {"dry_run": False, "max_capital_frac": 1.0,
                                      "max_basket_dollars": 20, "max_total_dollars": 20,
                                      "require_cross": True}})
    if not result or len({e["id"] for e in result}) != len(result):
        raise ValueError("select at least one pair, with no duplicate Kalshi events")
    return result


def register(store: Store, kalshi: Any, pmus: Any) -> dict[str, str]:
    selected = {}
    for venue in ("kalshi", "polymarket_us"):
        found = [a for a in accounts.accounts(store) if a["mode"] == "live" and a["venue"] == venue]
        if len(found) > 1:
            raise ValueError(f"multiple {venue} accounts: select the intended ledger before launching")
        if found:
            selected[venue] = found[0]["id"]
            continue
        identifier = "cross-market-" + venue
        if venue == "kalshi":
            accounts.register_kalshi(store, kalshi, identifier, 0, primary=True)
        else:
            accounts.register_polymarket_us(store, pmus, identifier)
        selected[venue] = identifier
    return selected


def prepare_pools(store: Store, configs: list[dict[str, Any]], selected: dict[str, str],
                  clients: dict[str, Any]) -> None:
    existing = {p["id"]: p for p in store.pools()}
    pending = []
    for config in configs:
        for venue, identifier in config["portfolio"].items():
            field = "kalshi" if venue == "kalshi" else "polymarket"
            markets = sorted({o[field] for o in config["pair"]["outcomes"]})
            if identifier in existing:
                p = existing[identifier]
                if (p["mode"] != "live" or p["venue"] != venue
                        or p["metadata"].get("account") != selected[venue]
                        or p["metadata"].get("retired_at")
                        or p["metadata"].get("markets") != markets):
                    raise ValueError(f"existing pool has incompatible ownership or scope: {identifier}")
            else:
                pending.append((venue, identifier, markets))
    for venue, account_id in selected.items():
        needed = 10 * sum(v == venue for v, _, _ in pending)
        reserve = accounts.pool(store, accounts.reserve_id(account_id))
        if accounts.free_cash(store, reserve) < needed:
            raise ValueError(f"{account_id} needs ${needed} unallocated cash for the missing pools")
    for venue, identifier, markets in pending:
        accounts.open_sub(store, identifier, selected[venue], "10", markets, api=clients[venue])
        print(f"Created {identifier}: $10", flush=True)
    groups = {g["id"]: g["pools"] for g in store.reporting_portfolios()}
    for config in configs:
        pools = list(config["portfolio"].values())
        if config["id"] not in groups:
            store.create_portfolio(config["id"], pools)
        elif set(groups[config["id"]]) != set(pools):
            raise ValueError(f"reporting portfolio has different pools: {config['id']}")


def reconcile(store: Store, kalshi: Any, pmus: Any, log: Path) -> None:
    statuses = [account_pass(store, kalshi), polymarket_us_pass(store, pmus)]
    log.write_text(json.dumps(statuses, indent=2) + "\n")
    if any(s["status"] != "healthy" or s.get("discrepancies") for s in statuses):
        raise ValueError(f"account reconciliation failed; inspect {log}")


def supervise(config_paths: list[Path], logs: Path) -> None:
    children: list[tuple[str, subprocess.Popen, bool]] = []
    cz = str(Path(sys.executable).with_name("czquant"))
    commands = [("kalshi-monitor", [cz, "monitor", "account", "--interval", "30"], True),
                ("pmus-monitor", [cz, "monitor", "polymarket-us", "--interval", "30"], True)]
    commands += [(p.stem, [sys.executable, str(STRATEGY / "trader.py"), "--experiment", str(p)], False)
                 for p in config_paths]
    try:
        for name, command, monitor in commands:
            with (logs / f"{name}.log").open("a") as output:
                proc = subprocess.Popen(command, cwd=STRATEGY, stdout=output,
                                        stderr=subprocess.STDOUT, start_new_session=True)
            children.append((name, proc, monitor))
            print(f"Started {name}: PID {proc.pid}; log {logs / (name + '.log')}", flush=True)
        (logs / "processes.json").write_text(json.dumps(
            [{"name": name, "pid": p.pid} for name, p, _ in children], indent=2) + "\n")
        print("LIVE execution enabled. Leave this terminal open. Ctrl-C stops processes, not positions.", flush=True)
        reported = set()
        while True:
            for name, proc, monitor in children:
                if proc.poll() is not None:
                    if monitor:
                        raise RuntimeError(f"{name} exited; stopping strategies. Inspect its log.")
                    if name not in reported:
                        print(f"{name} exited ({proc.returncode}); inspect its log. Other runs continue.", flush=True)
                        reported.add(name)
            time.sleep(2)
    finally:
        # Stop strategies first. Keep monitors running while their shutdown completes.
        for monitor in (False, True):
            running = [p for _, p, m in children if m == monitor and p.poll() is None]
            for proc in running:
                proc.send_signal(signal.SIGINT)
            for proc in running:
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    proc.wait(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", required=True, type=Path)
    args = parser.parse_args()
    required = ("STRATEGY_RUNS_DIR", "KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH",
                "POLYMARKET_US_KEY_ID", "POLYMARKET_US_SECRET_KEY_PATH")
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise ValueError("export required variables: " + ", ".join(missing))
    os.umask(0o077)
    root = Path(os.environ["STRATEGY_RUNS_DIR"]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.environ["STRATEGY_RUNS_DIR"] = str(root)
    with (root / "cross-market-launch.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("a launcher already owns this live ledger") from None
        configs = experiments(json.loads(args.pairs.read_text())["pairs"])
        out = root / "experiments/cross-market"
        logs = out / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        sys.path.insert(0, str(STRATEGY))
        load_experiment = importlib.import_module("trader").load_experiment
        paths = []
        for config in configs:
            path = out / f"{config['id']}.json"
            if path.exists() and json.loads(path.read_text()) != config:
                raise ValueError(f"existing configuration differs; inspect {path}")
            path.write_text(json.dumps(config, indent=2) + "\n")
            load_experiment(str(path))
            paths.append(path)
        kalshi, pmus = Kalshi(), PolymarketUS.from_env(authenticated=True)
        store = Store(root)
        try:
            selected = register(store, kalshi, pmus)
            reconcile(store, kalshi, pmus, logs / "reconciliation.json")
            prepare_pools(store, configs, selected, {"kalshi": kalshi, "polymarket_us": pmus})
            reconcile(store, kalshi, pmus, logs / "reconciliation.json")
        finally:
            store.close()
        supervise(paths, logs)


if __name__ == "__main__":
    def stop(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped. Pools and positions are preserved; reconcile and settle remaining holdings.")
    except (ValueError, RuntimeError) as exc:
        sys.exit(str(exc))
