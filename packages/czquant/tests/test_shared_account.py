"""One Kalshi account behind several live portfolios: sharing, allocation, remapping,
retirement, and reconciling the account against the group."""
from decimal import Decimal as D

import pytest

from czquant import Broker, Store
from czquant.monitor import account_pass
from czquant.operator import allocate, map_live, remap_kalshi, retire_live


class Kalshi:
    """A primary-only account whose cash, positions and settlements a test sets."""
    key_id = "fixture"

    def __init__(self, cents=10000, positions=(), settlements=()):
        self.cents, self.positions, self.settlements = cents, list(positions), list(settlements)

    def get(self, path, params=None):
        return {"balance": self.cents} if path == "/portfolio/balance" else {}

    def pages(self, path, key, **params):
        if path == "/portfolio/positions":
            return iter(self.positions)
        if path == "/portfolio/settlements":
            return iter([s for s in self.settlements if params.get("ticker") in (None, s["ticker"])])
        return iter([])


def buy(store, portfolio, ticker="M", quantity="10", price="0.40"):
    """Book a filled Kalshi buy straight into the ledger (what the monitor would book)."""
    store.append("transaction", {"reason": "fill", "venue": "kalshi", "fee": "0",
                                 "cash_delta": str(-D(quantity) * D(price)),
                                 "legs": [{"market": ticker, "side": "yes", "quantity_delta": quantity, "price": price}]},
                 portfolio)


def cash(store, portfolio):
    return Broker(store, portfolio).state().cash


def test_a_second_portfolio_needs_share_and_opens_with_the_unrecorded_cash(tmp_path):
    store, api = Store(tmp_path), Kalshi(cents=10000)
    map_live(store, api, "a", 0, primary=True)
    with pytest.raises(ValueError, match="--share-account"):
        map_live(store, api, "b", 0, primary=True)
    api.cents = 12500  # a $25 deposit nobody records yet
    map_live(store, api, "b", 0, primary=True, share=True)
    assert (cash(store, "a"), cash(store, "b")) == (100, 25)


def test_sharing_refuses_an_account_holding_what_no_portfolio_records(tmp_path):
    store, api = Store(tmp_path), Kalshi()
    map_live(store, api, "a", 0, primary=True)
    api.positions = [{"ticker": "M", "position_fp": "5"}]
    with pytest.raises(ValueError, match="do not record"):
        map_live(store, api, "b", 0, primary=True, share=True)


def test_allocation_moves_recorded_cash_and_the_group_reconciles(tmp_path):
    store, api = Store(tmp_path), Kalshi(cents=10000)
    map_live(store, api, "a", 0, primary=True)
    map_live(store, api, "b", 0, primary=True, share=True)
    allocate(store, "a", "b", D("40"))
    assert (cash(store, "a"), cash(store, "b")) == (60, 40)
    with pytest.raises(ValueError, match="free Kalshi cash"):
        allocate(store, "a", "b", D("61"))
    buy(store, "b")
    api.cents, api.positions = 9600, [{"ticker": "M", "position_fp": "10"}]
    assert account_pass(store, api)["status"] == "healthy"
    api.cents = 9500
    status = account_pass(store, api)
    assert {d["portfolio"] for d in status["discrepancies"] if d["kind"] == "cash_mismatch"} == {"a", "b"}


def test_an_inventory_mismatch_is_charged_to_the_portfolio_holding_that_market(tmp_path):
    store, api = Store(tmp_path), Kalshi(cents=10000)
    map_live(store, api, "a", 0, primary=True)
    map_live(store, api, "b", 0, primary=True, share=True)
    allocate(store, "a", "b", D("40"))
    buy(store, "b")
    api.cents, api.positions = 9600, [{"ticker": "M", "position_fp": "7"}]
    wrong = [d for d in account_pass(store, api)["discrepancies"] if d["kind"] == "inventory_mismatch"]
    assert [(d["portfolio"], d["markets"]) for d in wrong] == [("b", ["M"])]


def test_a_settlement_on_a_shared_account_pays_the_portfolio_holding_it(tmp_path):
    store, api = Store(tmp_path), Kalshi(cents=10000)
    map_live(store, api, "a", 0, primary=True)
    map_live(store, api, "b", 0, primary=True, share=True)
    allocate(store, "a", "b", D("40"))
    buy(store, "b")
    api.cents = 10600
    api.settlements = [{"ticker": "M", "market_result": "yes", "value": 100, "revenue": 1000,
                        "yes_count_fp": "10", "no_count_fp": "0", "settled_time": "2030-01-01T00:00:00Z"}]
    assert account_pass(store, api)["status"] == "healthy"
    assert (cash(store, "a"), cash(store, "b")) == (60, 46)


def test_retiring_settles_books_the_difference_and_frees_the_account(tmp_path):
    store, api = Store(tmp_path), Kalshi(cents=10000)
    map_live(store, api, "old", 0, primary=True)
    buy(store, "old", quantity="34", price="0.70")  # $76.20 left
    api.cents = 11370  # $34 settlement plus $3.50 from a trade outside the ledger
    with pytest.raises(ValueError, match="not settled"):
        retire_live(store, api, "old")
    api.settlements = [{"ticker": "M", "market_result": "yes", "value": 100, "revenue": 3400,
                        "yes_count_fp": "363.52", "no_count_fp": "329.52", "settled_time": "2030-11-04T05:33:46Z"}]
    with pytest.raises(ValueError, match="--book-difference"):
        retire_live(store, api, "old")
    result = retire_live(store, api, "old", book_difference=True)
    assert result["difference"] == "3.50" and result["released"] == {"kalshi": "113.70"}
    state = Broker(store, "old").state()
    assert state.cash == 0 and not state.lots and state.realized == D("10.20")  # 34 x (1 - 0.70)
    with pytest.raises(ValueError, match="retired"):
        Broker(store, "old", adapter=object()).submit({"ticker": "M", "side": "bid", "count": "1", "price": "0.5"})
    map_live(store, api, "new", 0, primary=True)  # the account is free again, at its full cash
    assert cash(store, "new") == D("113.70")
    assert account_pass(store, api)["status"] == "healthy"


def test_retiring_on_a_shared_account_hands_its_cash_to_a_sharer(tmp_path):
    store, api = Store(tmp_path), Kalshi(cents=10000)
    map_live(store, api, "a", 0, primary=True)
    map_live(store, api, "b", 0, primary=True, share=True)
    with pytest.raises(ValueError, match="--to"):
        retire_live(store, api, "a")
    retire_live(store, api, "a", to="b")
    assert (cash(store, "a"), cash(store, "b")) == (0, 100)
    assert account_pass(store, api)["status"] == "healthy"


def test_remapping_moves_an_empty_portfolio_to_another_account(tmp_path):
    store, api = Store(tmp_path), Kalshi(cents=0)
    store.create("x", mode="live", subaccount=1, exchange_index=0, venues=["kalshi", "polymarket_us"])
    api.cents = 20000
    remap_kalshi(store, api, "x", 0)
    [p] = store.portfolios()
    assert p["metadata"]["subaccount"] == 0 and p["metadata"]["venues"] == ["kalshi", "polymarket_us"]
    assert Broker(store, "x").state().venue_cash["kalshi"] == 200
    buy(store, "x")
    with pytest.raises(ValueError, match="Kalshi positions"):
        remap_kalshi(store, api, "x", 2)
