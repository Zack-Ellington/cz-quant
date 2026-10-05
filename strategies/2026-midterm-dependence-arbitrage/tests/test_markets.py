"""Aggregate markets: quotes from books, seat-bucket parsing, and tiling."""

from __future__ import annotations

import pytest

from strategy.markets import (
    Quote,
    build_seat_market,
    fetch_markets,
    parse_bucket,
    quote_from_book,
    traded_series,
)


def test_quote_from_book_uses_top_of_each_side():
    book = {"yes": [[0.30, 5.0], [0.32, 10.0]], "no": [[0.60, 3.0], [0.66, 1.0]]}
    q = quote_from_book("SERIES-26-X", book, {"last_price": 0.31, "volume": 1234.0})
    assert q.bid == pytest.approx(0.32)
    assert q.ask == pytest.approx(0.34)  # 1 - best NO bid
    assert q.mid == pytest.approx(0.33)
    assert q.spread == pytest.approx(0.02)
    assert (q.last, q.volume, q.series) == (0.31, 1234.0, "SERIES")


def test_empty_book_has_no_price():
    q = quote_from_book("X-1", {"yes": [], "no": []})
    assert q.bid is None and q.ask is None and q.mid is None and q.spread is None


def test_one_sided_book_uses_that_side():
    assert Quote("X", 0.4, None).mid == 0.4
    assert Quote("X", None, 0.6).mid == 0.6


@pytest.mark.parametrize(
    "title, expected",
    [("Below 210", (0, 209)), ("210-213", (210, 213)), ("Above 249", (250, 435))],
)
def test_house_titles(title, expected):
    assert parse_bucket(title, "custom", None, None, size=435) == expected


@pytest.mark.parametrize(
    "title, floor, cap, expected",
    [("Below 45", 0, 44, (0, 44)), ("51", 51, 51, (51, 51)), ("Above 52", 53, 100, (53, 100))],
)
def test_senate_titles_agree_with_strikes(title, floor, cap, expected):
    assert parse_bucket(title, "custom", floor, cap, size=100) == expected


def test_strike_only_buckets():
    # "less" with cap 53 means below 53; "greater" with floor 57 means above 57.
    assert parse_bucket("", "less", None, 53, size=100) == (0, 52)
    assert parse_bucket("", "greater", 57, None, size=100) == (58, 100)


def test_title_and_strikes_must_agree():
    with pytest.raises(ValueError, match="strikes give"):
        parse_bucket("45", "custom", 46, 46, size=100)


def test_unparseable_bucket_raises():
    with pytest.raises(ValueError, match="cannot parse"):
        parse_bucket("Toss-up", "custom", None, None, size=435)


def _event(titles):
    return {"markets": [{"ticker": f"E-{i}", "yes_sub_title": t, "strike_type": "custom",
                         "floor_strike": None, "cap_strike": None} for i, t in enumerate(titles)]}


def _quotes(n):
    return {f"E-{i}": Quote(f"E-{i}", 0.1, 0.2) for i in range(n)}


def test_buckets_must_tile_the_chamber():
    ok = build_seat_market("E", _event(["Below 210", "210-217", "Above 217"]), _quotes(3), size=435)
    assert [(b.lo, b.hi) for b in ok.buckets] == [(0, 209), (210, 217), (218, 435)]
    with pytest.raises(ValueError, match="gap or overlap"):
        build_seat_market("E", _event(["Below 210", "211-217", "Above 217"]), _quotes(3), size=435)
    with pytest.raises(ValueError, match="span"):
        build_seat_market("E", _event(["Below 210", "210-217"]), _quotes(2), size=435)


def test_fetch_markets_from_snapshot(aggregate_client):
    m = fetch_markets(aggregate_client)
    assert m.combo_mids() == pytest.approx({"DD": 0.605, "DR": 0.275, "RD": 0.015, "RR": 0.095})
    assert m.house_control.p_dem() == pytest.approx(0.885)
    assert m.same_party.mid == pytest.approx(0.70)
    house = m.house_seats
    assert [(b.lo, b.hi) for b in house.buckets] == [(0, 209), (210, 217), (218, 229), (230, 435)]
    assert house.overround() == pytest.approx(0.01)
    assert sum(house.probabilities()) == pytest.approx(1.0)
    assert house.prob_at_least(218) == pytest.approx((0.405 + 0.485) / 1.01)
    senate = m.senate_seats
    assert [(b.lo, b.hi) for b in senate.buckets][-1] == (52, 100)
    assert senate.prob_at_least(51) == pytest.approx((0.205 + 0.425) / 1.015)


def test_prob_at_least_needs_a_bucket_edge(aggregate_client):
    with pytest.raises(ValueError, match="edge"):
        fetch_markets(aggregate_client).house_seats.prob_at_least(219)


def test_traded_series(aggregate_client):
    assert traded_series(fetch_markets(aggregate_client)) == {
        "CONTROLH", "CONTROLS", "KXBALANCEPOWERCOMBO", "KXDHOUSESEATS",
        "KXDSENATESEATS", "KXSAMEPARTYCONGRESS",
    }
