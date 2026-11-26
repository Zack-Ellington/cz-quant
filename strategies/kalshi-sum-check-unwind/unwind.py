"""Sum check that sells back a partial basket. Paper only: nothing here can send an order.

Everything up to execution is the kalshi_sum_check paper trader (../kalshi_sum_check/paper_ws.py).
When the legs fill unequal quantities, the complete sets (the smallest fill across legs)
are kept and every contract above that is sold back at once with reduce-only IOC orders:
  * a YES leg is sold no lower than its entry price minus --unwind-slippage;
  * a NO leg (bought through a YES ask) is closed by buying YES back no higher than
    its entry YES price plus --unwind-slippage;
  * up to --unwind-attempts tries, --unwind-delay seconds apart, each on a fresh book.
Whatever cannot be sold within that limit stays open and settles like any other lot.

Usage
  uv run python unwind.py --feed rest
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass


from paper_ws import Config, Engine, Quote, leg_cost, log, main as run_main

TICK = 0.01


@dataclass
class UnwindConfig(Config):
    unwind_slippage: float = 0.10
    unwind_attempts: int = 3
    unwind_delay: float = 1.0


def unwind_orders(orders: list, results: list, slippage: float) -> list[dict]:
    """Reduce-only IOC orders that leave every leg at the smallest fill."""
    filled = [leg_cost(order, result) for order, result in zip(orders, results)]
    keep = min(qty for qty, _ in filled)
    out = []
    for order, (qty, paid) in zip(orders, filled):
        excess = int(round(qty - keep))
        if excess <= 0:
            continue
        entry = paid / qty  # per contract, fee included: the price that would break even
        if order["side"] == "bid":  # held YES: sell it
            side, limit = "ask", max(TICK, entry - slippage)
        else:  # held NO: buy YES back; NO cost c means YES price 1 - c
            side, limit = "bid", min(1 - TICK, 1 - entry + slippage)
        out.append({"ticker": order["ticker"], "side": side, "count": str(excess),
                    "price": str(round(limit, 2)), "time_in_force": "immediate_or_cancel",
                    "self_trade_prevention_type": "taker_at_cross", "reduce_only": True})
    return out


class UnwindEngine(Engine):
    strategy = "kalshi_sum_check_unwind"
    config_cls = UnwindConfig
    cfg: UnwindConfig

    async def repair(self, q: Quote, context: dict, orders: list, results: list):
        pending = unwind_orders(orders, results, self.cfg.unwind_slippage)
        planned = {o["ticker"]: int(o["count"]) for o in pending}
        sold = dict.fromkeys(planned, 0)
        for attempt in range(self.cfg.unwind_attempts):
            if not pending:
                break
            if attempt:
                await asyncio.sleep(self.cfg.unwind_delay)
            for order in pending:
                order["client_order_id"] = str(uuid.uuid4())
            outcomes = await self.submit_legs(pending, {**context, "repair": "unwind", "attempt": attempt + 1})
            left = []
            for order, result in zip(pending, outcomes):
                got = int(float(result.get("filled") or 0))
                sold[order["ticker"]] += got
                if got < int(order["count"]):
                    left.append({**order, "count": str(int(order["count"]) - got)})
            pending = left
        self.broker.observe("basket_unwind", {"context": context, "complete": not pending,
            "legs": [{"ticker": t, "excess": n, "sold": sold[t]} for t, n in planned.items()]})
        log.info("UNWIND %s sold %s of %s", q.event, sold, planned)


def add_arguments(p, d):
    p.add_argument("--unwind-slippage", dest="unwind_slippage", type=float, default=d.unwind_slippage,
                   help="worst price per contract below break-even when selling back (0.10)")
    p.add_argument("--unwind-attempts", dest="unwind_attempts", type=int, default=d.unwind_attempts,
                   help="tries to sell back the unhedged legs (3)")
    p.add_argument("--unwind-delay", dest="unwind_delay", type=float, default=d.unwind_delay,
                   help="seconds between tries (1)")


if __name__ == "__main__":
    run_main(engine_cls=UnwindEngine, add_arguments=add_arguments)
