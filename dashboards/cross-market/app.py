"""Live, read-only dashboard for cross-market experiments.

Reads the shared economic log ($STRATEGY_RUNS_DIR/accounting.sqlite3) on every
request. It imports no strategy code and sends nothing to any venue. Experiments
are found from run records whose configuration names an experiment and its two
venue pools (the cross-market-polymarket strategy records both).

    uv run python app.py            # http://127.0.0.1:8791/
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from flask import Flask, jsonify

from czquant import Store
from czquant.accounting import portfolio_state

ZERO = Decimal(0)
STRATEGY = "cross-market-polymarket"
MAX_SCANS = 1500  # per experiment, most recent

app = Flask(__name__)
_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "body": None, "store": None}
CACHE_SECONDS = 10.0  # a full pass takes 1-3 s on the 53 GB ledger


def pool_events(store: Store, pool: str, kinds: tuple[str, ...]) -> list[dict[str, Any]]:
    """A pool's events of some types, in effective-time order. Forces the per-pool index:
    with millions of rows SQLite otherwise scans every event of those types."""
    marks = ",".join("?" for _ in kinds)
    rows = store.db.execute(f"SELECT * FROM events INDEXED BY events_portfolio WHERE portfolio_id=? AND type IN ({marks})",
                            (pool, *kinds)).fetchall()
    events = [{**dict(r), "data": json.loads(r["data"])} for r in rows]
    return sorted(events, key=lambda e: (e["effective_at"], e["sequence"]))


def ts(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def experiments(store: Store) -> dict[str, dict[str, Any]]:
    """Latest run configuration per experiment ID."""
    found: dict[str, dict[str, Any]] = {}
    for event in sorted((dict(r) | {"data": json.loads(r["data"])} for r in store.db.execute(
            "SELECT * FROM events WHERE type='run' AND json_extract(data,'$.strategy')=?", (STRATEGY,))), key=lambda e: (e["effective_at"], e["sequence"])):
        data = event["data"]
        exp = (data.get("config") or {}).get("experiment")
        if data.get("strategy") != STRATEGY or not isinstance(exp, dict) or not exp.get("portfolio"):
            continue
        found[exp["id"]] = {"experiment": exp, "config": data["config"], "started_at": event["effective_at"],
                            "revision": data.get("revision")}
    return found


def gap_of(quotes: list[dict[str, Any]]) -> tuple[Decimal | None, list[dict[str, Any]]]:
    """1 minus the cheapest ask for each outcome, before fees. Positive: a set costs under $1."""
    best: dict[str, dict[str, Any]] = {}
    for q in quotes:
        if q.get("ask") is None:
            continue
        if q["role"] not in best or Decimal(q["ask"]) < Decimal(best[q["role"]]["ask"]):
            best[q["role"]] = q
    if len(best) != 2:
        return None, list(best.values())
    return 1 - sum((Decimal(q["ask"]) for q in best.values()), ZERO), list(best.values())


def windows(scans: list[dict[str, Any]], interval: float) -> list[dict[str, Any]]:
    """Runs of consecutive scans with a positive pre-fee gap. A basket that cleared the
    strategy's fee, size and return filters marks the window tradable."""
    result: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for scan in scans:
        positive = scan["gap"] is not None and Decimal(scan["gap"]) > 0
        if positive:
            if current is None:
                current = {"start": scan["t"], "end": scan["t"], "scans": 0, "max_gap": scan["gap"],
                           "max_net": None, "max_sets": 0, "tradable_scans": 0, "reasons": {}}
                result.append(current)
            current["end"], current["scans"] = scan["t"], current["scans"] + 1
            reason = scan["reason"] or "unknown"
            current["reasons"][reason] = current["reasons"].get(reason, 0) + 1
            if Decimal(scan["gap"]) > Decimal(current["max_gap"]):
                current["max_gap"] = scan["gap"]
            if scan["net"] is not None:
                current["tradable_scans"] += 1
                current["max_sets"] = max(current["max_sets"], scan["sets"] or 0)
                if current["max_net"] is None or Decimal(scan["net"]) > Decimal(current["max_net"]):
                    current["max_net"] = scan["net"]
        else:
            current = None
    for w in result:
        w["duration_s"] = round(ts(w["end"]) - ts(w["start"]) + interval)
        w["tradable"] = w["tradable_scans"] > 0
    return result


def _leg_error(outcome: dict[str, Any], limits: dict[str, str]) -> str:
    venue = "Kalshi" if outcome.get("venue") == "kalshi" else "Polymarket US"
    price = limits.get(outcome.get("ticker", ""))
    at = f" @ {Decimal(price) * 100:.2f}c" if price else ""
    if "unwind" in outcome:
        return f"REPAIR {venue}: sold back {outcome.get('sold')}/{outcome.get('unwind')} (status {outcome.get('status')})"
    return (f"IOC_{str(outcome.get('status', 'unknown')).upper()} {venue}: filled "
            f"{outcome.get('filled', '0')}/{outcome.get('asked', '?')}{at}")


def missed_opportunities(obs: list[dict[str, Any]], cooldown: float) -> list[dict[str, Any]]:
    """Opportunities the strategy found but did not fill, with what stopped each one.

    An opportunity is a run of consecutive scans where the strategy priced a basket
    that cleared its fee, size and return filters. Events after a scan and before the
    next one are its outcome: an execution (legs and how they ended), a recorded
    refusal (preflight, recheck, cooldown, risk), a pause or a halt. A run that
    filled a complete basket is not missed. Refusals the strategy rate-limits in its
    log are reported as unrecorded rather than guessed."""
    result: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    last_attempt: float | None = None

    def close() -> None:
        if current is None:
            return
        if not current["filled"]:
            if not current["errors"]:
                since = None if last_attempt is None else ts(current["start"]) - last_attempt
                current["errors"].append(
                    f"COOLDOWN: {round(since)}s after the last attempt (cooldown {round(cooldown)}s)"
                    if since is not None and 0 <= since < cooldown else
                    "NO_ATTEMPT_RECORDED: no execution or refusal logged (repeated refusals are rate-limited)")
            current["errors"] = list(dict.fromkeys(current["errors"]))
            result.append(current)

    for e in obs:
        d, t = e["data"], e["effective_at"]
        kind = d.get("kind")
        if kind == "scan_complete":
            basket = d.get("basket")
            if not basket:
                close()
                current = None
                continue
            if current is None:
                current = {"start": t, "end": t, "scans": 0, "max_net": basket["net"], "sets": basket.get("sets"),
                           "edge": basket.get("edge"), "errors": [], "filled": False}
            current["end"], current["scans"] = t, current["scans"] + 1
            if Decimal(basket["net"]) > Decimal(current["max_net"]):
                current.update(max_net=basket["net"], sets=basket.get("sets"), edge=basket.get("edge"))
            continue
        if current is None:
            if kind == "basket_execution":
                last_attempt = ts(t)
            continue
        if kind == "basket_execution":
            last_attempt = ts(t)
            ctx = d.get("context") or {}
            limits = {leg["ticker"]: leg.get("limit") for leg in ctx.get("intended_legs") or []}
            outcomes = d.get("outcomes") or []
            entries = [o for o in outcomes if "unwind" not in o]
            if entries and len(entries) == len(ctx.get("intended_legs") or []) and all(
                    o.get("status") == "filled" and o.get("filled") == o.get("asked") for o in entries):
                current["filled"] = True
            else:
                current["errors"] += [_leg_error(o, limits) for o in outcomes
                                      if "unwind" in o or o.get("status") != "filled"]
                if not current["errors"]:
                    current["errors"].append("PARTIAL: a leg filled short of the basket size")
        elif kind == "reject":
            detail = d.get("detail") or d.get("why")
            current["errors"].append(f"{str(d.get('reason')).upper()}" + (f": {detail}" if detail else ""))
        elif kind == "live_paused":
            current["errors"].append(f"PAUSED: {d.get('reason')}")
        elif kind == "halt":
            current["errors"].append(f"HALTED: {d.get('reason')}")
    close()
    for m in result:
        m["duration_s"] = round(ts(m["end"]) - ts(m["start"]))
    return result


def experiment_view(store: Store, info: dict[str, Any]) -> dict[str, Any]:
    exp, cfg = info["experiment"], info["config"]
    pools = exp["portfolio"]  # venue -> pool ID
    kalshi_pool = pools["kalshi"]
    cash = []
    for venue, pool in pools.items():
        state = portfolio_state(store, pool)
        reserved = store.reserved(pool)
        cash.append({"venue": venue, "pool": pool, "cash": money(state.cash), "reserved": money(reserved),
                     "available": money(state.cash - reserved), "funded": money(state.contributed),
                     "open_lots": len(state.lots), "fees": money(state.fees), "realized": money(state.realized)})

    # Heartbeats are most of the rows: parse every other observation, and only the last heartbeat.
    rows = store.db.execute("SELECT * FROM events INDEXED BY events_portfolio WHERE portfolio_id=? AND type='observation' "
                            "AND json_extract(data,'$.kind') != 'heartbeat'", (kalshi_pool,)).fetchall()
    last = store.db.execute("SELECT * FROM events INDEXED BY events_portfolio WHERE portfolio_id=? AND type='observation' "
                            "AND json_extract(data,'$.kind') = 'heartbeat' ORDER BY sequence DESC LIMIT 1", (kalshi_pool,)).fetchall()
    obs = sorted(({**dict(r), "data": json.loads(r["data"])} for r in [*rows, *last]),
                 key=lambda e: (e["effective_at"], e["sequence"]))
    all_runs = obs  # missed opportunities span every run of this experiment
    obs = [e for e in obs if e["effective_at"] >= info["started_at"] or e["data"]["kind"] != "scan_complete"]
    scans, attempts, rejects, halts, cleared = [], [], [], {}, set()
    heartbeat = None
    for e in obs:
        d, t = e["data"], e["effective_at"]
        kind = d["kind"]
        if kind == "scan_complete":
            gap, best = gap_of(d.get("quotes") or [])
            basket = d.get("basket") or {}
            scans.append({"t": t, "reason": d.get("reason"), "gap": None if gap is None else str(gap),
                          "best": best, "quotes": d.get("quotes") or [],
                          "net": basket.get("net"), "sets": basket.get("sets"), "edge": basket.get("edge")})
        elif kind == "basket_execution":
            ctx = d.get("context") or {}
            attempts.append({"t": t, "trade_id": ctx.get("trade_id"), "sets": ctx.get("sets"),
                             "expected_net": ctx.get("expected_net"), "edge": ctx.get("edge"),
                             "outcomes": d.get("outcomes") or []})
        elif kind == "reject":
            rejects.append({"t": t, "reason": d.get("reason")})
        elif kind == "halt":
            halts[e["record_id"]] = {"t": t, "reason": d.get("reason")}
        elif kind == "halt_cleared":
            cleared.add(d.get("halt_id"))
        elif kind == "heartbeat":
            heartbeat = {"t": t, "halted": d.get("halted"), "counters": d.get("counters")}

    # Executed trades: fills grouped by trade ID across both venue pools.
    trades: dict[str, dict[str, Any]] = {}
    for venue, pool in pools.items():
        for e in pool_events(store, pool, ("transaction",)):
            d = e["data"]
            if d.get("reason") not in ("fill", "settlement"):
                continue
            tid = (d.get("context") or {}).get("trade_id") or "untagged"
            trade = trades.setdefault(tid, {"trade_id": tid, "t": e["effective_at"], "legs": [], "cash": ZERO,
                                            "fees": ZERO, "held": {}, "settled": ZERO, "unwind": False})
            trade["cash"] += Decimal(d["cash_delta"])
            trade["fees"] += Decimal(d.get("fee") or "0")
            if (d.get("context") or {}).get("repair"):
                trade["unwind"] = True
            for leg in d.get("legs") or []:
                qty = Decimal(leg["quantity_delta"])
                key = f'{leg["market"]}|{leg["side"]}'
                trade["held"][key] = trade["held"].get(key, ZERO) + qty
                if d["reason"] == "settlement":
                    trade["settled"] += Decimal(d["cash_delta"])
                else:
                    trade["legs"].append({"t": e["effective_at"], "venue": venue, "market": leg["market"],
                                          "side": leg["side"], "qty": str(qty), "price": leg["price"],
                                          "fee": d.get("fee")})
    executed = []
    for trade in trades.values():
        if not trade["legs"]:
            continue
        open_qty = [q for q in trade["held"].values() if q > 0]
        sets = min(open_qty) if len(open_qty) == 2 else ZERO  # complete sets still held
        # Expected: each complete set pays $1; cash already includes costs, fees, unwinds, settlements.
        expected = trade["cash"] + sets
        executed.append({"trade_id": trade["trade_id"], "t": trade["t"], "legs": trade["legs"],
                         "open_sets": str(sets), "cash": money(trade["cash"]), "fees": money(trade["fees"]),
                         "expected_profit": money(expected), "unwound": trade["unwind"],
                         "complete": len(open_qty) == 2 or trade["settled"] > 0,
                         # A basket only exists if both outcomes were held at once.
                         "basket": len({k.split("|")[1] for k in trade["held"]}) == 2 or len({l["market"] + l["side"] for l in trade["legs"] if Decimal(l["qty"]) > 0}) >= 2})
    executed.sort(key=lambda x: x["t"])

    missed = missed_opportunities(all_runs, float(cfg.get("cooldown") or 300))
    scans = scans[-MAX_SCANS:]
    interval = float(cfg.get("interval_seconds") or 15)
    latest = scans[-1] if scans else None
    standing = [h for hid, h in halts.items() if hid not in cleared]
    # A cleared halt takes effect only when the process restarts; until then the
    # process still reports itself halted in its heartbeat.
    if heartbeat and heartbeat.get("halted") and not standing:
        standing = [{"t": heartbeat["t"], "reason": f"{heartbeat['halted']} (cleared in the ledger; restart the process to resume)"}]
    return {"id": exp["id"], "mode": exp["mode"], "dry_run": bool(cfg.get("dry_run")),
            "pair": {"kalshi_event": exp["pair"]["kalshi_event"], "polymarket_market": exp["pair"]["polymarket_market"],
                     "resolves_at": exp["pair"].get("resolves_at"), "start": (exp["pair"].get("evidence") or {}).get("start"),
                     "outcomes": exp["pair"]["outcomes"]},
            "started_at": info["started_at"], "revision": (info.get("revision") or "")[:10],
            "cash": cash, "latest": latest, "heartbeat": heartbeat, "halts": standing,
            "series": [{"t": s["t"], "gap": s["gap"], "tradable": s["net"] is not None} for s in scans],
            "windows": windows(scans, interval)[-50:], "attempts": attempts[-50:],
            "rejects": rejects[-20:], "executed": executed, "missed": missed[-50:],
            "totals": {"cash": money(sum((Decimal(c["cash"]) for c in cash), ZERO)),
                       "available": money(sum((Decimal(c["available"]) for c in cash), ZERO)),
                       "funded": money(sum((Decimal(c["funded"]) for c in cash), ZERO)),
                       "attempts": len(attempts), "executed": sum(1 for x in executed if x["basket"]),
                       "repaired": sum(1 for x in executed if not x["basket"]),
                       "expected_profit": money(sum((Decimal(x["expected_profit"]) for x in executed), ZERO)),
                       "scans": len(scans), "windows": len(windows(scans, interval)),
                       "missed": len(missed),
                       "missed_potential": money(sum((Decimal(m["max_net"]) for m in missed), ZERO))}}


@app.get("/api/state")
def state():
    # One connection and one computation at a time; several open tabs share a result
    # for a few seconds instead of each replaying the ledger.
    with _lock:
        if _cache["body"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS:
            return jsonify(_cache["body"])
        if _cache["store"] is None:
            _cache["store"] = Store(app.config["RUNS_DIR"])
        store = _cache["store"]  # its replay cache extends with rows other processes write
        body = compute(store)
        _cache.update(at=time.monotonic(), body=body)
        return jsonify(body)


def compute(store: Store) -> dict[str, Any]:
    views = [experiment_view(store, info) for info in experiments(store).values()]
    views.sort(key=lambda v: (v["mode"], v["pair"].get("start") or "", v["id"]))
    overall: dict[str, dict[str, Any]] = {}
    for v in views:
        o = overall.setdefault(v["mode"], {"experiments": 0, "cash": ZERO, "available": ZERO, "funded": ZERO,
                                           "attempts": 0, "executed": 0, "repaired": 0, "expected_profit": ZERO,
                                           "missed": 0, "missed_potential": ZERO})
        o["experiments"] += 1
        o["missed"] += v["totals"]["missed"]
        for key in ("cash", "available", "funded", "expected_profit", "missed_potential"):
            o[key] += Decimal(v["totals"][key])
        o["attempts"] += v["totals"]["attempts"]
        o["executed"] += v["totals"]["executed"]
        o["repaired"] += v["totals"]["repaired"]
    overall = {m: {k: (money(x) if isinstance(x, Decimal) else x) for k, x in o.items()} for m, o in overall.items()}
    return {"generated_at": datetime.now().astimezone().isoformat(), "overall": overall, "experiments": views}


@app.get("/")
def index():
    return (Path(__file__).with_name("index.html")).read_text(encoding="utf-8")


def main() -> None:
    import os
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--runs-dir", default=os.environ.get("STRATEGY_RUNS_DIR"))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8791)
    a = p.parse_args()
    if not a.runs_dir:
        p.error("export STRATEGY_RUNS_DIR or pass --runs-dir")
    app.config["RUNS_DIR"] = a.runs_dir
    # Single-threaded: the cached ledger connection belongs to the thread that opened
    # it (sqlite3 refuses cross-thread use), and requests are serialized anyway.
    app.run(host=a.host, port=a.port, debug=False, threaded=False)


if __name__ == "__main__":
    main()
