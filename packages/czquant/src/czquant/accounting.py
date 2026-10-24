"""Pure FIFO accounting replay; no strategy imports, I/O, or market assumptions."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .store import decimal

ZERO = Decimal(0)


@dataclass
class State:
    cash: Decimal = ZERO
    contributed: Decimal = ZERO
    realized: Decimal = ZERO
    fees: Decimal = ZERO
    rewards: Decimal = ZERO
    prepaid: Decimal = ZERO
    notional: Decimal = ZERO
    fills: int = 0
    settlements: int = 0
    lots: dict[str, dict[str, Any]] = field(default_factory=dict)
    markets: set[str] = field(default_factory=set)

    @property
    def cost(self) -> Decimal:
        return sum((lot["cost"] for lot in self.lots.values()), ZERO)

    def apply(self, event: dict[str, Any]) -> None:
        if event["type"] != "transaction":
            return
        d = event["data"]
        cash, external, fee = (decimal(d.get(k, "0")) for k in ("cash_delta", "external_delta", "fee"))
        prepaid = decimal(d.get("prepaid_delta", "0"))
        self.cash += cash
        self.contributed += external
        self.fees += fee
        self.prepaid += prepaid
        legs = d.get("legs", [])
        if len(legs) > 1:
            raise ValueError("accounting transactions have at most one contract leg")
        if not legs:
            self.realized += cash - external + prepaid
            if d["reason"] in ("rebate", "reward", "interest"):
                self.rewards += cash
            return
        leg = legs[0]
        qty = decimal(leg["quantity_delta"])
        market, side = leg["market"], leg["side"]
        if side not in ("yes", "no") or qty == 0:
            raise ValueError("invalid inventory delta")
        self.markets.add(market)
        if d["reason"] == "fill":
            self.fills += 1
            self.notional += abs(qty * decimal(leg["price"]))
        if d["reason"] == "settlement":
            self.settlements += 1
        if qty > 0:
            self.lots[event["record_id"]] = {**leg, "quantity": qty, "cost": -cash - prepaid,
                "opened_at": event["effective_at"], "context": d.get("context", {}),
                "order_id": d.get("order_id"), "record_id": event["record_id"]}
            return
        remaining, basis = -qty, ZERO
        for identifier, lot in list(self.lots.items()):
            if lot["market"] != market or lot["side"] != side:
                continue
            if leg.get("lot_id") and leg["lot_id"] != identifier:
                continue
            take = min(remaining, lot["quantity"])
            removed = lot["cost"] * take / lot["quantity"]
            basis += removed
            lot["quantity"] -= take
            lot["cost"] -= removed
            remaining -= take
            if lot["quantity"] == 0:
                del self.lots[identifier]
            if remaining == 0:
                break
        if remaining:
            raise ValueError(f"unmatched inventory retirement: {market} {side}")
        self.realized += cash + prepaid - basis


def active_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    excluded = {identifier for e in events if e["type"] == "correction" for identifier in e["data"]["targets"]}
    return [e for e in events if e["record_id"] not in excluded]


def replay(events: list[dict[str, Any]]) -> State:
    state = State()
    for event in active_events(events):
        state.apply(event)
    return state
