"""Exact money arithmetic.

Prices arrive from the API as floats, and float arithmetic on them goes wrong in
exactly the places trading cares about: ``1 - 0.34`` is ``0.6599999999999999``,
four legs that cost exactly $1.00 can sum to ``1.0000000000000002``, and
``1.0 - 1e-300`` is ``1.0``. Every dollar amount that decides a trade, a fee, or
a ledger entry is therefore a ``Decimal`` made here, at the boundary, and never
converted back to float for a decision.

Kalshi quotes dollars to four decimal places, so a price is snapped to that grid
when it is converted; that removes float noise without moving any real quote.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal

PRICE_QUANTUM = Decimal("0.0001")
ZERO = Decimal("0")
ONE = Decimal("1")


def price(value: float | int | str | Decimal) -> Decimal:
    """A quoted price as an exact Decimal on Kalshi's four-decimal grid."""
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, float):
        d = Decimal(repr(value))
    else:
        d = Decimal(str(value))
    return d.quantize(PRICE_QUANTUM, rounding=ROUND_HALF_EVEN)


def ceil_to(value: Decimal, quantum: Decimal) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_CEILING)


def floor_to(value: Decimal, quantum: Decimal) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_FLOOR)


def dollars(value: Decimal) -> str:
    """Format for ledgers and reports: always two decimals, more if needed."""
    text = f"{value:.6f}".rstrip("0")
    whole, _, frac = text.partition(".")
    return f"{whole}.{frac.ljust(2, '0')}"
