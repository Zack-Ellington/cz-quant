"""The primary account (subaccount 0) as a live portfolio: opt-in, flat, named by omission."""
import httpx
import pytest

from czquant import Broker, Store
from czquant.monitor import account_pass
from czquant.operator import map_live


class Kalshi:
    """A basic-tier account: no subaccounts, so naming one or listing transfers is refused."""
    key_id = "fixture"

    def __init__(self, positions=(), orders=(), balance_cents=5000):
        self.positions, self.orders, self.balance = list(positions), list(orders), balance_cents
        self.calls = []
        self.sent = []

    def _refuse_subaccounts(self, path, params):
        self.calls.append((path, dict(params or {})))
        if "subaccount" in (params or {}) or path.startswith("/portfolio/subaccounts"):
            request = httpx.Request("GET", "https://kalshi.test" + path)
            raise httpx.HTTPStatusError("403", request=request, response=httpx.Response(403, request=request))

    def get(self, path, params=None):
        self._refuse_subaccounts(path, params)
        return {"balance": self.balance} if path == "/portfolio/balance" else {}

    def pages(self, path, key, **params):
        self._refuse_subaccounts(path, params)
        if path == "/portfolio/positions":
            return iter(self.positions)
        if path == "/portfolio/orders":
            return iter(self.orders)
        return iter([])

    def create_order(self, order):
        self.sent.append(order)
        return {"order": {"order_id": "v1", "status": "executed", "fill_count_fp": order["count"]}}


def test_the_primary_account_must_be_dedicated_explicitly(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(ValueError, match="--primary-account"):
        map_live(store, Kalshi(), "live", 0)
    with pytest.raises(ValueError, match="1-63"):
        map_live(store, Kalshi(), "live", 64)
    assert not store.portfolios()


def test_a_primary_account_with_positions_is_refused(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(ValueError, match="flat"):
        map_live(store, Kalshi(positions=[{"ticker": "M", "position_fp": "5"}]), "live", 0, primary=True)


def test_a_flat_primary_account_maps_and_trades_without_naming_a_subaccount(tmp_path):
    store, api = Store(tmp_path), Kalshi()
    map_live(store, api, "live", 0, primary=True)
    [portfolio] = store.portfolios()
    assert portfolio["mode"] == "live" and portfolio["metadata"]["subaccount"] == 0
    assert Broker(store, "live").state().cash == 50
    assert all("subaccount" not in params for _, params in api.calls)
    broker = Broker(store, "live", api)
    broker.submit({"ticker": "M", "side": "bid", "count": "2", "price": "0.40"}, {})
    assert "subaccount" not in api.sent[0]
    with pytest.raises(ValueError, match="mapped subaccount"):
        broker.submit({"ticker": "M", "side": "bid", "count": "2", "price": "0.40", "subaccount": 3}, {})


def test_the_account_monitor_runs_on_a_primary_portfolio_without_subaccount_access(tmp_path):
    store, api = Store(tmp_path), Kalshi()
    map_live(store, api, "live", 0, primary=True)
    status = account_pass(store, api)
    assert status["status"] == "healthy"
    # A numbered subaccount still needs the transfer list: its refusal is not swallowed.
    store.create("sub", mode="live", subaccount=3)
    with pytest.raises(httpx.HTTPStatusError):
        account_pass(store, api)
