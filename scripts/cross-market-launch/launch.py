"""Fund and launch one live cross-market experiment; optionally retire flat old pools."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shlex
from decimal import Decimal
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
from czquant.store import decimal

WINDOWS = os.name == "nt"
if WINDOWS:
    import msvcrt
else:
    import fcntl

REPO = Path(__file__).resolve().parents[2]
STRATEGY = REPO / "strategies/cross-market-polymarket"


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
                  clients: dict[str, Any], cash: Decimal) -> None:
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
        needed = cash * sum(v == venue for v, _, _ in pending)
        reserve = accounts.pool(store, accounts.reserve_id(account_id))
        if accounts.free_cash(store, reserve) < needed:
            raise ValueError(f"{account_id} needs ${needed} unallocated cash for the missing pools")
    for venue, identifier, markets in pending:
        accounts.open_sub(store, identifier, selected[venue], cash, markets, api=clients[venue])
        print(f"Prepared {identifier}: ${cash}", flush=True)
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


def _try_lock(lock: Any) -> bool:
    """Take this ledger's launch lock without waiting. False if another process holds it."""
    try:
        if WINDOWS:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, PermissionError, OSError):
        return False


# Children run in their own process group so a stop request reaches only them.
# Windows has no SIGINT for child processes: they get CTRL_BREAK_EVENT instead.
CREATION_FLAGS = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if WINDOWS else 0
NEW_SESSION = not WINDOWS
STOP_SIGNAL = signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGINT


def supervise(config_paths: list[Path], logs: Path) -> None:
    children: list[tuple[str, subprocess.Popen, bool]] = []
    cz = str(Path(sys.executable).with_name("czquant.exe" if WINDOWS else "czquant"))
    commands = [("kalshi-monitor", [cz, "monitor", "account", "--interval", "30"], True),
                ("pmus-monitor", [cz, "monitor", "polymarket-us", "--interval", "30"], True)]
    commands += [(p.stem, [sys.executable, str(STRATEGY / "trader.py"), "--experiment", str(p)], False)
                 for p in config_paths]
    try:
        for name, command, monitor in commands:
            with (logs / f"{name}.log").open("a") as output:
                proc = subprocess.Popen(command, cwd=STRATEGY, stdout=output,
                                        stderr=subprocess.STDOUT, creationflags=CREATION_FLAGS,
                                        start_new_session=NEW_SESSION)
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
                proc.send_signal(STOP_SIGNAL)
            for proc in running:
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    proc.wait(timeout=10)


def replace_pools(store: Store, config: dict[str, Any], selected: dict[str, str],
                  clients: dict[str, Any], cash: Decimal, retire: list[str]) -> None:
    """One atomic ledger change; no venue cash transfer and no deletion of history."""
    if not cash.is_finite() or cash <= 0:
        raise ValueError("cash per venue must be positive")
    if set(retire) & set(config["portfolio"].values()):
        raise ValueError("replacement pools must have new identities")
    with store.transaction():
        existing = {p["id"]: p for p in store.pools()}
        for identifier in dict.fromkeys(retire):
            if identifier not in existing:
                raise ValueError(f"no cash pool {identifier}")
            p = existing[identifier]
            if p["metadata"].get("account") != selected.get(p["venue"]):
                raise ValueError(f"cannot retire a pool on another account: {identifier}")
            if not p["metadata"].get("retired_at"):
                accounts.close_sub(store, identifier, api=clients[p["venue"]])
        prepare_pools(store, [config], selected, clients, cash)


def publish_config(root: Path, config: dict[str, Any], retired: list[str]) -> Path:
    """Keep active experiment configs together and retain retired configurations."""
    out = root / "experiments/cross-market"
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.json"):
        pools = set(json.loads(old.read_text()).get("portfolio", {}).values())
        if pools and pools <= set(retired):
            archive = out / "retired" / old.name
            archive.parent.mkdir(exist_ok=True)
            if archive.exists() and archive.read_bytes() != old.read_bytes():
                raise ValueError(f"archived configuration differs: {archive}")
            old.replace(archive)
    target = out / (config["id"] + ".json")
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n")
    temporary.replace(target)
    return target


def acquire_lock(lock: Any, previous_launcher: Path | None) -> None:
    """Stop only an explicitly selected launcher holding this ledger's lock."""
    if _try_lock(lock):
        return
    if previous_launcher is None:
        raise ValueError("another launcher owns this ledger; stop it before replacing pools")
    if WINDOWS:
        # No lsof/ps to prove which process holds the lock: never signal a guess.
        raise ValueError("--stop-launcher is not supported on Windows; stop the previous launcher "
                         "in its terminal (Ctrl-C), then relaunch")
    owners = subprocess.run(["lsof", "-t", str(lock.name)], text=True, capture_output=True, check=False)
    candidates = [int(pid) for pid in owners.stdout.split() if int(pid) != os.getpid()]
    if len(candidates) != 1:
        raise ValueError("cannot identify a single previous launcher; stop it in its terminal")
    pid = candidates[0]
    command = subprocess.check_output(["ps", "-p", str(pid), "-o", "args="], text=True)
    if str(previous_launcher.resolve()) not in shlex.split(command):
        raise ValueError("lock owner does not match the selected previous launcher; refusing to signal it")
    print(f"Stopping previous launcher PID {pid}; waiting for its strategies and monitors", flush=True)
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if _try_lock(lock):
            return
        time.sleep(0.2)
    raise ValueError("previous launcher has not stopped; no allocations were changed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, type=Path)
    parser.add_argument("--cash-per-venue", required=True, type=decimal)
    parser.add_argument("--retire-pools", nargs="*", default=[])
    parser.add_argument("--stop-launcher", type=Path)
    args = parser.parse_args()
    required = ("STRATEGY_RUNS_DIR", "KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH",
                "POLYMARKET_US_KEY_ID", "POLYMARKET_US_SECRET_KEY_PATH")
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise ValueError("export required variables: " + ", ".join(missing))
    sys.path.insert(0, str(STRATEGY))
    load_experiment = importlib.import_module("trader").load_experiment
    path = args.experiment.resolve()
    cfg = load_experiment(str(path))
    if cfg.experiment.mode != "live" or cfg.dry_run:
        raise ValueError("select a live experiment with dry_run false")
    config = json.loads(path.read_text())
    if Path(config["id"]).name != config["id"] or config["id"] in (".", ".."):
        raise ValueError("experiment ID must be a plain filename")
    if not args.cash_per_venue.is_finite() or args.cash_per_venue <= 0:
        raise ValueError("cash per venue must be positive")
    os.umask(0o077)
    root = Path(os.environ["STRATEGY_RUNS_DIR"]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.environ["STRATEGY_RUNS_DIR"] = str(root)
    existing_config = root / "experiments/cross-market" / (config["id"] + ".json")
    if existing_config.exists() and json.loads(existing_config.read_text()) != config:
        raise ValueError(f"existing configuration differs; inspect {existing_config}")
    with (root / "cross-market-launch.lock").open("a") as lock:
        acquire_lock(lock, args.stop_launcher)
        logs = root / "experiments/cross-market/logs" / config["id"]
        logs.mkdir(parents=True, exist_ok=True)
        kalshi, pmus = Kalshi(), PolymarketUS.from_env(authenticated=True)
        store = Store(root)
        try:
            selected = register(store, kalshi, pmus)
            reconcile(store, kalshi, pmus, logs / "reconciliation.json")
            replace_pools(store, config, selected, {"kalshi": kalshi, "polymarket_us": pmus},
                          args.cash_per_venue, args.retire_pools)
            reconcile(store, kalshi, pmus, logs / "reconciliation.json")
        finally:
            store.close()
        path = publish_config(root, config, args.retire_pools)
        print("Allocations ready; existing pool balances were retained on restart", flush=True)
        supervise([path], logs)


if __name__ == "__main__":
    def stop(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    if hasattr(signal, "SIGHUP"):  # absent on Windows
        signal.signal(signal.SIGHUP, stop)
    if hasattr(signal, "SIGBREAK"):  # Windows: Ctrl-Break stops the launcher like Ctrl-C
        signal.signal(signal.SIGBREAK, stop)
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped. Pools and positions are preserved; reconcile and settle remaining holdings.")
    except (ValueError, RuntimeError) as exc:
        sys.exit(str(exc))
