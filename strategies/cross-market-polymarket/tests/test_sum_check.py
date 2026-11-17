from decimal import Decimal as D

from czquant.polymarket import kalshi_layout
from sum_check import best, kalshi_offer, leg_fee, polymarket_offer, price

SPORTS = {"fees_enabled": True, "rate": "0.05", "exponent": 1}


def kalshi_book(ask, depth="100"):
    """A Kalshi book with a YES ask at `ask` (a NO bid at 1 - ask)."""
    return {"orderbook_fp": {"yes_dollars": [], "no_dollars": [[str(1 - D(ask)), depth]]}}


def poly_book(ask, depth="100"):
    return kalshi_layout({"bids": [], "asks": [{"price": ask, "size": depth}]})


def offers(k_bos, p_bos, k_sac, p_sac, depth="100"):
    return {"BOS": [kalshi_offer("BOS", "K-BOS", kalshi_book(k_bos, depth), D(1)),
                    polymarket_offer("BOS", "PM:bos", poly_book(p_bos, depth), SPORTS)],
            "SAC": [kalshi_offer("SAC", "K-SAC", kalshi_book(k_sac, depth), D(1)),
                    polymarket_offer("SAC", "PM:sac", poly_book(p_sac, depth), SPORTS)]}


def test_each_venue_charges_its_own_fee():
    k, p = offers("0.60", "0.60", "0.40", "0.40")["BOS"]
    fills = [(D("0.60"), D("100"))]
    assert leg_fee(k, fills) == D("1.68")  # 0.07 x 100 x 0.6 x 0.4, rounded up to the cent
    assert leg_fee(p, fills) == D("1.2")  # 0.05 x 100 x 0.6 x 0.4, to five decimals


def test_a_cross_venue_basket_below_a_dollar_after_fees_is_found():
    # Kalshi has Boston cheap; Polymarket has Sacramento cheap. 0.55 + 0.38 = 0.93 before fees.
    basket, why = best(offers("0.55", "0.62", "0.45", "0.38"), days=0.5, risk_free=0.05, max_sets=100,
                       max_capital=D("1000"), min_net=D("0.25"), min_edge=D("0.01"))
    assert why == "ok" and basket is not None and basket.sets == 100
    assert {(leg["venue"], leg["ticker"]) for leg in basket.legs} == {("kalshi", "K-BOS"), ("polymarket", "PM:sac")}
    fees = D("1.74") + D("1.178")  # Kalshi 0.07 x 100 x .55 x .45 = 1.7325 -> 1.74; Polymarket 0.05 x 100 x .38 x .62
    assert basket.cost == D("93") and basket.fees == fees and basket.net == 100 - 93 - fees


def test_one_venue_baskets_are_left_to_the_other_strategies_unless_asked():
    # Both legs are cheapest on Polymarket: no cross-venue basket clears a dollar.
    cheap = offers("0.70", "0.50", "0.70", "0.45")
    assert best(cheap, 0.5, 0.05, 100, D("1000"), D("0.25"), D("0.01"))[0] is None
    basket, why = best(cheap, 0.5, 0.05, 100, D("1000"), D("0.25"), D("0.01"), require_cross=False)
    assert why == "ok" and basket is not None and {leg["venue"] for leg in basket.legs} == {"polymarket"}


def test_no_edge_after_fees_is_rejected_with_a_reason():
    basket, why = best(offers("0.51", "0.52", "0.48", "0.49"), 0.5, 0.05, 100, D("1000"), D("0.25"), D("0.01"))
    assert basket is None and why == "no_edge"


def test_size_stops_at_the_thinner_book_and_the_capital_cap():
    o = offers("0.55", "0.62", "0.45", "0.38", depth="40")
    basket = price((o["BOS"][0], o["SAC"][1]), 40, 0.5, 0.05)
    assert basket is not None and price((o["BOS"][0], o["SAC"][1]), 41, 0.5, 0.05) is None
    capped, _ = best(o, 0.5, 0.05, 100, D("20"), D("0.25"), D("0.01"))
    assert capped is not None and capped.capital <= 20
