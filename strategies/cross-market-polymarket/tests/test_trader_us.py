"""The trader on Polymarket US: pairing, paper fills, and the live guards (dry run)."""
from decimal import Decimal as D

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from czquant import Broker, Store
from czquant.operator import map_live_cross
from czquant.polymarket_us import PolymarketUS, kalshi_layout

from matching import us_game
from tests.test_trader import EVENT, LEAGUES, START, FakeKalshi
from trader import Config, Engine

SLUG = "aec-nba-bos-sac"
TICKER = "PMUS:" + SLUG
TEAMS = [{"id": 17, "name": "Boston Celtics", "abbreviation": "bos"},
         {"id": 32, "name": "Sacramento Kings", "abbreviation": "sac"}]


def market(status="MARKET_STATUS_OPEN"):
    return {"id": "m1", "slug": SLUG, "status": status, "active": True, "closed": False, "feeCoefficient": 0.095,
            "sportsMarketTypeV2": "SPORTS_MARKET_TYPE_MONEYLINE",
            "marketSides": [{"long": True, "teamId": 17, "description": "Celtics"},
                            {"long": False, "teamId": 32, "description": "Kings"}]}


class FakeUSVenue(PolymarketUS):
    """Celtics (YES) offered at 0.66 and bid at 0.62, so Kings (NO) cost 0.38."""

    def __init__(self, depth="100", authenticated=False, dry_run=False):
        super().__init__("key" if authenticated else None,
                         ed25519.Ed25519PrivateKey.generate() if authenticated else None, min_interval=0, dry_run=dry_run)
        self.depth, self.sent = depth, []

    def events(self, page=100, max_events=None, **params):
        yield {"slug": "nba-bos-sac-" + START.date().isoformat(), "startTime": START.isoformat().replace("+00:00", "Z"),
               "teams": TEAMS, "marketGroups": [{"id": "PRIMARY", "marketIds": ["m1"]}]}
        yield {"slug": "nba-champ-2027", "marketGroups": [{"id": "PRIMARY", "marketIds": []}]}

    def markets_by_id(self, ids):
        return [market()] if ids == ["m1"] else []

    def book_raw(self, slug):
        return {"marketData": {"marketSlug": slug, "bids": [{"px": {"value": "0.62"}, "qty": self.depth}],
                               "offers": [{"px": {"value": "0.66"}, "qty": self.depth}]}}

    def market_raw(self, slug):
        return market()

    def private(self, method, path, params=None, body=None):
        self.sent.append((method, path, body))
        raise AssertionError("no private request in these tests")


def test_a_us_moneyline_names_each_team_by_side():
    game = us_game({"slug": "nba-bos-sac", "startTime": "2026-11-22T03:00:00Z", "teams": TEAMS}, market())
    assert game is not None and game.tokens == {"bos": "YES", "sac": "NO"}
    assert str(game.day) == "2026-11-21"  # 10 pm Eastern on the 21st
    spread = {**market(), "sportsMarketTypeV2": "SPORTS_MARKET_TYPE_SPREAD"}
    assert us_game({"teams": TEAMS}, spread) is None


@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("CZQUANT_PORTFOLIO", "pm")
    return tmp_path


def build(cfg, us, kalshi=None, kalshi_live=None):
    return Engine(cfg, kalshi or FakeKalshi(), None, LEAGUES, {"pairs": {}, "blocked": set()}, "test",
                  us=us, kalshi_live=kalshi_live)


def test_paper_on_polymarket_us_buys_each_team_where_it_is_cheaper(runs):
    Store(runs).create("pm", "1000")
    engine = build(Config(venue="polymarket_us", kalshi_latency=0, polymarket_latency=0, max_event_frac=1.0),
                   FakeUSVenue())
    assert engine.kalshi_broker.venue == "kalshi" and engine.poly_broker.venue == "polymarket_us"
    engine.step()
    held = {(lot["market"], lot["side"]): lot["quantity"] for lot in Broker(engine.store, "pm").state().lots.values()}
    assert held == {(f"{EVENT}-BOS", "yes"): 100, (TICKER, "no"): 100}  # Celtics on Kalshi, Kings on Polymarket US
    fill = [e["data"] for e in engine.store.events(kinds=("transaction",)) if e["data"].get("venue") == "polymarket_us"]
    assert fill and D(fill[0]["fee"]) == D("2.24")  # 0.095 x 100 x 0.62 x 0.38, to the cent


def test_a_theta_override_prices_with_it(runs):
    Store(runs).create("pm", "1000")
    engine = build(Config(venue="polymarket_us", pmus_theta=0.0695, max_event_frac=1.0), FakeUSVenue())
    engine.discover()
    basket = engine.scan()[0][0]
    us_leg = [leg for leg in basket.legs if leg["venue"] == "polymarket_us"][0]
    assert D(us_leg["fee"]) == D("1.64")  # 0.0695 x 100 x 0.38 x 0.62 = 1.6375


class LiveKalshi(FakeKalshi):
    key_id = "fixture"

    def __init__(self):
        super().__init__()
        self.sent = []

    def get(self, path, params=None):
        if path == "/portfolio/balance":
            return {"balance_dollars": "300"}
        return super().get(path, params)

    def pages(self, path, key, **params):
        if path.startswith("/portfolio"):
            return iter([])
        return super().pages(path, key, **params)

    def create_order(self, order):
        self.sent.append(order)
        raise AssertionError("dry run must not send")


class MappingUS(FakeUSVenue):
    def private(self, method, path, params=None, body=None):
        if path == "/v1/account/balances":
            return {"balances": [{"currency": "USD", "currentBalance": "500"}]}
        if path == "/v1/portfolio/positions":
            return {"positions": {}, "eof": True}
        if path == "/v1/orders/open":
            return {"orders": []}
        return super().private(method, path, params, body)


def live_cfg(**kw):
    return Config(venue="polymarket_us", live=True, max_event_frac=1.0, max_basket_dollars=25,
                  max_total_dollars=100, max_sets=20, **kw)


def test_live_needs_a_live_portfolio(runs):
    Store(runs).create("pm", "1000")
    with pytest.raises(ValueError, match="not a live portfolio"):
        build(live_cfg(dry_run=True), MappingUS(authenticated=True, dry_run=True), kalshi_live=LiveKalshi())


def test_a_paper_run_refuses_a_live_portfolio(runs):
    map_live_cross(Store(runs), LiveKalshi(), MappingUS(authenticated=True), "pm", subaccount=3)
    with pytest.raises(ValueError, match="pass --live"):
        build(Config(venue="polymarket_us"), FakeUSVenue())


def test_live_dry_run_sizes_within_the_caps_and_sends_nothing(runs):
    map_live_cross(Store(runs), LiveKalshi(), MappingUS(authenticated=True), "pm", subaccount=3)
    us, kalshi = MappingUS(authenticated=True, dry_run=True), LiveKalshi()
    engine = build(live_cfg(dry_run=True), us, kalshi_live=kalshi)
    engine.step()
    record = [e["data"] for e in engine.store.events() if e["data"].get("kind") == "basket_execution"][-1]
    assert record["context"]["sets"] <= 20 and D(record["context"]["intended_legs"][0]["vwap"]) * 20 <= 25
    assert [o["status"] for o in record["outcomes"]] == ["dry_run"]  # zero filled: the next leg isn't sent
    assert kalshi.sent == [] and not [s for s in us.sent if s[1] == "/v1/orders"]
    assert engine.store.reserved("pm") == 0 and not engine.halted


def test_live_waits_for_its_monitors_and_halts_on_a_discrepancy(runs):
    store = Store(runs)
    map_live_cross(store, LiveKalshi(), MappingUS(authenticated=True), "pm", subaccount=3)
    engine = build(live_cfg(), MappingUS(authenticated=True), kalshi_live=LiveKalshi())
    assert engine.live_block() == "no_account_monitor"
    store.append("health", {"component": "account", "status": "healthy", "discrepancies": []})
    store.append("health", {"component": "polymarket_us", "status": "degraded", "discrepancies": [
        {"portfolio": "pm", "kind": "cash_mismatch"}]})
    assert engine.live_block() == "halted" and "cash_mismatch" in (engine.halted or "")


def test_live_halts_on_an_order_with_an_unknown_outcome(runs):
    store = Store(runs)
    map_live_cross(store, LiveKalshi(), MappingUS(authenticated=True), "pm", subaccount=3)
    engine = build(live_cfg(dry_run=True), MappingUS(authenticated=True, dry_run=True), kalshi_live=LiveKalshi())
    store.db.execute("INSERT INTO orders(id,portfolio_id,request,context,status,reserved) VALUES('o','pm',?,?,'unknown','1')",
                     ('{"ticker": "%s"}' % TICKER, "{}"))
    assert engine.live_block() == "halted"
