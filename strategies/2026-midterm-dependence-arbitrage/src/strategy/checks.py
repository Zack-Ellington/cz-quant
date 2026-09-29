"""Model-free arbitrage checks.

These need no model. Each check is a basket of contracts; its payout in every
settlement state is computed by enumerating the states, not asserted by hand, so
a mis-specified basket cannot report a free lunch.

Settlement states
-----------------
The combo legs, the control markets, and the same-party market all settle on
Kalshi's CONTROL rules, whose payout criterion is the *party membership of the
chamber's leader*: the Speaker of the House and the President pro tempore of the
Senate (https://assets.kalshi.com/contract_terms/CONTROL.pdf). A leader is a
Democrat (D), a Republican (R), or neither (O): an independent, or an office
still vacant at expiration. The rules map O to neither party, so every D- or
R-conditioned leg pays nothing in it. That gives nine states per chamber pair:

    DD DR DO   RD RR RO   OD OR OO     (House leader, Senate leader)

Over the four D/R states these identities hold exactly:

    DD + DR + RD + RR = 1                                          (sums)
    DD + DR = House-D,  DD + RD = Senate-D,  DD + RR = Same-party   (marginals)
    DD >= House-D + Senate-D - 1,  DR >= House-D - Senate-D, ...    (Frechet)

Each check reports its payout over the D/R states and its worst payout over all
nine. A basket that is profitable after fees in every state is an arbitrage
(``ARB``). One that is profitable only while both leaders are D or R -- the
same-party basket, for instance, pays nothing if the Speaker is an independent
-- is labeled conditional (``cond``), never guaranteed.

The seat-count checks live in their own state space (which bucket the seat
total lands in; caucusing independents count with their party). Bucket sums are
exact. "Democrats hold >= 218 seats" vs House control is only a near-identity --
a vacancy or a failed leadership vote could separate them -- so those checks
have ``exact=False`` and are never labeled arbitrage.

Money and size
--------------
Every price, cost, fee, and edge is an exact ``Decimal`` (see ``money.py``). Each
leg is bought as a taker: YES at the ask, NO at one minus the bid. A check's size
is the smallest resting size across its legs, and its fees are computed for an
order of ``min(contracts, size)`` sets -- the quantity that could actually fill.
``ARB`` also requires at least one full set.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from strategy import money
from strategy.constants import HOUSE_MAJORITY, SENATE_DEM_CONTROL
from strategy.fees import DEFAULT_CONTRACTS, FeeSchedule
from strategy.markets import AggregateMarkets, Quote, SeatMarket

LEADERS = ("D", "R", "O")
STATES = tuple(h + s for h in LEADERS for s in LEADERS)  # all nine
DR_STATES = ("DD", "DR", "RD", "RR")  # both leaders a Democrat or Republican

HOUSE_D = frozenset(s for s in STATES if s[0] == "D")
HOUSE_R = frozenset(s for s in STATES if s[0] == "R")
SENATE_D = frozenset(s for s in STATES if s[1] == "D")
SENATE_R = frozenset(s for s in STATES if s[1] == "R")
SAME = frozenset({"DD", "RR"})  # an independent leader is no party


@dataclass(frozen=True)
class Leg:
    ticker: str
    side: str  # "yes" or "no"
    price: Decimal  # dollars paid per contract
    size: float  # contracts available at that price
    schedule: FeeSchedule
    pays: frozenset  # states in which this leg pays $1
    event_ticker: str | None = None


@dataclass(frozen=True)
class Check:
    name: str
    kind: str  # "sum", "marginal", "frechet", "seats"
    exact: bool  # True: settlement identity. False: near-identity with basis risk.
    legs: tuple[Leg, ...]
    states: tuple  # every settlement state of this basket's state space
    normal_states: tuple  # the states the identity assumes (D/R leaders)
    contracts: int = DEFAULT_CONTRACTS  # largest order the fees assume

    def payout_in(self, state) -> int:
        return sum(1 for leg in self.legs if state in leg.pays)

    @property
    def payout(self) -> int:
        """Guaranteed payout per set over the normal (D/R) states."""
        return min(self.payout_in(s) for s in self.normal_states)

    @property
    def worst_payout(self) -> int:
        """Guaranteed payout per set over every settlement state."""
        return min(self.payout_in(s) for s in self.states)

    @property
    def cost(self) -> Decimal:
        return sum((leg.price for leg in self.legs), money.ZERO)

    @property
    def size(self) -> float:
        """Full sets available at the quoted prices."""
        return min(leg.size for leg in self.legs)

    @property
    def order(self) -> int:
        """Sets the fees are charged on: the notional order, capped by size."""
        return max(1, min(self.contracts, math.floor(self.size)))

    @property
    def fees(self) -> Decimal:
        """Fees per set for an order of ``order`` sets, each leg its own order."""
        n = self.order
        return sum((leg.schedule.fee(leg.price, n) for leg in self.legs), money.ZERO) / n

    @property
    def raw_edge(self) -> Decimal:
        return self.payout - self.cost

    @property
    def net_edge(self) -> Decimal:
        return self.raw_edge - self.fees

    @property
    def worst_net_edge(self) -> Decimal:
        return self.worst_payout - self.cost - self.fees

    @property
    def verdict(self) -> str:
        if self.net_edge <= 0:
            return "fees" if self.raw_edge > 0 else "ok"
        if not self.exact:
            return "basis"
        if self.size < 1:
            return "thin"
        return "ARB" if self.worst_net_edge > 0 else "cond"

    def payout_by_control_state(self) -> dict[str, int]:
        """Payout per set in each of the nine leadership states.

        A seat-bucket sum pays the same whatever the leadership, so it maps to
        its constant payout.
        """
        if set(self.states) == set(STATES):
            return {s: self.payout_in(s) for s in STATES}
        return {s: self.worst_payout for s in STATES}


@dataclass(frozen=True)
class _Instrument:
    quote: Quote
    yes_pays: frozenset


def run_checks(
    markets: AggregateMarkets,
    fees: Mapping[str, FeeSchedule],
    contracts: int = DEFAULT_CONTRACTS,
) -> list[Check]:
    """Every model-free check the available quotes support."""
    instruments = [_Instrument(q, frozenset({code})) for code, q in markets.combo.items()]
    instruments += [
        _Instrument(markets.house_control.dem, HOUSE_D),
        _Instrument(markets.house_control.rep, HOUSE_R),
        _Instrument(markets.senate_control.dem, SENATE_D),
        _Instrument(markets.senate_control.rep, SENATE_R),
    ]
    if markets.same_party is not None:
        instruments.append(_Instrument(markets.same_party, SAME))

    def schedule(quote: Quote) -> FeeSchedule:
        return fees.get(quote.series, FeeSchedule())

    def exposure(states: frozenset) -> Leg | None:
        """Cheapest single open contract paying $1 in at least ``states``.

        A contract that also pays in other states is at least as good for a long
        basket; the leg keeps its true payout set, so the enumeration credits
        those extra payouts (e.g. Senate-R NO also pays if the leader is an
        independent, which Senate-D YES does not).
        """
        options = []
        for inst in instruments:
            q = inst.quote
            if not q.is_open:
                continue
            no_pays = frozenset(STATES) - inst.yes_pays
            if states <= inst.yes_pays and q.ask is not None:
                options.append(
                    Leg(q.ticker, "yes", money.price(q.ask), q.ask_size, schedule(q), inst.yes_pays, q.event_ticker)
                )
            if states <= no_pays and q.bid is not None:
                options.append(
                    Leg(q.ticker, "no", 1 - money.price(q.bid), q.bid_size, schedule(q), no_pays, q.event_ticker)
                )
        return min(
            options,
            key=lambda leg: leg.price + leg.schedule.fee(leg.price, contracts) / contracts,
            default=None,
        )

    def basket(name: str, kind: str, exposures: list[frozenset]) -> Check | None:
        legs = [exposure(s) for s in exposures]
        if any(leg is None for leg in legs):
            return None
        return Check(name, kind, True, tuple(legs), STATES, DR_STATES, contracts)

    one = lambda code: frozenset({code})  # noqa: E731
    not_ = lambda code: frozenset(STATES) - {code}  # noqa: E731
    specs = [
        ("combo: buy all four legs", "sum", [one(c) for c in DR_STATES]),
        ("combo: sell all four legs", "sum", [not_(c) for c in DR_STATES]),
        ("House control: D + R", "sum", [HOUSE_D, HOUSE_R]),
        ("Senate control: D + R", "sum", [SENATE_D, SENATE_R]),
        ("DD + DR vs House-D (short)", "marginal", [one("DD"), one("DR"), HOUSE_R]),
        ("DD + DR vs House-D (long)", "marginal", [one("RD"), one("RR"), HOUSE_D]),
        ("DD + RD vs Senate-D (short)", "marginal", [one("DD"), one("RD"), SENATE_R]),
        ("DD + RD vs Senate-D (long)", "marginal", [one("DR"), one("RR"), SENATE_D]),
        ("DD + RR vs same-party (short)", "marginal", [one("DD"), one("RR"), frozenset(STATES) - SAME]),
        ("DD + RR vs same-party (long)", "marginal", [one("DR"), one("RD"), SAME]),
        ("DD >= House-D + Senate-D - 1", "frechet", [one("DD"), HOUSE_R, SENATE_R]),
        ("RR >= House-R + Senate-R - 1", "frechet", [one("RR"), HOUSE_D, SENATE_D]),
        ("DR >= House-D - Senate-D", "frechet", [one("DR"), HOUSE_R, SENATE_D]),
        ("RD >= Senate-D - House-D", "frechet", [one("RD"), HOUSE_D, SENATE_R]),
    ]
    checks = [c for spec in specs if (c := basket(*spec)) is not None]

    for label, seat_market, threshold, dem, rep in (
        ("House", markets.house_seats, HOUSE_MAJORITY, HOUSE_D, HOUSE_R),
        ("Senate", markets.senate_seats, SENATE_DEM_CONTROL, SENATE_D, SENATE_R),
    ):
        if seat_market is None:
            continue
        checks += _seat_checks(
            label, seat_market, threshold, exposure(dem), exposure(rep), schedule, contracts
        )
    return checks


def _seat_checks(label, market: SeatMarket, threshold, dem_leg, rep_leg, schedule, contracts) -> list[Check]:
    """Bucket sums (exact) and buckets-vs-control (near-identity)."""
    states = tuple(range(len(market.buckets)))
    yes_legs, no_legs = [], []
    for i, b in enumerate(market.buckets):
        q = b.quote
        if not q.is_open:
            continue
        if q.ask is not None:
            yes_legs.append(
                Leg(q.ticker, "yes", money.price(q.ask), q.ask_size, schedule(q), frozenset({i}), q.event_ticker)
            )
        if q.bid is not None:
            no_legs.append(
                Leg(q.ticker, "no", 1 - money.price(q.bid), q.bid_size, schedule(q),
                    frozenset(states) - {i}, q.event_ticker)
            )

    checks = []
    if len(yes_legs) == len(states):
        checks.append(Check(f"{label} seats: buy all buckets", "sum", True, tuple(yes_legs), states, states, contracts))
    if len(no_legs) == len(states):
        checks.append(Check(f"{label} seats: sell all buckets", "sum", True, tuple(no_legs), states, states, contracts))

    # Buckets at or above the majority line pay exactly when that party would
    # control -- assuming the seat count decides control on Feb 1.
    upper = frozenset(i for i, b in enumerate(market.buckets) if b.lo >= threshold)
    lower = frozenset(states) - upper
    by_bucket = {next(iter(leg.pays)): leg for leg in yes_legs}
    for name, side_buckets, control_leg, control_states in (
        (f"{label} seats >= {threshold} vs {label}-D (short)", upper, rep_leg, lower),
        (f"{label} seats >= {threshold} vs {label}-D (long)", lower, dem_leg, upper),
    ):
        if control_leg is None or not all(i in by_bucket for i in side_buckets):
            continue
        legs = [by_bucket[i] for i in sorted(side_buckets)]
        legs.append(
            Leg(control_leg.ticker, control_leg.side, control_leg.price, control_leg.size,
                control_leg.schedule, control_states, control_leg.event_ticker)
        )
        checks.append(Check(name, "seats", False, tuple(legs), states, states, contracts))
    return checks
