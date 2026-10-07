"""Repeated sum-check scans with running statistics, for the live dashboard.

Each scan updates a rolling state file with, per event:
  * how often the walked YES ask sum was under 1 (buy-every-leg signal)
    and how often the walked YES bid sum was over 1 (sell-every-leg signal)
  * the lowest and highest ask and bid sums seen so far
  * the latest taker edge, ROI and APR

It then writes two JSON documents sized for the dashboard's database:
  dash_summary.json  current scan totals, all-time extremes, estimated return, top events
  dash_history.json  one point per scan, for the trend charts

Usage:
  python monitor.py --once                 # one scan (what the scheduled task runs)
  python monitor.py --interval 900         # scan every 15 minutes until stopped
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from sum_check import build_parser, scan

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_DIR = os.path.join(REPO_ROOT, "runs", "kalshi_sum_check")
MAX_EVENTS_PUSHED = 300  # keeps dash_summary well under the 256 KiB document cap
MAX_HISTORY = 5000  # kept locally, ~52 days at one scan per 15 minutes
MAX_HISTORY_PUSHED = 500  # thinned for the dashboard; stored docs run ~1.5x local size
PRUNE_AFTER = timedelta(days=7)  # drop events not seen for this long
HIST_LO, HIST_HI, HIST_STEP = 0.80, 1.20, 0.01


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _r(x, nd=4):
    return None if x is None else round(x, nd)


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _dump(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


def _lo(a, b):
    return b if a is None else a if b is None else min(a, b)


def _hi(a, b):
    return b if a is None else a if b is None else max(a, b)


def _histogram(values):
    n_bins = round((HIST_HI - HIST_LO) / HIST_STEP)
    counts = [0] * (n_bins + 2)  # [below, bins..., above]
    for v in values:
        if v < HIST_LO:
            counts[0] += 1
        elif v >= HIST_HI:
            counts[-1] += 1
        else:
            counts[1 + int((v - HIST_LO) / HIST_STEP + 1e-9)] += 1
    return counts


def update_state(state: dict, rows: list, scanned_at: str, size: float) -> dict:
    """Fold one scan's rows into the running state. Returns this scan's totals."""
    events = state.setdefault("events", {})
    state["scans"] = state.get("scans", 0) + 1
    state.setdefault("first_scan_at", scanned_at)
    state["last_scan_at"] = scanned_at
    state["size"] = size
    ext = state.setdefault("extremes", {})

    n_under = n_over = n_under_exh = n_over_exh = 0
    opp_net = opp_gross = opp_fees = opp_cap = opp_apr_cap = opp_gross_apr_cap = 0.0
    opp_count = 0
    fee_eaten = 0  # positive edge before fees, none after
    fee_eaten_gross = 0.0
    for r in rows:
        exh = "likely_non_exhaustive" not in r["flags"]
        sa, sb = r["sum_ask"], r["sum_bid"]
        under = sa is not None and sa < 1 - 1e-9
        over = sb is not None and sb > 1 + 1e-9
        n_under += under
        n_over += over
        n_under_exh += under and exh
        n_over_exh += over and exh

        e = events.setdefault(r["event_ticker"], {"first_seen": scanned_at, "obs": 0, "under": 0, "over": 0, "positive": 0})
        e.update(
            title=r["title"],
            series=r["series_ticker"],
            category=r["category"],
            n=r["n_legs"],
            last_seen=scanned_at,
            last_ask=_r(sa),
            last_bid=_r(sb),
            last_mid=_r(r["mid_sum"]),
            best=r["best_taker"],
            net=_r(r[f"{r['best_taker']}_net"] if r["best_taker"] else None, 2),
            gross=_r(r[f"{r['best_taker']}_gross"] if r["best_taker"] else None, 2),
            fees=_r(r[f"{r['best_taker']}_fees"] if r["best_taker"] else None, 2),
            roi=_r(r["best_roi"]),
            apr=_r(r["best_apr"]),
            days=_r(r["days_to_settle"], 2),
            exh=exh,
            flags=r["flags"],
        )
        e["obs"] += 1
        e["under"] += under
        e["over"] += over
        e["ask_lo"], e["ask_hi"] = _lo(e.get("ask_lo"), e["last_ask"]), _hi(e.get("ask_hi"), e["last_ask"])
        e["bid_lo"], e["bid_hi"] = _lo(e.get("bid_lo"), e["last_bid"]), _hi(e.get("bid_hi"), e["last_bid"])

        # Estimated return: every exhaustive event with a positive taker edge right now.
        # Fees are already in `net`; gross and fees are kept to show what Kalshi takes.
        net = e["net"]
        if exh and net is not None and net > 0:
            e["positive"] += 1
            cap = net / r["best_roi"] if r["best_roi"] else 0.0
            opp_count += 1
            opp_net += net
            opp_gross += e["gross"]
            opp_fees += e["fees"]
            opp_cap += cap
            if r["best_apr"] is not None:
                opp_apr_cap += r["best_apr"] * cap
                gross_roi = e["gross"] / (cap - e["fees"]) if cap > e["fees"] else 0.0
                opp_gross_apr_cap += gross_roi * 365 / max(r["days_to_settle"], 1 / 24) * cap
        elif exh:
            gross_best = max((r[k] for k in ("taker_long_gross", "taker_short_gross") if r.get(k) is not None), default=None)
            if gross_best is not None and gross_best > 0:
                fee_eaten += 1
                fee_eaten_gross += gross_best

        # All-time extremes on the sides that matter: the cheapest full set of YES
        # (lowest ask sum) and the richest full set of bids (highest bid sum).
        if exh:
            for key, val, better in (
                ("ask_lo", sa, lambda a, b: a < b),
                ("bid_hi", sb, lambda a, b: a > b),
            ):
                if val is not None and (key not in ext or better(val, ext[key]["value"])):
                    ext[key] = {"value": _r(val), "event": r["event_ticker"], "title": r["title"], "at": scanned_at}

    cutoff = (datetime.fromisoformat(scanned_at.replace("Z", "+00:00")) - PRUNE_AFTER).isoformat()
    for k in [k for k, e in events.items() if e["last_seen"].replace("Z", "+00:00") < cutoff]:
        del events[k]

    exh_rows = [r for r in rows if "likely_non_exhaustive" not in r["flags"]]
    point = {
        "t": scanned_at,
        "events": len(rows),
        "under": n_under,
        "over": n_over,
        "under_exh": n_under_exh,
        "over_exh": n_over_exh,
        "opps": opp_count,
        "gross": round(opp_gross, 2),  # edge before Kalshi fees
        "fees": round(opp_fees, 2),  # Kalshi taker fees on those trades
        "net": round(opp_net, 2),  # gross - fees
        "cap": round(opp_cap, 2),  # cost of the trades, fees included
        "apr": _r(opp_apr_cap / opp_cap) if opp_cap else None,
        "apr_gross": _r(opp_gross_apr_cap / opp_cap) if opp_cap else None,
        "fee_eaten": fee_eaten,
        "fee_eaten_gross": round(fee_eaten_gross, 2),
        "min_ask": _r(min((r["sum_ask"] for r in exh_rows if r["sum_ask"] is not None), default=None)),
        "max_bid": _r(max((r["sum_bid"] for r in exh_rows if r["sum_bid"] is not None), default=None)),
    }
    state.setdefault("history", []).append(point)
    state["history"] = state["history"][-MAX_HISTORY:]
    state["hist_ask"] = _histogram(r["sum_ask"] for r in exh_rows if r["sum_ask"] is not None)
    state["hist_bid"] = _histogram(r["sum_bid"] for r in exh_rows if r["sum_bid"] is not None)
    return point


def thin(points: list, n: int) -> list:
    """Evenly sample at most n points, always keeping the newest."""
    if len(points) <= n:
        return points
    step = (len(points) - 1) / (n - 1)
    return [points[round(i * step)] for i in range(n)]


def build_summary(state: dict) -> dict:
    scans = state["scans"]
    events = state["events"]
    last = state["last_scan_at"]

    def interest(item):
        k, e = item
        dev = max(
            (1 - e["last_ask"]) if e.get("last_ask") is not None else -9,
            (e["last_bid"] - 1) if e.get("last_bid") is not None else -9,
        )
        return (e.get("exh", True), e["under"] + e["over"] > 0, (e["under"] + e["over"]) / e["obs"], dev)

    top = sorted(events.items(), key=interest, reverse=True)[:MAX_EVENTS_PUSHED]
    ever = [e for e in events.values() if (e["under"] or e["over"]) and e.get("exh", True)]
    return {
        "updated_at": last,
        "first_scan_at": state["first_scan_at"],
        "scans": scans,
        "size": state["size"],
        "tracked": len(events),
        "ever_under": sum(1 for e in ever if e["under"]),
        "ever_over": sum(1 for e in ever if e["over"]),
        "ever_positive": sum(1 for e in events.values() if e["positive"]),
        "current": state["history"][-1],
        "extremes": state["extremes"],
        "hist": {"lo": HIST_LO, "hi": HIST_HI, "step": HIST_STEP, "ask": state["hist_ask"], "bid": state["hist_bid"]},
        "events": [
            {"ticker": k, "live": e["last_seen"] == last, **{f: e.get(f) for f in (
                "title", "category", "n", "obs", "under", "over", "positive", "last_ask", "last_bid",
                "ask_lo", "ask_hi", "bid_lo", "bid_hi", "best", "gross", "fees", "net", "roi", "apr", "days", "exh", "flags",
            )}}
            for k, e in top
        ],
    }


def run_once(args, out_dir):
    state_path = os.path.join(out_dir, "state.json")
    state = _load(state_path, {})
    scan_args = build_parser().parse_args(["--size", str(args.size), "--rps", str(args.rps)])
    rows, _ = scan(scan_args)
    scanned_at = _now_iso()
    point = update_state(state, rows, scanned_at, args.size)
    _dump(state_path, state)
    _dump(os.path.join(out_dir, "dash_summary.json"), build_summary(state))
    _dump(os.path.join(out_dir, "dash_history.json"), {"updated_at": scanned_at, "points": thin(state["history"], MAX_HISTORY_PUSHED)})
    print(
        f"{scanned_at} scan #{state['scans']}: {point['events']} events, "
        f"{point['under']} with asks < 1, {point['over']} with bids > 1, "
        f"{point['opps']} taker opportunities worth ${point['net']:.2f}",
        file=sys.stderr,
    )


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--once", action="store_true", help="run a single scan and exit")
    p.add_argument("--interval", type=float, default=900, help="seconds between scans (default 900)")
    p.add_argument("--size", type=float, default=100, help="contract sets to walk each book to (default 100)")
    p.add_argument("--rps", type=float, default=10.0)
    p.add_argument("--out-dir", default=DEFAULT_DIR, help="where state and dashboard JSON go")
    args = p.parse_args(argv)
    os.makedirs(args.out_dir, exist_ok=True)
    while True:
        started = time.monotonic()
        run_once(args, args.out_dir)
        if args.once:
            return
        time.sleep(max(0.0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
