"""Per-order IOC/FOK simulation against fresh books; no coordinated-fill guarantee."""

from __future__ import annotations

import time
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from typing import Any

from .broker import Broker
from .store import decimal, utc


from .book import levels


class Paper:
    policy = "ioc-depth-v1"

    def __init__(self, client: Any, latency: float = 0.3):
        self.client, self.latency = client, latency
        self.series: dict[str, Any] = {}

    def create_order(self, order: dict[str, Any], broker: Broker) -> dict[str, Any]:
        tif = order.get("time_in_force", "immediate_or_cancel")
        if tif not in ("immediate_or_cancel", "fill_or_kill") or order.get("post_only"):
            broker.finish(order["client_order_id"], "rejected")
            raise NotImplementedError("paper policy supports taker IOC/FOK orders only")
        time.sleep(self.latency)
        ticker, identifier = order["ticker"], order["client_order_id"]
        raw, market = self.client.book(ticker), self.client.market(ticker)
        observed = utc()
        if market.get("status") not in ("active", "open"):
            broker.finish(identifier, "canceled")
            return {"client_order_id": identifier, "status": "canceled", "filled": "0"}
        series_id = market.get("series_ticker") or market.get("event_ticker", "").split("-")[0]
        if series_id not in self.series:
            self.series[series_id] = self.client.get(f"/series/{series_id}").get("series", {})
        schedule = self.series[series_id]
        book = levels(raw)
        source_side = "no" if order["side"] == "bid" else "yes"
        limit, requested = decimal(order["price"]), decimal(order["count"])
        now = time.time()
        store = broker.store
        with store.transaction():
            store.db.execute("""CREATE TABLE IF NOT EXISTS consumed (
                portfolio TEXT, market TEXT, side TEXT, price TEXT, quantity TEXT, absent_since REAL,
                PRIMARY KEY(portfolio,market,side,price))""")
            update_consumed(store, ticker, book, now)
            consumed = {decimal(r[0]): decimal(r[1]) for r in store.db.execute(
                "SELECT price,quantity FROM consumed WHERE portfolio=? AND market=? AND side=?",
                (broker.portfolio, ticker, source_side))}
            fills, left = [], requested
            for native_price, depth in book[source_side]:
                yes_price = 1 - native_price if source_side == "no" else native_price
                if (order["side"] == "bid" and yes_price > limit) or (order["side"] == "ask" and yes_price < limit):
                    continue
                qty = min(left, max(Decimal(0), depth - consumed.get(native_price, Decimal(0))))
                if qty > 0:
                    fills.append((native_price, yes_price, qty))
                    left -= qty
                if not left:
                    break
            quantity = requested - left
            if tif == "fill_or_kill" and left:
                quantity, fills = Decimal(0), []
            if order.get("reduce_only"):
                closing = "no" if order["side"] == "bid" else "yes"
                held = sum((lot["quantity"] for lot in broker.state().lots.values()
                            if lot["market"] == ticker and lot["side"] == closing), Decimal(0))
                if quantity > held:
                    broker.finish(identifier, "rejected")
                    return {"client_order_id": identifier, "status": "rejected", "filled": "0"}
            fee_type = schedule.get("fee_type", "quadratic")
            if fee_type not in ("quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"):
                raise ValueError(f"unsupported paper fee type: {fee_type}")
            multiplier = decimal(schedule.get("fee_multiplier", "1"))
            base = sum((q * p * (1 - p) for _, p, q in fills), Decimal(0))
            fee = (Decimal("0.07") * multiplier * base).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
            evidence = {"policy": self.policy, "observed_at": observed, "book": raw,
                        "fee_schedule": schedule, "levels": [[str(p), str(q)] for _, p, q in fills],
                        "latency_seconds": self.latency}
            store.append("market", {"ticker": ticker, "market": market, "book": raw}, source=None, at=observed)
            if quantity:
                average = sum((p * q for _, p, q in fills), Decimal(0)) / quantity
                # Check exact cash effects including closing opposing positions and fees.
                state = broker.state()
                closing = "no" if order["side"] == "bid" else "yes"
                held = sum((lot["quantity"] for lot in state.lots.values()
                            if lot["market"] == ticker and lot["side"] == closing), Decimal(0))
                closed = min(quantity, held)
                opening_price = average if order["side"] == "bid" else 1 - average
                cash_change = closed * (1 - opening_price) - (quantity - closed) * opening_price - fee
                other_reservations = store.reserved(broker.portfolio) - decimal(broker._order(identifier)["reserved"])
                if state.cash + cash_change < other_reservations:
                    broker.finish(identifier, "rejected")
                    return {"client_order_id": identifier, "status": "rejected", "filled": "0"}
                remaining_fee = fee
                for index, (_, price, qty) in enumerate(fills):
                    allocated_fee = (fee * qty / quantity).quantize(Decimal("0.0001"), rounding=ROUND_DOWN) if index < len(fills)-1 else remaining_fee
                    remaining_fee -= allocated_fee
                    broker.fill(identifier, str(qty), str(price), str(allocated_fee),
                                f"paper:{identifier}:{index}", evidence)
                for native_price, _, qty in fills:
                    current = consumed.get(native_price, Decimal(0)) + qty
                    store.db.execute("INSERT OR REPLACE INTO consumed VALUES(?,?,?,?,?,NULL)",
                                     (broker.portfolio, ticker, source_side, str(native_price), str(current)))
            broker.finish(identifier, "filled" if quantity == requested else "canceled")
            return {"client_order_id": identifier, "status": "filled" if quantity == requested else "canceled",
                    "filled": str(quantity), "fee": str(fee), "evidence": evidence}


def update_consumed(store: Any, ticker: str, book: dict[str, Any], now: float) -> None:
    exists = store.db.execute("SELECT 1 FROM sqlite_master WHERE name='consumed'").fetchone()
    if not exists:
        return
    for row in store.db.execute("SELECT * FROM consumed WHERE market=?", (ticker,)).fetchall():
        present = decimal(row["price"]) in {p for p, _ in book[row["side"]]}
        key = (row["portfolio"], ticker, row["side"], row["price"])
        if present:
            store.db.execute("UPDATE consumed SET absent_since=NULL WHERE portfolio=? AND market=? AND side=? AND price=?", key)
        elif row["absent_since"] is None:
            store.db.execute("UPDATE consumed SET absent_since=? WHERE portfolio=? AND market=? AND side=? AND price=?", (now, *key))
        elif now - row["absent_since"] >= 300:
            store.db.execute("DELETE FROM consumed WHERE portfolio=? AND market=? AND side=? AND price=?", key)
