"""Conservative limit-price budgets for two-leg Kalshi/Polymarket US baskets."""
from decimal import Decimal, ROUND_CEILING
from typing import Any, Callable

D = Decimal


def fee_bound(leg: dict[str, Any], price: Decimal, quantity: int) -> Decimal:
    # Bound fees at every possible fill price below the limit, including mixed fills.
    p = min(price, D("0.5"))
    return (quantity * D(leg["fee_coefficient"]) * p * (1 - p)).quantize(D("0.01"), rounding=ROUND_CEILING)


def cost(leg: dict[str, Any], price: Decimal, quantity: int) -> Decimal:
    return quantity * price + fee_bound(leg, price, quantity)


def limit_for(leg: dict[str, Any], quantity: int, budget: Callable[[int], Decimal],
              fundable: Callable[[Decimal], bool], minimum: Decimal = D("0")) -> Decimal | None:
    """Highest valid tick funded at full size and safe for every integer partial fill."""
    tick = D(leg["tick"])
    if not tick.is_finite() or tick <= 0:
        raise ValueError("invalid market tick")
    low, high, found = 1, int((1 - tick) / tick), None
    while low <= high:
        mid = (low + high) // 2
        price = tick * mid
        safe = all(cost(leg, price, n) <= budget(n) for n in range(1, quantity + 1)) and fundable(price)
        if safe:
            found, low = price, mid + 1
        else:
            high = mid - 1
    return found if found is not None and found >= minimum else None


def split_limits(legs: list[dict[str, Any]], quantity: int, edge: Decimal,
                 fundable: Callable[[dict[str, Any], Decimal], bool]) -> list[dict[str, Any]] | None:
    """Split the remaining all-in allowance equally for every possible partial size."""
    originals = [D(leg["limit"]) for leg in legs]
    result = []
    for i, leg in enumerate(legs):
        def budget(n: int) -> Decimal:
            costs = [cost(l, p, n) for l, p in zip(legs, originals)]
            return costs[i] + (n * (1 - edge) - sum(costs)) / 2
        price = limit_for(leg, quantity, budget, lambda p: fundable(leg, p), originals[i])
        if price is None:
            return None
        result.append({**leg, "limit": str(price), "fee": str(fee_bound(leg, price, quantity))})
    # Spend residual allowance in submission order, keeping the other leg's
    # already allocated limit. Tick rounding and pool caps can leave usable slack.
    for i in sorted(range(len(result)), key=lambda i: D(result[i].get("depth", "0"))):
        leg = result[i]
        def remaining(n: int) -> Decimal:
            return n * (1 - edge) - sum(cost(other, D(other["limit"]), n)
                                        for j, other in enumerate(result) if j != i)
        price = limit_for(leg, quantity, remaining, lambda p: fundable(leg, p), D(leg["limit"]))
        if price is not None:
            result[i] = {**leg, "limit": str(price), "fee": str(fee_bound(leg, price, quantity))}
    return result


def second_limit(leg: dict[str, Any], quantity: int, edge: Decimal,
                 first_price: Decimal, first_fee_per_pair: Decimal,
                 fundable: Callable[[Decimal], bool]) -> Decimal | None:
    # Allocate recorded first-leg fees proportionally to matched contracts.
    # Repair of excess inventory remains a separate source of profit or loss.
    return limit_for(leg, quantity, lambda n: n * (1 - edge - first_price - first_fee_per_pair), fundable)
