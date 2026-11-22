"""Paper bets at a sportsbook's quoted odds, and routing a basket across venues."""

from __future__ import annotations

import time
from datetime import datetime
from decimal import Decimal
from typing import Any

from .broker import Broker
from .draftkings import DraftKings, OutOfCredits, is_sportsbook, parse_ticker
from .store import Store, decimal, utc


class SportsbookPaper:
    """A bet is accepted whole at the current odds or not at all.

    After `latency` the selection is read fresh. The bet is declined if the
    selection is gone, suspended, in play (unless allowed), last updated by the
    source more than `max_age` seconds ago, refused for lack of odds credits, or now costs more than the limit price (the odds shortened).
    Better odds are accepted at the new price, like a sportsbook's "accept better
    odds" setting. Each selection takes at most `max_stake` per portfolio, a stand-in
    for the book's unpublished account limits. The margin is in the price; the fee is 0.
    """

    policy = "sportsbook-fixed-odds-v1"

    def __init__(self, client: DraftKings, latency: float = 1.0, max_stake: str | None = None,
                 max_age: float = 1800.0, allow_in_play: bool = False):
        self.client, self.latency, self.max_age, self.allow_in_play = client, latency, max_age, allow_in_play
        self.max_stake = decimal(max_stake) if max_stake is not None else client.max_stake

    def create_order(self, order: dict[str, Any], broker: Broker) -> dict[str, Any]:
        identifier, ticker = order["client_order_id"], order["ticker"]
        if (not is_sportsbook(ticker) or order["side"] != "bid" or order.get("time_in_force") != "fill_or_kill"
                or order.get("post_only") or order.get("reduce_only")):
            broker.finish(identifier, "rejected")
            raise NotImplementedError("sportsbook paper accepts fill-or-kill backs (side bid) only; a bet cannot be sold")
        time.sleep(self.latency)
        try:
            selection, unavailable = self.client.selection(ticker, fresh=True), None
        except OutOfCredits:
            selection, unavailable = None, "no_credits"
        observed = utc()
        market, book = self.client.market(ticker, selection), self.client.book(selection)
        store = broker.store
        store.append("market", {"ticker": ticker, "market": market, "book": book}, source=None, at=observed)
        limit, quantity = decimal(order["price"]), decimal(order["count"])
        reason = None
        if unavailable:
            reason = unavailable
        elif selection is None:
            reason = "not_offered"
        elif selection.suspended:
            reason = "suspended"
        elif selection.started() and not self.allow_in_play:
            reason = "in_play"
        elif time.time() - _epoch(selection.updated_at) > self.max_age:
            reason = "stale_odds"
        elif selection.price > limit:
            reason = "odds_shortened"
        evidence = {"policy": self.policy, "observed_at": observed, "market": market, "book": book,
                    "raw": selection.raw if selection else None, "latency_seconds": self.latency}
        with store.transaction():
            if reason is None:
                assert selection is not None
                price = selection.price
                stake = quantity * price
                held = sum((lot["cost"] for lot in broker.state().lots.values() if lot["market"] == ticker), Decimal(0))
                if held + stake > self.max_stake:
                    reason = "stake_limit"
            if reason is not None:
                broker.observe("sportsbook_declined", {"client_order_id": identifier, "ticker": ticker,
                                                       "reason": reason, "limit": str(limit), "market": market})
                broker.finish(identifier, "canceled")
                return {"client_order_id": identifier, "status": "canceled", "filled": "0", "reason": reason}
            other_reservations = store.reserved(broker.portfolio) - decimal(broker._order(identifier)["reserved"])
            if broker.state().cash - stake < other_reservations:
                broker.finish(identifier, "rejected")
                return {"client_order_id": identifier, "status": "rejected", "filled": "0"}
            broker.fill(identifier, str(quantity), str(price), "0", f"paper:{identifier}:0", evidence)
            broker.finish(identifier, "filled")
        return {"client_order_id": identifier, "status": "filled", "filled": str(quantity), "fee": "0",
                "price": str(price), "odds": evidence["market"].get("odds"), "evidence": evidence}


class Route:
    """Paper adapters by venue, so one broker and one portfolio's cash serve a basket
    whose legs are on Kalshi and on a sportsbook or Polymarket."""

    def __init__(self, kalshi: Any, sportsbook: SportsbookPaper | None = None, polymarket: Any = None,
                 polymarket_us: Any = None):
        self.kalshi, self.sportsbook, self.polymarket = kalshi, sportsbook, polymarket
        self.polymarket_us = polymarket_us
        self.policy = "+".join(a.policy for a in (kalshi, sportsbook, polymarket, polymarket_us) if a is not None)

    def create_order(self, order: dict[str, Any], broker: Broker) -> dict[str, Any]:
        from .polymarket import is_polymarket
        from .polymarket_us import is_polymarket_us
        ticker = order["ticker"]
        adapter = (self.sportsbook if is_sportsbook(ticker) else
                   self.polymarket_us if is_polymarket_us(ticker) else
                   self.polymarket if is_polymarket(ticker) else self.kalshi)
        if adapter is None:
            raise ValueError(f"no paper adapter for {ticker}")
        return adapter.create_order(order, broker)


def operator_settle(store: Store, ticker: str, result: str, note: str) -> int:
    """Settle every paper lot on a sportsbook selection from an operator's reading of
    the result: yes (it won), no (it lost) or void (stakes returned). Returns lots settled."""
    parse_ticker(ticker)
    if result not in ("yes", "no", "void") or not note.strip():
        raise ValueError("result must be yes, no or void, with a note citing the evidence")
    observed = utc()
    market = {"ticker": ticker, "venue": "draftkings", "status": "finalized", "void": result == "void",
              "settlement_value_dollars": None if result == "void" else ("1" if result == "yes" else "0"),
              "result": {"source": "operator", "note": note}}
    store.append("market", {"ticker": ticker, "market": market, "book": {}}, at=observed)
    return settle_lots(store, ticker, market, {"market": market, "observed_at": observed}, "sportsbook-operator")


def settle_lots(store: Store, ticker: str, market: dict[str, Any], evidence: dict[str, Any], run_id: str) -> int:
    """Pay every paper lot on a finalized selection. A void returns each lot's stake."""
    settled = 0
    for p in store.portfolios():
        if p["mode"] != "paper":
            continue
        broker = Broker(store, p["id"], run_id=run_id)
        for lot_id, lot in list(broker.state().lots.items()):
            if lot["market"] != ticker:
                continue
            if lot["side"] != "yes":
                raise ValueError(f"lot {lot_id} on {ticker} is not a YES position")
            value = lot["price"] if market["void"] else market["settlement_value_dollars"]
            broker.settle(lot_id, str(value), evidence)
            settled += 1
    return settled


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()
