"""Thin wrapper around Kalshi V2 orders plus durable context and accounting."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal, ROUND_CEILING
from typing import Any

import httpx

from .accounting import State, portfolio_state
from .store import Store, decimal, encode


class Broker:
    def __init__(self, store: Store, portfolio: str, adapter: Any = None,
                 run_id: str | None = None):
        self.store, self.portfolio, self.adapter = store, portfolio, adapter
        self.run_id = run_id or uuid.uuid4().hex
        rows = [p for p in store.portfolios() if p["id"] == portfolio]
        if not rows:
            raise ValueError("create the portfolio explicitly before opening its broker")
        self.mode, self.metadata = rows[0]["mode"], rows[0]["metadata"]

    def state(self) -> State:
        return portfolio_state(self.store, self.portfolio)

    def _reservation(self, order: dict[str, Any]) -> Decimal:
        """Cash to hold for an order: the cost of the contracts it opens (net of any
        opposite position it closes) plus a conservative fee allowance."""
        quantity, price = decimal(order["count"]), decimal(order["price"])
        closing_side = "no" if order["side"] == "bid" else "yes"
        held = sum((lot["quantity"] for lot in self.state().lots.values()
                    if lot["market"] == order["ticker"] and lot["side"] == closing_side), Decimal(0))
        opening = max(Decimal(0), quantity - held)
        cost = opening * (price if order["side"] == "bid" else 1 - price)
        # Paper's exact fee is checked before execution. Reserve a conservative default fee.
        return cost + (quantity * Decimal("0.0175")).quantize(Decimal("0.01"), rounding=ROUND_CEILING)

    @property
    def available(self) -> Decimal:
        return self.state().cash - self.store.reserved(self.portfolio)

    def observe(self, kind: str, data: dict[str, Any]) -> str:
        return self.store.append("observation", {"kind": kind, **data}, self.portfolio, self.run_id)

    def start(self, strategy: str, revision: str, config: dict[str, Any]) -> str:
        return self.store.append("run", {"status": "started", "strategy": strategy,
            "revision": revision, "config": config}, self.portfolio, self.run_id)

    def submit(self, order: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.adapter is None:
            raise ValueError("execution adapter required")
        order, context = dict(order), json.loads(encode(context or {}))
        if order["side"] not in ("bid", "ask"):
            raise ValueError("V2 side must be bid or ask (YES book)")
        quantity, price = decimal(order["count"]), decimal(order["price"])
        if quantity <= 0 or not 0 <= price <= 1:
            raise ValueError("invalid quantity or price")
        identifier = order.setdefault("client_order_id", str(uuid.uuid4()))
        if self.mode == "live":
            subaccount = self.metadata.get("subaccount")
            if subaccount is None or order.get("subaccount", subaccount) != subaccount:
                raise ValueError("live order must use the mapped subaccount")
            order["subaccount"] = subaccount
            exchange = self.metadata.get("exchange_index", 0)
            if order.get("exchange_index", exchange) != exchange:
                raise ValueError("cannot change portfolio exchange")
            order["exchange_index"] = exchange
        with self.store.transaction():
            prior = self.store.db.execute("SELECT * FROM orders WHERE id=?", (identifier,)).fetchone()
            if prior:
                if prior["portfolio_id"] != self.portfolio or prior["request"] != encode(order) or prior["context"] != encode(context):
                    raise ValueError("client order ID reused for a different request")
                return dict(prior)  # Never resubmit an uncertain write implicitly.
            reserve = self._reservation(order)
            if self.mode == "paper" and reserve > self.available:
                self.observe("order_rejected", {"reason": "insufficient_cash", "order": order, "context": context})
                return {"client_order_id": identifier, "status": "rejected", "filled": "0"}
            self.store.db.execute("INSERT INTO orders(id,portfolio_id,run_id,request,context,status,reserved) VALUES(?,?,?,?,?,?,?)",
                                  (identifier, self.portfolio, self.run_id, encode(order), encode(context), "pending", str(reserve)))
            self.store.append("order", {"status": "intent", "order": order, "context": context}, self.portfolio, self.run_id)
        try:
            if self.mode == "paper":
                return self.adapter.create_order(order, self)
            response = self.adapter.create_order(order)
        except httpx.HTTPStatusError as exc:
            if self.mode == "paper":
                self.finish(identifier, "failed")
            elif 400 <= exc.response.status_code < 500 and exc.response.status_code not in (408, 409, 429):
                self.finish(identifier, "rejected")
            else:
                self.finish(identifier, "unknown", release=False)
            raise
        except Exception:
            self.finish(identifier, "failed" if self.mode == "paper" else "unknown", release=self.mode == "paper")
            raise
        with self.store.transaction():
            self.store.db.execute("UPDATE orders SET venue_id=?,status='acknowledged' WHERE id=?",
                                  (response.get("order", response).get("order_id"), identifier))
            self.store.append("order", {"status": "acknowledged", "client_order_id": identifier,
                "response": response}, self.portfolio, self.run_id)
        return response

    def finish(self, identifier: str, status: str, release: bool = True) -> None:
        with self.store.transaction():
            self.store.db.execute("UPDATE orders SET status=?,reserved=CASE WHEN ? THEN '0' ELSE reserved END WHERE id=? AND portfolio_id=?",
                                  (status, release, identifier, self.portfolio))
            self.store.append("order", {"client_order_id": identifier, "status": status}, self.portfolio, self.run_id)

    def cancel(self, identifier: str) -> dict[str, Any]:
        row = self._order(identifier)
        if self.mode == "paper":
            self.finish(identifier, "canceled")
            return {"status": "canceled"}
        self._require_venue_id(row)
        response = self.adapter.cancel_order(row["venue_id"], {"subaccount": self.metadata["subaccount"]})
        self.observe("cancel_acknowledged", {"client_order_id": identifier, "response": response})
        # Account monitor releases reservations after fills and terminal status reconcile.
        return response

    def amend(self, identifier: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Amend a resting live order and resize its cash reservation.

        Before the venue call the reservation is raised to cover the larger of the old
        and new order, so a lost acknowledgement never leaves the order under-reserved.
        After the acknowledgement it is set to the amended order's own requirement.
        """
        if self.mode == "paper":
            raise NotImplementedError("IOC paper orders do not rest; submit a new order")
        if changes.get("subaccount", self.metadata["subaccount"]) != self.metadata["subaccount"]:
            raise ValueError("cannot change portfolio ownership")
        if any(k in changes for k in ("ticker", "side", "client_order_id")):
            raise ValueError("amend can change count and price only; submit a new order instead")
        request = {**changes, "subaccount": self.metadata["subaccount"]}
        with self.store.transaction():
            row = self._order(identifier)
            self._require_venue_id(row)
            if row["status"] not in ("acknowledged", "pending"):
                raise ValueError(f"cannot amend an order that is {row['status']}")
            amended = {**json.loads(row["request"]), **changes}
            quantity, price = decimal(amended["count"]), decimal(amended["price"])
            if quantity <= 0 or not 0 <= price <= 1 or quantity < decimal(row["filled"]):
                raise ValueError("invalid amended quantity or price")
            # Like submit, live cash sufficiency is the exchange's call; the ledger only
            # keeps the reservation honest so `available` never overstates free cash.
            old, new = decimal(row["reserved"]), self._reservation(amended)
            held = max(old, new)
            self.store.db.execute("UPDATE orders SET reserved=? WHERE id=?", (str(held), identifier))
            self.observe("amend_intent", {"client_order_id": identifier, "changes": request,
                                          "reserved_before": str(old), "reserved_during": str(held)})
        result = self.adapter.amend_order(row["venue_id"], request)
        with self.store.transaction():
            self.store.db.execute("UPDATE orders SET request=?,reserved=? WHERE id=?",
                                  (encode(amended), str(new), identifier))
            self.observe("amend_acknowledged", {"client_order_id": identifier, "response": result,
                                                "reserved": str(new)})
        return result

    @staticmethod
    def _require_venue_id(row: dict[str, Any]) -> None:
        if not row.get("venue_id"):
            raise ValueError("the venue order ID is unknown (acknowledgement not received); "
                             "wait for the account monitor to recover it")

    def _order(self, identifier: str) -> dict[str, Any]:
        row = self.store.db.execute("SELECT * FROM orders WHERE id=? AND portfolio_id=?", (identifier, self.portfolio)).fetchone()
        if not row:
            raise ValueError("order does not belong to this portfolio")
        return dict(row)

    def fill(self, identifier: str, quantity: str, yes_price: str, fee: str,
             source: str, evidence: dict[str, Any], at: str | None = None) -> None:
        """Normalize a YES-book fill into closing and opening contract transactions."""
        with self.store.transaction():
            receipt = {"source": source, "order_id": identifier, "quantity": str(decimal(quantity)),
                       "yes_price": str(decimal(yes_price)), "fee": str(decimal(fee)), "evidence": evidence}
            prior = self.store.db.execute("SELECT data FROM events WHERE source_key=?", (source + ":receipt",)).fetchone()
            if prior:
                if json.loads(prior[0]) != receipt:
                    raise ValueError("conflicting fill receipt")
                return
            row = self._order(identifier)
            order, context = json.loads(row["request"]), json.loads(row["context"])
            qty, price, charge = decimal(quantity), decimal(yes_price), decimal(fee)
            if qty <= 0 or not 0 <= price <= 1 or decimal(row["filled"]) + qty > decimal(order["count"]):
                raise ValueError("invalid fill")
            closing = "no" if order["side"] == "bid" else "yes"
            held = sum((lot["quantity"] for lot in self.state().lots.values()
                        if lot["market"] == order["ticker"] and lot["side"] == closing), Decimal(0))
            close_qty = min(qty, held)
            pieces = [(closing, -close_qty), ("yes" if closing == "no" else "no", qty - close_qty)]
            for side, delta in pieces:
                if delta == 0:
                    continue
                contract_price = price if side == "yes" else 1 - price
                allocated_fee = charge * abs(delta) / qty
                cash = -delta * contract_price - allocated_fee
                self.store.append("transaction", {"reason": "fill", "cash_delta": str(cash), "fee": str(allocated_fee),
                    "order_id": identifier, "context": context, "evidence": evidence,
                    "legs": [{"market": order["ticker"], "side": side, "quantity_delta": str(delta), "price": str(contract_price)}]},
                    self.portfolio, self.run_id, source + ":" + side, at)
            self.store.append("execution", receipt, self.portfolio, self.run_id, source + ":receipt", at)
            total = decimal(row["filled"]) + qty
            self.store.db.execute("UPDATE orders SET filled=? WHERE id=?", (str(total), identifier))

    def settle(self, lot_id: str, yes_value: str, evidence: dict[str, Any]) -> None:
        if self.mode != "paper":
            raise ValueError("live settlements require authoritative account cash evidence")
        with self.store.transaction():
            lot = self.state().lots.get(lot_id)
            if lot is None:
                return
            value = decimal(yes_value)
            if not 0 <= value <= 1:
                raise ValueError("invalid payout value")
            price = value if lot["side"] == "yes" else 1 - value
            self.store.append("transaction", {"reason": "settlement", "cash_delta": str(lot["quantity"] * price), "fee": "0",
                "context": lot["context"], "evidence": evidence,
                "legs": [{"market": lot["market"], "side": lot["side"], "quantity_delta": str(-lot["quantity"]),
                          "price": str(price), "lot_id": lot_id}]}, self.portfolio, self.run_id,
                f"paper-settlement:{self.portfolio}:{lot_id}")

    def correct(self, targets: list[str], reason: str) -> str:
        if not reason:
            raise ValueError("correction reason required")
        with self.store.transaction():
            known = {e["record_id"]: e for e in self.store.events(self.portfolio, kinds=("transaction", "correction"))}
            if not targets or not set(targets) <= known.keys():
                raise ValueError("correction targets must belong to this portfolio")
            if any(known[t]["type"] != "transaction" for t in targets):
                raise ValueError("corrections target economic transactions")
            identifier = self.store.append("correction", {"targets": targets, "reason": reason}, self.portfolio, self.run_id,
                                           at=min(known[t]["effective_at"] for t in targets))
            # A corrected fill no longer counts toward its order, so the genuine fill can
            # still be booked and the order's overfill check stays accurate.
            removed: dict[str, Decimal] = {}
            for target in targets:
                data = known[target]["data"]
                if data.get("reason") == "fill" and data.get("order_id"):
                    qty = sum((abs(decimal(leg["quantity_delta"])) for leg in data.get("legs", [])), Decimal(0))
                    removed[data["order_id"]] = removed.get(data["order_id"], Decimal(0)) + qty
            for order_id, qty in removed.items():
                row = self._order(order_id)
                self.store.db.execute("UPDATE orders SET filled=? WHERE id=?",
                                      (str(max(Decimal(0), decimal(row["filled"]) - qty)), order_id))
            self.state()  # Roll back if dependent inventory events would become invalid.
            return identifier
