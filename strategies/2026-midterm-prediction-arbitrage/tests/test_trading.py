"""Paper trading: quantity-capped edges, sizing caps, confirmation, settlement."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from strategy.checks import run_checks
from strategy.fees import FeeSchedule
from strategy.ledger import Run, read_events
from strategy.markets import AggregateMarkets, ControlMarket, Quote
from strategy.output import ev_after_fees
from strategy.pnl import positions_from_events
from strategy.trading import (
    Candidate,
    ModelView,
    Policy,
    arbitrage_candidates,
    model_candidates,
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


def _cand(price="0.50", depth=1000.0, value="0.70"):
    from strategy.checks import STATES, Leg

    leg = Leg(f"{COMBO}-DD", "yes", D(price), depth, FeeSchedule(), frozenset({"DD"}), COMBO)
    pays = {s: int(s == "DD") for s in STATES}
    return Candidate("model:buy DD", "model", (leg,), pays, D(value), D("0.01"))


def test_size_is_capped_by_depth_cash_and_the_position_limit():
    c = _cand()
    depth = {(f"{COMBO}-DD", "yes"): 50.0}
    assert size(c, depth, D("1000"), Policy())[0] == 50  # depth
    depth = {(f"{COMBO}-DD", "yes"): 10_000.0}
    n, _ = size(c, depth, D("20"), Policy())  # cash
    assert c.outlay(n) <= D("20") < c.outlay(n + 1)
    n, _ = size(c, depth, D("1000"), Policy(max_position=D("100")))  # position cap
    assert c.outlay(n) <= D("100") < c.outlay(n + 1)


def test_size_rejects_with_a_reason():
    depth = {(f"{COMBO}-DD", "yes"): 0.4}
    assert size(_cand(), depth, D("1000"), Policy()) == (0, "no depth left at the confirmed prices")
    depth = {(f"{COMBO}-DD", "yes"): 100.0}
    n, reason = size(_cand(), depth, D("0.10"), Policy())
    assert n == 0 and "one set needs" in reason
    n, reason = size(_cand(value="0.51"), depth, D("1000"), Policy())
    assert n == 0 and "does not exceed threshold" in reason


def test_only_guaranteed_arbitrage_is_unblocked():
    same = {**BOOK, f"{COMBO}-DR": (0.24, 0.25, 500.0)}
    m = markets(same)
    m = AggregateMarkets(m.combo, m.house_control, m.senate_control,
                         Quote("KXSAMEPARTYCONGRESS-27FEB01", 0.58, 0.60, bid_size=500, ask_size=500),
                         None, None)
    cands = {c.basket: c for c in arbitrage_candidates(run_checks(m, {}))}
    assert cands["arbitrage:DD + RR vs same-party (long)"].blocked.startswith("conditional")


def _trade(tmp_path, book=None, confirm_book=None, model=MODEL, policy=Policy(), clock=None):
    run = Run.start(tmp_path, "s", {}, clock or (lambda: T0))
    m = markets(book)
    trade(run, BookClient(confirm_book or book or BOOK), m, run_checks(m, {}), model, {}, policy, 100)
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
