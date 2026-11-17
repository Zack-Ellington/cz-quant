from decimal import Decimal

import pytest
from czquant import Broker, Store
from czquant.paper import Paper

from strategy import runner
from strategy.runner import Config, decide

CFG = Config()


def market(ticker, bid, ask, status="active"):
    return {"ticker": ticker, "status": status, "yes_bid_dollars": bid, "yes_ask_dollars": ask}


def event(a=("0.84", "0.86"), b=("0.14", "0.16"), name="KXNCAAWBGAME-26NOV20AAABBB"):
    return {"event_ticker": name, "markets": [market(name + "-AAA", *a), market(name + "-BBB", *b)]}


def test_buys_favorite_in_range():
    d = decide(event(), set(), CFG)
    assert d.accepted and d.ticker.endswith("-AAA") and d.ask == Decimal("0.86")
    assert d.mid == Decimal("0.85") and d.count == 29  # floor(25 / 0.86)


@pytest.mark.parametrize("a,b,reason", [
    (("0.92", "0.94"), ("0.06", "0.08"), "favorite outside price range"),
    (("0.70", "0.72"), ("0.28", "0.30"), "favorite outside price range"),
    (("0.83", "0.87"), ("0.13", "0.17"), "spread too wide"),
    (("0", "0.86"), ("0.14", "0.16"), "no two-sided quote"),
    (("0.84", "1"), ("0.14", "0.16"), "no two-sided quote"),
])
def test_rejections(a, b, reason):
    assert decide(event(a, b), set(), CFG).reason == reason


def test_range_is_inclusive_and_picks_either_side():
    d = decide(event(("0.09", "0.11"), ("0.89", "0.91")), set(), CFG)
    assert d.accepted and d.ticker.endswith("-BBB") and d.mid == Decimal("0.90")


def test_one_order_per_event_and_needs_two_open_markets():
    e = event()
    assert decide(e, {e["event_ticker"]}, CFG).reason == "already ordered on this event"
    e["markets"][1]["status"] = "closed"
    assert decide(e, set(), CFG).reason == "market not open"
    e["markets"].pop()
    assert decide(e, set(), CFG).reason == "1 markets, need 2"


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("WCBB_LOW", "0.75")
    monkeypatch.setenv("WCBB_STAKE", "10")
    c = Config.from_env()
    assert c.low == Decimal("0.75") and c.stake == Decimal("10")
    monkeypatch.setenv("WCBB_LOW", "0.40")
    with pytest.raises(ValueError):
        Config.from_env()


class FakeKalshi:
    """Public reads only: one open game whose favorite asks 0.86 for 100 contracts."""

    def __init__(self):
        self.events = [event()]

    def pages(self, path, key, **params):
        assert path == "/events" and params["series_ticker"] == "KXNCAAWBGAME"
        yield from self.events

    def book(self, ticker):  # YES bids and NO bids; YES ask 0.86 = NO bid 0.14
        return {"orderbook_fp": {"yes_dollars": [["0.84", "100"]], "no_dollars": [["0.14", "100"]]}}

    def market(self, ticker):
        return {"ticker": ticker, "status": "active", "series_ticker": "KXNCAAWBGAME"}

    def get(self, path, params=None):
        return {"series": {"fee_type": "quadratic", "fee_multiplier": "1"}}


def test_paper_fill_on_ledger_and_no_repeat_across_restart(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.create("wcbb", "1000")
    client = FakeKalshi()
    broker = Broker(store, "wcbb", Paper(client, latency=0))
    runner.run(broker=broker, client=client, scans=range(2))

    state = broker.state()
    lots = list(state.lots.values())
    assert len(lots) == 1 and lots[0]["side"] == "yes" and lots[0]["quantity"] == 29
    assert lots[0]["context"]["event_ticker"] == event()["event_ticker"]
    fee = Decimal("0.07") * 29 * Decimal("0.86") * Decimal("0.14")  # 0.2444 -> 0.25
    assert state.cash == Decimal("1000") - 29 * Decimal("0.86") - Decimal("0.25") and fee < Decimal("0.25")

    # A restarted process (new broker, new run) still sees the event as traded.
    again = Broker(store, "wcbb", Paper(client, latency=0))
    runner.run(broker=again, client=client, scans=range(1))
    assert len(again.state().lots) == 1
    scans = [e for e in store.events("wcbb") if e["data"].get("kind") == "scan_complete"]
    assert scans[-1]["data"]["reasons"] == {"already ordered on this event": 1}


def test_refuses_live_portfolio(tmp_path):
    store = Store(tmp_path)
    store.create("live", "0", mode="live", subaccount=1)
    with pytest.raises(ValueError, match="paper only"):
        runner.run(broker=Broker(store, "live"), client=FakeKalshi(), scans=range(1))
