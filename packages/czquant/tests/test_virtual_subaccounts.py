"""Virtual subaccounts: a registered account's reserve, subs loaded and unloaded from it
while others trade, market scopes, netting, lot moves, and reconciliation."""
from decimal import Decimal as D

import pytest

from czquant import Broker, Store
from czquant.accounting import portfolio_state
from czquant.accounts import (allocate, book_reserve_difference, close_sub, members, move_lots, open_sub,
                              register_kalshi, register_paper, register_polymarket_us, summary)
from czquant.monitor import account_pass, polymarket_us_pass
from czquant.operator import map_live

from test_polymarket_us import SLUG, TICKER, FakeUS


class Fills:
    """A paper or live adapter stub that fills every order whole at its price."""
    takes_broker = True

    def __init__(self, venue="kalshi", mode="paper"):
        self.venue, self.mode = venue, mode
        self.n = 0

    def create_order(self, order, broker):
        self.n += 1
        broker.fill(order["client_order_id"], order["count"], order["price"], "0", f"stub:{order['client_order_id']}", {})
        broker.finish(order["client_order_id"], "executed")
        return {"status": "executed", "filled": order["count"]}


class Kalshi:
    """One Kalshi account whose balance, positions and transfers a test sets."""
    key_id = "fixture"

    def __init__(self, dollars="1000", positions=(), transfers=()):
        self.dollars, self.positions, self.transfers = D(dollars), list(positions), list(transfers)

    def get(self, path, params=None):
        return {"balance_dollars": str(self.dollars)} if path == "/portfolio/balance" else {}

    def pages(self, path, key, **params):
        if path == "/portfolio/positions":
            return iter(self.positions)
        if path == "/portfolio/subaccounts/transfers":
            return iter(self.transfers)
        return iter([])


def order(ticker="M", side="bid", count="10", price="0.40"):
    return {"ticker": ticker, "side": side, "count": count, "price": price, "time_in_force": "immediate_or_cancel"}


def cash(store, pool):
    return portfolio_state(store, pool).cash


def test_paper_subs_draw_on_the_reserve_and_return_to_it(tmp_path):
    store = Store(tmp_path)
    register_paper(store, "acct", "kalshi", "1000")
    open_sub(store, "a", "acct", "300")
    open_sub(store, "b", "acct", "200", markets=["KXNBA*"])
    assert [p["id"] for p in members(store, "acct")] == ["acct:reserve", "a", "b"]
    assert (cash(store, "acct:reserve"), cash(store, "a"), cash(store, "b")) == (500, 300, 200)

    a = Broker(store, "a", Fills())
    a.submit(order())
    assert cash(store, "a") == D("296")
    allocate(store, "acct:reserve", "a", "100")
    assert Broker(store, "a").available == D("396")
    assert summary(store, "acct")["recorded_cash"] == "996.00"

    # Unloading needs a flat sub; its cash goes back to the reserve with its P&L.
    with pytest.raises(ValueError, match="still holds"):
        close_sub(store, "a")
    assert close_sub(store, "b") == 200
    assert cash(store, "acct:reserve") == 600
    with pytest.raises(ValueError, match="not an active"):
        allocate(store, "acct:reserve", "b", "1")


def test_the_reserve_cannot_trade_and_a_scope_is_enforced(tmp_path):
    store = Store(tmp_path)
    register_paper(store, "acct", "kalshi", "1000")
    open_sub(store, "nba", "acct", "100", markets=["KXNBA*", "KXEXACT"])
    with pytest.raises(ValueError, match="account reserve"):
        Broker(store, "acct:reserve", Fills()).submit(order())
    nba = Broker(store, "nba", Fills())
    with pytest.raises(ValueError, match="outside cash pool"):
        nba.submit(order("KXNFL-1"))
    assert nba.submit(order("KXNBA-GAME-BOS"))["status"] == "executed"
    assert nba.submit(order("KXEXACT", count="1"))["status"] == "executed"


def test_allocation_stays_inside_one_account(tmp_path):
    store = Store(tmp_path)
    register_paper(store, "one", "kalshi", "100")
    register_paper(store, "two", "kalshi", "100")
    open_sub(store, "a", "one", "50")
    with pytest.raises(ValueError, match="different accounts"):
        allocate(store, "a", "two:reserve", "10")
    with pytest.raises(ValueError, match="of free"):
        allocate(store, "a", "one:reserve", "51")
    store.create_pool("loose", "kalshi", "100")
    with pytest.raises(ValueError, match="different accounts"):
        allocate(store, "loose", "a", "1")


def test_moving_lots_keeps_their_basis_and_books_no_profit(tmp_path):
    store = Store(tmp_path)
    register_paper(store, "acct", "kalshi", "1000")
    open_sub(store, "a", "acct", "100")
    open_sub(store, "b", "acct", "0")
    Broker(store, "a", Fills()).submit(order(count="10", price="0.40"))
    assert move_lots(store, "a", "b", "M", "yes", "4") == 4
    sa, sb = portfolio_state(store, "a"), portfolio_state(store, "b")
    assert (sa.realized, sb.realized) == (0, 0)
    assert sa.cost == D("2.4") and sb.cost == D("1.6")
    assert (sa.cash, sb.cash) == (D("96"), 0)
    with pytest.raises(ValueError, match="holds 6"):
        move_lots(store, "a", "b", "M", "yes", "7")
    move_lots(store, "a", "b", "M", "yes")
    assert close_sub(store, "a") == 96
    assert portfolio_state(store, "b").cost == D("4")


def test_a_sub_opened_mid_run_joins_a_reporting_portfolio(tmp_path):
    store = Store(tmp_path)
    register_paper(store, "acct", "kalshi", "1000")
    open_sub(store, "a", "acct", "100")
    store.create_portfolio("trial", ["a"])
    Broker(store, "a", Fills()).submit(order())
    open_sub(store, "b", "acct", "50")
    store.add_to_portfolio("trial", ["b"])
    assert store.reporting_portfolios()[0]["pools"] == ["a", "b"]
    open_sub(store, "c", "acct", "50")
    Broker(store, "c", Fills()).submit(order())
    with pytest.raises(ValueError, match="trading history"):
        store.add_to_portfolio("trial", ["c"])


def test_a_live_kalshi_account_reconciles_reserve_and_subs_together(tmp_path):
    store, api = Store(tmp_path), Kalshi("1000")
    register_kalshi(store, api, "main", 0, primary=True)
    open_sub(store, "a", "main", "400", markets=["M"], api=api)
    open_sub(store, "b", "main", "100", api=api)
    assert members(store, "main")[1]["metadata"]["subaccount"] == 0
    assert account_pass(store, api)["status"] == "healthy"

    # A transfer into the account is booked once, to the reserve.
    api.dollars += 50
    api.transfers = [{"transfer_id": "t1", "from_subaccount": 7, "to_subaccount": 0, "amount_cents": 5000,
                      "exchange_index": 0, "created_ts": 4102444800}]
    status = account_pass(store, api)
    assert status["status"] == "healthy", status["discrepancies"]
    assert (cash(store, "main:reserve"), cash(store, "a"), cash(store, "b")) == (550, 400, 100)

    # A primary-account deposit has no transfer record: the operator books it.
    api.dollars += 25
    assert account_pass(store, api)["status"] == "degraded"
    with pytest.raises(ValueError, match="note"):
        book_reserve_difference(store, api, "main", "")
    assert book_reserve_difference(store, api, "main", "ACH 11/26") == 25
    assert account_pass(store, api)["status"] == "healthy"


def test_registration_adopts_pools_mapped_before_it(tmp_path):
    store, api = Store(tmp_path), Kalshi("1000")
    map_live(store, api, "old", 0, primary=True)
    api.dollars += 200
    register_kalshi(store, api, "main", 0, primary=True)
    assert [p["id"] for p in members(store, "main")] == ["main:reserve", "old"]
    assert cash(store, "main:reserve") == 200
    with pytest.raises(ValueError, match="already registered"):
        register_kalshi(store, api, "again", 0, primary=True)
    allocate(store, "old", "main:reserve", "300", api=api)
    assert account_pass(store, api)["status"] == "healthy"


def test_live_subs_cannot_hold_opposite_sides_of_one_market(tmp_path):
    store, api = Store(tmp_path), Kalshi("1000")
    register_kalshi(store, api, "main", 0, primary=True)
    open_sub(store, "a", "main", "100", api=api)
    open_sub(store, "b", "main", "100", api=api)
    Broker(store, "a", Fills(mode="live")).submit(order(side="bid"))  # a holds YES
    b = Broker(store, "b", Fills(mode="live"))
    before = store.events()
    assert b.check(order(side="ask"))["reason"] == "cross_pool_netting"
    assert store.events() == before and b.orders() == []
    result = b.submit(order(side="ask"))  # would buy NO: Kalshi nets it against a's YES
    assert result["status"] == "rejected" and "a holds" in result["reason"]
    assert b.submit(order(side="bid"))["status"] == "executed"  # the same side is fine
    a = Broker(store, "a", Fills(mode="live"))
    assert a.submit(order(side="ask", count="10", price="0.50"))["status"] == "executed"  # a closing is fine
    # Registered paper accounts use the same ownership constraints as live accounts.
    register_paper(store, "p", "kalshi", "100")
    open_sub(store, "x", "p", "50")
    open_sub(store, "y", "p", "50")
    Broker(store, "x", Fills()).submit(order(side="bid"))
    assert Broker(store, "y", Fills()).submit(order(side="ask"))["status"] == "rejected"
    # An unscoped receiver can take owned lots explicitly without inventing a fill.
    assert move_lots(store, "x", "y", "M", "yes") == 10


def test_polymarket_us_subs_share_the_account_and_its_activity_goes_to_the_reserve(tmp_path):
    store, us = Store(tmp_path), FakeUS()
    register_polymarket_us(store, us, "pmus")
    open_sub(store, "game", "pmus", "200", markets=[TICKER], api=us)
    assert polymarket_us_pass(store, us)["status"] == "healthy"
    us.cash += 40
    deposit = {"type": "ACTIVITY_TYPE_ACCOUNT_DEPOSIT", "accountBalanceChange": {
        "transactionId": "d1", "amount": {"value": "40"}, "status": "ACCOUNT_BALANCE_CHANGE_STATUS_COMPLETED",
        "updateTime": "2100-01-01T00:00:00Z"}}

    class Depositing(FakeUS):
        def private(self, method, path, params=None, body=None):
            if path == "/v1/portfolio/activities":
                return {"activities": [deposit], "eof": True}
            return super().private(method, path, params, body)

    depositing = Depositing()
    depositing.cash = us.cash
    status = polymarket_us_pass(store, depositing)
    assert status["status"] == "healthy", status["discrepancies"]
    assert (cash(store, "pmus:reserve"), cash(store, "game")) == (340, 200)
    assert status["balances"][0]["pools"] == ["pmus:reserve", "game"]
    assert SLUG


def _pending_deposit(status="PENDING"):
    return {"type": "ACTIVITY_TYPE_ACCOUNT_DEPOSIT", "accountBalanceChange": {
        "transactionId": "ach1", "status": f"ACCOUNT_BALANCE_CHANGE_STATUS_{status}", "amount": {"value": "51"},
        "createTime": "2000-01-01T00:00:00Z", "updateTime": "2000-01-01T00:00:01Z", "transactions": []}}


class Clearing(FakeUS):
    """A $51 ACH deposit clearing since before registration, counted in buying power."""

    def __init__(self, status="PENDING", buying_power="51"):
        super().__init__()
        self.status, self.buying_power = status, buying_power

    def private(self, method, path, params=None, body=None):
        if path == "/v1/account/balances":
            reservation = "51" if self.status == "PENDING" else "0"
            current = "51" if self.status in ("PENDING", "COMPLETED") else "0"
            return {"balances": [{"currency": "USD", "currentBalance": current, "depositReservation": reservation,
                                  "buyingPower": self.buying_power}]}
        if path == "/v1/portfolio/activities":
            return {"activities": [_pending_deposit(self.status)], "eof": True}
        return super().private(method, path, params, body)


def test_buying_power_on_a_clearing_deposit_is_an_advance_booked_once(tmp_path):
    store, us = Store(tmp_path), Clearing()
    register_polymarket_us(store, us, "pmus")
    assert cash(store, "pmus:reserve") == 0  # registration leaves the clearing deposit out
    status = polymarket_us_pass(store, us)
    assert status["status"] == "healthy", status["discrepancies"]
    assert cash(store, "pmus:reserve") == 51
    booked = [e["data"] for e in store.events("pmus:reserve", kinds=("transaction",)) if e["data"].get("purpose")]
    assert sorted(d["purpose"] for d in booked) == ["account_opening", "buying_power_advance"]
    open_sub(store, "game", "pmus", "16.66", api=us)  # the live backing check counts the advance
    assert polymarket_us_pass(store, us)["status"] == "healthy"
    assert cash(store, "pmus:reserve") + cash(store, "game") == 51  # not booked twice

    # Completion adds nothing new; a failed transfer takes the advance back and is reported.
    assert polymarket_us_pass(store, Clearing("COMPLETED"))["status"] == "healthy"
    assert cash(store, "pmus:reserve") + cash(store, "game") == 51


def test_a_failed_clearing_deposit_reverses_its_buying_power_advance(tmp_path):
    store, us = Store(tmp_path), Clearing()
    register_polymarket_us(store, us, "pmus")
    polymarket_us_pass(store, us)
    status = polymarket_us_pass(store, Clearing("FAILED", buying_power="0"))
    assert cash(store, "pmus:reserve") == 0
    assert any(d["kind"] == "deposit_failed" for d in status["discrepancies"])


def test_buying_power_below_the_deposit_books_only_what_is_spendable(tmp_path):
    store = Store(tmp_path)
    register_polymarket_us(store, Clearing(), "pmus")
    polymarket_us_pass(store, Clearing(buying_power="20"))
    assert cash(store, "pmus:reserve") == 20


def test_pool_reads_use_the_pool_index(tmp_path):
    """A pool's events are read through its own index, not by scanning every event of a type."""
    store = Store(tmp_path)
    register_paper(store, "acct", "kalshi", "100")
    plan = store.db.execute("EXPLAIN QUERY PLAN SELECT * FROM events INDEXED BY events_portfolio "
                            "WHERE portfolio_id=? AND type IN ('order')", ("acct:reserve",)).fetchall()
    assert any("events_portfolio" in row[3] for row in plan)
    assert [i[1] for i in store.db.execute("PRAGMA index_list(orders)") if i[1].startswith("orders_")] \
        == ["orders_venue", "orders_portfolio"] or {"orders_venue", "orders_portfolio"} <= \
        {i[1] for i in store.db.execute("PRAGMA index_list(orders)")}
    assert [e["type"] for e in store.events("acct:reserve", kinds=("transaction",))] == ["transaction"]


def test_the_account_monitor_finishes_an_order_once(tmp_path):
    store, api = Store(tmp_path), Kalshi("1000")
    register_kalshi(store, api, "main", 0, primary=True)
    open_sub(store, "a", "main", "100", api=api)
    b = Broker(store, "a")
    store.db.execute("INSERT INTO orders(id,portfolio_id,run_id,request,context,status,reserved,venue_id) "
                     "VALUES('c1','a','r',?,'{}','acknowledged','5','v1')",
                     ('{"ticker":"M","side":"bid","count":"10","price":"0.4"}',))
    api.orders = [{"order_id": "v1", "status": "canceled", "fill_count_fp": "0"}]
    original = api.pages
    api.pages = lambda path, key, **p: iter(api.orders) if path == "/portfolio/orders" else original(path, key, **p)
    for _ in range(3):
        account_pass(store, api)
    finished = [e for e in store.events("a", kinds=("order",)) if e["data"].get("status") == "canceled"]
    assert len(finished) == 1 and b.available == 100


ROUND_TRIP = {"event_ticker": "E", "exchange_index": 0, "fee_cost": "0.505800", "market_result": "yes",
              "no_count_fp": "16.00", "revenue": 0, "settled_time": "2100-01-01T00:00:00Z", "ticker": "E-PHI",
              "value": 100, "yes_count_fp": "16.00"}


def test_a_bought_and_sold_back_market_settles_without_halting_the_account(tmp_path):
    """Kalshi reports a closed round trip as 16 YES and 16 NO with no revenue."""
    store, api = Store(tmp_path), Kalshi("1000")
    register_kalshi(store, api, "main", 0, primary=True)
    open_sub(store, "a", "main", "100", api=api)
    open_sub(store, "b", "main", "100", api=api)
    original = api.pages
    api.pages = lambda path, key, **p: iter([ROUND_TRIP]) if path == "/portfolio/settlements" else original(path, key, **p)
    status = account_pass(store, api)
    assert not [d for d in status["discrepancies"] if d["kind"] == "unresolved_settlement"], status["discrepancies"]


def test_an_unexplained_settlement_flags_only_the_pool_that_traded_it(tmp_path):
    store, api = Store(tmp_path), Kalshi("1000")
    register_kalshi(store, api, "main", 0, primary=True)
    open_sub(store, "a", "main", "100", api=api)
    open_sub(store, "b", "main", "100", api=api)
    store.db.execute("INSERT INTO orders(id,portfolio_id,run_id,request,context,status,reserved) "
                     "VALUES('c1','a','r',?,'{}','filled','0')", ('{"ticker":"E-PHI","side":"bid","count":"16","price":"0.67"}',))
    original = api.pages
    odd = {**ROUND_TRIP, "no_count_fp": "10.00"}  # net 6 YES the ledger never recorded
    api.pages = lambda path, key, **p: iter([odd]) if path == "/portfolio/settlements" else original(path, key, **p)
    flagged = {d["portfolio"] for d in account_pass(store, api)["discrepancies"] if d["kind"] == "unresolved_settlement"}
    assert flagged == {"a"}


def test_latest_health_returns_the_newest_record_per_component(tmp_path):
    store = Store(tmp_path)
    assert store.latest_health("account") is None
    store.append("health", {"component": "account", "status": "healthy"}, at="2100-01-01T00:00:00+00:00")
    store.append("health", {"component": "polymarket_us", "status": "healthy"}, at="2100-01-01T00:00:05+00:00")
    store.append("health", {"component": "account", "status": "degraded"}, at="2100-01-01T00:00:10+00:00")
    assert store.latest_health("account")["data"]["status"] == "degraded"
    assert store.latest_health("polymarket_us")["data"]["status"] == "healthy"
