"""Sum check that retries the missing legs of a partial basket. Paper only: nothing here can send an order.

Everything up to execution is the kalshi_sum_check paper trader (../kalshi_sum_check/paper_ws.py).
When the legs fill unequal quantities, each short leg is bought again up to the
intended number of sets with IOC orders, as long as the completed basket still pays:
  * the budget is the basket's payout (sets x $1 long, sets x $(n-1) short) plus
    --retry-max-loss, less what the filled contracts cost with fees;
  * the budget left after estimated fees is shared across the missing contracts, so
    each leg's limit is its intended price plus an equal share of the remaining slack;
  * up to --retry-attempts tries, --retry-delay seconds apart, each on a fresh book.
If no price inside the budget fills, the basket stays partial and settles as it is.

Usage
  uv run python retry.py --feed rest
"""

from __future__ import annotations

import asyncio
import math
import os
import sys
import uuid
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "kalshi_sum_check"))

from paper_ws import Config, Engine, Quote, leg_cost, log, main as run_main  # noqa: E402
from pricing import taker_fee  # noqa: E402

TICK = 0.01


@dataclass
class RetryConfig(Config):
    retry_max_loss: float = 0.0
    retry_attempts: int = 3
    retry_delay: float = 1.0


def retry_orders(q: Quote, orders: list, got: list[float], paid: list[float],
                 fee_type: str, fee_mult: float, max_loss: float) -> list[dict]:
    """IOC orders for each leg's missing contracts, priced so the full basket loses at most max_loss.

    `got` and `paid` are each leg's filled contracts and dollars paid so far.
    Prices are per contract in the contract bought: YES for a long basket, NO for a short one.
    """
    payout = q.payout  # sets x $1 long, sets x $(n-1) short
    missing = [max(0, int(round(q.sets - g))) for g in got]
    total_missing = sum(missing)
    if not total_missing:
        return []
    intended = [leg["vwap"] for leg in q.legs]
    fees = [taker_fee(n * c * (1 - c), fee_type, fee_mult) for n, c in zip(missing, intended)]
    slack = (payout + max_loss - sum(paid) - sum(fees)
             - sum(n * c for n, c in zip(missing, intended))) / total_missing
    if slack < 0:
        return []  # even at the intended prices the completed basket would lose more than allowed
    out = []
    for order, n, c in zip(orders, missing, intended):
        if n <= 0:
            continue
        ceiling = min(1 - TICK, c + slack)  # contract price, rounded to the cent that keeps it inside the budget
        if order["side"] == "bid":  # YES at most the ceiling
            yes = math.floor(ceiling * 100 + 1e-9) / 100
        else:  # NO through a YES ask: YES price at least 1 - ceiling
            yes = math.ceil((1 - ceiling) * 100 - 1e-9) / 100
        out.append({**{k: order[k] for k in ("ticker", "side", "time_in_force", "self_trade_prevention_type")},
                    "count": str(n), "price": f"{yes:.2f}"})
    return out


class RetryEngine(Engine):
    strategy = "kalshi_sum_check_retry"
    config_cls = RetryConfig
    cfg: RetryConfig

    async def repair(self, q: Quote, context: dict, orders: list, results: list):
        u = self.universe.get(q.event, {})
        by_ticker = {o["ticker"]: o for o in orders}
        got, paid = {}, {}
        for order, result in zip(orders, results):
            got[order["ticker"]], paid[order["ticker"]] = leg_cost(order, result)
        tickers = [o["ticker"] for o in orders]
        bought = dict.fromkeys(tickers, 0.0)
        stopped = "attempts"
        for attempt in range(self.cfg.retry_attempts):
            pending = retry_orders(q, orders, [got[t] for t in tickers], [paid[t] for t in tickers],
                                   u.get("fee_type", "quadratic"), u.get("fee_mult", 1.0), self.cfg.retry_max_loss)
            if not pending:
                stopped = "complete" if all(got[t] >= q.sets - 1e-9 for t in tickers) else "budget"
                break
            if attempt:
                await asyncio.sleep(self.cfg.retry_delay)
            for order in pending:
                order["client_order_id"] = str(uuid.uuid4())
            outcomes = await self.submit_legs(pending, {**context, "repair": "retry", "attempt": attempt + 1})
            for order, result in zip(pending, outcomes):
                n, cost = leg_cost(by_ticker[order["ticker"]], result)
                got[order["ticker"]] += n
                paid[order["ticker"]] += cost
                bought[order["ticker"]] += n
        complete = all(abs(got[t] - q.sets) < 1e-9 for t in tickers)
        self.broker.observe("basket_retry", {"context": context, "complete": complete,
            "stopped": "complete" if complete else stopped,
            "legs": [{"ticker": t, "filled": got[t], "retried": bought[t], "paid": round(paid[t], 4)} for t in tickers]})
        log.info("RETRY %s complete=%s bought %s", q.event, complete, bought)


def add_arguments(p, d):
    p.add_argument("--retry-max-loss", dest="retry_max_loss", type=float, default=d.retry_max_loss,
                   help="dollars the completed basket may lose to get hedged (0 = break-even)")
    p.add_argument("--retry-attempts", dest="retry_attempts", type=int, default=d.retry_attempts,
                   help="tries to buy the missing legs (3)")
    p.add_argument("--retry-delay", dest="retry_delay", type=float, default=d.retry_delay,
                   help="seconds between tries (1)")


if __name__ == "__main__":
    run_main(engine_cls=RetryEngine, add_arguments=add_arguments)
