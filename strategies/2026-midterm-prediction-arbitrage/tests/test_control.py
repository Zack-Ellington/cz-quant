"""Control rules, with the tie cases that markets get wrong pinned down."""

from __future__ import annotations

from strategy.control import house_control, senate_control


def test_senate_fifty_fifty_goes_republican():
    # 50-50 is broken by the Republican VP.
    assert senate_control(50) == "R"
    assert senate_control(51) == "D"
    assert senate_control(49) == "R"


def test_senate_extremes():
    assert senate_control(0) == "R"
    assert senate_control(100) == "D"


def test_house_knife_edge():
    assert house_control(217) == "R"
    assert house_control(218) == "D"


def test_house_extremes():
    assert house_control(0) == "R"
    assert house_control(435) == "D"
