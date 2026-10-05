"""Positions and cash, rebuilt from the ledger rows.

Nothing here reads a strategy's memory: every number comes from the ``fill``,
``settlement``, and ``mark`` rows of ``ledger.jsonl`` files (see ``ledger.py``),
joined by position id across runs. A position opened in one run and settled
weeks later in another is one position.

Aggregates (``summarize``):

* **volume** -- sum of price x quantity over fills;
* **capital committed** -- purchase cost plus entry fees, split open / settled;
* **open value** -- quantity times the latest mark price over the open legs,
  or None if some open leg was never marked;
* **settled net P&L** -- settlement receipts minus entry capital;
* **return on committed capital** -- settled P&L over the entry capital of the
  settled positions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from strategy.money import ZERO


@dataclass
class Position:
    position_id: str
    strategy: str
    run_id: str  # entry run
    entry_time: str
    basket: str
    legs: list[dict] = field(default_factory=list)  # fill rows
    volume: Decimal = ZERO
    entry_capital: Decimal = ZERO  # purchase cost plus entry fees
    fees: Decimal = ZERO
    receipt: Decimal | None = None
    received_at: str | None = None
    settled_run: str | None = None
    marks: dict[tuple[str, str], tuple[Decimal, str]] = field(default_factory=dict)  # (ticker, side) -> (price, ts)

    @property
    def is_open(self) -> bool:
        return self.receipt is None

    @property
    def quantity(self) -> int:
        """Sets held: every leg of a basket is filled at the same quantity."""
        return int(self.legs[0]["quantity"]) if self.legs else 0

    @property
    def settled_pnl(self) -> Decimal | None:
        if self.receipt is None:
            return None
        return self.receipt - self.entry_capital

    @property
    def value(self) -> Decimal | None:
        """What the open legs are worth at their latest marks; None if any leg is unmarked."""
        if not self.is_open:
            return None
        total = ZERO
        for leg in self.legs:
            mark = self.marks.get((leg["ticker"], leg["side"]))
            if mark is None:
                return None
            total += Decimal(leg["quantity"]) * mark[0]
        return total

    @property
    def marked_pnl(self) -> Decimal | None:
        value = self.value
        return None if value is None else value - self.entry_capital


def positions_from_ledger(rows: list[dict]) -> dict[str, Position]:
    """Join fills, settlements, and marks by position id."""
    positions: dict[str, Position] = {}
    for r in rows:
        pid = r["position_id"]
        if r["type"] == "fill":
            p = positions.get(pid)
            if p is None:
                p = positions[pid] = Position(pid, r["strategy"], r["run_id"], r["ts"], r["basket"])
            price, qty = Decimal(r["price"]), int(r["quantity"])
            p.volume += price * qty
            p.entry_capital -= Decimal(r["cash_delta"])
            p.fees += Decimal(r["fee"])
            p.legs.append(r)
        elif pid in positions and r["type"] == "settlement":
            p = positions[pid]
            p.receipt = (p.receipt or ZERO) + Decimal(r["cash_delta"])
            p.received_at = r["ts"]
            p.settled_run = r["run_id"]
        elif pid in positions and r["type"] == "mark":
            positions[pid].marks[(r["ticker"], r["side"])] = (Decimal(r["price"]), r["ts"])
    return positions


def free_cash(rows: list[dict], bankroll: Decimal) -> Decimal:
    """Paper cash: bankroll plus every cash delta (marks move no cash)."""
    return Decimal(bankroll) + sum((Decimal(r["cash_delta"]) for r in rows), ZERO)


@dataclass(frozen=True)
class Summary:
    positions: int
    open: int
    settled: int
    volume: Decimal
    capital_open: Decimal
    capital_settled: Decimal
    open_value: Decimal | None  # None if an open leg is unmarked
    settled_pnl: Decimal
    return_on_capital: Decimal | None  # None until something has settled

    @property
    def capital(self) -> Decimal:
        return self.capital_open + self.capital_settled


def summarize(positions: list[Position]) -> Summary:
    open_ = [p for p in positions if p.is_open]
    settled = [p for p in positions if not p.is_open]
    capital_settled = sum((p.entry_capital for p in settled), ZERO)
    settled_pnl = sum((p.settled_pnl for p in settled), ZERO)
    values = [p.value for p in open_]
    return Summary(
        positions=len(positions),
        open=len(open_),
        settled=len(settled),
        volume=sum((p.volume for p in positions), ZERO),
        capital_open=sum((p.entry_capital for p in open_), ZERO),
        capital_settled=capital_settled,
        open_value=None if any(v is None for v in values) else sum(values, ZERO),
        settled_pnl=settled_pnl,
        return_on_capital=(settled_pnl / capital_settled) if capital_settled else None,
    )
