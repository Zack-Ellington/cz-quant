"""One venue's execution boundary, with durable ownership, context and accounting."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal, ROUND_CEILING
from typing import Any

import httpx

from .accounting import State, portfolio_state, venue_of
from .store import Store, decimal, encode


PRIMARY = 0  # the account itself; Kalshi's default when no subaccount is named
# Fee allowance reserved per contract until the exact fee is known: Kalshi's taker fee
# peaks at 1.75 cents (0.07 x 0.25); Polymarket US's at 2.375 cents (0.095 x 0.25).
FEE_ALLOWANCE = {"kalshi": Decimal("0.0175"), "polymarket_us": Decimal("0.025")}


def execution_summary(fills: list[tuple[Decimal, Decimal, Decimal]]) -> dict[str, str]:
    """Quantity, YES notional, fees and price bounds from authoritative fills."""
    return {"quantity": str(sum((q for _, q, _ in fills), Decimal(0))),
            "yes_notional": str(sum((p * q for p, q, _ in fills), Decimal(0))),
            "fee": str(sum((f for _, _, f in fills), Decimal(0))),
            "yes_price_min": str(min(p for p, _, _ in fills)),
            "yes_price_max": str(max(p for p, _, _ in fills))}


def scope(metadata: dict[str, Any]) -> dict[str, Any]:
    """Request parameters naming a live portfolio's account. The primary account
    (mapped with --primary-account) is named by omission: no subaccount field."""
    sub = metadata["subaccount"]
    return {} if sub == PRIMARY else {"subaccount": sub}


class Broker:
    def __init__(self, store: Store, pool_id: str, adapter: Any = None,
                 run_id: str | None = None, *, venue: str | None = None):
        self.store, self.pool_id, self.adapter = store, pool_id, adapter
        pool = next((p for p in store.pools() if p["id"] == pool_id), None)
        if pool is None:
            raise ValueError("select an explicit venue cash pool; legacy portfolios are report-only")
        self.venue = pool["venue"]
        if (venue is not None and venue != self.venue) or getattr(adapter, "venue", self.venue) != self.venue:
            raise ValueError("execution adapter belongs to a different cash-pool venue")
        self.run_id = run_id or uuid.uuid4().hex
        self.mode, self.metadata = pool["mode"], pool["metadata"]

    @property
    def portfolio(self) -> str:
        """Deprecated ownership spelling for older single-venue callers."""
        return self.pool_id

    def state(self) -> State:
        """Only this cash pool's cash and holdings, in paper and live mode."""
        return portfolio_state(self.store, self.pool_id)

    def _check_venue(self, ticker: str) -> None:
        if venue_of(ticker) != self.venue:
            raise ValueError(f"{self.venue} broker cannot execute or modify {ticker}")

    def _inventory(self, ticker: str, exclude: str | None = None) -> tuple[Decimal, Decimal, Decimal]:
        """Confirmed net inventory and still-possible buys/sells of the YES book.

        Booked fills replace confirmations rather than adding to them. Pending
        quantities are kept separately: they can reserve closes or open exposure.
        """
        acknowledgements = {e['data']['client_order_id']: e['data']
                            for e in self.store.events(self.pool_id, kinds=('order',))
                            if e['data'].get('status') == 'acknowledged'}
        net = sum((lot['quantity'] * (1 if lot['side'] == 'yes' else -1)
                   for lot in self.state().lots.values() if lot['market'] == ticker), Decimal(0))
        pending = {'bid': Decimal(0), 'ask': Decimal(0)}
        for row in self.store.db.execute('SELECT * FROM orders WHERE portfolio_id=?', (self.pool_id,)):
            other = json.loads(row['request'])
            if other['ticker'] != ticker:
                continue
            ack = acknowledgements.get(row['id'], {})
            confirmed = decimal(ack.get('confirmed_filled', '0'))
            booked = decimal(row['filled'])
            net += max(Decimal(0), confirmed - booked) * (1 if other['side'] == 'bid' else -1)
            if (row['id'] != exclude and row['status'] in ('pending', 'unknown', 'acknowledged')
                    and not ack.get('terminal')):
                pending[other['side']] += max(Decimal(0), decimal(other['count']) - max(booked, confirmed))
        return net, pending['bid'], pending['ask']

    def _closing_quantity(self, order: dict[str, Any]) -> Decimal:
        net, bids, asks = self._inventory(order['ticker'], order.get('client_order_id'))
        return max(Decimal(0), net - asks if order['side'] == 'ask' else -net - bids)

    def _netting_conflict(self, order: dict[str, Any]) -> str | None:
        from .accounts import netting_conflict
        if decimal(order['count']) <= self._closing_quantity(order):
            return None
        pool = {'id': self.pool_id, 'venue': self.venue, 'mode': self.mode, 'metadata': self.metadata}
        return netting_conflict(self.store, pool, order['ticker'], 'yes' if order['side'] == 'bid' else 'no')

    def _check_active(self, ticker: str) -> None:
        # Called under the write transaction: a long-running broker must observe
        # retirement performed by another process before it records another intent.
        self.metadata = next(p['metadata'] for p in self.store.pools() if p['id'] == self.pool_id)
        if self.metadata.get('retired_at'):
            raise ValueError(f'cash pool {self.pool_id} is retired')
        if self.metadata.get('reserve'):
            raise ValueError(f'{self.pool_id} is an account reserve: open a virtual subaccount to trade')
        from .accounts import market_in_scope
        if not market_in_scope(self.metadata.get('markets'), ticker):
            raise ValueError(f"{ticker} is outside cash pool {self.pool_id}'s markets {self.metadata['markets']}")

    def _reservation(self, order: dict[str, Any]) -> Decimal:
        """Reserve opening cost and fees, allowing closes of confirmed inventory."""
        quantity, price = decimal(order['count']), decimal(order['price'])
        held = self._closing_quantity(order)
        if order.get('reduce_only') and quantity > held:
            raise ValueError('reduce-only quantity exceeds unreserved holdings in this cash pool')
        opening = max(Decimal(0), quantity - held)
        opening_price = price if order['side'] == 'bid' else 1 - price
        cost = opening * opening_price
        # Paper's exact fee is checked before execution. Reserve a conservative default fee.
        allowance = FEE_ALLOWANCE.get(venue_of(order["ticker"]), FEE_ALLOWANCE["kalshi"])
        fees = (quantity * allowance).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        closing_proceeds = min(quantity, held) * (1 - opening_price)
        return max(Decimal(0), cost + fees - closing_proceeds)

    @property
    def available(self) -> Decimal:
        """Unreserved cash owned by this venue allocation, in either mode."""
        return self.state().cash - self.store.reserved(self.pool_id)

    @property
    def unresolved_orders(self) -> int:
        return self.store.db.execute(
            "SELECT COUNT(*) FROM orders WHERE portfolio_id=? AND status IN ('unknown','pending')",
            (self.pool_id,)).fetchone()[0]

    def orders(self, statuses: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        """This pool's broker-owned orders: client ID, status, booked fill and context.

        Read-only. Strategies use this instead of the private orders table.
        """
        rows = self.store.db.execute("SELECT id, status, filled, context FROM orders WHERE portfolio_id=?",
                                     (self.pool_id,)).fetchall()
        result = [{"client_order_id": r[0], "status": r[1], "filled": decimal(r[2] or "0"),
                   "context": json.loads(r[3] or "{}")} for r in rows]
        return [o for o in result if statuses is None or o["status"] in statuses]

    def observe(self, kind: str, data: dict[str, Any]) -> str:
        return self.store.append("observation", {"kind": kind, **data}, self.pool_id, self.run_id)

    def start(self, strategy: str, revision: str, config: dict[str, Any]) -> str:
        return self.store.append("run", {"status": "started", "strategy": strategy,
            "revision": revision, "config": config}, self.pool_id, self.run_id)

    def _validate_order(self, order: dict[str, Any]) -> None:
        if self.adapter is None:
            raise ValueError("execution adapter required")
        if getattr(self.adapter, "mode", self.mode) != self.mode:
            raise ValueError("execution adapter and cash pool must use the same mode")
        self._check_venue(order["ticker"])
        if order["side"] not in ("bid", "ask"):
            raise ValueError("V2 side must be bid or ask (YES book)")
        quantity, price = decimal(order["count"]), decimal(order["price"])
        if quantity <= 0 or not 0 <= price <= 1:
            raise ValueError("invalid quantity or price")
        self._check_active(order['ticker'])
        if self.mode == "live" and self.venue == "kalshi":
            subaccount = self.metadata.get("subaccount")
            if subaccount is None or order.get("subaccount", subaccount) != subaccount:
                raise ValueError("live order must use the mapped subaccount")
            if order.get("exchange_index", self.metadata.get("exchange_index", 0)) != self.metadata.get("exchange_index", 0):
                raise ValueError("cannot change cash-pool exchange")

    def _check_budget(self, order: dict[str, Any]) -> dict[str, Any]:
        reserve = self._reservation(order)
        other = self._netting_conflict(order)
        if other:
            return {"status": "rejected", "reason": "cross_pool_netting", "with": other, "reserved": str(reserve),
                    "detail": f"{other} holds or may open the opposite side of {order['ticker']} on this account"}
        if reserve > self.available:
            return {"status": "rejected", "reason": "insufficient_cash", "reserved": str(reserve)}
        return {"status": "ready", "reserved": str(reserve)}

    def check(self, order: dict[str, Any]) -> dict[str, Any]:
        """Validate an intended order without reserving cash or contacting the venue.

        Returns ready/rejected and required cash. A ready result is not a reservation;
        submit repeats these checks atomically when recording the intent.
        """
        with self.store.transaction():
            self._validate_order(order)
            return self._check_budget(order)

    def submit(self, order: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
        order, context = dict(order), json.loads(encode(context or {}))
        identifier = order.setdefault("client_order_id", str(uuid.uuid4()))
        with self.store.transaction():
            self._validate_order(order)
            if self.mode == "live" and self.venue == "kalshi":
                order.pop("subaccount", None)
                order.update(scope(self.metadata))
                order["exchange_index"] = self.metadata.get("exchange_index", 0)
            prior = self.store.db.execute("SELECT * FROM orders WHERE id=?", (identifier,)).fetchone()
            if prior:
                if prior["portfolio_id"] != self.pool_id or prior["request"] != encode(order) or prior["context"] != encode(context):
                    raise ValueError("client order ID reused for a different request")
                return dict(prior)  # Never resubmit an uncertain write implicitly.
            checked = self._check_budget(order)
            if checked["status"] == "rejected":
                self.observe("order_rejected", {**checked, "order": order, "context": context})
                return {"client_order_id": identifier, "status": "rejected", "filled": "0",
                        "reason": checked.get("detail", checked["reason"])}
            reserve = decimal(checked["reserved"])
            self.store.db.execute("INSERT INTO orders(id,portfolio_id,run_id,request,context,status,reserved) VALUES(?,?,?,?,?,?,?)",
                                  (identifier, self.pool_id, self.run_id, encode(order), encode(context), "pending", str(reserve)))
            self.store.append("order", {"status": "intent", "order": order, "context": context, "reserved": str(reserve)}, self.pool_id, self.run_id)
        try:
            if self.mode == "paper":
                return self.adapter.create_order(order, self)
            if getattr(self.adapter, "takes_broker", False):
                # The adapter records the venue ID, fills and status itself.
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
        body = response.get('order', response)
        self.acknowledge(identifier, body.get('order_id'), response,
                         body.get('fill_count_fp', body.get('fill_count')),
                         body.get('status') in ('executed', 'canceled'))
        return response

    def acknowledge(self, identifier: str, venue_id: str | None, response: dict[str, Any],
                    confirmed_filled: Any = None, terminal: bool = False) -> None:
        """Record confirmed inventory without guessing fill prices, fees or cash."""
        with self.store.transaction():
            row = self._order(identifier)
            data = {'status': 'acknowledged', 'client_order_id': identifier, 'response': response,
                    'terminal': terminal and confirmed_filled is not None}
            if confirmed_filled is not None:
                quantity = decimal(confirmed_filled)
                if not quantity.is_finite() or not 0 <= quantity <= decimal(json.loads(row['request'])['count']):
                    raise ValueError('invalid confirmed fill quantity')
                data['confirmed_filled'] = str(quantity)
            self.store.db.execute("UPDATE orders SET venue_id=?,status='acknowledged' WHERE id=?",
                                  (venue_id, identifier))
            self.store.append('order', data, self.pool_id, self.run_id)

    def finish(self, identifier: str, status: str, release: bool = True) -> None:
        with self.store.transaction():
            self._order(identifier)
            self.store.db.execute("UPDATE orders SET status=?,reserved=CASE WHEN ? THEN '0' ELSE reserved END WHERE id=? AND portfolio_id=?",
                                  (status, release, identifier, self.pool_id))
            self.store.append("order", {"client_order_id": identifier, "status": status, "release": release}, self.pool_id, self.run_id)

    def cancel(self, identifier: str) -> dict[str, Any]:
        row = self._order(identifier)
        if self.mode == "paper":
            self.finish(identifier, "canceled")
            return {"status": "canceled"}
        self._require_venue_id(row)
        if not callable(getattr(self.adapter, "cancel_order", None)):
            raise NotImplementedError(f"{self.venue} adapter does not support cancel")
        response = self.adapter.cancel_order(row["venue_id"], scope(self.metadata) if self.venue == "kalshi" else {})
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
        self._order(identifier)
        if self.venue != "kalshi" or not callable(getattr(self.adapter, "amend_order", None)):
            raise NotImplementedError(f"{self.venue} adapter does not support amend")
        if changes.get("subaccount", self.metadata["subaccount"]) != self.metadata["subaccount"]:
            raise ValueError("cannot change cash-pool ownership")
        if any(k in changes for k in ("ticker", "side", "client_order_id")):
            raise ValueError("amend can change count and price only; submit a new order instead")
        request = {**{k: v for k, v in changes.items() if k != "subaccount"}, **scope(self.metadata)}
        with self.store.transaction():
            row = self._order(identifier)
            self._require_venue_id(row)
            if row["status"] not in ("acknowledged", "pending"):
                raise ValueError(f"cannot amend an order that is {row['status']}")
            amended = {**json.loads(row["request"]), **changes}
            self._check_active(amended['ticker'])
            quantity, price = decimal(amended["count"]), decimal(amended["price"])
            if quantity <= 0 or not 0 <= price <= 1 or quantity < decimal(row["filled"]):
                raise ValueError("invalid amended quantity or price")
            remaining = quantity - decimal(row['filled'])
            old = decimal(row['reserved'])
            unfilled = {**amended, 'count': str(remaining)}
            new = self._reservation(unfilled) if remaining else Decimal(0)
            other = self._netting_conflict(unfilled) if remaining else None
            if other:
                raise ValueError(f"{other} holds or may open the opposite side of {amended['ticker']} on this account")
            held = max(old, new)
            if held - old > self.available:
                raise ValueError("insufficient cash in this pool for amendment")
            # Publish both possible quantities before the venue call. Other pools
            # must see increased exposure even if the amendment acknowledgement is
            # delayed or lost; a reduction is not effective until it is confirmed.
            pending = {**json.loads(row['request']), 'count': str(max(
                quantity, decimal(json.loads(row['request'])['count'])))}
            self.store.db.execute("UPDATE orders SET request=?,reserved=? WHERE id=?",
                                  (encode(pending), str(held), identifier))
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
        row = self.store.db.execute("SELECT * FROM orders WHERE id=? AND portfolio_id=?", (identifier, self.pool_id)).fetchone()
        if not row:
            raise ValueError("order does not belong to this cash pool")
        self._check_venue(json.loads(row["request"])["ticker"])
        return dict(row)

    def fill(self, identifier: str, quantity: str, yes_price: str, fee: str,
             source: str, evidence: dict[str, Any], at: str | None = None) -> None:
        """Normalize a YES-book fill into closing and opening contract transactions."""
        with self.store.transaction():
            row = self._order(identifier)
            receipt = {"source": source, "order_id": identifier, "quantity": str(decimal(quantity)),
                       "yes_price": str(decimal(yes_price)), "fee": str(decimal(fee)), "evidence": evidence}
            prior = self.store.db.execute("SELECT data FROM events WHERE source_key=?", (source + ":receipt",)).fetchone()
            if prior:
                if json.loads(prior[0]) != receipt:
                    raise ValueError("conflicting fill receipt")
                return
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
                self.store.append("transaction", {"reason": "fill", "venue": venue_of(order["ticker"]),
                    "cash_delta": str(cash), "fee": str(allocated_fee),
                    "order_id": identifier, "context": context, "evidence": evidence,
                    "legs": [{"market": order["ticker"], "side": side, "quantity_delta": str(delta), "price": str(contract_price)}]},
                    self.pool_id, self.run_id, source + ":" + side, at)
            self.store.append("execution", receipt, self.pool_id, self.run_id, source + ":receipt", at)
            total = decimal(row["filled"]) + qty
            self.store.db.execute("UPDATE orders SET filled=? WHERE id=?", (str(total), identifier))

    def settle(self, lot_id: str, yes_value: str, evidence: dict[str, Any]) -> None:
        if self.mode != "paper":
            raise ValueError("live settlements require authoritative account cash evidence")
        with self.store.transaction():
            lot = self.state().lots.get(lot_id)
            if lot is None:
                owner = self.store.db.execute('SELECT portfolio_id FROM events WHERE record_id=?', (lot_id,)).fetchone()
                if owner and owner['portfolio_id'] != self.pool_id:
                    raise ValueError('lot belongs to a different cash pool')
                return
            self._check_venue(lot["market"])
            value = decimal(yes_value)
            if not 0 <= value <= 1:
                raise ValueError("invalid payout value")
            price = value if lot["side"] == "yes" else 1 - value
            self.store.append("transaction", {"reason": "settlement", "cash_delta": str(lot["quantity"] * price), "fee": "0",
                "context": lot["context"], "evidence": evidence,
                "legs": [{"market": lot["market"], "side": lot["side"], "quantity_delta": str(-lot["quantity"]),
                          "price": str(price), "lot_id": lot_id}]}, self.pool_id, self.run_id,
                f"paper-settlement:{self.pool_id}:{lot_id}")

    def correct(self, targets: list[str], reason: str) -> str:
        if not reason:
            raise ValueError("correction reason required")
        with self.store.transaction():
            known = {e["record_id"]: e for e in self.store.events(self.pool_id, kinds=("transaction", "correction"))}
            if not targets or not set(targets) <= known.keys():
                raise ValueError("correction targets must belong to this cash pool")
            if any(known[t]["type"] != "transaction" for t in targets):
                raise ValueError("corrections target economic transactions")
            for target in targets:
                data = known[target]["data"]
                legs = data.get("legs", [])
                venue = venue_of(legs[0]["market"]) if legs else data.get("venue", "kalshi")
                if venue != self.venue:
                    raise ValueError("correction targets must belong to this venue")
            identifier = self.store.append("correction", {"targets": targets, "reason": reason}, self.pool_id, self.run_id,
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
