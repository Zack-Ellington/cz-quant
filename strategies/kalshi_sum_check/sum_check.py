"""Kalshi mutually-exclusive event sum check.

In an event whose outcomes are mutually exclusive and exhaustive, exactly one YES
pays $1, so the YES prices across its markets should sum to 1. This scans every
open mutually-exclusive event and, for a target size of N contract sets, reports:

  * sum of YES asks and sum of YES bids, each walked through the book to N
  * net edge as a taker (cross the book) and as a maker (rest one tick inside),
    after the event's own series fee schedule
  * time to settlement and the annualized return on capital

Two trades exist per event:
  long  = buy YES on every leg.  Pays N.        Edge = N - sum(asks)   - fees
  short = buy NO on every leg.   Pays (n-1)*N.  Edge = sum(bids) - N   - fees
          (buying NO at 1 - bid is the same as selling YES at bid)

Usage:
  python sum_check.py --size 100 --out results.csv
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from datetime import datetime, timezone

from kalshi_client import KalshiClient
from pricing import (
    KNOWN_FEE_TYPES,
    maker_buy_price,
    maker_fee,
    maker_sell_price,
    parse_book,
    taker_fee,
    walk,
)

MIN_DAYS = 1 / 24  # floor so markets settling within the hour don't annualize to infinity
# The API has no "exhaustive" field: "Which country becomes the 51st state?" is flagged
# mutually_exclusive but can resolve with every leg NO. If mid prices sum to less than
# 1 - this, the market itself is pricing a "none of the above" and the long trade isn't an arb.
NON_EXHAUSTIVE_GAP = 0.10


def _ts(s: str | None):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _f(x):
    return float(x) if x not in (None, "") else None


def annualize(roi, days):
    if roi is None or days is None or roi <= -1:
        return None, None
    years = max(days, MIN_DAYS) / 365.0
    simple = roi / years
    try:
        compound = math.expm1(math.log1p(roi) / years)
    except OverflowError:
        compound = math.inf
    return simple, compound


def analyze_event(event: dict, books: dict, series: dict, size: float, now: datetime,
                  non_exhaustive_gap: float = NON_EXHAUSTIVE_GAP):
    """Returns (summary_row, leg_rows) or None if the event can't be evaluated."""
    markets = event.get("markets") or []
    if any(m.get("result") == "yes" for m in markets):
        return None  # already resolved
    legs = [m for m in markets if m.get("status") == "active"]
    if len(legs) < 2:
        return None
    flags = []
    if any(m.get("status") != "active" and m.get("result") != "no" for m in markets):
        flags.append("inactive_unresolved_leg")

    s = series.get(event.get("series_ticker"), {})
    fee_type = s.get("fee_type", "quadratic")
    fee_mult = float(s.get("fee_multiplier", 1))
    if fee_type not in KNOWN_FEE_TYPES:
        flags.append(f"unknown_fee_type:{fee_type}")

    n = len(legs)
    leg_rows = []
    parsed = []
    for m in legs:
        book = parse_book(books.get(m["ticker"], {}))
        parsed.append((m, book))
        leg_rows.append(
            {
                "event_ticker": event["event_ticker"],
                "ticker": m["ticker"],
                "outcome": m.get("yes_sub_title") or m.get("title"),
                "best_bid": book.best_bid,
                "best_ask": book.best_ask,
                "bid_depth": sum(q for _, q in book.yes_bids),
                "ask_depth": sum(q for _, q in book.yes_asks),
            }
        )

    if any(b.best_ask is None for _, b in parsed):
        flags.append("leg_without_ask")
    if any(b.best_bid is None for _, b in parsed):
        flags.append("leg_without_bid")

    # Top of book.
    top_ask = sum(b.best_ask for _, b in parsed) if "leg_without_ask" not in flags else None
    top_bid = sum(b.best_bid or 0.0 for _, b in parsed)
    mid_sum = sum(((b.best_bid or 0.0) + (b.best_ask if b.best_ask is not None else 1.0)) / 2 for _, b in parsed)
    if mid_sum < 1 - non_exhaustive_gap:
        flags.append("likely_non_exhaustive")

    # --- taker long: lift YES asks on every leg, walked to N (or the thinnest leg's depth)
    long_size = min([size] + [r["ask_depth"] for r in leg_rows])
    taker_long = _taker(parsed, long_size, side="ask", fee_type=fee_type, fee_mult=fee_mult)
    if 0 < long_size < size:
        flags.append(f"ask_depth_only_{long_size:g}")

    # --- taker short: hit YES bids (buy NO) on every leg
    short_size = min([size] + [r["bid_depth"] for r in leg_rows])
    taker_short = _taker(parsed, short_size, side="bid", fee_type=fee_type, fee_mult=fee_mult)
    if 0 < short_size < size:
        flags.append(f"bid_depth_only_{short_size:g}")

    if taker_long:
        tl_sum = taker_long["notional"] / long_size
        tl_net = long_size - taker_long["notional"] - taker_long["fees"]
        tl_cap = taker_long["notional"] + taker_long["fees"]
    else:
        tl_sum = tl_net = tl_cap = None
    if taker_short:
        ts_sum = taker_short["notional"] / short_size
        ts_net = taker_short["notional"] - short_size - taker_short["fees"]
        ts_cap = n * short_size - taker_short["notional"] + taker_short["fees"]
    else:
        ts_sum = ts_net = ts_cap = None

    # --- maker: rest one order per leg at size N; assumes every leg fills (leg risk!)
    mb = [maker_buy_price(b, m.get("price_ranges")) for m, b in parsed]
    ms = [maker_sell_price(b, m.get("price_ranges")) for m, b in parsed]
    mk_long_fees = sum(maker_fee(size * p * (1 - p), fee_type, fee_mult) for p in mb)
    mk_short_fees = sum(maker_fee(size * p * (1 - p), fee_type, fee_mult) for p in ms)
    ml_sum, ms_sum = sum(mb), sum(ms)
    ml_net = size * (1 - ml_sum) - mk_long_fees
    ml_cap = size * ml_sum + mk_long_fees
    ms_net = size * (ms_sum - 1) - mk_short_fees
    ms_cap = size * (n - ms_sum) + mk_short_fees

    for r, p_b, p_s in zip(leg_rows, mb, ms):
        r["maker_bid_px"], r["maker_ask_px"] = p_b, p_s

    # --- time to settlement: the event pays out when its last leg expires
    settle = max(
        (_ts(m.get("expected_expiration_time")) or _ts(m.get("close_time")) for m in legs),
        default=None,
    )
    days = (settle - now).total_seconds() / 86400 if settle else None
    if any(m.get("can_close_early") for m in legs):
        flags.append("can_close_early")

    strategies = {
        "taker_long": (tl_net, tl_cap, long_size, taker_long["fees"] if taker_long else None),
        "taker_short": (ts_net, ts_cap, short_size, taker_short["fees"] if taker_short else None),
        "maker_long": (ml_net, ml_cap, size, mk_long_fees),
        "maker_short": (ms_net, ms_cap, size, mk_short_fees),
    }
    row = {
        "event_ticker": event["event_ticker"],
        "series_ticker": event.get("series_ticker"),
        "title": event.get("title"),
        "category": event.get("category"),
        "n_legs": n,
        "collateral_return": event.get("collateral_return_type") or "",
        "fee_type": fee_type,
        "fee_mult": fee_mult,
        "top_sum_ask": top_ask,
        "top_sum_bid": top_bid,
        "mid_sum": mid_sum,
        "sum_ask": tl_sum,
        "sum_bid": ts_sum,
        "dev_ask": tl_sum - 1 if tl_sum is not None else None,
        "dev_bid": ts_sum - 1 if ts_sum is not None else None,
        "maker_sum_bid": ml_sum,
        "maker_sum_ask": ms_sum,
        "settle_time": settle.isoformat() if settle else None,
        "days_to_settle": days,
    }
    best = None
    for name, (net, cap, qty, fees) in strategies.items():
        roi = net / cap if net is not None and cap else None
        simple, compound = annualize(roi, days)
        row[f"{name}_size"] = qty
        row[f"{name}_gross"] = net + fees if net is not None else None  # edge before Kalshi fees
        row[f"{name}_fees"] = fees
        row[f"{name}_net"] = net
        row[f"{name}_roi"] = roi
        row[f"{name}_apr"] = simple
        row[f"{name}_apy"] = compound
        # Rank on taker trades only: maker numbers assume every resting leg fills.
        if name.startswith("taker") and net is not None and qty and (best is None or net / qty > best[1]):
            best = (name, net / qty, roi, simple)
    row["best_taker"], row["best_edge_per_set"], row["best_roi"], row["best_apr"] = best or (None,) * 4
    row["flags"] = ";".join(flags)
    return row, leg_rows


def _taker(parsed, qty, side, fee_type, fee_mult):
    if qty <= 0:
        return None
    notional = fees = 0.0
    for _, book in parsed:
        fill = walk(book.yes_asks if side == "ask" else book.yes_bids, qty)
        notional += fill.notional
        # Hitting a YES bid = buying NO at 1 - p; p(1-p) is symmetric so fee_base is the same.
        fees += taker_fee(fill.fee_base, fee_type, fee_mult)
    return {"notional": notional, "fees": fees}


def scan(args) -> tuple[list, list]:
    """Fetch and analyze every matching event. Returns (event_rows, leg_rows), unsorted."""
    client = KalshiClient(max_rps=args.rps)
    print("Fetching series fee schedules...", file=sys.stderr)
    series = client.all_series()

    print("Fetching open events...", file=sys.stderr)
    events = []
    for ev in client.iter_open_events(series_ticker=args.series):
        if not ev.get("mutually_exclusive"):
            continue
        if args.category and (ev.get("category") or "").lower() != args.category.lower():
            continue
        active = [m for m in ev.get("markets") or [] if m.get("status") == "active"]
        if len(active) < 2:
            continue
        if args.prescreen is not None and not _passes_prescreen(active, args.prescreen):
            continue
        events.append(ev)
        if args.max_events and len(events) >= args.max_events:
            break

    tickers = [m["ticker"] for ev in events for m in ev["markets"] if m.get("status") == "active"]
    print(f"{len(events)} mutually-exclusive events, {len(tickers)} legs. Fetching order books...", file=sys.stderr)
    books = client.orderbooks(tickers)

    now = datetime.now(timezone.utc)
    rows, legs = [], []
    for ev in events:
        res = analyze_event(ev, books, series, args.size, now, args.non_exhaustive_gap)
        if res:
            rows.append(res[0])
            legs.extend(res[1])
    return rows, legs


def run(args) -> list:
    rows, legs = scan(args)
    if args.min_dev:
        rows = [r for r in rows if _max_dev(r) >= args.min_dev]
    rows.sort(key=lambda r: r["best_edge_per_set"] if r["best_edge_per_set"] is not None else -9, reverse=True)

    if args.out:
        _write_csv(args.out, rows)
        print(f"Wrote {len(rows)} events to {args.out}", file=sys.stderr)
    if args.legs_out:
        keep = {r["event_ticker"] for r in rows}
        _write_csv(args.legs_out, [l for l in legs if l["event_ticker"] in keep])
    shown = rows if args.show_non_exhaustive else [r for r in rows if "likely_non_exhaustive" not in r["flags"]]
    _print_table(shown[: args.top], args.size)
    return rows


def _passes_prescreen(markets, x):
    """Cheap filter on the top of book embedded in /events. Walking only makes sums worse,
    so an event with top sum_ask > 1 + x and top sum_bid < 1 - x can't be a taker arb."""
    asks = [_f(m.get("yes_ask_dollars")) for m in markets]
    bids = [_f(m.get("yes_bid_dollars")) or 0.0 for m in markets]
    sum_ask = sum(a for a in asks if a) if all(asks) else math.inf
    return sum_ask <= 1 + x or sum(bids) >= 1 - x


def _max_dev(r):
    devs = [abs(r[k]) for k in ("dev_ask", "dev_bid") if r[k] is not None]
    return max(devs) if devs else 0.0


def _write_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _fmt(x, spec):
    if x is None:
        return "-"
    if isinstance(x, float) and math.isinf(x):
        return "inf"
    return format(x, spec)


def _print_table(rows, size):
    hdr = f"{'event':<34}{'n':>4}{'Σask':>8}{'Σbid':>8}{'tkL $':>9}{'tkS $':>9}{'mkL $':>9}{'mkS $':>9}{'days':>8}  {'best taker':<12}{'APR':>9}  flags"
    print(f"\nTarget size: {size:g} contract sets (net $ per {size:g} sets, after fees)")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(
            f"{r['event_ticker'][:33]:<34}{r['n_legs']:>4}"
            f"{_fmt(r['sum_ask'], '.4f'):>8}{_fmt(r['sum_bid'], '.4f'):>8}"
            f"{_fmt(r['taker_long_net'], '.2f'):>9}{_fmt(r['taker_short_net'], '.2f'):>9}"
            f"{_fmt(r['maker_long_net'], '.2f'):>9}{_fmt(r['maker_short_net'], '.2f'):>9}"
            f"{_fmt(r['days_to_settle'], '.1f'):>8}  {(r['best_taker'] or '-'):<12}"
            f"{_fmt(r['best_apr'], '.1%'):>9}  {r['flags']}"
        )


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--size", type=float, default=100, help="target contract sets to walk the book to (default 100)")
    p.add_argument("--series", help="restrict to one series ticker")
    p.add_argument("--category", help="restrict to one event category, e.g. Elections")
    p.add_argument("--max-events", type=int, help="stop after this many events (for quick runs)")
    p.add_argument("--prescreen", type=float, help="skip events whose top-of-book sums are > X away from 1 on both sides")
    p.add_argument("--min-dev", type=float, default=0.0, help="only report events whose walked sum deviates from 1 by >= this")
    p.add_argument("--non-exhaustive-gap", type=float, default=NON_EXHAUSTIVE_GAP,
                   help="flag events whose mid prices sum below 1 - X (default 0.10)")
    p.add_argument("--show-non-exhaustive", action="store_true",
                   help="include likely non-exhaustive events in the printed table (always in the CSV)")
    p.add_argument("--top", type=int, default=25, help="rows to print (default 25)")
    p.add_argument("--out", help="write the full event table to this CSV")
    p.add_argument("--legs-out", help="write per-leg detail to this CSV")
    p.add_argument("--rps", type=float, default=10.0, help="max requests per second (default 10)")
    return p


def main(argv=None):
    run(build_parser().parse_args(argv))


if __name__ == "__main__":
    main()
