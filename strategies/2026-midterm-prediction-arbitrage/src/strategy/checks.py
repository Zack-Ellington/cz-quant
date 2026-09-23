"""Model-free arbitrage checks.

These need no model at all. Each check is a basket of contracts whose combined
payout is at least ``payout`` dollars in *every* possible outcome; if buying the
basket costs less than that, the difference is locked in. The guaranteed payout
is computed by enumerating the settlement states, not asserted by hand, so a
mis-specified basket cannot report a free lunch.

The combo legs, the control markets, and the same-party market all settle on
Kalshi's CONTROL rules, so over the four states DD / DR / RD / RR (House winner,
Senate winner) these identities hold exactly:

    DD + DR + RD + RR = 1                  (sums)
    DD + DR = House-D,  DD + RD = Senate-D,  DD + RR = Same-party   (marginals)
    DD >= House-D + Senate-D - 1, DR >= House-D - Senate-D, ...     (Frechet)

The Fréchet lower bounds are the only restriction the control markets place on
the joint: any combo price below one is a mispricing no matter how the chambers
are correlated.

The seat-count checks compare "Democrats hold >= 218 seats" with House control
(and >= 51 with the Senate). Those are near-identities, not settlement
identities -- a vacancy on Feb 1 or a failed Speaker vote could separate them --
so they are reported with ``exact=False`` and never labeled an arbitrage.

Every leg is bought as a taker: YES at the ask, or NO at one minus the bid. A
check also reports its size: how many full sets can be bought at the quoted
prices, the smallest resting size across its legs. Fees are charged for an
order of ``min(contracts, size)`` sets, rounding up to the cent per order as
Kalshi does, so a thin book pays the rounding it would really pay. A check is
labeled an arbitrage only if it is exact, positive after fees, and at least one
full set is available. Capital is locked until the markets settle on Feb 1,
2027, so even a real arbitrage must clear the cost of carry to be worth taking.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from strategy.constants import HOUSE_MAJORITY, SENATE_DEM_CONTROL
from strategy.fees import DEFAULT_CONTRACTS, FeeSchedule
from strategy.markets import AggregateMarkets, Quote, SeatMarket

STATES = ("DD", "DR", "RD", "RR")  # (House winner, Senate winner)

HOUSE_D = frozenset({"DD", "DR"})
HOUSE_R = frozenset({"RD", "RR"})
SENATE_D = frozenset({"DD", "RD"})
SENATE_R = frozenset({"DR", "RR"})
SAME = frozenset({"DD", "RR"})
SPLIT = frozenset({"DR", "RD"})


@dataclass(frozen=True)
class Leg:
    ticker: str
    side: str  # "yes" or "no"
    price: float  # dollars paid per contract
    size: float  # contracts available at that price
    schedule: FeeSchedule
    pays: frozenset  # states in which this leg pays $1


@dataclass(frozen=True)
class Check:
    name: str
    kind: str  # "sum", "marginal", "frechet", "seats"
    exact: bool  # True: settlement identity. False: near-identity with basis risk.
    legs: tuple[Leg, ...]
    payout: int  # guaranteed payout per set, in the worst state
    contracts: int = DEFAULT_CONTRACTS  # order size the fees assume, at most

    @property
    def cost(self) -> float:
        return sum(leg.price for leg in self.legs)

    @property
    def size(self) -> float:
        """Full sets available at the quoted prices."""
        return min(leg.size for leg in self.legs)

    @property
    def order(self) -> int:
        """Sets the fee is charged on: the notional order, capped by size."""
        return max(1, min(self.contracts, int(self.size)))

    @property
    def fees(self) -> float:
        """Taker fees per set for an order of ``order`` sets."""
        return sum(leg.schedule.taker_per_contract(leg.price, self.order) for leg in self.legs)

    @property
    def raw_edge(self) -> float:
        return self.payout - self.cost

    @property
    def net_edge(self) -> float:
        return self.raw_edge - self.fees

    @property
    def verdict(self) -> str:
        if self.net_edge > 0:
            if not self.exact:
                return "basis"
            return "ARB" if self.size >= 1 else "thin"
        if self.raw_edge > 0:
            return "fees"
        return "ok"


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
        """Cheapest single contract paying $1 in exactly ``states``."""
        options = []
        for inst in instruments:
            q = inst.quote
            if inst.yes_pays == states and q.ask is not None:
                options.append(Leg(q.ticker, "yes", q.ask, q.ask_size, schedule(q), states))
            if frozenset(STATES) - inst.yes_pays == states and q.bid is not None:
                options.append(Leg(q.ticker, "no", 1.0 - q.bid, q.bid_size, schedule(q), states))
        return min(
            options,
            key=lambda leg: leg.price + leg.schedule.taker_per_contract(leg.price, contracts),
            default=None,
        )

    def basket(name: str, kind: str, exposures: list[frozenset]) -> Check | None:
        legs = [exposure(s) for s in exposures]
        if any(leg is None for leg in legs):
            return None
        payout = min(sum(1 for leg in legs if s in leg.pays) for s in STATES)
        return Check(name, kind, True, tuple(legs), payout, contracts)

    one = lambda code: frozenset({code})  # noqa: E731
    not_ = lambda code: frozenset(STATES) - {code}  # noqa: E731
    specs = [
        ("combo: buy all four legs", "sum", [one(c) for c in STATES]),
        ("combo: sell all four legs", "sum", [not_(c) for c in STATES]),
        ("House control: D + R", "sum", [HOUSE_D, HOUSE_R]),
        ("Senate control: D + R", "sum", [SENATE_D, SENATE_R]),
        ("DD + DR vs House-D (short)", "marginal", [one("DD"), one("DR"), HOUSE_R]),
        ("DD + DR vs House-D (long)", "marginal", [one("RD"), one("RR"), HOUSE_D]),
        ("DD + RD vs Senate-D (short)", "marginal", [one("DD"), one("RD"), SENATE_R]),
        ("DD + RD vs Senate-D (long)", "marginal", [one("DR"), one("RR"), SENATE_D]),
        ("DD + RR vs same-party (short)", "marginal", [one("DD"), one("RR"), SPLIT]),
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
        if q.ask is not None:
            yes_legs.append(Leg(q.ticker, "yes", q.ask, q.ask_size, schedule(q), frozenset({i})))
        if q.bid is not None:
            no_legs.append(
                Leg(q.ticker, "no", 1.0 - q.bid, q.bid_size, schedule(q), frozenset(states) - {i})
            )

    def payout(legs):
        return min(sum(1 for leg in legs if s in leg.pays) for s in states)

    checks = []
    if len(yes_legs) == len(states):
        checks.append(
            Check(f"{label} seats: buy all buckets", "sum", True, tuple(yes_legs), payout(yes_legs), contracts)
        )
    if len(no_legs) == len(states):
        checks.append(
            Check(f"{label} seats: sell all buckets", "sum", True, tuple(no_legs), payout(no_legs), contracts)
        )

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
                control_leg.schedule, control_states)
        )
        checks.append(Check(name, "seats", False, tuple(legs), payout(legs), contracts))
    return checks
