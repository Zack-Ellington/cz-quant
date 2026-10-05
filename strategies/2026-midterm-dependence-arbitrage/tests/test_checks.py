"""Model-free checks: nine settlement states, exact money, executable size."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from strategy.checks import DR_STATES, STATES, run_checks
from strategy.fees import FeeSchedule
from strategy.markets import AggregateMarkets, ControlMarket, Quote, fetch_markets, quote_from_book

NO_FEES = {}  # every series falls back to the default quadratic schedule
COMBO = "KXBALANCEPOWERCOMBO-27FEB"


def _q(ticker, bid, ask, size=1_000.0, **kw):
    return Quote(ticker, bid, ask, bid_size=size, ask_size=size, **kw)


def _markets(combo=None, house=(0.88, 0.89, 0.11, 0.12), senate=(0.62, 0.63, 0.37, 0.38), same=None):
    combo = combo or {"DD": (0.60, 0.61), "DR": (0.27, 0.28), "RD": (0.01, 0.02), "RR": (0.09, 0.10)}
    return AggregateMarkets(
        combo={k: _q(f"KXBALANCEPOWERCOMBO-27FEB-{k}", b, a) for k, (b, a) in combo.items()},
        house_control=ControlMarket(_q("CONTROLH-2026-D", *house[:2]), _q("CONTROLH-2026-R", *house[2:])),
        senate_control=ControlMarket(_q("CONTROLS-2026-D", *senate[:2]), _q("CONTROLS-2026-R", *senate[2:])),
        same_party=None if same is None else _q("KXSAMEPARTYCONGRESS-27FEB01", *same),
        house_seats=None,
        senate_seats=None,
    )


def _by_name(checks):
    return {c.name: c for c in checks}


def test_nine_leadership_states():
    # Each leader is a Democrat, a Republican, or neither (independent or vacant).
    assert len(STATES) == 9 and set(DR_STATES) < set(STATES)
    assert {"DO", "OD", "OO"} <= set(STATES)


def test_consistent_book_has_no_arbitrage(aggregate_client):
    checks = run_checks(fetch_markets(aggregate_client), NO_FEES)
    assert checks
    assert all(c.verdict != "ARB" for c in checks)


def test_payouts_are_enumerated_over_every_state(aggregate_client):
    checks = _by_name(run_checks(fetch_markets(aggregate_client), NO_FEES))
    buy_all = checks["combo: buy all four legs"]
    assert (buy_all.payout, buy_all.worst_payout) == (1, 0)  # nothing pays if a leader is O
    assert buy_all.payout_in("OD") == 0
    sell_all = checks["combo: sell all four legs"]
    assert (sell_all.payout, sell_all.worst_payout) == (3, 3)  # four NOs: 3 pay, 4 if a leader is O
    assert checks["House seats: sell all buckets"].worst_payout == 3  # four buckets
    assert checks["Senate seats: sell all buckets"].worst_payout == 4  # five buckets


def test_independent_speaker_makes_the_same_party_basket_conditional():
    # The review's case: an independent Speaker (House leader O) and a Republican
    # president pro tempore make DR, RD, and same-party YES all false.
    m = _markets(combo={"DD": (0.60, 0.61), "DR": (0.24, 0.25), "RD": (0.01, 0.02), "RR": (0.09, 0.10)},
                 same=(0.58, 0.60))
    check = _by_name(run_checks(m, NO_FEES))["DD + RR vs same-party (long)"]
    assert check.cost == D("0.87")
    assert check.payout == 1 and check.payout_in("OR") == 0
    assert check.net_edge > 0
    assert check.worst_net_edge < 0
    assert check.verdict == "cond"
    assert check.payout_by_control_state()["OR"] == 0


def test_an_arbitrage_must_pay_in_every_state():
    # Senate-D and Senate-R bid 0.70 and 0.40: both NOs cost 0.90 and at least
    # one pays whoever leads the Senate -- both do if the leader is independent.
    check = _by_name(run_checks(_markets(senate=(0.70, 0.71, 0.40, 0.41)), NO_FEES))["Senate control: D + R"]
    assert [leg.side for leg in check.legs] == ["no", "no"]
    assert check.cost == D("0.90")
    assert (check.payout, check.worst_payout) == (1, 1)
    assert check.payout_in("DO") == 2
    assert check.verdict == "ARB"


def test_a_leg_paying_in_extra_states_still_counts():
    # Senate-R NO pays in every non-Republican state, a superset of Senate-D YES.
    checks = run_checks(_markets(senate=(0.62, 0.70, 0.37, 0.38)), NO_FEES)
    leg = _by_name(checks)["DD + RD vs Senate-D (long)"].legs[-1]
    assert (leg.ticker, leg.side, leg.price) == ("CONTROLS-2026-R", "no", D("0.63"))


def test_money_is_exact_where_floats_are_not():
    # An ask is 1 - the best NO bid. NO bids of 0.99, 0.68, 0.33 give asks of 1c,
    # 32c, 67c -- exactly $1 -- but in floating point the three asks sum to
    # 0.9999999999999999, a phantom positive edge that the old float code
    # reported as "positive before fees". In Decimal there is no edge.
    assert 1 - ((1.0 - 0.99) + (1.0 - 0.68) + (1.0 - 0.33)) > 0
    def from_no(ticker, no_bid, yes_bid=None):
        book = {"yes": [[yes_bid, 1000.0]] if yes_bid else [], "no": [[no_bid, 1000.0]]}
        return quote_from_book(ticker, book)

    m = _markets()
    m.combo["DD"] = from_no(f"{COMBO}-DD", 0.99)
    m.combo["DR"] = from_no(f"{COMBO}-DR", 0.68)
    m = AggregateMarkets(
        m.combo,
        ControlMarket(from_no("CONTROLH-2026-D", 0.66, yes_bid=0.20), from_no("CONTROLH-2026-R", 0.33)),
        m.senate_control, None, None, None,
    )
    check = _by_name(run_checks(m, NO_FEES))["DD + DR vs House-D (short)"]
    assert [leg.price for leg in check.legs] == [D("0.01"), D("0.32"), D("0.67")]
    assert check.cost == D("1")
    assert check.raw_edge == 0
    assert check.verdict == "ok"


def test_fees_can_erase_a_raw_edge():
    near = {"DD": (0.60, 0.605), "DR": (0.27, 0.275), "RD": (0.01, 0.015), "RR": (0.09, 0.10)}
    check = _by_name(run_checks(_markets(near), NO_FEES))["combo: buy all four legs"]
    assert check.raw_edge == D("0.005")
    assert check.net_edge < 0
    assert check.verdict == "fees"


def test_cheapest_way_to_hold_an_exposure_is_used():
    # "House goes R" costs 0.15 as House-R YES but 0.10 as House-D NO (bid 0.90),
    # which also pays if the Speaker is an independent.
    checks = run_checks(_markets(house=(0.90, 0.91, 0.08, 0.15)), NO_FEES)
    leg = _by_name(checks)["DD + DR vs House-D (short)"].legs[-1]
    assert (leg.ticker, leg.side, leg.price) == ("CONTROLH-2026-D", "no", D("0.10"))


def test_fees_are_charged_at_the_executable_order():
    cheap = {"DD": (0.55, 0.56), "DR": (0.24, 0.25), "RD": (0.01, 0.02), "RR": (0.06, 0.07)}
    deep = _by_name(run_checks(_markets(cheap), NO_FEES))["combo: buy all four legs"]
    m = _markets(cheap)
    m.combo["DD"] = _q("KXBALANCEPOWERCOMBO-27FEB-DD", 0.55, 0.56, size=1.0)
    one_set = _by_name(run_checks(m, NO_FEES))["combo: buy all four legs"]
    assert (deep.order, one_set.order) == (100, 1)
    # One contract per order: each leg's fee and balance round up to the cent.
    f = FeeSchedule()
    assert one_set.fees == sum(f.fee(p, 1) for p in ("0.56", "0.25", "0.02", "0.07"))
    assert one_set.fees > deep.fees


def test_thin_book_is_not_an_arbitrage():
    m = _markets(senate=(0.70, 0.71, 0.40, 0.41))
    m = AggregateMarkets(m.combo, m.house_control,
                         ControlMarket(_q("CONTROLS-2026-D", 0.70, 0.71, size=0.5),
                                       _q("CONTROLS-2026-R", 0.40, 0.41)),
                         None, None, None)
    check = _by_name(run_checks(m, NO_FEES))["Senate control: D + R"]
    assert check.size == 0.5 and check.net_edge > 0
    assert check.verdict == "thin"


def test_resolved_markets_are_not_traded():
    m = _markets()
    m.combo["DD"] = _q("KXBALANCEPOWERCOMBO-27FEB-DD", 0.60, 0.61, status="finalized", result="yes")
    checks = run_checks(m, NO_FEES)
    assert checks
    # No basket trades the resolved market; a {DD} exposure, if any, comes from
    # another open contract that also pays in DD, and payouts stay enumerated.
    assert all(leg.ticker != f"{COMBO}-DD" for c in checks for leg in c.legs)
    buy_all = _by_name(checks)["combo: buy all four legs"]
    assert buy_all.payout_in("DD") >= 1


def test_seat_vs_control_checks_are_not_exact(aggregate_client):
    checks = run_checks(fetch_markets(aggregate_client), NO_FEES)
    seat_vs_control = [c for c in checks if c.kind == "seats"]
    assert seat_vs_control and all(not c.exact for c in seat_vs_control)
    assert all(c.exact for c in checks if c.name.endswith("all buckets"))


def test_missing_quote_skips_the_check():
    m = _markets()
    m.combo["DD"] = Quote("KXBALANCEPOWERCOMBO-27FEB-DD", 0.60, None, bid_size=1_000.0)
    checks = _by_name(run_checks(m, NO_FEES))
    # DD has no ask, so no basket buys DD YES ...
    assert not any(leg.ticker == f"{COMBO}-DD" and leg.side == "yes"
                   for c in checks.values() for leg in c.legs)
    # ... but selling all four still uses the DD bid, which exists.
    sell_all = checks["combo: sell all four legs"]
    assert any(leg.ticker == f"{COMBO}-DD" and leg.side == "no" for leg in sell_all.legs)


def test_fee_schedule_per_series_is_applied():
    cheap = {"DD": (0.55, 0.56), "DR": (0.24, 0.25), "RD": (0.01, 0.02), "RR": (0.06, 0.07)}
    default = _by_name(run_checks(_markets(cheap), NO_FEES))["combo: buy all four legs"]
    doubled = _by_name(
        run_checks(_markets(cheap), {"KXBALANCEPOWERCOMBO": FeeSchedule("quadratic", 2.0)})
    )["combo: buy all four legs"]
    assert doubled.fees == pytest.approx(2 * default.fees, abs=D("0.0004"))
