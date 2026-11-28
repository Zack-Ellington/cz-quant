"""Kalshi live execution adapter.

A live order goes through Broker.submit, which records the intent before anything
is sent. With `dry_run` nothing is sent: the order is built (and, for Polymarket US,
signed), recorded as `dry_run`, and reported filled 0.

Kalshi fills are booked by the account monitor (`czquant monitor account`), from the
venue's fill history; the adapter reports confirmed quantity, cost and fee evidence
for immediate execution decisions without creating spendable cash. Polymarket US
fills are booked by its adapter from the synchronous execution reports (czquant.polymarket_us).
"""

from __future__ import annotations

from decimal import InvalidOperation
from typing import Any

from .broker import Broker
from .store import decimal

KALSHI_TERMINAL = {"executed": "filled", "canceled": "canceled"}


class KalshiLive:
    mode = "live"
    venue = "kalshi"
    policy = "kalshi-live-v1"
    takes_broker = True

    def __init__(self, api: Any, dry_run: bool = False):
        self.api, self.dry_run = api, dry_run

    def create_order(self, order: dict[str, Any], broker: Broker) -> dict[str, Any]:
        identifier = order["client_order_id"]
        if order.get("time_in_force") not in ("immediate_or_cancel", "fill_or_kill") or order.get("post_only"):
            broker.finish(identifier, "rejected")
            raise NotImplementedError("live trading here sends taker IOC or FOK orders only")
        if self.dry_run:
            broker.finish(identifier, "dry_run")
            broker.observe("dry_run_order", {"client_order_id": identifier, "request": order})
            return {"client_order_id": identifier, "status": "dry_run", "filled": "0", "request": order}
        response = self.api.create_order(order)
        body = response.get("order", response)
        venue_id = body.get("order_id")
        raw_fill = body.get("fill_count_fp", body.get("fill_count"))
        status = KALSHI_TERMINAL.get(body.get("status", ""))
        # V2 create replies omit status. Explicit zero remaining quantity proves
        # completion; a partial IOC cancels its unfilled remainder. Never infer
        # completion from HTTP success, the requested TIF, or fill count alone.
        if "status" not in body and venue_id and raw_fill is not None and body.get("remaining_count") is not None:
            try:
                filled, remaining = decimal(raw_fill), decimal(body["remaining_count"])
                requested = decimal(order["count"])
                if filled.is_finite() and remaining.is_finite() and 0 <= filled <= requested and remaining == 0:
                    status = "filled" if filled == requested else "canceled"
            except (InvalidOperation, TypeError, ValueError):
                pass  # Preserve ambiguous evidence for account-monitor recovery.
        broker.acknowledge(identifier, venue_id, response, raw_fill, terminal=status is not None)
        if raw_fill is None or status is None:
            # An IOC that isn't terminal in its own reply is unexpected: report it, never guess.
            broker.finish(identifier, "unknown", release=False)
            broker.observe("order_unresolved", {"client_order_id": identifier, "response": response})
            return {"client_order_id": identifier, "status": "unknown", "filled": "0",
                    "order": {"order_id": venue_id}, "response": response}
        evidence: dict[str, str] = {}
        if decimal(raw_fill) > 0:
            try:
                average = decimal(body["average_fill_price"])
                fee = decimal(body["average_fee_paid"])
                limit = decimal(order["price"])
                if (not average.is_finite() or not fee.is_finite() or not 0 <= average <= 1 or fee < 0
                        or (average > limit if order["side"] == "bid" else average < limit)):
                    raise ValueError("invalid execution price or fee")
                quantity = decimal(raw_fill)
                # An average is not a bound for a partially matched basket. The
                # submitted limit bounds every fill; exact notional bounds capital.
                evidence = {"quantity": str(quantity), "yes_notional": str(average * quantity),
                            "fee": str(fee * quantity),
                            "yes_price_min": "0" if order["side"] == "bid" else str(limit),
                            "yes_price_max": str(limit) if order["side"] == "bid" else "1"}
            except (KeyError, InvalidOperation, TypeError, ValueError):
                broker.observe("execution_evidence_unavailable", {"client_order_id": identifier,
                    "reason": "Kalshi response lacks valid average fill price and fee"})
        elif status is not None:
            # A terminal empty IOC has no executions left to reconcile.
            broker.finish(identifier, status)
        # Filled reservations stay until the account monitor books exact receipts.
        return {"client_order_id": identifier, "status": status, "filled": str(decimal(raw_fill)),
                "order": {"order_id": venue_id}, "response": response,
                **({"execution": evidence} if evidence else {})}
