"""Kalshi trading fees, including the balance-rounding step.

Kalshi's fee has two parts (https://docs.kalshi.com/getting_started/fee_rounding):

1. The **trade fee**: the quadratic model fee rounded *up* to $0.000001,

       model fee = M * rate * C * P * (1 - P),
       rate = 0.07 for takers, 0.0175 for makers where maker fees apply,

   with ``C`` contracts at price ``P`` dollars and ``M`` the series multiplier.
2. The **rounding fee**: balances are kept at $0.01 for non-direct members and
   $0.0001 for direct members. The balance change (trade proceeds minus trade
   fee) is floored to that precision and the difference is charged. Rounding
   fees go into a per-order accumulator; once it holds a whole unit of
   precision it is rebated, never enough to make the net fee negative.

So a non-direct member buying one contract at $0.055 pays a trade fee of
$0.003639 (model fee $0.00363825), the balance change -0.058639 floors to -0.06,
and the net fee is $0.005 -- Kalshi's published example, and a regression test.
Rounding the fee itself up to the cent, as an earlier version did, charges
$0.01 there.

Paper results assume a **non-direct member** account (``DEFAULT_ACCOUNT``), the
retail case. A series' ``fee_type`` decides whether makers pay: ``quadratic``
charges takers only; ``quadratic_with_maker_fees`` and the combo variant charge
makers too; ``flat`` is not used by any market these strategies trade and is
rejected.

Fees are always computed at the quantity actually filled. Because of the
rounding, a fee is not proportional to quantity, so it must never be scaled from
a reference order size.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from strategy import money
from strategy.money import ZERO

TAKER_RATE = Decimal("0.07")
MAKER_RATE = Decimal("0.0175")
FEE_QUANTUM = Decimal("0.000001")
ACCOUNT_PRECISION = {"non-direct": Decimal("0.01"), "direct": Decimal("0.0001")}
DEFAULT_ACCOUNT = "non-direct"
DEFAULT_CONTRACTS = 100

_MAKER_FEE_TYPES = {"quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"}
_KNOWN_FEE_TYPES = {"quadratic"} | _MAKER_FEE_TYPES


@dataclass(frozen=True)
class OrderCost:
    """Cash effect of one fill. Every field is exact."""

    contracts: int
    price: Decimal
    side: str  # "buy" or "sell"
    gross: Decimal  # cash from the trade before fees: -C*P to buy, +C*P to sell
    trade_fee: Decimal
    rounding_fee: Decimal
    rebate: Decimal

    @property
    def fee(self) -> Decimal:
        """Net fee: trade fee plus rounding fee minus rebate."""
        return self.trade_fee + self.rounding_fee - self.rebate

    @property
    def cash_delta(self) -> Decimal:
        """Change in the account balance."""
        return self.gross - self.fee


@dataclass(frozen=True)
class FeeSchedule:
    fee_type: str = "quadratic"
    multiplier: float = 1.0
    account: str = DEFAULT_ACCOUNT

    def __post_init__(self) -> None:
        if self.fee_type not in _KNOWN_FEE_TYPES:
            raise ValueError(f"unsupported Kalshi fee_type {self.fee_type!r}")
        if self.account not in ACCOUNT_PRECISION:
            raise ValueError(f"unknown account type {self.account!r}")

    @property
    def precision(self) -> Decimal:
        return ACCOUNT_PRECISION[self.account]

    def model_fee(self, price, contracts: int, maker: bool = False) -> Decimal:
        """The unrounded quadratic fee."""
        if maker and self.fee_type not in _MAKER_FEE_TYPES:
            return ZERO
        p = money.price(price)
        rate = MAKER_RATE if maker else TAKER_RATE
        return Decimal(str(self.multiplier)) * rate * Decimal(contracts) * p * (1 - p)

    def order(self, price, contracts: int, side: str = "buy", maker: bool = False) -> OrderCost:
        """Cost of a single-fill order of ``contracts`` at ``price``."""
        return FeeAccumulator(self, maker).fill(price, contracts, side)

    def fee(self, price, contracts: int, maker: bool = False) -> Decimal:
        """Net fee of a single-fill buy (the fee is the same for a sell)."""
        return self.order(price, contracts, "buy", maker).fee

    # Float conveniences for display only; decisions use ``order`` / ``fee``.
    def taker(self, price, contracts: int = DEFAULT_CONTRACTS) -> float:
        return float(self.fee(price, contracts))

    def maker(self, price, contracts: int = DEFAULT_CONTRACTS) -> float:
        return float(self.fee(price, contracts, maker=True))

    def taker_per_contract(self, price, contracts: int = DEFAULT_CONTRACTS) -> float:
        return float(self.fee(price, contracts) / contracts)


@dataclass
class FeeAccumulator:
    """The per-order rounding accumulator; feed it the order's fills in order."""

    schedule: FeeSchedule
    maker: bool = False
    balance: Decimal = field(default=ZERO)

    def fill(self, price, contracts: int, side: str = "buy") -> OrderCost:
        if contracts <= 0:
            raise ValueError("a fill needs a positive number of contracts")
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be buy or sell, got {side!r}")
        p = money.price(price)
        gross = Decimal(contracts) * p * (-1 if side == "buy" else 1)
        trade_fee = money.ceil_to(self.schedule.model_fee(p, contracts, self.maker), FEE_QUANTUM)
        before = gross - trade_fee
        aligned = money.floor_to(before, self.schedule.precision)
        rounding_fee = before - aligned
        self.balance += rounding_fee
        # Rebate whole units of precision, never more than this fill's fees.
        rebate = min(money.floor_to(self.balance, self.schedule.precision), trade_fee + rounding_fee)
        rebate = max(rebate, ZERO)
        self.balance -= rebate
        return OrderCost(contracts, p, side, gross, trade_fee, rounding_fee, rebate)


def schedule_from_series(payload: dict | None, account: str = DEFAULT_ACCOUNT) -> FeeSchedule:
    """Fee schedule from a normalized series payload (``api.normalize_series``)."""
    if not payload:
        return FeeSchedule(account=account)
    return FeeSchedule(payload["fee_type"], float(payload["fee_multiplier"]), account)


def fee_schedules(client, series: set[str], account: str = DEFAULT_ACCOUNT) -> dict[str, FeeSchedule]:
    """Fetch the fee schedule for each series ticker in ``series``."""
    return {s: schedule_from_series(client.fetch_series(s), account) for s in sorted(series)}
