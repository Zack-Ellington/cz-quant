"""Pure FIFO accounting replay; no strategy imports, I/O, or market assumptions."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .store import decimal

ZERO = Decimal(0)


def venue_of(market: str) -> str:
    """Known venue names; keep other namespaces distinct for historical replay."""
    if market.startswith("PMUS:"):
        return "polymarket_us"
    if market.startswith("PM:"):
        return "polymarket"
    namespace, separator, _ = market.partition(":")
    return namespace.lower() if separator else "kalshi"


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
    # Cash by venue: a transaction's `venue`, else its leg's ticker's, else Kalshi.
    venue_cash: dict[str, Decimal] = field(default_factory=dict)
    reservations: dict[str, Decimal] = field(default_factory=dict)

    @property
    def reserved(self) -> Decimal:
        return sum(self.reservations.values(), ZERO)

    @property
    def cost(self) -> Decimal:
        return sum((lot["cost"] for lot in self.lots.values()), ZERO)

    def apply(self, event: dict[str, Any]) -> None:
        d = event['data']
        if event['type'] == 'order':
            if d.get('status') == 'intent' and 'reserved' in d:
                self.reservations[d['order']['client_order_id']] = decimal(d['reserved'])
            elif d.get('release'):
                self.reservations.pop(d['client_order_id'], None)
        elif event['type'] == 'observation':
            if d.get('kind') == 'amend_intent':
                self.reservations[d['client_order_id']] = decimal(d['reserved_during'])
            elif d.get('kind') == 'amend_acknowledged':
                self.reservations[d['client_order_id']] = decimal(d['reserved'])
        if event["type"] != "transaction":
            return
        d = event["data"]
        cash, external, fee = (decimal(d.get(k, "0")) for k in ("cash_delta", "external_delta", "fee"))
        prepaid = decimal(d.get("prepaid_delta", "0"))
        self.cash += cash
        legs_ = d.get("legs") or []
        venue = d.get("venue") or (venue_of(legs_[0]["market"]) if legs_ else "kalshi")
        self.venue_cash[venue] = self.venue_cash.get(venue, ZERO) + cash
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


ECONOMIC = ("transaction", "correction", "order", "observation")


def portfolio_state(store: Any, portfolio: str) -> State:
    """The portfolio's replayed state, extended incrementally from a per-store cache.

    A full replay reads every transaction, which made each submit, fill and correction
    slower as the ledger grew. New rows are applied on top of the cached state only
    when that is guaranteed to equal a full replay: no new correction, and no new row
    effective before the latest row already applied (FIFO lots depend on order).
    Anything else, including a rolled-back transaction (the store clears the cache),
    falls back to a full replay. The returned State is shared; treat it as read-only.
    """
    cached = store.state_cache.get(portfolio)
    if cached is not None:
        sequence, latest, state = cached
        new = store.events(portfolio, kinds=ECONOMIC, after_sequence=sequence)
        if not new:
            return state
        if all(e["type"] != "correction" and e["effective_at"] >= latest for e in new):
            try:
                for event in sorted(new, key=lambda e: (e["effective_at"], e["sequence"])):
                    state.apply(event)
            except Exception:
                store.state_cache.pop(portfolio, None)  # partly applied: never reuse it
                raise
            store.state_cache[portfolio] = (max(e["sequence"] for e in new),
                                            max(latest, max(e["effective_at"] for e in new)), state)
            return state
    events = store.events(portfolio, kinds=ECONOMIC)
    state = replay(events)
    if events:
        store.state_cache[portfolio] = (max(e["sequence"] for e in events), max(e["effective_at"] for e in events), state)
    return state


def combine_states(states: list[State]) -> State:
    """Sum independently replayed owners; never net FIFO lots across cash pools."""
    result = State()
    for state in states:
        for name in ("cash", "contributed", "realized", "fees", "rewards", "prepaid", "notional", "fills", "settlements"):
            setattr(result, name, getattr(result, name) + getattr(state, name))
        result.lots.update(state.lots)
        result.reservations.update(state.reservations)
        result.markets.update(state.markets)
        for venue, cash in state.venue_cash.items():
            result.venue_cash[venue] = result.venue_cash.get(venue, ZERO) + cash
    return result
