"""Model-free checks: enumerated payouts, arbitrage detection, fees."""

from __future__ import annotations

import pytest

from strategy.checks import run_checks
from strategy.fees import FeeSchedule
from strategy.markets import AggregateMarkets, ControlMarket, Quote, fetch_markets

NO_FEES = {}  # every series falls back to the default quadratic schedule


def _q(ticker, bid, ask, size=1_000.0):
    return Quote(ticker, bid, ask, bid_size=size, ask_size=size)


def _markets(combo=None, house=(0.88, 0.89, 0.11, 0.12), senate=(0.62, 0.63, 0.37, 0.38)):
    combo = combo or {"DD": (0.60, 0.61), "DR": (0.27, 0.28), "RD": (0.01, 0.02), "RR": (0.09, 0.10)}
    return AggregateMarkets(
        combo={k: _q(f"KXBALANCEPOWERCOMBO-27FEB-{k}", b, a) for k, (b, a) in combo.items()},
        house_control=ControlMarket(_q("CONTROLH-2026-D", *house[:2]), _q("CONTROLH-2026-R", *house[2:])),
        senate_control=ControlMarket(_q("CONTROLS-2026-D", *senate[:2]), _q("CONTROLS-2026-R", *senate[2:])),
        same_party=None,
        house_seats=None,
        senate_seats=None,
    )


def _by_name(checks):
    return {c.name: c for c in checks}


def test_consistent_book_has_no_arbitrage(aggregate_client):
    m = fetch_markets(aggregate_client)
    checks = run_checks(m, NO_FEES)
    assert checks
    assert all(c.verdict != "ARB" for c in checks)


def test_payouts_are_enumerated_not_assumed(aggregate_client):
    checks = _by_name(run_checks(fetch_markets(aggregate_client), NO_FEES))
    assert checks["combo: buy all four legs"].payout == 1
    assert checks["combo: sell all four legs"].payout == 3  # four NOs: three always pay
    assert checks["DD + DR vs House-D (short)"].payout == 1
    assert checks["DD >= House-D + Senate-D - 1"].payout == 1
    assert checks["House seats: sell all buckets"].payout == 3  # four buckets
    assert checks["Senate seats: sell all buckets"].payout == 4  # five buckets


def test_underpriced_combo_is_an_arbitrage():
    cheap = {"DD": (0.55, 0.56), "DR": (0.24, 0.25), "RD": (0.01, 0.02), "RR": (0.06, 0.07)}
    check = _by_name(run_checks(_markets(cheap), NO_FEES))["combo: buy all four legs"]
    assert check.cost == pytest.approx(0.90)
    assert check.raw_edge == pytest.approx(0.10)
    assert check.net_edge == pytest.approx(0.10 - check.fees)
    assert check.fees > 0
    assert check.verdict == "ARB"


def test_fees_can_erase_a_raw_edge():
    near = {"DD": (0.60, 0.605), "DR": (0.27, 0.275), "RD": (0.01, 0.015), "RR": (0.09, 0.10)}
    check = _by_name(run_checks(_markets(near), NO_FEES))["combo: buy all four legs"]
    assert check.raw_edge == pytest.approx(0.005)
    assert check.net_edge < 0
    assert check.verdict == "fees"


def test_marginal_identity_violation():
    # DD + DR asks = 0.80, but House-D bids 0.90: buy DD, DR, and House-R.
    combo = {"DD": (0.49, 0.50), "DR": (0.29, 0.30), "RD": (0.05, 0.06), "RR": (0.14, 0.15)}
    check = _by_name(run_checks(_markets(combo, house=(0.90, 0.91, 0.09, 0.10)), NO_FEES))[
        "DD + DR vs House-D (short)"
    ]
    assert check.payout == 1
    assert check.cost == pytest.approx(0.50 + 0.30 + 0.10)
    assert check.verdict == "ARB"


def test_frechet_lower_bound():
    # House-D >= 0.90 and Senate-D >= 0.80 force DD >= 0.70; DD offered at 0.60.
    combo = {"DD": (0.59, 0.60), "DR": (0.20, 0.21), "RD": (0.10, 0.11), "RR": (0.05, 0.06)}
    checks = run_checks(_markets(combo, house=(0.90, 0.91, 0.09, 0.10), senate=(0.80, 0.81, 0.19, 0.20)), NO_FEES)
    check = _by_name(checks)["DD >= House-D + Senate-D - 1"]
    assert check.cost == pytest.approx(0.60 + 0.10 + 0.20)
    assert check.raw_edge == pytest.approx(0.10)
    assert check.verdict == "ARB"


def test_cheapest_way_to_hold_an_exposure_is_used():
    # "House goes R" costs 0.15 as House-R YES, but 0.10 as House-D NO (bid 0.90).
    checks = run_checks(_markets(house=(0.90, 0.91, 0.08, 0.15)), NO_FEES)
    leg = _by_name(checks)["DD + DR vs House-D (short)"].legs[-1]
    assert (leg.ticker, leg.side, leg.price) == ("CONTROLH-2026-D", "no", pytest.approx(0.10))


def test_seat_vs_control_checks_are_not_exact(aggregate_client):
    checks = run_checks(fetch_markets(aggregate_client), NO_FEES)
    seat_vs_control = [c for c in checks if c.kind == "seats"]
    assert seat_vs_control and all(not c.exact for c in seat_vs_control)
    assert all(c.exact for c in checks if c.name.endswith("all buckets"))


def test_missing_quote_skips_the_check():
    m = _markets()
    m.combo["DD"] = Quote("KXBALANCEPOWERCOMBO-27FEB-DD", 0.60, None, bid_size=1_000.0)
    names = _by_name(run_checks(m, NO_FEES))
    assert "combo: buy all four legs" not in names
    assert "combo: sell all four legs" in names  # uses the DD bid, which exists


def test_fee_schedule_per_series_is_applied():
    cheap = {"DD": (0.55, 0.56), "DR": (0.24, 0.25), "RD": (0.01, 0.02), "RR": (0.06, 0.07)}
    default = _by_name(run_checks(_markets(cheap), NO_FEES))["combo: buy all four legs"]
    doubled = _by_name(
        run_checks(_markets(cheap), {"KXBALANCEPOWERCOMBO": FeeSchedule("quadratic", 2.0)})
    )["combo: buy all four legs"]
    # Fees round up to the cent per 100-contract order, so doubling the rate can
    # land up to 0.01 cent per contract off exactly double on each of four legs.
    assert doubled.fees == pytest.approx(2 * default.fees, abs=4e-4)


def test_arbitrage_needs_a_full_set_at_the_quoted_prices():
    cheap = {"DD": (0.55, 0.56), "DR": (0.24, 0.25), "RD": (0.01, 0.02), "RR": (0.06, 0.07)}
    m = _markets(cheap)
    m.combo["RD"] = _q("KXBALANCEPOWERCOMBO-27FEB-RD", 0.01, 0.02, size=0.5)
    check = _by_name(run_checks(m, NO_FEES))["combo: buy all four legs"]
    assert check.size == 0.5
    assert check.net_edge > 0
    assert check.verdict == "thin"


def test_fees_are_charged_on_the_executable_order():
    cheap = {"DD": (0.55, 0.56), "DR": (0.24, 0.25), "RD": (0.01, 0.02), "RR": (0.06, 0.07)}
    deep = _by_name(run_checks(_markets(cheap), NO_FEES))["combo: buy all four legs"]
    m = _markets(cheap)
    m.combo["DD"] = _q("KXBALANCEPOWERCOMBO-27FEB-DD", 0.55, 0.56, size=1.0)
    one_set = _by_name(run_checks(m, NO_FEES))["combo: buy all four legs"]
    assert (deep.order, one_set.order) == (100, 1)
    # One contract per order: each leg's fee rounds up to the next cent.
    # 0.07*.56*.44 = 1.72c -> 2c, .25 -> 1.31c -> 2c, .02 -> 0.14c -> 1c, .07 -> 0.46c -> 1c.
    assert one_set.fees == pytest.approx(0.02 + 0.02 + 0.01 + 0.01)
    assert one_set.fees > deep.fees
