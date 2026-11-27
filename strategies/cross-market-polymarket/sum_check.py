"""The sum check across Kalshi and Polymarket.

Exactly one team wins a game. Buying one contract on every team pays $1 whatever
happens, so if buying each team where it is cheapest costs less than $1 in total,
after fees, the difference is locked in:

    edge per set = 1 - sum over teams of min(Kalshi YES ask + fee, Polymarket token ask + fee)

kalshi_sum_check buys every team on Kalshi. This strategy prices each team on both
venues and, by default, keeps only baskets with a leg on each (`require_cross`).
Every leg is walked through its book. Kalshi legs pay the series taker fee rounded
up to the cent per order; Polymarket legs pay shares x rate x p x (1 - p), rounded
to 5 decimals. Both are what the shared paper adapters charge.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from itertools import product
from typing import Any

from czquant.book import levels as book_levels
from czquant.polymarket import fee as polymarket_fee
from czquant.polymarket_us import fee as polymarket_us_fee

TAKER = Decimal("0.07")
APR_CAP = 1e6
MIN_DAYS = 1 / 24


@dataclass(frozen=True)
class Offer:
    """One way to buy one team: asks (price, quantity), best first, and that venue's fee."""
    role: str  # the team, as Kalshi's market ticker for it
    venue: str  # kalshi or polymarket
    ticker: str
    asks: tuple[tuple[Decimal, Decimal], ...]
    fee_multiplier: Decimal | None = None  # Kalshi: the series multiplier
    fee_schedule: Any = None  # Polymarket: the market's schedule (a dict); kept hashable by the caller
    tick: Decimal = Decimal("0.01")
    contract: str = "YES"  # Kalshi legs can buy NO; a Polymarket leg is always a token bought outright


def kalshi_offer(role: str, ticker: str, raw_book: dict, fee_multiplier: Decimal, contract: str = "YES") -> Offer:
    """Asks from a Kalshi-layout book: a NO bid at p is a YES ask at 1 - p, and a YES bid
    at p is a NO ask at 1 - p."""
    opposite = book_levels(raw_book)["no" if contract == "YES" else "yes"]
    return Offer(role, "kalshi", ticker, tuple(sorted((1 - p, q) for p, q in opposite)),
                 fee_multiplier=fee_multiplier, contract=contract)


def polymarket_offer(role: str, ticker: str, raw_book: dict, schedule: dict) -> Offer:
    """A token's asks, from its book in the Kalshi layout (czquant.polymarket.kalshi_layout)."""
    no = book_levels(raw_book)["no"]
    return Offer(role, "polymarket", ticker, tuple(sorted((1 - p, q) for p, q in no)),
                 fee_schedule=tuple(sorted(schedule.items())))


def polymarket_us_offer(role: str, ticker: str, raw_book: dict, theta: Decimal, contract: str, tick: Decimal = Decimal("0.01")) -> Offer:
    """One side of a Polymarket US market, from its book in the Kalshi layout. The book
    is the YES side's, as on Kalshi: YES is bought at the offers, NO at 1 - the bids."""
    opposite = book_levels(raw_book)["no" if contract == "YES" else "yes"]
    return Offer(role, "polymarket_us", ticker, tuple(sorted((1 - p, q) for p, q in opposite)),
                 fee_schedule=(("theta", str(theta)),), contract=contract, tick=tick)


def leg_fee(offer: Offer, fills: list[tuple[Decimal, Decimal]]) -> Decimal:
    if offer.venue == "polymarket_us":
        return polymarket_us_fee(fills, Decimal(dict(offer.fee_schedule or ())["theta"]))
    if offer.venue == "polymarket":
        return polymarket_fee(fills, dict(offer.fee_schedule or ()))
    base = sum((q * p * (1 - p) for p, q in fills), Decimal(0))
    return (TAKER * (offer.fee_multiplier or Decimal(1)) * base).quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def walk(offer: Offer, quantity: Decimal) -> list[tuple[Decimal, Decimal]] | None:
    fills, left = [], quantity
    for price, depth in offer.asks:
        if left <= 0:
            break
        take = min(depth, left)
        fills.append((price, take))
        left -= take
    return fills if left <= 0 else None


@dataclass
class Basket:
    sets: int
    legs: list[dict] = field(default_factory=list)
    cost: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    days: float = 0.0
    hurdle: float = 0.0

    @property
    def capital(self) -> Decimal:
        return self.cost + self.fees

    @property
    def net(self) -> Decimal:
        return self.sets - self.capital

    @property
    def edge(self) -> Decimal:
        return self.net / self.sets

    @property
    def excess(self) -> Decimal:
        return self.net - self.capital * Decimal(str(self.hurdle))

    @property
    def apr(self) -> float:
        roi = float(self.net / self.capital) if self.capital else -1.0
        if roi <= -1:
            return -1.0
        try:
            return min(math.expm1(math.log1p(roi) * 365 / self.days), APR_CAP)
        except OverflowError:
            return APR_CAP

    def summary(self) -> dict:
        return {"sets": self.sets, "cost": str(self.cost), "fees": str(self.fees), "capital": str(self.capital),
                "net": str(self.net), "edge": str(self.edge), "apr": self.apr, "days": self.days,
                "legs": self.legs}


def price(offers: tuple[Offer, ...], sets: int, days: float, risk_free: float) -> Basket | None:
    days = max(days, MIN_DAYS)
    basket = Basket(sets, days=days, hurdle=(1 + risk_free) ** (days / 365) - 1)
    for offer in offers:
        fills = walk(offer, Decimal(sets))
        if fills is None:
            return None
        cost = sum((p * q for p, q in fills), Decimal(0))
        fee = leg_fee(offer, fills)
        basket.cost += cost
        basket.fees += fee
        basket.legs.append({"role": offer.role, "venue": offer.venue, "ticker": offer.ticker, "contract": offer.contract,
                            "tick": str(offer.tick), "fee_coefficient": str(TAKER * (offer.fee_multiplier or Decimal(1)) if offer.venue == "kalshi" else dict(offer.fee_schedule or ()).get("theta", "0")),
                            "qty": sets, "vwap": str(cost / sets), "limit": str(max(p for p, _ in fills)),
                            "fee": str(fee), "levels": [[str(p), str(q)] for p, q in fills],
                            "depth": str(sum((q for _, q in offer.asks), Decimal(0)))})
    return basket


def _sizes(offers: tuple[Offer, ...], top: int) -> list[int]:
    """Sizes at which some leg moves to its next price level, plus the largest."""
    sizes = {top}
    for offer in offers:
        cumulative = Decimal(0)
        for _, depth in offer.asks:
            cumulative += depth
            if cumulative >= top:
                break
            sizes.add(int(cumulative))
    return sorted(s for s in sizes if 1 <= s <= top)


def best(offers: dict[str, list[Offer]], days: float, risk_free: float, max_sets: int, max_capital: Decimal,
         min_net: Decimal, min_edge: Decimal, max_apr: float | None = None,
         require_cross: bool = True, fundable: Callable[[Basket], bool] | None = None) -> tuple[Basket | None, str]:
    """The most profitable basket above the hurdle, or (None, why).

    Every combination of one offer per team (with legs on both venues when
    `require_cross`) is sized from one set up to the depth, max_sets and max_capital,
    at each size where a leg's price changes, keeping the largest profit above the hurdle.
    The optional fundable check further bounds size by the selected broker pools;
    it must be nonbinding and monotone in quantity for a fixed combination.
    """
    found, why = None, "no_edge"
    for combo in product(*offers.values()):
        if require_cross and len({o.venue for o in combo}) < 2:
            continue
        depth = min(sum((q for _, q in o.asks), Decimal(0)) for o in combo)
        top = min(int(depth), max_sets)
        lo, hi = 0, top
        while lo < hi:  # capital grows with size: the largest size within max_capital
            mid = (lo + hi + 1) // 2
            b = price(combo, mid, days, risk_free)
            if b is not None and b.capital <= max_capital and (fundable is None or fundable(b)):
                lo = mid
            else:
                hi = mid - 1
        if lo < 1:
            why = why if found else ("capital_limit" if top else "no_depth")
            continue
        for size in _sizes(combo, lo):
            b = price(combo, size, days, risk_free)
            if b is None or (fundable is not None and not fundable(b)):
                continue
            if b.edge < min_edge or b.net < min_net:
                why = why if found else ("below_min_edge" if b.net > 0 else why)
                continue
            if b.excess <= 0:
                why = why if found else "below_risk_free"
                continue
            if max_apr is not None and b.apr > max_apr:
                why = why if found else "above_max_apr"
                continue
            if found is None or b.excess > found.excess:
                found = b
    return found, ("ok" if found else why)
