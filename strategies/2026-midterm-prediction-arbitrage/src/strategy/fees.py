"""Kalshi trading fees.

Kalshi charges a quadratic fee per order, rounded up to the next cent:

    taker fee = round_up(M * 0.07   * C * P * (1 - P))
    maker fee = round_up(M * 0.0175 * C * P * (1 - P))   (only where maker fees apply)

where ``C`` is the number of contracts in the order, ``P`` the price paid in
dollars, and ``M`` the series fee multiplier. The fee peaks at P = 0.50 (1.75
cents per contract for a taker) and vanishes toward 0 and 1. A series'
``fee_type`` decides whether makers pay: ``quadratic`` charges takers only,
``quadratic_with_maker_fees`` (and the combo variant) charge makers as well,
``flat`` is not used by any market this strategy trades and is rejected.

Every market this strategy reads is ``quadratic`` with multiplier 1 (checked at
run time from the series endpoint). Because the fee is rounded up per order, the
per-contract cost depends on order size; the checks price a notional order of
``DEFAULT_CONTRACTS`` contracts per leg, where the rounding adds at most 0.01
cents per contract.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

TAKER_RATE = 0.07
MAKER_RATE = 0.0175
DEFAULT_CONTRACTS = 100

_MAKER_FEE_TYPES = {"quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"}
_KNOWN_FEE_TYPES = {"quadratic"} | _MAKER_FEE_TYPES


@dataclass(frozen=True)
class FeeSchedule:
    fee_type: str = "quadratic"
    multiplier: float = 1.0

    def __post_init__(self) -> None:
        if self.fee_type not in _KNOWN_FEE_TYPES:
            raise ValueError(f"unsupported Kalshi fee_type {self.fee_type!r}")

    def taker(self, price: float, contracts: int = DEFAULT_CONTRACTS) -> float:
        """Taker fee in dollars for one order of ``contracts`` at ``price``."""
        return _round_up_cents(self.multiplier * TAKER_RATE * contracts * price * (1.0 - price))

    def maker(self, price: float, contracts: int = DEFAULT_CONTRACTS) -> float:
        """Maker fee in dollars; zero on series without maker fees."""
        if self.fee_type not in _MAKER_FEE_TYPES:
            return 0.0
        return _round_up_cents(self.multiplier * MAKER_RATE * contracts * price * (1.0 - price))

    def taker_per_contract(self, price: float, contracts: int = DEFAULT_CONTRACTS) -> float:
        return self.taker(price, contracts) / contracts


def _round_up_cents(dollars: float) -> float:
    # Round to 1e-9 first so 0.0175 * 100 does not become 1.7500000000000002
    # and get pushed up a whole cent.
    return math.ceil(round(dollars * 100.0, 9)) / 100.0


def schedule_from_series(payload: dict | None) -> FeeSchedule:
    """Fee schedule from a normalized series payload (``api.normalize_series``)."""
    if not payload:
        return FeeSchedule()
    return FeeSchedule(payload["fee_type"], float(payload["fee_multiplier"]))


def fee_schedules(client, series: set[str]) -> dict[str, FeeSchedule]:
    """Fetch the fee schedule for each series ticker in ``series``."""
    return {s: schedule_from_series(client.fetch_series(s)) for s in sorted(series)}
