from decimal import Decimal as D

from sum_check import Offer, best, price, taker_fee


def kalshi(role, asks, mult="1"):
    return Offer(role, "kalshi", "K-" + role, tuple((D(p), D(q)) for p, q in asks), D(mult))


def book(role, p, stake="500"):
    return Offer(role, "draftkings", "DK-" + role, ((D(p), D(stake) / D(p)),))


def run(offers, **kw):
    args = dict(days=0.25, risk_free=0.05, max_sets=500, max_capital=D("1000"), min_net=D("0.10"), min_edge=D("0.01"))
    args.update(kw)
    return best(offers, **args)


def test_fee_matches_the_paper_adapter():
    assert taker_fee([(D("0.70"), D("100"))], D("1")) == D("1.47")
    assert taker_fee([(D("0.70"), D("1"))], D("1")) == D("0.02")  # 0.0147 rounded up


def test_cross_venue_basket_found_and_all_kalshi_excluded():
    offers = {"home": [kalshi("home", [("0.70", "100")]), book("home", "0.80")],
              "away": [kalshi("away", [("0.25", "100")]), book("away", "0.25")]}
    basket, why = run(offers)
    # All-Kalshi (0.70 + 0.25) is kalshi_sum_check's trade; the cheapest mixed basket
    # is Kalshi home + DraftKings away, which has no fee on the bet.
    assert why == "ok" and basket is not None
    assert {(l["venue"], l["role"]) for l in basket.legs} == {("kalshi", "home"), ("draftkings", "away")}
    assert basket.sets == 100 and basket.cost == D("95") and basket.fees == D("1.47")
    assert basket.net == D("3.53") and basket.edge == D("0.0353")


def test_no_basket_when_the_margin_eats_the_gap():
    offers = {"home": [kalshi("home", [("0.76", "100")]), book("home", "0.80")],
              "away": [kalshi("away", [("0.30", "100")]), book("away", "0.25")]}
    assert run(offers) == (None, "no_edge")


def test_sizing_stops_where_the_walked_book_kills_the_edge():
    offers = {"home": [kalshi("home", [("0.70", "40"), ("0.74", "200")])],
              "away": [book("away", "0.25")]}
    basket, _ = run(offers)
    assert basket is not None and basket.sets == 40  # past 40, the 0.74 level leaves less than the minimum edge


def test_capital_and_stake_limits_cap_the_size():
    offers = {"home": [kalshi("home", [("0.70", "1000")])], "away": [book("away", "0.25", stake="10")]}
    basket, _ = run(offers)
    assert basket is not None and basket.sets == 40  # a $10 stake buys 40 contracts at 0.25
    offers = {"home": [kalshi("home", [("0.70", "1000")])], "away": [book("away", "0.25")]}
    basket, _ = run(offers, max_capital=D("20"))
    assert basket is not None and basket.capital <= 20 and basket.sets == 20


def test_price_records_limits_and_levels():
    b = price((kalshi("home", [("0.70", "10"), ("0.71", "10")]), book("away", "0.25")), 15, 1.0, 0.05)
    assert b is not None
    home = b.legs[0]
    assert home["limit"] == "0.71" and home["levels"] == [["0.70", "10"], ["0.71", "5"]]
    assert b.legs[1]["fee"] == "0"
