"""The sum check across Kalshi and DraftKings.

Exactly one outcome of a game wins. Buying one contract on every outcome pays $1
whatever happens, so if buying each outcome where it is cheapest costs less than
$1 in total, after fees, the difference is locked in:

    edge per set = 1 - sum over outcomes of min(Kalshi YES ask + fee, 1 / DraftKings odds)

kalshi_sum_check buys every outcome on Kalshi. This strategy prices each outcome on
both venues and keeps only baskets with at least one leg on each. A DraftKings bet
of stake s at decimal odds d is s * d contracts at 1/d (the margin is in the
price, no fee). Kalshi legs are walked through the book and pay the series taker
fee, rounded up to the cent per leg, as the shared paper adapter charges it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING
from itertools import product

from czquant.book import levels as book_levels

TAKER = Decimal("0.07")
APR_CAP = 1e6
MIN_DAYS = 1 / 24


@dataclass(frozen=True)
class Offer:
    """One way to buy one outcome: YES asks (price, quantity), best first."""
    role: str
    venue: str  # kalshi or draftkings
    ticker: str
    asks: tuple[tuple[Decimal, Decimal], ...]
    fee_multiplier: Decimal | None = None  # Kalshi series multiplier; None: fee in the price


def kalshi_offer(role: str, ticker: str, raw_book: dict, fee_multiplier: Decimal) -> Offer:
    """YES asks from a Kalshi book: a NO bid at p is a YES ask at 1 - p."""
    no = book_levels(raw_book)["no"]
    return Offer(role, "kalshi", ticker, tuple(sorted((1 - p, q) for p, q in no)), fee_multiplier)


def sportsbook_offer(selection, max_stake: Decimal) -> Offer:
    price = selection.price
    return Offer(selection.role, "draftkings", selection.ticker, ((price, (max_stake / price).quantize(Decimal("0.01"))),))


def taker_fee(fills: list[tuple[Decimal, Decimal]], multiplier: Decimal) -> Decimal:
    base = sum((q * p * (1 - p) for p, q in fills), Decimal(0))
    return (TAKER * multiplier * base).quantize(Decimal("0.01"), rounding=ROUND_CEILING)


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
        fee = taker_fee(fills, offer.fee_multiplier) if offer.fee_multiplier is not None else Decimal(0)
        basket.cost += cost
        basket.fees += fee
        basket.legs.append({"role": offer.role, "venue": offer.venue, "ticker": offer.ticker, "contract": "YES",
                            "qty": sets, "vwap": str(cost / sets), "limit": str(max(p for p, _ in fills)),
                            "fee": str(fee), "levels": [[str(p), str(q)] for p, q in fills]})
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
         min_net: Decimal, min_edge: Decimal, max_apr: float | None = None) -> tuple[Basket | None, str]:
    """The most profitable cross-venue basket above the hurdle, or (None, why).

    Every combination of one offer per outcome with legs on both venues is sized
    from one set up to the depth, max_sets and max_capital, at each size where a
    leg's price changes, keeping the size with the largest profit above the hurdle.
    """
    found, why = None, "no_edge"
    for combo in product(*offers.values()):
        if len({o.venue for o in combo}) < 2:
            continue
        depth = min(sum((q for _, q in o.asks), Decimal(0)) for o in combo)
        top = min(int(depth), max_sets)
        lo, hi = 0, top
        while lo < hi:  # capital grows with size: the largest size within max_capital
            mid = (lo + hi + 1) // 2
            b = price(combo, mid, days, risk_free)
            if b is not None and b.capital <= max_capital:
                lo = mid
            else:
                hi = mid - 1
        if lo < 1:
            why = why if found else ("capital_limit" if top else "no_depth")
            continue
        for size in _sizes(combo, lo):
            b = price(combo, size, days, risk_free)
            if b is None:
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
