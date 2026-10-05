"""Paper trading: signals, sizing, a confirming second scan, fills, settlement.

Paper mode is the only mode. Nothing here can send an order: fills are
simulated at quoted prices and written to the ledger labeled ``simulated``. Live
execution is issue #5, which needs the capital and position limits decided
first.

What the strategy wants to trade (``signal`` events):

* **Arbitrage baskets** -- model-free checks with a positive edge after fees.
  Only exact baskets that pay in *every* settlement state (``ARB``) are
  accepted. A conditional basket (it pays nothing if a chamber leader is an
  independent or the office is vacant), a near-identity, or a basket with less
  than one set on offer is recorded and rejected with that reason.
* **Model trades** -- a combo leg the strategy's model prices away from the
  market: buy YES when the model probability exceeds the ask, buy NO when it is
  below the bid. The edge must beat ``min_edge`` plus the model's own
  uncertainty for that outcome, which each strategy supplies.

Every edge is computed at the quantity that would actually fill, with fees at
that quantity: a one-contract book pays a one-contract fee.

Policy (``Policy``): fixed paper bankroll, hold to settlement, one open position
per basket, a cap per position, quantity capped by resting depth and by free
cash (purchase cost plus entry fees are reserved). Candidates are confirmed on a
second scan -- their legs' books are fetched again -- and filled at that scan's
prices; overlapping baskets share the depth of that scan, so the same resting
contracts are never used twice.

Sizing (``size``): the quantity is the largest the caps allow -- resting depth,
free cash, and the position cap -- and each accepted signal records which one
set it (``limit``). The edge must still beat the threshold at that quantity.

Settlement: at the start of each run, every open position whose markets have
all resolved is settled at $1 per winning contract, and the receipt is
recorded with the run's date as the date of cash receipt.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Mapping, NamedTuple

from strategy import money
from strategy.api import Client
from strategy.checks import DR_STATES, STATES, Check, Leg
from strategy.fees import FeeSchedule
from strategy.ledger import Run, iso, read_events
from strategy.markets import AggregateMarkets, Quote, all_quotes, quote_from_book
from strategy.money import ZERO
from strategy.pnl import free_cash, positions_from_events
from strategy.snapshot import scan_view

CONFIRM_TAG = "confirm"


@dataclass(frozen=True)
class ModelView:
    """What trading needs from a strategy's model."""

    name: str
    combo: Mapping[str, float]  # P(DD), P(DR), P(RD), P(RR)
    uncertainty: Mapping[str, float]  # per combo outcome, added to the threshold

    def state_probs(self) -> dict[str, float]:
        """All nine leadership states; the models put nothing on O leaders."""
        return {s: float(self.combo.get(s, 0.0)) for s in STATES}

    def expected(self, payout_by_state: Mapping[str, int]) -> Decimal:
        return sum(
            (Decimal(repr(p)) * payout_by_state[s] for s, p in self.state_probs().items()), ZERO
        )


def market_state_probs(markets: AggregateMarkets) -> dict[str, float] | None:
    """The combo's midpoints normalized to sum to one, over the nine states.

    The combo pays nothing when a leader is neither party, so its prices put
    nothing on those states either. None if a leg has no quote.
    """
    mids = {code: markets.combo[code].mid for code in DR_STATES}
    if any(m is None for m in mids.values()) or sum(mids.values()) <= 0:
        return None
    total = sum(mids.values())
    return {s: (mids[s] / total if s in mids else 0.0) for s in STATES}


@dataclass(frozen=True)
class Policy:
    bankroll: Decimal = Decimal("1000")
    max_position: Decimal = Decimal("100")  # entry capital per position, a hard cap
    min_edge: Decimal = Decimal("0.01")  # per set, after fees


@dataclass(frozen=True)
class Candidate:
    basket: str  # stable key: one open position per basket
    kind: str  # "arbitrage" or "model"
    legs: tuple[Leg, ...]
    payout_by_state: Mapping[str, int]  # per set, nine leadership states
    value: Decimal  # per set: guaranteed payout (arbitrage) or model expectation
    threshold: Decimal  # edge per set must exceed this
    blocked: str | None = None  # policy reason to reject before sizing

    @property
    def cost(self) -> Decimal:
        return sum((leg.price for leg in self.legs), ZERO)

    def fees(self, quantity: int) -> Decimal:
        """Total fees for ``quantity`` sets, each leg its own order."""
        return sum((leg.schedule.fee(leg.price, quantity) for leg in self.legs), ZERO)

    def outlay(self, quantity: int) -> Decimal:
        """Cash needed for ``quantity`` sets: cost plus entry fees."""
        return Decimal(quantity) * self.cost + self.fees(quantity)

    def edge(self, quantity: int) -> Decimal:
        """Value minus cost minus fees, per set, at ``quantity``."""
        return self.value - self.cost - self.fees(quantity) / quantity

    def executable(self, contracts: int) -> int:
        """Sets fillable at the quoted prices, up to ``contracts``."""
        return min(contracts, math.floor(min(leg.size for leg in self.legs)))


# --- Signals -----------------------------------------------------------------

_BLOCKED = {
    "cond": "conditional: pays nothing if a chamber leader is an independent or the office is vacant",
    "basis": "near-identity, not a settlement identity",
    "thin": "less than one full set on offer",
}


def arbitrage_candidates(checks: list[Check]) -> list[Candidate]:
    """Checks with a positive edge after fees; only ``ARB`` is unblocked."""
    return [
        Candidate(
            basket=f"arbitrage:{c.name}",
            kind="arbitrage",
            legs=c.legs,
            payout_by_state=c.payout_by_control_state(),
            value=Decimal(c.worst_payout),
            threshold=ZERO,
            blocked=_BLOCKED.get(c.verdict),
        )
        for c in checks
        if c.net_edge > 0
    ]


def model_candidates(
    markets: AggregateMarkets,
    model: ModelView,
    fees: Mapping[str, FeeSchedule],
    policy: Policy,
    contracts: int,
) -> list[Candidate]:
    """Combo legs with a positive edge at their executable quantity."""
    out = []
    for code in DR_STATES:
        q = markets.combo[code]
        if not q.is_open:
            continue
        schedule = fees.get(q.series, FeeSchedule())
        uncertainty = Decimal(repr(round(model.uncertainty.get(code, 0.0), 6)))
        threshold = policy.min_edge + uncertainty
        yes_pays = frozenset({code})
        options = []
        if q.ask is not None:
            options.append(("buy", Leg(q.ticker, "yes", money.price(q.ask), q.ask_size, schedule, yes_pays, q.event_ticker)))
        if q.bid is not None:
            no_pays = frozenset(STATES) - yes_pays
            options.append(("sell", Leg(q.ticker, "no", 1 - money.price(q.bid), q.bid_size, schedule, no_pays, q.event_ticker)))
        for action, leg in options:
            pays = {s: int(s in leg.pays) for s in STATES}
            cand = Candidate(f"model:{action} {code}", "model", (leg,), pays, model.expected(pays),
                             threshold)
            n = cand.executable(contracts)
            if n >= 1 and cand.edge(n) > 0:
                out.append(cand)
    return out


# --- Sizing and confirmation -------------------------------------------------------


class Sizing(NamedTuple):
    quantity: int  # 0 when rejected
    reason: str  # why it was rejected; "" when accepted
    limit: str = ""  # what set the quantity: "depth", "cash", or "max position"


def size(
    cand: Candidate,
    depth: Mapping[tuple[str, str], float],
    cash: Decimal,
    policy: Policy,
) -> Sizing:
    """The quantity to fill: the largest that depth, cash, and the position cap allow."""
    available = math.floor(min(depth.get((leg.ticker, leg.side), 0.0) for leg in cand.legs))
    if available < 1:
        return Sizing(0, "no depth left at the confirmed prices")
    budget = min(cash, policy.max_position)
    if cand.outlay(1) > budget:
        return Sizing(0, f"one set needs {money.dollars(cand.outlay(1))}, {money.dollars(budget)} available")
    lo, hi = 1, available
    while lo < hi:  # outlay is nondecreasing in quantity
        mid = (lo + hi + 1) // 2
        if cand.outlay(mid) <= budget:
            lo = mid
        else:
            hi = mid - 1
    quantity = lo
    limit = "depth" if quantity == available else ("cash" if cash < policy.max_position else "max position")
    edge = cand.edge(quantity)
    if edge <= cand.threshold:
        return Sizing(0, (
            f"edge {edge * 100:+.2f}c/set at {quantity} sets does not exceed "
            f"threshold {cand.threshold * 100:.2f}c"
        ), limit)
    return Sizing(quantity, "", limit)


def confirm(cand: Candidate, client: Client, books: dict) -> Candidate | str:
    """Reprice ``cand`` from a fresh read of each leg's book."""
    legs = []
    for leg in cand.legs:
        if leg.ticker not in books:
            books[leg.ticker] = quote_from_book(leg.ticker, client.fetch_orderbook(leg.ticker))
        q: Quote = books[leg.ticker]
        if leg.side == "yes":
            price, available = q.ask, q.ask_size
        else:
            price, available = (None if q.bid is None else 1 - money.price(q.bid)), q.bid_size
        if price is None:
            return f"not confirmed: {leg.ticker} {leg.side} no longer offered"
        legs.append(replace(leg, price=money.price(price), size=available))
    return replace(cand, legs=tuple(legs))


# --- One paper-trading pass ----------------------------------------------------------


def record_scan(run: Run, markets: AggregateMarkets, model: ModelView, source: str) -> None:
    run.event(
        "scan",
        source=source,
        model=model.name,
        model_combo=dict(model.combo),
        uncertainty=dict(model.uncertainty),
        state_probs=model.state_probs(),
        market_state_probs=market_state_probs(markets),
        combo={c: _quote_fields(q) for c, q in markets.combo.items()},
        house_control=_quote_fields(markets.house_control.dem),
        senate_control=_quote_fields(markets.senate_control.dem),
    )


def settle(run: Run, markets: AggregateMarkets, client: Client) -> None:
    """Settle every open position whose markets have all resolved."""
    events = read_events(run.directory.parent.parent, run.strategy)
    lookup = {q.ticker: q for q in all_quotes(markets)}
    for pos in positions_from_events(events).values():
        if not pos.is_open:
            continue
        results = {}
        for fill in pos.legs:
            quote = lookup.get(fill["ticker"]) or _fetch_quote(client, fill)
            results[fill["ticker"]] = None if quote is None else quote.result
        if not results or not all(r in ("yes", "no") for r in results.values()):
            continue
        receipt = Decimal(sum(int(f["quantity"]) for f in pos.legs if results[f["ticker"]] == f["side"]))
        run.event(
            "settlement",
            pos.position_id,
            receipt=receipt,
            received_at=iso(run.clock()),
            results=results,
            entry_capital=pos.entry_capital,
            later_costs=ZERO,
            pnl=receipt - pos.entry_capital,
        )


def trade(
    run: Run,
    client: Client,
    markets: AggregateMarkets,
    checks: list[Check],
    model: ModelView,
    fees: Mapping[str, FeeSchedule],
    policy: Policy,
    contracts: int,
    confirm_delay: float = 0.0,
) -> None:
    """Settle, generate signals, confirm, and fill."""
    settle(run, markets, client)
    runs_dir = run.directory.parent.parent
    events = read_events(runs_dir, run.strategy)
    positions = positions_from_events(events)
    open_baskets = {p.basket: p.position_id for p in positions.values() if p.is_open}
    cash = free_cash(events, policy.bankroll)

    cands = arbitrage_candidates(checks) + model_candidates(markets, model, fees, policy, contracts)
    cands.sort(key=lambda c: (c.blocked is not None, -_scan_edge(c, contracts), c.basket))

    pending = [c for c in cands if c.blocked is None and c.basket not in open_baskets]
    if pending and confirm_delay > 0:
        time.sleep(confirm_delay)
    view, books = scan_view(client, CONFIRM_TAG), {}
    confirmed = {c.basket: confirm(c, view, books) for c in pending}
    depth: dict[tuple[str, str], float] = {}
    for c in confirmed.values():
        if isinstance(c, Candidate):
            for leg in c.legs:
                depth.setdefault((leg.ticker, leg.side), leg.size)

    opened = 0
    for c in cands:
        base = dict(basket=c.basket, kind=c.kind, value=c.value, threshold=c.threshold,
                    scan_edge=_scan_edge(c, contracts))
        if c.blocked is not None:
            run.event("signal", accepted=False, reason=c.blocked, **base)
            continue
        if c.basket in open_baskets:
            run.event("signal", accepted=False, reason=f"already open as {open_baskets[c.basket]}", **base)
            continue
        cc = confirmed[c.basket]
        if isinstance(cc, str):
            run.event("signal", accepted=False, reason=cc, **base)
            continue
        sizing = size(cc, depth, cash, policy)
        if not sizing.quantity:
            run.event("signal", accepted=False, reason=sizing.reason, limit=sizing.limit, **base)
            continue
        quantity = sizing.quantity
        opened += 1
        pid = f"{run.run_id}-{opened:02d}"
        run.event(
            "signal",
            pid,
            accepted=True,
            reason="confirmed",
            quantity=quantity,
            edge=cc.edge(quantity),
            limit=sizing.limit,
            payout_by_state=dict(cc.payout_by_state),
            legs=[{"ticker": leg.ticker, "side": leg.side, "price": leg.price} for leg in cc.legs],
            **base,
        )
        for leg in cc.legs:
            cost = leg.schedule.order(leg.price, quantity, "buy")
            run.event(
                "fill",
                pid,
                simulated=True,
                mode="paper",
                scan=CONFIRM_TAG,
                ticker=leg.ticker,
                event_ticker=leg.event_ticker,
                side=leg.side,
                price=cost.price,
                quantity=quantity,
                trade_fee=cost.trade_fee,
                rounding_fee=cost.rounding_fee,
                fee=cost.fee,
                cash_delta=cost.cash_delta,
            )
            depth[(leg.ticker, leg.side)] -= quantity
            cash += cost.cash_delta


def _scan_edge(c: Candidate, contracts: int) -> Decimal:
    n = c.executable(contracts)
    return c.edge(n) if n >= 1 else Decimal("-1")


def _quote_fields(q: Quote) -> dict:
    return {"ticker": q.ticker, "bid": q.bid, "ask": q.ask, "bid_size": q.bid_size,
            "ask_size": q.ask_size, "status": q.status, "result": q.result}


def _fetch_quote(client: Client, fill: dict) -> Quote | None:
    """Status of a leg's market that the scan did not read."""
    event = client.fetch_event(fill["event_ticker"]) if fill.get("event_ticker") else None
    if event is None:
        return None
    summary = next((m for m in event["markets"] if m["ticker"] == fill["ticker"]), None)
    return None if summary is None else quote_from_book(fill["ticker"], None, summary)
