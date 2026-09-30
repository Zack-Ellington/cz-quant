"""Shared rendering: model-free checks, trade EV, the paper section.

Model-specific tables live in ``report.py``. This module is shared by the
2026 midterm strategies and should stay identical between them.

Every number in the paper section is read back from ``ledger.jsonl`` files and
names the run directory that produced it.
"""

from __future__ import annotations

import math
from decimal import Decimal

from strategy import money
from strategy.checks import Check
from strategy.fees import FeeSchedule
from strategy.markets import Quote
from strategy.pnl import free_cash, positions_from_events, summarize

LABELS = {
    "DD": "Democrats sweep",
    "DR": "D House / R Senate",
    "RD": "R House / D Senate",
    "RR": "Republicans sweep",
}


# --- Small formatters ------------------------------------------------------------


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def cents(value) -> str:
    return f"{float(value) * 100:+.1f}c"


def usd(value: Decimal | float) -> str:
    value = Decimal(str(value)) if not isinstance(value, Decimal) else value
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def edge(model: float, market: float | None) -> str:
    return "n/a" if market is None else f"{(model - market) * 100:+.1f}pp"


def bid_ask(quote: Quote) -> str:
    bid = "-" if quote.bid is None else f"{quote.bid * 100:.1f}"
    ask = "-" if quote.ask is None else f"{quote.ask * 100:.1f}"
    return f"{bid}/{ask}"


def size_text(sets: float) -> str:
    return f"{sets:,.0f}" if sets >= 1 else f"{sets:.2f}"


# --- Trade EV at an executable quantity -------------------------------------------


def ev_after_fees(
    model: float, quote: Quote, schedule: FeeSchedule, contracts: int
) -> tuple[str, Decimal, int] | None:
    """Best of buying YES at the ask or NO at the bid, per contract, after fees.

    The quantity is capped by the contracts resting at that price, and the fee
    is computed at that quantity, so a one-contract book pays a one-contract
    fee. Returns ``(action, EV per contract, quantity)`` or ``None`` if neither
    side has a positive EV at an executable quantity.
    """
    if not quote.is_open:
        return None
    p = Decimal(repr(model))
    best = None
    if quote.ask is not None:
        n = min(contracts, math.floor(quote.ask_size))
        if n >= 1:
            ask = money.price(quote.ask)
            best = ("buy", p - ask - schedule.fee(ask, n) / n, n)
    if quote.bid is not None:
        n = min(contracts, math.floor(quote.bid_size))
        if n >= 1:
            no = 1 - money.price(quote.bid)
            ev = (1 - p) - no - schedule.fee(no, n) / n
            if best is None or ev > best[1]:
                best = ("sell", ev, n)
    return best if best is not None and best[1] > 0 else None


def ev_text(model: float, quote: Quote, fees: Mapping[str, FeeSchedule], contracts: int) -> str:
    trade = ev_after_fees(model, quote, fees.get(quote.series, FeeSchedule()), contracts)
    if trade is None:
        return "-"
    action, ev, n = trade
    return f"{action} {cents(ev)} x{n}"


# --- Model-free checks ----------------------------------------------------------


def checks_section(checks: list[Check], contracts: int) -> list[str]:
    name_w = max((len(c.name) for c in checks), default=10)
    header = (
        f"{'Check'.ljust(name_w)}  {'Cost':>7}  {'Raw':>7}  {'Net':>7}  "
        f"{'Worst':>8}  {'Size':>7}  Verdict"
    )
    lines = [
        f"Model-free checks (taker; per set, in cents; fees at min({contracts}, size) sets; "
        "Raw/Net assume D or R leaders, Worst covers independent or vacant leaders)",
        header,
        "-" * len(header),
    ]
    for c in checks:
        lines.append(
            f"{c.name.ljust(name_w)}  {c.cost:>7.4f}  {cents(c.raw_edge):>7}  "
            f"{cents(c.net_edge):>7}  {cents(c.worst_net_edge):>8}  {size_text(c.size):>7}  {c.verdict}"
        )
    counts = {v: sum(1 for c in checks if c.verdict == v) for v in ("ARB", "cond", "fees")}
    lines.append(
        f"{counts['ARB']} arbitrage(s) in every settlement state; {counts['cond']} conditional on "
        f"D/R leaders; {counts['fees']} positive only before fees."
    )
    return lines


# --- Paper section of a run -------------------------------------------------------


def paper_section(run_label: str, run_id: str, events: list[dict], bankroll: Decimal) -> list[str]:
    """What this run did on paper, read from the ledger of every run so far."""
    mine = [e for e in events if e["run_id"] == run_id]
    positions = positions_from_events(events)
    open_ = [p for p in positions.values() if p.is_open]
    lines = [
        f"Paper trading - {run_label} (paper mode: simulated fills, nothing sent)",
        f"Bankroll {usd(bankroll)}; free cash {usd(free_cash(events, bankroll))} after this run; "
        f"{len(open_)} open position(s), entry capital {usd(sum((p.entry_capital for p in open_), money.ZERO))}",
    ]
    settled = [e for e in mine if e["type"] == "settlement"]
    for e in settled:
        p = positions[e["position_id"]]
        lines.append(
            f"  settled {p.position_id} {p.basket}: receipt {usd(Decimal(e['receipt']))}, "
            f"P&L {usd(Decimal(e['pnl']))} on {e['received_at'][:10]}"
        )
    signals = [e for e in mine if e["type"] == "signal"]
    accepted = [e for e in signals if e["accepted"]]
    lines.append(f"Signals: {len(accepted)} accepted, {len(signals) - len(accepted)} rejected")
    for e in signals:
        if e["accepted"]:
            lines.append(
                f"  + {e['position_id']} {e['basket']}: {e['quantity']} x{_limit_text(e.get('limit'))}, "
                f"edge {cents(Decimal(e['edge']))}/set > threshold {cents(Decimal(e['threshold']))}"
            )
        else:
            lines.append(f"  - {e['basket']}: {e['reason']}")
    for e in mine:
        if e["type"] == "fill":
            lines.append(
                f"  fill {e['position_id']} {e['ticker']} {e['side'].upper()} {e['quantity']} @ "
                f"{Decimal(e['price']):.4f}, fee {usd(Decimal(e['fee']))}, cash {usd(Decimal(e['cash_delta']))}"
                + (" (simulated)" if e.get("simulated") else "")
            )
    if open_:
        summary = summarize(open_)
        lines.append(f"Open book: projected minimum profit {usd(summary.projected_min_profit)} (worst settlement state)")
    return lines


def _limit_text(limit: str | None) -> str:
    return f" (capped by {limit})" if limit else ""
