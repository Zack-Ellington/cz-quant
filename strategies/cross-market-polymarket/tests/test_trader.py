"""One scan end to end against fake venues: discover, pair, price, execute, unwind."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from czquant import Broker, Store
from czquant.accounting import combine_states
from czquant.polymarket import Polymarket

from trader import Config, Engine
from experiments import Experiment, Outcome, SelectedPair

DAY = (datetime.now(timezone.utc) + timedelta(days=3)).date()
START = datetime(DAY.year, DAY.month, DAY.day, 23, 0, tzinfo=timezone.utc)  # 7 pm Eastern, same date
EVENT = f"KXNBAGAME-{DAY:%y}{DAY:%b}{DAY:%d}BOSSAC".upper()
LEAGUES = {"nba": {"enabled": True, "kalshi_series": "KXNBAGAME", "polymarket_tag": "nba", "abbr_aliases": {}}}


def experiment(venue="polymarket", mode="paper", kalshi_pool="k", poly_pool="pm"):
    us = venue == "polymarket_us"
    return Experiment("bos-sac", kalshi_pool, poly_pool, venue, mode, SelectedPair(
        "nba", EVENT, "nba-bos-sac-" + START.date().isoformat() if us else "nba-bos-sac",
        "aec-nba-bos-sac" if us else "m1", (
            Outcome(EVENT + "-BOS", "YES", "PMUS:aec-nba-bos-sac" if us else "PM:tok-bos", "YES"),
            Outcome(EVENT + "-SAC", "YES", "PMUS:aec-nba-bos-sac" if us else "PM:tok-sac", "NO" if us else "YES"))))


class FakeKalshi:
    """Boston's YES ask is 0.55 and Sacramento's 0.70 on Kalshi."""

    def __init__(self, asks=None, depth="100"):
        self.asks = asks or {"BOS": "0.55", "SAC": "0.70"}
        self.depth = depth
        self.fill_depth: str | None = None  # what the paper fill sees, if the book thins after pricing

    def raw(self, ticker, depth=None):
        ask = D(self.asks[ticker.rsplit("-", 1)[-1]])
        return {"orderbook_fp": {"yes_dollars": [], "no_dollars": [[str(1 - ask), depth or self.depth]]}}

    def get(self, path, params=None):
        if path.startswith("/series/"):
            return {"series": {"fee_type": "quadratic", "fee_multiplier": 1}}
        if path == "/markets/orderbooks":
            return {"orderbooks": [{"ticker": t, **self.raw(t)} for _, t in params]}
        raise AssertionError(path)

    def pages(self, path, key, **params):
        end = (START + timedelta(hours=3)).isoformat()
        yield {"event_ticker": EVENT, "series_ticker": "KXNBAGAME", "markets": [
            {"ticker": f"{EVENT}-{a}", "yes_sub_title": n, "status": "active", "expected_expiration_time": end}
            for n, a in (("Boston", "BOS"), ("Sacramento", "SAC"))]}

    def market(self, ticker):
        return {"ticker": ticker, "status": "active", "series_ticker": "KXNBAGAME"}

    def book(self, ticker):
        return self.raw(ticker, self.fill_depth)


class FakePoly(Polymarket):
    """Polymarket has Sacramento at 0.38 and Boston at 0.66; `depth` contracts at each ask."""

    def __init__(self, depth="100"):
        super().__init__(min_interval=0)
        self.depth = depth
        self.asks = {"tok-bos": "0.66", "tok-sac": "0.38"}

    def market_json(self):
        return {"id": "m1", "question": "Celtics vs. Kings", "sportsMarketType": "moneyline",
                "outcomes": json.dumps(["Celtics", "Kings"]), "clobTokenIds": json.dumps(["tok-bos", "tok-sac"]),
                "outcomePrices": json.dumps(["0.6", "0.4"]), "closed": False, "acceptingOrders": True,
                "feesEnabled": True, "feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True}}

    def canned_book(self, token):
        bid = str(D(self.asks[token]) - D("0.02"))  # a bid two cents under the ask, for sell-backs
        return {"asset_id": token, "bids": [{"price": bid, "size": self.depth}],
                "asks": [{"price": self.asks[token], "size": self.depth}]}

    def _request(self, method, url, **kwargs):
        if url.endswith("/events/keyset"):
            return {"events": [{"slug": "nba-bos-sac", "eventDate": str(DAY), "startTime": START.isoformat(),
                     "teams": [{"name": "Celtics", "abbreviation": "bos"}, {"name": "Kings", "abbreviation": "sac"}],
                     "markets": [self.market_json()]}], "next_cursor": None}
        if url.endswith("/markets"):
            return [self.market_json()]
        if url.endswith("/books"):
            return [self.canned_book(row["token_id"]) for row in kwargs["json"]]
        if url.endswith("/book"):
            return self.canned_book(kwargs["params"]["token_id"])
        raise AssertionError(url)


@pytest.fixture
def engine_for(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_RUNS_DIR", str(tmp_path))
    store = Store(tmp_path)
    store.create_pool("k", "kalshi", "600")
    store.create_pool("pm", "polymarket", "400")
    store.close()

    def build(kalshi=None, poly=None):
        return Engine(Config(experiment=experiment(), kalshi_latency=0, polymarket_latency=0, max_event_frac=1.0, max_sets=500, max_basket_dollars=None, max_total_dollars=None),
                      kalshi or FakeKalshi(), poly or FakePoly(), LEAGUES, {"pairs": {}, "blocked": set()}, "test")
    return build


def test_a_scan_pairs_prices_and_fills_a_cross_venue_basket(engine_for):
    engine = engine_for()
    engine.step()
    assert engine.kalshi_broker.venue == "kalshi" and engine.poly_broker.venue == "polymarket"
    assert engine.kalshi_broker.run_id == engine.poly_broker.run_id == engine.run_id
    state = combine_states([b.state() for b in engine.brokers.values()])
    held = {lot["market"]: lot["quantity"] for lot in state.lots.values()}
    assert held == {f"{EVENT}-BOS": 100, "PM:tok-sac": 100}  # Boston on Kalshi, Sacramento on Polymarket
    record = [e["data"] for e in engine.store.events() if e["data"].get("kind") == "basket_execution"][-1]
    assert record["context"]["polymarket_event"] == "nba-bos-sac" and record["context"]["match"]["method"] == "abbreviations"
    assert all(o["filled"] == "100" for o in record["outcomes"])


def test_a_short_first_leg_sizes_the_next(engine_for):
    poly = FakePoly(depth="40")  # Polymarket shows only 40, the thinner book: it goes first
    engine = engine_for(poly=poly)
    engine.discover()
    candidates = engine.scan()
    assert candidates and candidates[0][0].sets == 40
    basket, pair, league = candidates[0]
    poly.depth = "25"  # by the time the order lands, only 25 are left
    engine.execute(basket, pair, league)
    state = combine_states([b.state() for b in engine.brokers.values()])
    held = {lot["market"]: lot["quantity"] for lot in state.lots.values()}
    assert held == {f"{EVENT}-BOS": 25, "PM:tok-sac": 25}  # Kalshi was asked for 25, not 40


def test_no_basket_without_an_edge(engine_for):
    engine = engine_for(kalshi=FakeKalshi({"BOS": "0.66", "SAC": "0.42"}))
    engine.step()
    assert not combine_states([b.state() for b in engine.brokers.values()]).lots
    scan = [e["data"] for e in engine.store.events() if e["data"].get("kind") == "scan_complete"][-1]
    assert scan["counts"]["ok"] == 1 and scan["candidates"] == 0


def test_games_in_progress_are_traded(engine_for, monkeypatch):
    import trader
    engine = engine_for()
    engine.discover()
    for minutes in (-1, 30, 8 * 60):  # just before kickoff, in play, past the expected payout
        now = START.timestamp() + minutes * 60
        monkeypatch.setattr(trader.time, "time", lambda now=now: now)
        [(basket, _, _)] = engine.scan()
        assert basket.days >= 1 / 24


def test_a_short_later_leg_has_the_extras_sold_back(engine_for):
    kalshi, poly = FakeKalshi(), FakePoly(depth="40")
    engine = engine_for(kalshi=kalshi, poly=poly)
    engine.discover()
    basket, pair, league = engine.scan()[0]
    kalshi.fill_depth = "10"  # priced at 100, but when the Kalshi leg lands only 10 are left
    engine.execute(basket, pair, league)
    state = combine_states([b.state() for b in engine.brokers.values()])
    held = {lot["market"]: lot["quantity"] for lot in state.lots.values()}
    assert held == {f"{EVENT}-BOS": 10, "PM:tok-sac": 10}  # 30 Sacramento tokens sold back at 0.36
    record = [e["data"] for e in engine.store.events() if e["data"].get("kind") == "basket_execution"][-1]
    unwind = [o for o in record["outcomes"] if "unwind" in o][0]
    assert (unwind["ticker"], unwind["unwind"], unwind["sold"], unwind["floor"]) == ("PM:tok-sac", "30", "30", "0.33")


def test_main_refuses_to_run_without_a_runs_dir(monkeypatch, capsys):
    import trader
    monkeypatch.delenv("STRATEGY_RUNS_DIR", raising=False)
    with pytest.raises(SystemExit):
        trader.main(["--experiment", "missing.json", "--once"])
    assert "STRATEGY_RUNS_DIR" in capsys.readouterr().err


def test_restart_recovers_both_venues_without_refunding(engine_for):
    engine = engine_for()
    engine.step()
    cash, positions = engine.cash, engine.positions
    restarted = engine_for()
    assert restarted.cash == cash and restarted.positions == positions
    assert restarted.kalshi_broker.available + restarted.poly_broker.available == cash
    assert restarted.run_id != engine.run_id
    assert len(restarted.kalshi_broker.state().lots) == len(restarted.poly_broker.state().lots) == 1


def test_paper_rejects_a_basket_when_only_the_other_venue_has_enough_cash(engine_for):
    engine = engine_for()
    engine.store.append('transaction', {'reason': 'funding', 'cash_delta': '-390',
                                      'external_delta': '-390', 'legs': []}, engine.poly_broker.pool_id)
    engine.step()
    assert engine.kalshi_broker.available == 600 and engine.poly_broker.available == 10
    assert not engine.store.events(kinds=('order',))


def test_cli_risk_defaults_are_identical_in_paper_and_live(tmp_path, monkeypatch):
    import trader
    monkeypatch.setenv('STRATEGY_RUNS_DIR', str(tmp_path))
    configs = []

    class Captured(Exception):
        pass

    def capture(config):
        configs.append(config)
        raise Captured()

    monkeypatch.setattr(trader, 'Engine', capture)
    from dataclasses import asdict
    for mode in ("paper", "live"):
        path = tmp_path / f"{mode}.json"
        path.write_text(json.dumps({**asdict(experiment(venue="polymarket_us", mode=mode)), "parameters": {}}))
        with pytest.raises(Captured):
            trader.main(["--experiment", str(path), "--once"])
    assert [(c.max_sets, c.max_basket_dollars, c.max_total_dollars) for c in configs] == [(20, 25, 100)] * 2
