"""Positions, P&L aggregation, and simulated P&L -- all from the ledger.

Nothing here reads a strategy's memory: every number is rebuilt from the
``fill``, ``signal``, and ``settlement`` events of ``ledger.jsonl`` files, joined
by position id across runs. A position opened in one run and settled weeks later
in another is one position.

Aggregates (per entry run and in total):

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

**Simulated P&L.** The projected minimum answers "what if everything goes
wrong"; it says nothing about what the strategy expects. So open positions are
also valued under the strategy's own model: settlement outcomes are drawn from
the model's probability of each leadership state (recorded in the ``scan``
event), and each draw's P&L is the book's payout in that state minus its entry
capital. The draws give the expected P&L, its spread, and the probability of a
loss. It is a model valuation, labeled as such, and never mixed into earned P&L.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import numpy as np

from strategy.checks import STATES
from strategy.money import ZERO

SIMULATIONS = 100_000


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

    def payout_in(self, state: str) -> Decimal:
        return Decimal(self.quantity) * self.payout_by_state[state]


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


@dataclass(frozen=True)
class SimulatedPnL:
    model: str
    n: int
    seed: int
    mean: float
    sd: float
    p05: float
    p50: float
    p95: float
    prob_loss: float
    positions: int


def simulate_pnl(
    positions: list[Position],
    state_probs: dict[str, float],
    model: str,
    n: int = SIMULATIONS,
    seed: int = 0,
) -> SimulatedPnL | None:
    """Draw settlement states from the model and value the open book in each."""
    open_ = [p for p in positions if p.is_open]
    if not open_:
        return None
    probs = np.array([max(state_probs.get(s, 0.0), 0.0) for s in STATES], dtype=np.float64)
    if probs.sum() <= 0:
        return None
    probs /= probs.sum()
    payout = np.array(
        [float(sum((p.payout_in(s) for p in open_), ZERO)) for s in STATES], dtype=np.float64
    )
    capital = float(sum((p.entry_capital for p in open_), ZERO))
    draws = np.random.default_rng(seed).choice(len(STATES), size=n, p=probs)
    pnl = payout[draws] - capital
    return SimulatedPnL(
        model=model,
        n=n,
        seed=seed,
        mean=float(pnl.mean()),
        sd=float(pnl.std()),
        p05=float(np.percentile(pnl, 5)),
        p50=float(np.percentile(pnl, 50)),
        p95=float(np.percentile(pnl, 95)),
        prob_loss=float((pnl < 0).mean()),
        positions=len(open_),
    )


def latest_model(events: list[dict]) -> tuple[str, dict[str, float], str] | None:
    """(model name, state probabilities, run id) of the most recent scan."""
    for e in reversed(events):
        if e["type"] == "scan" and e.get("state_probs"):
            return e["model"], {s: float(v) for s, v in e["state_probs"].items()}, e["run_id"]
    return None


def select_positions(
    positions: dict[str, Position], since: date | None, until: date | None
) -> list[Position]:
    """Positions whose entry date is within [since, until], by entry time."""
    chosen = []
    for p in positions.values():
        entered = date.fromisoformat(p.entry_time[:10])
        if since is not None and entered < since:
            continue
        if until is not None and entered > until:
            continue
        chosen.append(p)
    return sorted(chosen, key=lambda p: (p.entry_time, p.position_id))
