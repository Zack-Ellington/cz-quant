"""Order-book walking, tick sizes and Kalshi fee math. Prices are in dollars (0-1)."""

from __future__ import annotations

import math
from dataclasses import dataclass

# Kalshi fee schedule: fee = round_up_to_cent(coef * multiplier * C * P * (1 - P)).
TAKER_COEF = 0.07
MAKER_COEF = 0.0175
# fee_type values seen on GET /series. Combo maker fees only apply to combo orders,
# so single-market resting orders on those series are maker-free.
MAKER_FEE_TYPES = {"quadratic_with_maker_fees"}
KNOWN_FEE_TYPES = {"quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"}


@dataclass
class Book:
    """Both sides expressed from the YES contract's point of view."""

    yes_bids: list  # [(price, qty)], best (highest) first
    yes_asks: list  # [(price, qty)], best (lowest) first

    @property
    def best_bid(self):
        return self.yes_bids[0][0] if self.yes_bids else None

    @property
    def best_ask(self):
        return self.yes_asks[0][0] if self.yes_asks else None


def _num(x) -> float:
    return float(x)


def parse_book(raw: dict) -> Book:
    """Kalshi only publishes bids. A NO bid at p is a YES ask at 1 - p."""
    if "yes_dollars" in raw or "no_dollars" in raw:
        yes = [(_num(p), _num(q)) for p, q in raw.get("yes_dollars") or []]
        no = [(_num(p), _num(q)) for p, q in raw.get("no_dollars") or []]
    else:  # legacy integer-cents format
        yes = [(_num(p) / 100, _num(q)) for p, q in raw.get("yes") or []]
        no = [(_num(p) / 100, _num(q)) for p, q in raw.get("no") or []]
    yes_bids = sorted(((p, q) for p, q in yes if q > 0), key=lambda l: -l[0])
    yes_asks = sorted(((round(1 - p, 6), q) for p, q in no if q > 0), key=lambda l: l[0])
    return Book(yes_bids, yes_asks)


@dataclass
class Fill:
    filled: float
    notional: float  # sum(q * p)
    fee_base: float  # sum(q * p * (1 - p)), the quadratic fee kernel
    levels: int

    @property
    def vwap(self):
        return self.notional / self.filled if self.filled else None


def walk(levels: list, size: float) -> Fill:
    """Consume price levels best-first until `size` contracts are filled."""
    filled = notional = fee_base = 0.0
    used = 0
    for price, qty in levels:
        if filled >= size - 1e-9:
            break
        take = min(qty, size - filled)
        filled += take
        notional += take * price
        fee_base += take * price * (1 - price)
        used += 1
    return Fill(filled, notional, fee_base, used)


def round_up_cent(x: float) -> float:
    return math.ceil(round(x * 100, 6)) / 100


def taker_fee(fee_base: float, fee_type: str, multiplier: float) -> float:
    return round_up_cent(TAKER_COEF * multiplier * fee_base)


def maker_fee(fee_base: float, fee_type: str, multiplier: float) -> float:
    if fee_type not in MAKER_FEE_TYPES:
        return 0.0
    return round_up_cent(MAKER_COEF * multiplier * fee_base)


def tick_at(price_ranges: list | None, price: float) -> float:
    """Tick size at `price` from a market's price_ranges ([{start, end, step}])."""
    for r in price_ranges or []:
        if _num(r["start"]) - 1e-9 <= price <= _num(r["end"]) + 1e-9:
            return _num(r["step"])
    return 0.01


def maker_buy_price(book: Book, price_ranges) -> float:
    """Price to rest a YES bid at: one tick inside the spread, else join the bid."""
    bid, ask = book.best_bid, book.best_ask
    if bid is None:
        return tick_at(price_ranges, 0.0)  # empty bid side: post at the minimum tick
    t = tick_at(price_ranges, bid)
    if ask is None or bid + t < ask - 1e-9:
        return round(bid + t, 6)
    return bid


def maker_sell_price(book: Book, price_ranges) -> float:
    """Price to rest a YES offer at: one tick inside the spread, else join the ask."""
    bid, ask = book.best_bid, book.best_ask
    if ask is None:
        return round(1 - tick_at(price_ranges, 1.0), 6)
    t = tick_at(price_ranges, ask)
    if bid is None or ask - t > bid + 1e-9:
        return round(ask - t, 6)
    return ask
