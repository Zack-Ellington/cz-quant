"""Kalshi fees: the quadratic trade fee plus the account's balance rounding."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from strategy.fees import FeeAccumulator, FeeSchedule, schedule_from_series


def test_published_non_direct_member_example():
    # docs.kalshi.com/getting_started/fee_rounding: buy 1 at $0.055, model fee
    # $0.00363825 -> trade fee $0.003639, balance change floors to -$0.06, so
    # the rounding fee is $0.001361 and the combined fee $0.005.
    o = FeeSchedule().order(0.055, 1)
    assert o.trade_fee == D("0.003639")
    assert o.rounding_fee == D("0.001361")
    assert o.fee == D("0.005")
    assert o.cash_delta == D("-0.06")


def test_old_cent_rounding_overcharged_the_published_example():
    # The review's reproduction: the earlier code returned $0.01 here.
    assert FeeSchedule().fee(0.055, 1) == D("0.005")
    assert FeeSchedule().taker(0.055, 1) == pytest.approx(0.005)


def test_direct_members_round_to_a_hundredth_of_a_cent():
    o = FeeSchedule(account="direct").order(0.055, 1)
    assert o.cash_delta == D("-0.0587")  # -0.058639 floored to $0.0001
    assert o.fee == D("0.0037")


def test_exact_amounts_are_not_bumped():
    # 0.07 * 100 * 0.25 = 1.75 exactly; float arithmetic made it 1.7500000000000002.
    assert FeeSchedule().fee(0.5, 100) == D("1.75")
    assert FeeSchedule().order(0.5, 100).cash_delta == D("-51.75")


def test_one_contract_at_fifty_cents_costs_two_cents():
    assert FeeSchedule().fee(0.5, 1) == D("0.02")


def test_fee_is_not_proportional_to_quantity():
    # Per-contract fee at 1 contract (2c) is not the per-contract fee at 100 (1.75c):
    # fees must be computed at the filled quantity, never scaled.
    f = FeeSchedule()
    assert f.fee(0.5, 1) * 100 != f.fee(0.5, 100)


def test_float_noise_in_the_price_is_removed():
    # 1 - 0.34 == 0.6599999999999999 in floating point.
    assert FeeSchedule().order(1 - 0.34, 200).price == D("0.66")


def test_selling_receives_cash_and_pays_the_same_fee():
    f = FeeSchedule()
    buy, sell = f.order(0.30, 10, "buy"), f.order(0.30, 10, "sell")
    assert buy.fee == sell.fee
    assert buy.cash_delta < 0 < sell.cash_delta


def test_symmetric_in_price_and_zero_at_the_extremes():
    f = FeeSchedule()
    assert f.fee(0.3, 100) == f.fee(0.7, 100)
    assert f.fee(0.0, 100) == 0
    assert f.fee(1.0, 100) == 0


def test_makers_pay_only_rounding_on_plain_quadratic():
    f = FeeSchedule("quadratic")
    assert f.model_fee(0.5, 100, maker=True) == 0
    assert f.maker(0.5, 100) == 0.0  # 50.00 aligns to the cent: no rounding either


def test_maker_fees_where_the_series_charges_them():
    assert FeeSchedule("quadratic_with_maker_fees").fee(0.5, 100, maker=True) == D("0.44")  # 0.4375 up


def test_multiplier_scales_the_fee():
    assert FeeSchedule("quadratic", 2.0).fee(0.5, 100) == D("3.50")


def test_accumulator_rebates_whole_units_and_never_goes_negative():
    acc = FeeAccumulator(FeeSchedule())
    fills = [acc.fill(0.055, 1) for _ in range(3)]  # each overpays 0.001361 in rounding
    assert all(o.fee >= 0 for o in fills)
    assert all(o.rebate == 0 for o in fills)  # 3 x 0.001361 < one cent
    assert acc.balance == D("0.004083")


def test_bad_inputs_are_rejected():
    with pytest.raises(ValueError, match="flat"):
        FeeSchedule("flat")
    with pytest.raises(ValueError, match="account"):
        FeeSchedule(account="institutional")
    with pytest.raises(ValueError, match="positive"):
        FeeSchedule().order(0.5, 0)


def test_schedule_from_series_payload():
    assert schedule_from_series(None) == FeeSchedule()
    got = schedule_from_series({"fee_type": "quadratic", "fee_multiplier": 0.5}, account="direct")
    assert got == FeeSchedule("quadratic", 0.5, "direct")
