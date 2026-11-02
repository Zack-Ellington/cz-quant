"""Live execution against a fake Kalshi: limits, fills read from the venue, unwinds, halts."""

from __future__ import annotations

import itertools
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from czquant import Broker, Store

import strategy.trading as trading
from strategy import live
from strategy.checks import run_checks
from strategy.ledger import Run, read_events
from strategy.trading import Policy, record_scan, trade
from tests.test_trading import BOOK, MODEL, T0, BookClient, markets


class Venue:
    """Kalshi as the live broker sees it. `fills` scripts each order's fill count."""

    def __init__(self, fills=None, fail=False):
        self.orders, self.fills, self.fail = [], list(fills or []), fail
        self.ids = itertools.count(1)
        self.key_id = "key"

    def create_order(self, order):
        if self.fail:
            raise RuntimeError("connection reset")
        self.orders.append(order)
        n = self.fills.pop(0) if self.fills else order["count"]
        return {"order": {"order_id": f"v{next(self.ids)}", "status": "executed" if str(n) == order["count"] else "canceled",
                          "fill_count_fp": str(n)}}

    def get(self, path, params=None):
        raise AssertionError("terminal orders need no polling")


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_RUNS_DIR", str(tmp_path / "common"))
    monkeypatch.setenv("CZQUANT_PORTFOLIO", "election-live")
    store = Store(tmp_path / "common")
    store.create("election-live", mode="live", subaccount=3)
    store.append("transaction", {"reason": "funding", "cash_delta": "500", "external_delta": "500", "fee": "0",
                                 "legs": []}, "election-live")
    return store


def run_live(tmp_path, monkeypatch, venue, limits):
    monkeypatch.setattr(trading, "Kalshi", lambda **kwargs: venue)
    run = Run.start(tmp_path / "diag", "s", {}, lambda: T0)
    m = markets()
    record_scan(run, m, MODEL, "test book")
    trade(run, BookClient(BOOK), m, run_checks(m, {}), MODEL, {}, Policy(), 100, live=limits)
    return read_events(tmp_path / "diag", "s")


def test_a_dry_run_logs_orders_and_sends_none(tmp_path, monkeypatch, account):
    venue = Venue()
    events = run_live(tmp_path, monkeypatch, venue, live.LiveLimits(D("50")))
    logged = [e for e in events if e["type"] == "live_order"]
    assert logged and all(e["dry_run"] for e in logged) and venue.orders == []
    assert account.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_orders_stay_inside_the_spend_cap_and_are_never_sent_twice(tmp_path, monkeypatch, account):
    venue = Venue()
    limits = live.LiveLimits(D("50"), send_orders=True)
    events = run_live(tmp_path, monkeypatch, venue, limits)
    assert venue.orders and limits.spent <= D("50")
    assert all(o["subaccount"] == 3 for o in venue.orders)  # the broker pins the mapped subaccount
    accepted = {e["basket"] for e in events if e["type"] == "signal" and e.get("accepted")}
    # The monitor hasn't reconciled these fills yet, so the baskets still count as open.
    again = Venue()
    events = run_live(tmp_path, monkeypatch, again, live.LiveLimits(D("50"), send_orders=True))
    second = [e for e in events if e["type"] == "signal" and e["run_id"] == events[-1]["run_id"]]
    assert not {e["basket"] for e in second if e.get("accepted")} & accepted  # no basket re-entered
    assert all(str(e["reason"]).startswith("already open") for e in second if e["basket"] in accepted)
    assert set(live.open_baskets(Broker(account, "election-live"))) >= accepted


def leg(ticker, side="yes", price="0.40"):
    return SimpleNamespace(ticker=ticker, side=side, price=D(price))


class Recorder:
    def __init__(self):
        self.events = []

    def event(self, kind, *args, **fields):
        self.events.append({"type": kind, **fields})


def test_later_legs_follow_the_smallest_fill_and_extras_are_sold_back(account):
    venue = Venue(fills=[10, 4, 4, 6])  # A fills 10, B fills 4, C asked 4 gets 4, then the unwind of A sells 6
    broker = Broker(account, "election-live", venue)
    limits = live.LiveLimits(D("100"), send_orders=True)
    got = live.execute(broker, venue, Recorder(), [leg("A"), leg("B", "no", "0.30"), leg("C")], 10, D("11"),
                       {"basket": "b", "position_id": "p"}, limits)
    assert [o["count"] for o in venue.orders] == ["10", "10", "4", "6"]
    unwind = venue.orders[-1]
    assert (unwind["ticker"], unwind["side"], unwind["price"], unwind["reduce_only"]) == ("A", "ask", "0.35", True)
    assert got == {("A", "yes"): 4, ("B", "no"): 4, ("C", "yes"): 4} and limits.halted is None
    assert limits.spent == D("11") * 4 / 10


def test_a_leg_that_fills_nothing_stops_the_basket_and_unwinds_the_rest(account):
    venue = Venue(fills=[10, 0, 10])
    broker = Broker(account, "election-live", venue)
    limits = live.LiveLimits(D("100"), send_orders=True)
    got = live.execute(broker, venue, Recorder(), [leg("A"), leg("B"), leg("C")], 10, D("12"),
                       {"basket": "b", "position_id": "p"}, limits)
    assert [(o["ticker"], o["count"]) for o in venue.orders] == [("A", "10"), ("B", "10"), ("A", "10")]
    assert venue.orders[-1]["reduce_only"] and got[("A", "yes")] == 0 and limits.spent == 0


def test_an_unknown_outcome_halts_this_run_and_the_next(tmp_path, monkeypatch, account):
    venue = Venue(fail=True)
    limits = live.LiveLimits(D("50"), send_orders=True)
    run_live(tmp_path, monkeypatch, venue, limits)
    assert limits.halted and "unknown outcome" in limits.halted
    later = live.LiveLimits(D("50"), send_orders=True)
    events = run_live(tmp_path, monkeypatch, Venue(), later)
    second = [e for e in events if e["type"] == "signal" and e["run_id"] == events[-1]["run_id"]]
    assert later.halted and second and not any(e.get("accepted") for e in second)


def test_an_unsold_extra_halts(account):
    venue = Venue(fills=[10, 2, 3])  # the unwind sells only 3 of 8
    limits = live.LiveLimits(D("100"), send_orders=True)
    live.execute(Broker(account, "election-live", venue), venue, Recorder(), [leg("A"), leg("B")], 10, D("8"),
                 {"basket": "b", "position_id": "p"}, limits)
    assert limits.halted and "5 unhedged YES on A" in limits.halted


def test_the_stop_file_stops_new_baskets(tmp_path, monkeypatch, account):
    stop = tmp_path / "STOP"
    stop.write_text("")
    venue = Venue()
    events = run_live(tmp_path, monkeypatch, venue, live.LiveLimits(D("50"), send_orders=True, stop_file=stop))
    assert venue.orders == [] and any("stop file" in str(e.get("reason")) for e in events)


def test_paper_and_live_portfolios_cannot_be_swapped(tmp_path, monkeypatch, account):
    with pytest.raises(ValueError, match="run with --mode live"):
        monkeypatch.setattr(trading, "Kalshi", lambda **kwargs: Venue())
        run = Run.start(tmp_path / "p", "s", {}, lambda: T0)
        m = markets()
        trade(run, BookClient(BOOK), m, run_checks(m, {}), MODEL, {}, Policy(), 100)
    account.create("election-paper")
    monkeypatch.setenv("CZQUANT_PORTFOLIO", "election-paper")
    with pytest.raises(ValueError, match="map-live"):
        run_live(tmp_path, monkeypatch, Venue(), live.LiveLimits(D("50")))


class Refused(Venue):
    """Kalshi refusing the request outright, as with an unsigned or badly signed order."""

    def create_order(self, order):
        import httpx
        request = httpx.Request("POST", "https://kalshi.test/portfolio/events/orders")
        raise httpx.HTTPStatusError("401 Unauthorized", request=request, response=httpx.Response(401, request=request))


def test_a_rejected_order_stops_the_run_without_a_standing_halt(tmp_path, monkeypatch, account):
    limits = live.LiveLimits(D("50"), send_orders=True)
    run_live(tmp_path, monkeypatch, Refused(), limits)
    assert limits.halted is None and "rejected" in str(limits.stopped_run)
    assert live.standing_halt(Broker(account, "election-live")) is None
    assert [tuple(r) for r in account.db.execute("SELECT status FROM orders")] == [("rejected",)]


def test_a_rejected_later_leg_unwinds_the_earlier_ones(account):
    class SecondRefused(Venue):
        def create_order(self, order):
            if order["ticker"] == "B":
                return Refused().create_order(order)
            return super().create_order(order)
    venue = SecondRefused()
    limits = live.LiveLimits(D("100"), send_orders=True)
    got = live.execute(Broker(account, "election-live", venue), venue, Recorder(), [leg("A"), leg("B")], 10, D("8"),
                       {"basket": "b", "position_id": "p"}, limits)
    assert [(o["ticker"], o.get("reduce_only", False)) for o in venue.orders] == [("A", False), ("A", True)]
    assert got[("A", "yes")] == 0 and limits.stopped_run and limits.halted is None


def test_sending_orders_needs_a_key(tmp_path, monkeypatch, account):
    venue = Venue()
    venue.key_id = None
    with pytest.raises(ValueError, match="KALSHI_API_KEY_ID"):
        run_live(tmp_path, monkeypatch, venue, live.LiveLimits(D("50"), send_orders=True))
    run_live(tmp_path, monkeypatch, venue, live.LiveLimits(D("50")))  # a dry run needs no key
