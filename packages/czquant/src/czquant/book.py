"""Pure normalization of Kalshi public orderbook data."""
from typing import Any
from decimal import Decimal
from .store import decimal

def levels(raw: dict[str, Any]) -> dict[str, list[tuple[Decimal, Decimal]]]:
    book = raw.get("orderbook_fp") or raw.get("orderbook") or raw
    out = {}
    for side in ("yes", "no"):
        rows = book.get(side + "_dollars_fp", book.get(side + "_dollars"))
        dollars = rows is not None
        if rows is None:
            rows = book.get(side, [])
        out[side] = sorted([(decimal(p) / (1 if dollars else 100), decimal(q)) for p, q in (rows or [])
                            if decimal(q) > 0], reverse=True)
    return out

