"""Exact money: prices snap to Kalshi's grid, rounding goes the stated way."""

from __future__ import annotations

from decimal import Decimal as D

from strategy import money


def test_price_removes_float_noise():
    assert money.price(1 - 0.34) == D("0.66")
    assert money.price(0.1 + 0.2) == D("0.3")
    assert money.price("0.9110") == D("0.911")
    assert money.price(D("0.00700000001")) == D("0.007")


def test_four_legs_that_cost_a_dollar_sum_to_exactly_a_dollar():
    legs = [0.913, 1 - 0.913]  # a float sum of these can miss 1.0
    assert sum(money.price(x) for x in legs) == D("1")


def test_directed_rounding():
    assert money.ceil_to(D("0.00363825"), D("0.000001")) == D("0.003639")
    assert money.floor_to(D("-0.058639"), D("0.01")) == D("-0.06")
    assert money.floor_to(D("0.051361"), D("0.01")) == D("0.05")


def test_dollar_format():
    assert money.dollars(D("6.5")) == "6.50"
    assert money.dollars(D("0.005")) == "0.005"
    assert money.dollars(D("-0.060000")) == "-0.06"
