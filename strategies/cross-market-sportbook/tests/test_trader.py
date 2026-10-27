from datetime import datetime, timedelta, timezone
from decimal import Decimal as D, ROUND_CEILING

import pytest

from czquant import Store
from czquant.accounting import portfolio_state
from czquant.draftkings import DraftKings, implied_price, parse_oddsapi
from matching import EASTERN
import trader

START = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=6)
DAY = START.astimezone(EASTERN).strftime("%y%b%d").upper()
EVENT = f"KXNBAGAME-{DAY}BKNBOS"
DK_AWAY = "DK:oddsapi:basketball_nba:e1:ML2:away"
LEAGUES = {"nba": {"kalshi_series": "KXNBAGAME", "oddsapi": "basketball_nba", "draw": False, "rules": "fixture"}}


class Kalshi:
    """Public Kalshi reads for one game; books are YES asks (price, quantity)."""

    def __init__(self, books):
        self.books = books

    def raw(self, ticker):
        p, q = self.books[ticker]
        return {"ticker": ticker, "orderbook_fp": {"yes_dollars": [], "no_dollars": [[str(1 - D(p)), str(q)]]}}

    def pages(self, path, key, **params):
        end = (START + timedelta(hours=3)).isoformat().replace("+00:00", "Z")
        yield {"event_ticker": EVENT, "series_ticker": "KXNBAGAME", "markets": [
            {"ticker": f"{EVENT}-{abbr}", "yes_sub_title": name, "status": "active", "expected_expiration_time": end}
            for name, abbr in (("Brooklyn", "BKN"), ("Boston", "BOS"))]}

    def get(self, path, params=None):
        if path == "/markets/orderbooks":
            return {"orderbooks": [self.raw(t) for _, t in params]}
        return {"series": {"fee_type": "quadratic", "fee_multiplier": "1"}}

    def book(self, ticker):
        return self.raw(ticker)

    def market(self, ticker):
        return {"ticker": ticker, "series_ticker": "KXNBAGAME", "status": "active"}


class Source:
    """DraftKings odds by read: read n uses odds[min(n, last)] as (home, away)."""
    name = "oddsapi"

    def __init__(self, *odds):
        self.odds, self.reads = odds, 0

    def selections(self, league):
        home, away = self.odds[min(self.reads, len(self.odds) - 1)]
        self.reads += 1
        now = datetime.now(timezone.utc).isoformat()
        return parse_oddsapi([{"id": "e1", "commence_time": START.isoformat(), "home_team": "Boston Celtics",
            "away_team": "Brooklyn Nets", "bookmakers": [{"key": "draftkings", "last_update": now, "markets": [
                {"key": "h2h", "last_update": now, "outcomes": [{"name": "Boston Celtics", "price": home},
                                                                {"name": "Brooklyn Nets", "price": away}]}]}]}], league)

    def results(self, league, events):
        return {}


def engine(tmp_path, monkeypatch, source):
    monkeypatch.setenv("STRATEGY_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("CZQUANT_PORTFOLIO", "xm")
    store = Store(tmp_path)
    if not store.portfolios():
        store.create("xm", "1000")
    kalshi = Kalshi({f"{EVENT}-BOS": ("0.70", "200"), f"{EVENT}-BKN": ("0.40", "200")})
    cfg = trader.Config(kalshi_latency=0, book_latency=0)
    eng = trader.Engine(cfg, kalshi=kalshi, book=DraftKings(source), leagues=LEAGUES,
                        pairs={"pairs": {}, "blocked": set()}, revision="test")
    return eng, store


def test_scan_backs_draftkings_then_buys_kalshi(tmp_path, monkeypatch):
    eng, store = engine(tmp_path, monkeypatch, Source((1.25, 4.0)))
    eng.run(once=True)
    state = portfolio_state(store, "xm")
    holdings = {lot["market"]: lot["quantity"] for lot in state.lots.values()}
    # $100 per-game cap: 103 sets of Kalshi BOS at 0.70 (+ fee) and DraftKings BKN at 0.25.
    assert holdings == {DK_AWAY: 103, f"{EVENT}-BOS": 103}
    fee = (D("0.07") * 103 * D("0.70") * D("0.30")).quantize(D("0.01"), rounding=ROUND_CEILING)
    assert state.cash == D("1000") - D("72.10") - fee - 103 * implied_price(D("4"))
    assert len({lot["context"]["trade_id"] for lot in state.lots.values()}) == 1
    (execution,) = [e["data"] for e in store.events() if e["data"].get("kind") == "basket_execution"]
    assert [o["venue"] for o in execution["outcomes"]] == ["draftkings", "kalshi"]
    assert execution["context"]["match"]["method"] == "names"
    kinds = {e["data"].get("kind") for e in store.events() if e["type"] == "observation"}
    assert kinds >= {"universe", "scan_complete", "opportunity", "heartbeat", "run_stop"}


def test_declined_bet_sends_no_kalshi_order(tmp_path, monkeypatch):
    # The scan and the re-check see 4.0; when the bet is placed the odds are 3.0.
    eng, store = engine(tmp_path, monkeypatch, Source((1.25, 4.0), (1.25, 4.0), (1.30, 3.0)))
    eng.run(once=True)
    assert not portfolio_state(store, "xm").lots
    orders = [dict(r) for r in store.db.execute("SELECT request,status FROM orders")]
    assert len(orders) == 1 and orders[0]["status"] == "canceled" and DK_AWAY in orders[0]["request"]
    (execution,) = [e["data"] for e in store.events() if e["data"].get("kind") == "basket_execution"]
    assert execution["outcomes"] == [{"ticker": DK_AWAY, "venue": "draftkings", "status": "canceled",
                                      "filled": "0", "reason": "odds_shortened"}]


def test_no_trade_without_a_cross_venue_edge(tmp_path, monkeypatch):
    eng, store = engine(tmp_path, monkeypatch, Source((1.25, 3.2)))  # 0.70 + 0.3125 > 1
    eng.run(once=True)
    assert not store.db.execute("SELECT 1 FROM orders").fetchone()
    (scan,) = [e["data"] for e in store.events() if e["data"].get("kind") == "scan_complete"]
    assert scan["counts"] == {"ok": 1, "priced_no_edge": 1} and scan["candidates"] == 0


def test_complete_losing_basket_halts_and_survives_restart(tmp_path, monkeypatch):
    eng, store = engine(tmp_path, monkeypatch, Source((1.25, 4.0)))
    context = {"trade_id": "t", "kalshi_event": EVENT,
               "intended_legs": [{"ticker": t, "contract": "YES", "qty": 10} for t in ("A", "B")]}
    for t, paid in (("A", "4.5"), ("B", "0")):
        lot = store.append("transaction", {"reason": "fill", "cash_delta": "-5", "fee": "0", "context": context,
                           "legs": [{"market": t, "side": "yes", "quantity_delta": "10", "price": "0.5"}]}, "xm")
        store.append("transaction", {"reason": "settlement", "cash_delta": paid, "fee": "0", "context": context,
                     "legs": [{"market": t, "side": "yes", "quantity_delta": "-10",
                               "price": str(D(paid) / 10), "lot_id": lot}]}, "xm")
    eng.resume()
    assert eng.halted == "settled trade t lost $5.50"
    restarted, _ = engine(tmp_path, monkeypatch, Source((1.25, 4.0)))
    assert restarted.halted == eng.halted
    restarted.run(once=True)
    assert not store.db.execute("SELECT 1 FROM orders").fetchone()


def test_league_selection():
    with pytest.raises(ValueError, match="unknown leagues"):
        trader.load_leagues(("cricket",))
    assert set(trader.load_leagues()) == {"nba", "nhl", "mlb"}  # NFL stays off: ties push on DraftKings
    assert set(trader.load_leagues(("nfl",))) == {"nfl"}
