"""Kalshi's quadratic fee: formula, rounding, maker rules, multiplier."""

from __future__ import annotations

import pytest

from strategy.fees import FeeSchedule, schedule_from_series


def test_taker_fee_peaks_at_fifty_cents():
    fees = FeeSchedule()
    assert fees.taker(0.5, 100) == pytest.approx(1.75)  # 0.07 * 100 * 0.25
    assert fees.taker_per_contract(0.5, 100) == pytest.approx(0.0175)


def test_rounds_up_to_the_next_cent():
    fees = FeeSchedule()
    assert fees.taker(0.5, 1) == pytest.approx(0.02)  # 0.0175 -> 0.02
    assert fees.taker(0.10, 10) == pytest.approx(0.07)  # 0.063 -> 0.07


def test_exact_cents_are_not_bumped_by_float_error():
    # 0.07 * 100 * 0.25 is 1.7500000000000002 in floating point.
    assert FeeSchedule().taker(0.5, 100) == 1.75


def test_symmetric_and_zero_at_the_extremes():
    fees = FeeSchedule()
    assert fees.taker(0.3, 100) == fees.taker(0.7, 100)
    assert fees.taker(0.0, 100) == 0.0
    assert fees.taker(1.0, 100) == 0.0


def test_makers_pay_nothing_on_plain_quadratic():
    assert FeeSchedule("quadratic").maker(0.5, 100) == 0.0


def test_maker_fees_where_the_series_charges_them():
    assert FeeSchedule("quadratic_with_maker_fees").maker(0.5, 100) == pytest.approx(0.44)  # 0.4375 up


def test_multiplier_scales_the_fee():
    assert FeeSchedule("quadratic", 2.0).taker(0.5, 100) == pytest.approx(3.50)


def test_unknown_fee_type_is_rejected():
    with pytest.raises(ValueError, match="flat"):
        FeeSchedule("flat")


def test_schedule_from_series_payload():
    assert schedule_from_series(None) == FeeSchedule()
    assert schedule_from_series({"fee_type": "quadratic", "fee_multiplier": 0.5}) == FeeSchedule("quadratic", 0.5)
