"""Positions and cash, rebuilt from the ledger.

Nothing here reads a strategy's memory: every number comes from the ``signal``,
``fill``, and ``settlement`` events of ``ledger.jsonl`` files, joined by
position id across runs. A position opened in one run and settled weeks later
in another is one position.

Aggregates (``summarize``):

* **volume** -- sum of price x quantity over fills;
* **capital committed** -- purchase cost plus entry fees, split open / settled;
* **projected minimum profit** (open positions) -- the payout the position is
  guaranteed in its worst settlement state, minus entry capital, minus remaining
  costs. Kept apart from earned P&L;
* **settled net P&L** -- settlement receipts minus entry capital minus later
  costs;
* **return on committed capital** -- settled P&L over the entry capital of the
  settled positions.

Holding to settlement has no later costs on Kalshi (no settlement fee), so the
remaining- and later-cost terms are zero today; they are kept in the formulas so
an exit or fee change has somewhere to go.
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
    kind: str
    quantity: int
    payout_by_state: dict[str, int]  # per set
    volume: Decimal = ZERO
    entry_capital: Decimal = ZERO  # purchase cost plus entry fees
    fees: Decimal = ZERO
    legs: list[dict] = field(default_factory=list)
    receipt: Decimal | None = None
    received_at: str | None = None
    settled_run: str | None = None
    later_costs: Decimal = ZERO

    @property
    def is_open(self) -> bool:
        return self.receipt is None

    @property
    def min_payout(self) -> Decimal:
        return Decimal(self.quantity) * min(self.payout_by_state.values())

    @property
    def projected_min_profit(self) -> Decimal:
        remaining_costs = ZERO
        return self.min_payout - self.entry_capital - remaining_costs

    @property
    def settled_pnl(self) -> Decimal | None:
        if self.receipt is None:
            return None
        return self.receipt - self.entry_capital - self.later_costs


def positions_from_events(events: list[dict]) -> dict[str, Position]:
    """Join accepted signals, fills, and settlements by position id."""
    positions: dict[str, Position] = {}
    for e in events:
        pid = e.get("position_id")
        if e["type"] == "signal" and e.get("accepted"):
            positions[pid] = Position(
                position_id=pid,
                strategy=e["strategy"],
                run_id=e["run_id"],
                entry_time=e["ts"],
                basket=e["basket"],
                kind=e["kind"],
                quantity=int(e["quantity"]),
                payout_by_state={s: int(v) for s, v in e["payout_by_state"].items()},
            )
        elif e["type"] == "fill" and pid in positions:
            p = positions[pid]
            price, qty = Decimal(e["price"]), int(e["quantity"])
            p.volume += price * qty
            p.entry_capital -= Decimal(e["cash_delta"])
            p.fees += Decimal(e["fee"])
            p.legs.append(e)
        elif e["type"] == "settlement" and pid in positions:
            p = positions[pid]
            p.receipt = Decimal(e["receipt"])
            p.received_at = e["received_at"]
            p.settled_run = e["run_id"]
            p.later_costs = Decimal(e.get("later_costs", "0"))
    return positions


def free_cash(events: list[dict], bankroll: Decimal) -> Decimal:
    """Paper cash: bankroll plus every fill's cash delta and every receipt."""
    cash = Decimal(bankroll)
    for e in events:
        if e["type"] == "fill":
            cash += Decimal(e["cash_delta"])
        elif e["type"] == "settlement":
            cash += Decimal(e["receipt"])
    return cash


@dataclass(frozen=True)
class Summary:
    positions: int
    open: int
    settled: int
    volume: Decimal
    capital_open: Decimal
    capital_settled: Decimal
    projected_min_profit: Decimal  # open positions only
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
    return Summary(
        positions=len(positions),
        open=len(open_),
        settled=len(settled),
        volume=sum((p.volume for p in positions), ZERO),
        capital_open=sum((p.entry_capital for p in open_), ZERO),
        capital_settled=capital_settled,
        projected_min_profit=sum((p.projected_min_profit for p in open_), ZERO),
        settled_pnl=settled_pnl,
        return_on_capital=(settled_pnl / capital_settled) if capital_settled else None,
    )
