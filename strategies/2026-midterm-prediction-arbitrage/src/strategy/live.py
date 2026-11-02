"""Live execution through the shared broker: real Kalshi orders, with hard limits.

Paper trading is untouched; this module is used only with ``--mode live`` and a
portfolio mapped to a Kalshi subaccount (``czquant map-live``). Without
``--send-orders`` a live run is a dry run: it reads the live account and logs
every order it would send, and sends none.

Accounting stays with the shared account monitor (``czquant monitor account``),
which records fills, settlements and cash from Kalshi. This module only reads an
order's fill count from the venue so the run can act on it straight away:

* **Legs follow the fills.** A basket's legs go out one at a time as IOC orders.
  Each later leg asks for no more than the smallest fill so far, so a short leg
  never leaves more unhedged contracts than the legs before it.
* **Extras are sold back.** Contracts beyond the complete sets are closed with
  reduce-only IOC orders no worse than ``unwind_slippage`` from what they cost.
* **Unknown means stop.** If an order's outcome can't be read, nothing more is
  sent: the run halts with a ``halt`` record for an operator to resolve.
* **Open baskets include unreconciled orders.** A basket with an order the
  monitor hasn't reconciled yet counts as open, so it is never entered twice.
* **Limits.** ``max_spend`` caps the entry capital one run may commit, and a
  file named STOP in the strategy's runs directory stops new baskets.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from czquant import Broker
from czquant.broker import scope
from czquant.store import decimal

TERMINAL = ("executed", "canceled", "cancelled")
OPEN_ORDER_STATUSES = ("pending", "acknowledged", "unknown")
TICK = Decimal("0.01")


@dataclass
class LiveLimits:
    max_spend: Decimal  # entry capital this run may commit, fees included
    send_orders: bool = False  # False: log every order, send none
    unwind_slippage: Decimal = Decimal("0.05")
    fill_wait: float = 5.0  # seconds to wait for an order to reach a final status
    stop_file: Path | None = None
    spent: Decimal = field(default=Decimal(0))
    halted: str | None = None  # persistent: written as a halt record, cleared by an operator
    stopped_run: str | None = None  # this run only, e.g. Kalshi rejected an order outright


def open_baskets(broker: Broker) -> dict[str, str]:
    """Baskets held as lots, plus baskets with an order the monitor hasn't reconciled."""
    held = {lot["context"]["basket"]: lot["context"]["position_id"] for lot in broker.state().lots.values()
            if "basket" in lot["context"]}
    rows = broker.store.db.execute(
        "SELECT context FROM orders WHERE portfolio_id=? AND (status IN (%s) OR CAST(filled AS REAL) > 0)"
        % ",".join("?" * len(OPEN_ORDER_STATUSES)), (broker.portfolio, *OPEN_ORDER_STATUSES)).fetchall()
    for (raw,) in rows:
        context = json.loads(raw or "{}")
        if context.get("basket"):
            held.setdefault(context["basket"], context.get("position_id", "unreconciled order"))
    return held


def order_fill(api: Any, broker: Broker, response: dict[str, Any], wait: float) -> Decimal | None:
    """Contracts an IOC order filled, read from the venue. None if it can't be known."""
    order = response.get("order", response)
    deadline = time.monotonic() + wait
    while True:
        status = order.get("status")
        count = order.get("fill_count_fp", order.get("fill_count"))
        if status in TERMINAL and count is not None:
            return decimal(count)
        venue_id = order.get("order_id")
        if venue_id is None or time.monotonic() >= deadline:
            return None
        time.sleep(0.5)
        try:
            order = api.get(f"/portfolio/orders/{venue_id}", scope(broker.metadata)).get("order", {})
        except Exception:
            order = {"order_id": venue_id}


def buy_order(ticker: str, side: str, price: Decimal, count: int) -> dict[str, Any]:
    """Buy YES at most `price`, or buy NO at most `price` (a YES ask at 1 - price)."""
    return {"ticker": ticker, "side": "bid" if side == "yes" else "ask", "count": str(count),
            "price": str(price if side == "yes" else 1 - price),
            "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross"}


def close_order(ticker: str, side: str, price: Decimal, count: int, slippage: Decimal) -> dict[str, Any]:
    """Reduce-only IOC that sells `count` contracts no worse than `price` - `slippage`."""
    floor = max(TICK, price - slippage)  # per contract held
    yes_limit = floor if side == "yes" else 1 - floor  # sell YES at >= floor; buy YES back at <= 1 - floor
    return {"ticker": ticker, "side": "ask" if side == "yes" else "bid", "count": str(count),
            "price": str(yes_limit), "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross", "reduce_only": True}


def standing_halt(broker: Broker) -> str | None:
    """The newest halt on this portfolio that no halt_cleared record has cleared."""
    events = broker.store.events(broker.portfolio, kinds=("observation",), observations=("halt", "halt_cleared"))
    cleared = {e["data"].get("halt_id") for e in events if e["data"]["kind"] == "halt_cleared"}
    halts = [e for e in events if e["data"]["kind"] == "halt" and e["record_id"] not in cleared]
    return halts[-1]["data"]["reason"] if halts else None


def stopped(limits: LiveLimits) -> str | None:
    if limits.halted or limits.stopped_run:
        return limits.halted or limits.stopped_run
    if limits.stop_file is not None and limits.stop_file.exists():
        return f"stop file present: {limits.stop_file}"
    return None


def execute(broker: Broker, api: Any, run: Any, legs: list, quantity: int, outlay: Decimal,
            context: dict[str, Any], limits: LiveLimits) -> dict[tuple[str, str], Decimal]:
    """Send one basket. Returns the contracts each leg filled (zero after an unwind)."""
    filled: dict[tuple[str, str], Decimal] = {}
    if not limits.send_orders:
        for leg in legs:
            run.event("live_order", context["position_id"], dry_run=True,
                      order=buy_order(leg.ticker, leg.side, leg.price, quantity), basket=context["basket"])
            filled[(leg.ticker, leg.side)] = Decimal(quantity)
        limits.spent += outlay
        return filled
    target, sent = Decimal(quantity), []
    for leg in legs:
        if target < 1:
            break
        order = buy_order(leg.ticker, leg.side, leg.price, int(target))
        order["client_order_id"] = str(uuid.uuid4())
        try:
            response = broker.submit(order, context)
            got = order_fill(api, broker, response, limits.fill_wait)
        except Exception as exc:
            run.event("live_order", context["position_id"], order=order, basket=context["basket"],
                      error=f"{type(exc).__name__}: {exc}"[:300])
            if order_status(broker, order["client_order_id"]) == "rejected":
                # Kalshi refused the order outright: nothing filled. Stop this run, unwind
                # what earlier legs bought, and leave no standing halt.
                limits.stopped_run = f"Kalshi rejected the order on {leg.ticker}: {exc}"[:300]
                run.event("note", message=f"live run stopped: {limits.stopped_run}")
                break
            got = None
        if got is None:
            halt(broker, run, limits, f"order on {leg.ticker} has an unknown outcome; "
                 "check the account before trading again")
            return filled  # nothing is unwound against an unknown position
        run.event("live_order", context["position_id"], order=order, basket=context["basket"], filled=str(got))
        filled[(leg.ticker, leg.side)] = got
        sent.append(leg)
        target = min(target, got)
    keep = target if len(sent) == len(legs) else Decimal(0)
    limits.spent += outlay * keep / quantity
    for leg in sent:
        extra = filled[(leg.ticker, leg.side)] - keep
        if extra > 0:
            unwind(broker, api, run, leg, int(extra), context, limits)
            filled[(leg.ticker, leg.side)] = keep
    return filled


def order_status(broker: Broker, client_order_id: str) -> str | None:
    row = broker.store.db.execute("SELECT status FROM orders WHERE id=? AND portfolio_id=?",
                                  (client_order_id, broker.portfolio)).fetchone()
    return row[0] if row else None


def unwind(broker: Broker, api: Any, run: Any, leg: Any, count: int, context: dict[str, Any],
           limits: LiveLimits) -> None:
    order = close_order(leg.ticker, leg.side, leg.price, count, limits.unwind_slippage)
    try:
        got = order_fill(api, broker, broker.submit(order, {**context, "repair": "unwind"}), limits.fill_wait)
    except Exception as exc:
        run.event("live_order", context["position_id"], order=order, error=f"{type(exc).__name__}: {exc}"[:300])
        got = None
    run.event("live_unwind", context["position_id"], basket=context["basket"], ticker=leg.ticker,
              side=leg.side, excess=count, sold=None if got is None else str(got))
    if got is None or got < count:
        halt(broker, run, limits, f"{count - (got or 0)} unhedged {leg.side.upper()} on {leg.ticker} "
             "could not be sold back")


def halt(broker: Broker, run: Any, limits: LiveLimits, reason: str) -> None:
    limits.halted = reason
    broker.observe("halt", {"reason": reason})
    run.event("note", message=f"live trading halted: {reason}")
