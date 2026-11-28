"""Record scan prices using the execution pricer, without another market connection."""
from collections.abc import Callable
from decimal import Decimal
from sum_check import Basket, Offer, price


def measure(offers: dict[str, list[Offer]], names: list[str], max_sets: int,
            capital: Decimal, fundable: Callable[[Basket], bool]) -> dict:
    legs = []
    for i, name in enumerate(names):
        k, p = offers[str(i)]
        legs.append({"name": name, "kalshi": k.ticker, "polymarket": p.ticker,
                     "kalshi_ask": k.asks[0][0] if k.asks else None,
                     "polymarket_ask": p.asks[0][0] if p.asks else None})
    routes = []
    for i, j in ((0, 1), (1, 0)):
        combo = (offers[str(i)][0], offers[str(j)][1])
        feasible = []
        one = None
        for size in range(1, max_sets + 1):
            basket = price(combo, size, 1, 0)
            if basket is None:
                break
            q = {"sets": size, "net": basket.net, "net_per_set": basket.edge,
                 "fee_per_set": basket.fees / size,
                 "kalshi_price": basket.legs[0]["vwap"], "polymarket_price": basket.legs[1]["vwap"]}
            if size == 1:
                one = q
            if basket.capital <= capital and fundable(basket):
                feasible.append(q)
        best = max(feasible, key=lambda q: q["net"], default=None)
        gap = max(feasible, key=lambda q: (q["net_per_set"], q["net"]), default=None)
        routes.append({"id": str(i), "name": f"{names[i]} on Kalshi + {names[j]} on Polymarket",
                       "one_set": one, "best": best, "max_gap": gap,
                       "opportunity": bool(gap and gap["net_per_set"] > 0)})
    return {"legs": legs, "routes": routes, "capital_limit": capital}
