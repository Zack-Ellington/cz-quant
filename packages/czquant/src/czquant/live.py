"""Live execution adapters: Kalshi, and a router for a portfolio trading two venues.

A live order goes through Broker.submit, which records the intent before anything
is sent. With `dry_run` nothing is sent: the order is built (and, for Polymarket US,
signed), recorded as `dry_run`, and reported filled 0.

Kalshi fills are booked by the account monitor (`czquant monitor account`), from the
venue's fill history; the adapter only reports the order's own fill count so the
trader can size the next leg. Polymarket US fills are booked by its adapter from the
synchronous execution reports (czquant.polymarket_us).
"""

from __future__ import annotations

from typing import Any

from .broker import Broker
from .polymarket_us import PolymarketUSLive, is_polymarket_us
from .store import decimal

KALSHI_TERMINAL = {"executed": "filled", "canceled": "canceled"}


class KalshiLive:
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
        with broker.store.transaction():
            broker.store.db.execute("UPDATE orders SET venue_id=?, status='acknowledged' WHERE id=?",
                                    (venue_id, identifier))
        raw_fill = body.get("fill_count_fp", body.get("fill_count"))
        status = KALSHI_TERMINAL.get(body.get("status", ""))
        if raw_fill is None or status is None:
            # An IOC that isn't terminal in its own reply is unexpected: report it, never guess.
            broker.observe("order_unresolved", {"client_order_id": identifier, "response": response})
            return {"client_order_id": identifier, "status": "unknown", "filled": "0",
                    "order": {"order_id": venue_id}, "response": response}
        # The reservation stays until the account monitor books the fills and finishes the order.
        return {"client_order_id": identifier, "status": status, "filled": str(decimal(raw_fill)),
                "order": {"order_id": venue_id}, "response": response}


class LiveRoute:
    """One broker, one portfolio, two accounts: Kalshi tickers to Kalshi, PMUS: tickers
    to Polymarket US."""

    takes_broker = True

    def __init__(self, kalshi: KalshiLive, polymarket_us: PolymarketUSLive):
        self.kalshi, self.polymarket_us = kalshi, polymarket_us
        self.policy = kalshi.policy + "+" + polymarket_us.policy

    def create_order(self, order: dict[str, Any], broker: Broker) -> dict[str, Any]:
        adapter = self.polymarket_us if is_polymarket_us(order["ticker"]) else self.kalshi
        return adapter.create_order(order, broker)
