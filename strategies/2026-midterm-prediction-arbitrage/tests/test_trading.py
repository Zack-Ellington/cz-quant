"""Paper trading: quantity-capped edges, Kelly sizing within caps, confirmation,
settlement, and the valuation of the open book under three views."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from strategy.checks import run_checks
from strategy.fees import FeeSchedule
from strategy.checks import STATES, Leg
from strategy.ledger import Run, read_events
from strategy.markets import AggregateMarkets, ControlMarket, Quote
from strategy.output import ev_after_fees
from strategy.pnl import positions_from_events
from strategy.trading import (
    Candidate,
    ModelView,
    Policy,
    arbitrage_candidates,
    kelly_probs,
    kelly_quantity,
    market_state_probs,
    model_candidates,
    record_scan,
    settle,
    size,
    trade,
)

T0 = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
COMBO = "KXBALANCEPOWERCOMBO-27FEB"
MODEL = ModelView("test model", {"DD": 0.70, "DR": 0.20, "RD": 0.02, "RR": 0.08},
                  {"DD": 0.0, "DR": 0.0, "RD": 0.0, "RR": 0.0})

# ticker -> (bid, ask, size)
BOOK = {
    f"{COMBO}-DD": (0.59, 0.60, 500.0),
    f"{COMBO}-DR": (0.27, 0.28, 500.0),
    f"{COMBO}-RD": (0.01, 0.02, 500.0),
    f"{COMBO}-RR": (0.09, 0.10, 500.0),
    "CONTROLH-2026-D": (0.88, 0.89, 500.0),
    "CONTROLH-2026-R": (0.11, 0.12, 500.0),
    "CONTROLS-2026-D": (0.62, 0.63, 500.0),
    "CONTROLS-2026-R": (0.37, 0.38, 500.0),
}


def quote(ticker, spec=None, **kw):
    bid, ask, n = (spec or BOOK)[ticker]
    return Quote(ticker, bid, ask, bid_size=n, ask_size=n, event_ticker=ticker.rsplit("-", 1)[0], **kw)


def markets(book=None, **kw):
    book = book or BOOK
    return AggregateMarkets(
        combo={c: quote(f"{COMBO}-{c}", book, **kw) for c in ("DD", "DR", "RD", "RR")},
        house_control=ControlMarket(quote("CONTROLH-2026-D", book, **kw), quote("CONTROLH-2026-R", book, **kw)),
        senate_control=ControlMarket(quote("CONTROLS-2026-D", book, **kw), quote("CONTROLS-2026-R", book, **kw)),
        same_party=None, house_seats=None, senate_seats=None,
    )


class BookClient:
    """Serves the confirmation scan's order books."""

    def __init__(self, book):
        self.book = book

    def fetch_orderbook(self, ticker):
        bid, ask, n = self.book[ticker]
        return {"yes": [[bid, n]], "no": [[round(1 - ask, 4), n]]}

    def fetch_event(self, ticker):
        return None

    def fetch_series(self, ticker):
        return None


def test_the_reviews_one_contract_example_is_not_a_trade():
    # Model 0.518, one contract offered at 0.50: the one-contract fee is 2c, so
    # EV is 0.518 - 0.50 - 0.02 = -0.2c. At 100 contracts it would print +0.1c.
    one = Quote("X-1", None, 0.50, ask_size=1.0)
    assert ev_after_fees(0.518, one, FeeSchedule(), contracts=100) is None
    deep = Quote("X-1", None, 0.50, ask_size=100.0)
    action, ev, n = ev_after_fees(0.518, deep, FeeSchedule(), contracts=100)
    assert (action, n) == ("buy", 100) and ev == D("0.518") - D("0.50") - D("1.75") / 100


def test_model_candidates_use_the_executable_quantity():
    book = {**BOOK, f"{COMBO}-DD": (0.49, 0.50, 1.0)}
    m = markets(book)
    view = ModelView("m", {"DD": 0.518, "DR": 0.3, "RD": 0.02, "RR": 0.162}, {})
    assert not [c for c in model_candidates(m, view, {}, Policy(), 100) if c.basket == "model:buy DD"]


def test_model_edge_is_value_minus_cost_minus_fees():
    cands = {c.basket: c for c in model_candidates(markets(), MODEL, {}, Policy(), 100)}
    buy_dd = cands["model:buy DD"]
    assert buy_dd.value == D("0.7") and buy_dd.cost == D("0.60")
    assert buy_dd.edge(100) == D("0.7") - D("0.60") - FeeSchedule().fee("0.60", 100) / 100
    assert buy_dd.threshold == Policy().min_edge


def _cand(price="0.50", depth=1000.0, value="0.70", schedule=FeeSchedule(), uncertainty="0"):
    leg = Leg(f"{COMBO}-DD", "yes", D(price), depth, schedule, frozenset({"DD"}), COMBO)
    pays = {s: int(s == "DD") for s in STATES}
    return Candidate("model:buy DD", "model", (leg,), pays, D(value), D("0.01"), uncertainty=D(uncertainty))


def test_size_is_capped_by_depth_cash_and_the_position_limit():
    c = _cand()
    depth = {(f"{COMBO}-DD", "yes"): 50.0}
    assert size(c, depth, D("1000"), Policy())[0] == 50  # depth
    depth = {(f"{COMBO}-DD", "yes"): 10_000.0}
    n = size(c, depth, D("20"), Policy()).quantity  # cash
    assert c.outlay(n) <= D("20") < c.outlay(n + 1)
    n = size(c, depth, D("1000"), Policy(max_position=D("100"))).quantity  # position cap
    assert c.outlay(n) <= D("100") < c.outlay(n + 1)


def test_size_rejects_with_a_reason():
    depth = {(f"{COMBO}-DD", "yes"): 0.4}
    rejected = size(_cand(), depth, D("1000"), Policy())
    assert (rejected.quantity, rejected.reason) == (0, "no depth left at the confirmed prices")
    depth = {(f"{COMBO}-DD", "yes"): 100.0}
    n, reason, *_ = size(_cand(), depth, D("0.10"), Policy())
    assert n == 0 and "one set needs" in reason
    n, reason, *_ = size(_cand(value="0.51"), depth, D("1000"), Policy())
    assert n == 0 and "does not exceed threshold" in reason


# --- Fractional Kelly ------------------------------------------------------------------
# No fees, a 50c contract, P(DD) = 0.7: the bet doubles the stake or loses it.

FREE = FeeSchedule(multiplier=0.0)
P70 = {"DD": 0.7, "DR": 0.2, "RD": 0.02, "RR": 0.08}


def test_full_kelly_matches_the_binary_formula():
    # f* = (p - c) / (1 - c) = (0.7 - 0.5) / 0.5 = 40% of $1,000 = $400 = 800 contracts.
    q = kelly_quantity(_cand(schedule=FREE), 2000, D("1000"), None, P70, D("1"))
    assert abs(q - 800) <= 1


def test_fractional_kelly_is_the_crra_optimum():
    # Risk aversion 4: p (1 + x)^-4 = (1 - p) (1 - x)^-4, so (1 + x) / (1 - x) = (7/3)^(1/4).
    r = (0.7 / 0.3) ** 0.25
    stake = (r - 1) / (r + 1) * 1000  # $105.49, close to a quarter of Kelly's $400
    q = kelly_quantity(_cand(schedule=FREE), 2000, D("1000"), None, P70, D("0.25"))
    assert abs(q * 0.5 - stake) <= 0.5


def test_kelly_uses_the_edge_left_after_the_uncertainty():
    # Uncertainty 0.1 shades P(DD) to 0.6: f* = (0.6 - 0.5) / 0.5 = 20%, 400 contracts.
    cand = _cand(schedule=FREE, uncertainty="0.1")
    shaded = kelly_probs(cand, P70)
    assert shaded["DD"] == pytest.approx(0.6) and sum(shaded.values()) == pytest.approx(1.0)
    assert shaded["DR"] / shaded["RR"] == pytest.approx(0.2 / 0.08)  # losers keep their ratios
    assert abs(kelly_quantity(cand, 2000, D("1000"), None, shaded, D("1")) - 400) <= 1


def test_uncertainty_on_an_outcome_the_model_calls_impossible():
    # Selling RR when the model says P(RR) = 0: the shaded mass goes to RR itself.
    leg = Leg(f"{COMBO}-RR", "no", D("0.90"), 100.0, FREE, frozenset(STATES) - {"RR"}, COMBO)
    pays = {s: int(s != "RR") for s in STATES}
    cand = Candidate("model:sell RR", "model", (leg,), pays, D("1"), D("0.01"), uncertainty=D("0.05"))
    shaded = kelly_probs(cand, {"DD": 0.7, "DR": 0.3})
    assert shaded["RR"] == pytest.approx(0.05)
    assert shaded["DD"] + shaded["DR"] == pytest.approx(0.95)


def test_a_guaranteed_basket_is_sized_by_the_caps_alone():
    leg = Leg("CONTROLS-2026-D", "no", D("0.45"), 5000.0, FREE, frozenset(STATES), "CONTROLS-2026")
    other = Leg("CONTROLS-2026-R", "no", D("0.45"), 5000.0, FREE, frozenset(STATES), "CONTROLS-2026")
    cand = Candidate("arbitrage:x", "arbitrage", (leg, other), {s: 1 for s in STATES}, D("1"), D("0"))
    depth = {("CONTROLS-2026-D", "no"): 5000.0, ("CONTROLS-2026-R", "no"): 5000.0}
    sizing = size(cand, depth, D("1000"), Policy(), P70)
    assert sizing.quantity == sizing.cap == 111 and sizing.limit == "max position"  # $99.90 of $100


def test_an_open_position_on_the_same_outcome_shrinks_the_stake():
    cand = _cand(schedule=FREE)
    alone = kelly_quantity(cand, 2000, D("1000"), None, P70, D("0.25"))
    book = {s: D(200 if s == "DD" else 0) for s in STATES}  # already long 200 DD
    assert kelly_quantity(cand, 2000, D("800"), book, P70, D("0.25")) < alone


def test_size_reports_what_set_the_quantity():
    depth = {(f"{COMBO}-DD", "yes"): 10_000.0}
    rich = Policy(max_position=D("10000"))
    sizing = size(_cand(schedule=FREE), depth, D("1000"), rich, P70)
    assert sizing.limit == "kelly" and sizing.quantity == sizing.kelly < sizing.cap
    capped = size(_cand(schedule=FREE), depth, D("1000"), Policy(), P70)  # Kelly wants $105
    assert capped.limit == "max position" and capped.quantity == capped.cap == 200


def test_kelly_that_stakes_nothing_is_a_rejection():
    # Uncertainty wipes out the edge: P(DD) 0.7 - 0.25 = 0.45 < 0.50.
    depth = {(f"{COMBO}-DD", "yes"): 1000.0}
    sizing = size(_cand(schedule=FREE, uncertainty="0.25"), depth, D("1000"), Policy(), P70)
    assert sizing.quantity == 0 and "Kelly" in sizing.reason and sizing.kelly == 0


@pytest.mark.parametrize("fraction", ["0", "-0.5", "1.5"])
def test_the_kelly_fraction_must_be_in_zero_one(fraction):
    with pytest.raises(ValueError, match="kelly_fraction"):
        Policy(kelly_fraction=D(fraction))


def test_only_guaranteed_arbitrage_is_unblocked():
    same = {**BOOK, f"{COMBO}-DR": (0.24, 0.25, 500.0)}
    m = markets(same)
    m = AggregateMarkets(m.combo, m.house_control, m.senate_control,
                         Quote("KXSAMEPARTYCONGRESS-27FEB01", 0.58, 0.60, bid_size=500, ask_size=500),
                         None, None)
    cands = {c.basket: c for c in arbitrage_candidates(run_checks(m, {}))}
    assert cands["arbitrage:DD + RR vs same-party (long)"].blocked.startswith("conditional")


def _trade(tmp_path, book=None, confirm_book=None, model=MODEL, policy=Policy(), clock=None, siblings=()):
    run = Run.start(tmp_path, "s", {}, clock or (lambda: T0))
    m = markets(book)
    record_scan(run, m, model, "test book")
    trade(run, BookClient(confirm_book or book or BOOK), m, run_checks(m, {}), model, {}, policy, 100,
          siblings=siblings)
    return run, read_events(tmp_path, "s")


def test_trade_fills_at_the_confirmed_price_with_fees_at_the_filled_quantity(tmp_path):
    run, events = _trade(tmp_path)
    fills = [e for e in events if e["type"] == "fill"]
    dd = next(f for f in fills if f["ticker"] == f"{COMBO}-DD")
    n = dd["quantity"]
    cost = FeeSchedule().order("0.60", n)
    assert D(dd["fee"]) == cost.fee and D(dd["cash_delta"]) == cost.cash_delta
    assert dd["simulated"] is True and dd["mode"] == "paper"
    assert -D(dd["cash_delta"]) <= Policy().max_position
    assert any(e["type"] == "valuation" for e in events)


def test_accepted_signals_record_their_sizing(tmp_path):
    _, events = _trade(tmp_path)
    accepted = [e for e in events if e["type"] == "signal" and e["accepted"]]
    assert accepted
    for e in accepted:
        assert e["sizing"]["limit"] in ("kelly", "depth", "cash", "max position")
        assert e["quantity"] <= e["sizing"]["cap"]


def test_market_probabilities_are_normalized_midpoints():
    probs = market_state_probs(markets())
    mids = {"DD": 0.595, "DR": 0.275, "RD": 0.015, "RR": 0.095}
    for code, mid in mids.items():
        assert probs[code] == pytest.approx(mid / sum(mids.values()))
    assert sum(probs.values()) == pytest.approx(1.0) and probs["OO"] == 0.0


def test_the_open_book_is_valued_under_three_views(tmp_path):
    sibling = Run.start(tmp_path, "t", {}, lambda: T0)
    other = ModelView("sibling model", {"DD": 0.5, "DR": 0.3, "RD": 0.05, "RR": 0.15}, {})
    record_scan(sibling, markets(), other, "test book")
    _, events = _trade(tmp_path, siblings=("t", "never-ran"))
    vals = {e["view"] + ":" + e["model"]: e for e in events if e["type"] == "valuation"}
    assert list(vals) == ["model:test model", "market:the market", "sibling:sibling model of t",
                          "sibling:never-ran"]
    assert vals["sibling:sibling model of t"]["note"] == f"run {sibling.run_id}"
    assert "mean" not in vals["sibling:never-ran"] and "no scan" in vals["sibling:never-ran"]["note"]
    model, market = vals["model:test model"], vals["market:the market"]
    assert model["mean"] > market["mean"]  # the model grades its own trades higher


def test_a_sibling_scan_of_other_quotes_is_labeled(tmp_path):
    sibling = Run.start(tmp_path, "t", {}, lambda: T0)
    record_scan(sibling, markets(), MODEL, "live")
    _, events = _trade(tmp_path, siblings=("t",))
    note = next(e["note"] for e in events if e["type"] == "valuation" and e["view"] == "sibling")
    assert note == f"run {sibling.run_id}, live: not the same quotes"


def test_a_candidate_that_worsens_on_the_confirming_scan_is_rejected(tmp_path):
    worse = {**BOOK, f"{COMBO}-DD": (0.68, 0.69, 500.0)}
    _, events = _trade(tmp_path, confirm_book=worse)
    rejected = {e["basket"]: e["reason"] for e in events if e["type"] == "signal" and not e["accepted"]}
    assert "does not exceed threshold" in rejected["model:buy DD"]


def test_one_open_position_per_basket(tmp_path):
    _trade(tmp_path)
    _, events = _trade(tmp_path)  # a second run, same prices
    reasons = [e["reason"] for e in events if e["type"] == "signal" and not e["accepted"]]
    assert any(r.startswith("already open as") for r in reasons)


def test_overlapping_baskets_do_not_reuse_depth(tmp_path):
    # Two model trades on different legs plus depth of 30 each: each fill is
    # capped by the depth left after the previous fills of the same leg.
    thin = {k: (b, a, 30.0) for k, (b, a, _) in BOOK.items()}
    _, events = _trade(tmp_path, book=thin, policy=Policy(max_position=D("1000")))
    used = {}
    for e in events:
        if e["type"] == "fill":
            key = (e["ticker"], e["side"])
            used[key] = used.get(key, 0) + e["quantity"]
    assert used and all(q <= 30 for q in used.values())


def test_settlement_pays_one_dollar_per_winning_contract(tmp_path):
    run, events = _trade(tmp_path)
    opened = positions_from_events(events)
    resolved = markets(status="finalized")
    for code, q in resolved.combo.items():
        resolved.combo[code] = Quote(q.ticker, q.bid, q.ask, status="finalized",
                                     result="yes" if code == "DD" else "no")
    later = Run.start(tmp_path, "s", {}, lambda: T0.replace(month=11))
    settle(later, resolved, BookClient(BOOK))
    settled = {e["position_id"]: e for e in read_events(tmp_path, "s") if e["type"] == "settlement"}
    for pid, pos in opened.items():
        winning = sum(int(f["quantity"]) for f in pos.legs
                      if {"yes": "yes", "no": "no"}[f["side"]] == ("yes" if f["ticker"].endswith("-DD") else "no"))
        assert D(settled[pid]["receipt"]) == winning
        assert D(settled[pid]["pnl"]) == winning - pos.entry_capital


def test_unresolved_positions_stay_open(tmp_path):
    _trade(tmp_path)
    later = Run.start(tmp_path, "s", {}, lambda: T0.replace(month=10))
    settle(later, markets(), BookClient(BOOK))
    assert not [e for e in read_events(tmp_path, "s") if e["type"] == "settlement"]


@pytest.mark.parametrize("policy", [Policy(bankroll=D("0")), Policy(max_position=D("0.01"))])
def test_no_cash_no_fills(tmp_path, policy):
    _, events = _trade(tmp_path, policy=policy)
    assert not [e for e in events if e["type"] == "fill"]
