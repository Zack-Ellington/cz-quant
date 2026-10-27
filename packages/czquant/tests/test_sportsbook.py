"""DraftKings dataflow, the sportsbook paper adapter, venue routing and settlement."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import httpx
import pytest

from czquant import Broker, Store
from czquant.accounting import portfolio_state
from czquant.draftkings import (Blocked, DraftKings, Nash, OddsApi, OutOfCredits, Result, implied_price, make_ticker,
                                parse_nash, parse_oddsapi, parse_ticker, payout)
from czquant.monitor import market_pass, sportsbook_pass
from czquant.paper import Paper
from czquant.sportsbook import Route, SportsbookPaper, operator_settle


def iso(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def oddsapi_event(event="e1", home="Boston Celtics", away="Brooklyn Nets", home_odds="1.25", away_odds="4.40",
                  start=4.0, updated=0.0, draw=None):
    outcomes = [{"name": home, "price": float(home_odds)}, {"name": away, "price": float(away_odds)}]
    if draw:
        outcomes.append({"name": "Draw", "price": float(draw)})
    return {"id": event, "sport_key": "basketball_nba", "commence_time": iso(start), "home_team": home,
            "away_team": away, "bookmakers": [{"key": "draftkings", "title": "DraftKings", "last_update": iso(updated),
            "markets": [{"key": "h2h", "last_update": iso(updated), "outcomes": outcomes}]},
            {"key": "fanduel", "markets": [{"key": "h2h", "outcomes": []}]}]}


class Source:
    """A static DraftKings source."""
    name = "oddsapi"

    def __init__(self, events=None, results=None):
        self.events, self.result_map, self.reads = events or [], results or {}, 0

    def selections(self, league):
        self.reads += 1
        return parse_oddsapi(self.events, league)

    def results(self, league, events):
        return {e: r for e, r in self.result_map.items() if e in events}


T_HOME = make_ticker("oddsapi", "basketball_nba", "e1", "ML2", "home")
T_AWAY = make_ticker("oddsapi", "basketball_nba", "e1", "ML2", "away")


def bet(tmp_path, source, cash="1000", max_stake="500", **kwargs):
    store = Store(tmp_path)
    store.create("p", cash)
    adapter = SportsbookPaper(DraftKings(source, max_stake=max_stake), latency=0, **kwargs)
    return store, Broker(store, "p", adapter)


def back(broker, ticker, count, price, **extra):
    return broker.submit({"ticker": ticker, "side": "bid", "count": str(count), "price": str(price),
                          "time_in_force": "fill_or_kill", **extra}, {"trade_id": "t"})


# --------------------------------------------------------------------------- normalization

def test_tickers_round_trip_and_reject_malformed():
    assert parse_ticker(T_HOME) == {"source": "oddsapi", "league": "basketball_nba", "event": "e1",
                                    "kind": "ML2", "role": "home"}
    for bad in ("KXNBAGAME-26OCT27BKNBOS-BOS", "DK:oddsapi:nba:e1:ML4:home", "DK:a:b:c"):
        with pytest.raises(ValueError):
            parse_ticker(bad)
    with pytest.raises(ValueError):
        make_ticker("oddsapi", "nba", "e:1", "ML2", "home")


def test_implied_price_rounds_up_and_rejects_impossible_odds():
    assert implied_price(D("2")) == D("0.5")
    assert implied_price(D("1.91")) == D("0.523561")  # 0.5235602... rounded up
    with pytest.raises(ValueError):
        implied_price(D("1"))


def test_parse_oddsapi_keeps_only_draftkings_moneyline():
    rows = parse_oddsapi([oddsapi_event(), oddsapi_event("e2", draw="3.5")], "basketball_nba")
    by_ticker = {r.ticker: r for r in rows}
    assert by_ticker[T_HOME].odds == D("1.25") and by_ticker[T_HOME].name == "Boston Celtics"
    assert by_ticker[T_AWAY].price == implied_price(D("4.4"))
    three = [r for r in rows if r.event == "e2"]
    assert {r.kind for r in three} == {"ML3"} and {r.role for r in three} == {"home", "away", "draw"}
    assert parse_oddsapi([{**oddsapi_event(), "bookmakers": []}], "basketball_nba") == []


def test_parse_nash_relational_payload():
    data = {"events": [{"id": "31", "name": "BKN Nets @ BOS Celtics", "startEventDate": iso(3), "status": "NOT_STARTED",
                        "participants": [{"name": "BOS Celtics", "venueRole": "Home"},
                                         {"name": "BKN Nets", "venueRole": "Away"}]}],
            "markets": [{"id": "m1", "eventId": "31", "marketType": {"name": "Moneyline"}},
                        {"id": "m2", "eventId": "31", "marketType": {"name": "Spread"}}],
            "selections": [{"id": "s1", "marketId": "m1", "label": "BOS Celtics", "outcomeType": "Home",
                            "trueOdds": 1.25, "displayOdds": {"american": "−400"}},
                           {"id": "s2", "marketId": "m1", "label": "BKN Nets", "outcomeType": "Away", "trueOdds": 4.4},
                           {"id": "s3", "marketId": "m2", "label": "BOS -9.5", "outcomeType": "Home", "trueOdds": 1.9}]}
    rows = parse_nash(data, "42648", iso(0))
    assert [(r.role, r.odds, r.home, r.away) for r in rows] == [
        ("home", D("1.25"), "BOS Celtics", "BKN Nets"), ("away", D("4.4"), "BOS Celtics", "BKN Nets")]
    assert rows[0].ticker == "DK:nash:42648:31:ML2:home"


def test_nash_reports_a_cdn_refusal_without_retrying():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403, text="<HTML>Access Denied</HTML>")
    source = Nash("dkusnj", httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(Blocked, match="does not work around"):
        source.selections("42648")
    assert len(calls) == 1 and "python-httpx" in calls[0].headers["user-agent"]


def test_oddsapi_scores_become_results_and_never_log_the_key():
    def handler(request):
        assert request.url.params["apiKey"] == "k"
        if request.url.path.endswith("/scores"):
            return httpx.Response(200, json=[
                {"id": "e1", "completed": True, "home_team": "H", "away_team": "A",
                 "scores": [{"name": "H", "score": "101"}, {"name": "A", "score": "99"}]},
                {"id": "e2", "completed": True, "home_team": "H", "away_team": "A",
                 "scores": [{"name": "H", "score": "3"}, {"name": "A", "score": "3"}]},
                {"id": "e3", "completed": False, "home_team": "H", "away_team": "A", "scores": None}],
                headers={"x-requests-remaining": "480"})
        return httpx.Response(200, json=[oddsapi_event()])
    source = OddsApi("k", httpx.Client(transport=httpx.MockTransport(handler)))
    results = source.results("basketball_nba", ["e1", "e2", "e3"])
    assert results["e1"].winner == "home" and results["e2"].winner == "draw" and "e3" not in results
    assert source.remaining == 480 and "apiKey" not in str(results["e1"].evidence)
    assert len(source.selections("basketball_nba")) == 2


def test_payout_rules():
    win, tie, void = Result("home", False, {}), Result("draw", False, {}), Result(None, True, {})
    assert payout("ML2", "home", win) == 1 and payout("ML2", "away", win) == 0
    assert payout("ML2", "home", tie) is None  # two-way moneyline pushes
    assert payout("ML3", "draw", tie) == 1 and payout("ML3", "home", tie) == 0
    assert payout("ML3", "home", void) is None


# --------------------------------------------------------------------------- paper bets

def test_back_fills_whole_at_current_odds_without_fee(tmp_path):
    store, broker = bet(tmp_path, Source([oddsapi_event()]))
    price = implied_price(D("4.4"))
    result = back(broker, T_AWAY, 100, price)
    assert result["status"] == "filled" and result["fee"] == "0"
    state = portfolio_state(store, "p")
    (lot,) = state.lots.values()
    assert lot["market"] == T_AWAY and lot["side"] == "yes" and lot["quantity"] == 100
    assert state.cash == D("1000") - 100 * price and state.fees == 0
    assert store.reserved("p") == 0
    marks = [e for e in store.events() if e["type"] == "market"]
    assert marks and marks[-1]["data"]["book"]["orderbook_fp"]["yes_dollars"] == []


@pytest.mark.parametrize("event,limit,reason", [
    (oddsapi_event(away_odds="4.0"), implied_price(D("4.4")), "odds_shortened"),
    (oddsapi_event(start=-0.5), D("1"), "in_play"),
    (oddsapi_event(updated=-1), D("1"), "stale_odds"),
    (oddsapi_event("other"), D("1"), "not_offered"),
])
def test_back_is_declined_whole(tmp_path, event, limit, reason):
    store, broker = bet(tmp_path, Source([event]))
    result = back(broker, T_AWAY, 10, limit)
    assert result == {"client_order_id": result["client_order_id"], "status": "canceled", "filled": "0", "reason": reason}
    assert portfolio_state(store, "p").cash == 1000 and store.reserved("p") == 0


def test_better_odds_fill_at_the_better_price(tmp_path):
    store, broker = bet(tmp_path, Source([oddsapi_event(away_odds="5.0")]))
    back(broker, T_AWAY, 10, implied_price(D("4.4")))
    assert portfolio_state(store, "p").cash == D("998")


def test_stake_limit_is_cumulative_per_selection(tmp_path):
    store, broker = bet(tmp_path, Source([oddsapi_event(away_odds="2.0")]), max_stake="60")
    assert back(broker, T_AWAY, 100, "0.5")["status"] == "filled"  # $50 staked
    assert back(broker, T_AWAY, 30, "0.5")["reason"] == "stake_limit"  # $65 > $60
    assert back(broker, T_HOME, 40, "0.9")["status"] == "filled"  # another selection
    assert portfolio_state(store, "p").cash == D("1000") - 50 - 40 * implied_price(D("1.25"))


def test_a_bet_cannot_be_sold_or_rested(tmp_path):
    store, broker = bet(tmp_path, Source([oddsapi_event()]))
    with pytest.raises(NotImplementedError):
        broker.submit({"ticker": T_AWAY, "side": "ask", "count": "1", "price": "0.5",
                       "time_in_force": "fill_or_kill"})
    with pytest.raises(NotImplementedError):
        back(broker, T_AWAY, 1, "0.5", time_in_force="immediate_or_cancel")
    # The broker records an adapter's refusal as a failed paper order and releases its cash.
    assert {r["status"] for r in store.db.execute("SELECT status FROM orders")} == {"failed"}
    assert store.reserved("p") == 0 and not portfolio_state(store, "p").lots


class Kalshi:
    def book(self, ticker):
        return {"orderbook_fp": {"yes_dollars": [], "no_dollars": [["0.80", "50"]]}}

    def market(self, ticker):
        return {"ticker": ticker, "series_ticker": "S", "status": "active"}

    def get(self, path, params=None):
        return {"series": {"fee_type": "quadratic", "fee_multiplier": "1"}}


def test_route_sends_each_leg_to_its_venue_and_shares_cash(tmp_path):
    store = Store(tmp_path)
    store.create("p", "1000")
    adapter = Route(Paper(Kalshi(), latency=0), SportsbookPaper(DraftKings(Source([oddsapi_event()])), latency=0))
    broker = Broker(store, "p", adapter)
    kalshi = broker.submit({"ticker": "KXNBAGAME-26OCT27BKNBOS-BOS", "side": "bid", "count": "10", "price": "0.20",
                            "time_in_force": "immediate_or_cancel"}, {"trade_id": "t"})
    sportsbook = back(broker, T_AWAY, 10, "0.3")
    assert kalshi["status"] == "filled" and sportsbook["status"] == "filled"
    state = portfolio_state(store, "p")
    assert {lot["market"] for lot in state.lots.values()} == {"KXNBAGAME-26OCT27BKNBOS-BOS", T_AWAY}
    # Kalshi: 10 at $0.20 plus the taker fee 0.07 * 10 * 0.2 * 0.8 = 0.112, rounded up.
    assert state.cash == D("1000") - 2 - D("0.12") - 10 * implied_price(D("4.4"))


# --------------------------------------------------------------------------- monitoring and settlement

def test_kalshi_market_monitor_ignores_sportsbook_lots(tmp_path):
    store, broker = bet(tmp_path, Source([oddsapi_event()]))
    back(broker, T_AWAY, 10, "0.3")

    class NoSportsbook:
        def get(self, path, params=None):
            return {"markets": []}

        def market(self, ticker):
            raise AssertionError(f"Kalshi asked for {ticker}")
    status = market_pass(store, NoSportsbook())
    assert status["status"] == "healthy" and status["markets"] == 0


class Paid(Source):
    """A quota-metered source with raw reads: odds cost 1 credit, results 2."""

    def __init__(self, events, credits=100):
        super().__init__(events)
        self.remaining, self.remaining_at, self.fetches, self.result_calls = credits, 0.0, 0, 0

    def _spend(self, cost):
        self.remaining -= cost
        self.remaining_at = __import__("time").time()

    def fetch(self, league):
        self.fetches += 1
        self._spend(1)
        return list(self.events)

    def parse(self, raw, league, fetched_at):
        return parse_oddsapi(raw, league, "draftkings", fetched_at)

    def results(self, league, events):
        self.result_calls += 1
        self._spend(2)
        return super().results(league, events)


def test_monitor_marks_from_the_shared_cache_and_pays_only_for_paced_results(tmp_path):
    source = Paid([oddsapi_event(), oddsapi_event("e2", home_odds="2", away_odds="2")])
    cache = tmp_path / "odds_cache"
    store = Store(tmp_path)
    store.create("p", "1000")
    broker = Broker(store, "p", SportsbookPaper(DraftKings(source, cache_dir=cache), latency=0))
    back(broker, T_AWAY, 100, "0.3")
    back(broker, T_HOME, 100, "0.9")
    push = make_ticker("oddsapi", "basketball_nba", "e2", "ML2", "home")
    back(broker, push, 10, "0.5")
    assert source.fetches == 3  # one paid read per bet, at the fill
    monitor = DraftKings(source, cache_dir=cache)  # another process: shares only the cache directory
    monitor.settle_after_hours = -10  # treat every game as finished long enough to check
    status = sportsbook_pass(store, monitor)
    assert status["status"] == "healthy" and status["markets"] == 3 and status["settled_lots"] == 0
    assert source.fetches == 3 and source.result_calls == 1 and status["credits_remaining"] == 95
    marks = len([e for e in store.events() if e["type"] == "market"])
    status = sportsbook_pass(store, monitor)  # within results_minutes: nothing paid, nothing new to mark
    assert source.result_calls == 1 and len([e for e in store.events() if e["type"] == "market"]) == marks
    source.result_map = {"e1": Result("away", False, {"fixture": 1}), "e2": Result("draw", False, {"fixture": 2})}
    monitor.results_checked.clear()  # 30 minutes later
    status = sportsbook_pass(store, monitor)
    assert status["settled_lots"] == 3 and status["status"] == "healthy" and source.result_calls == 2
    state = portfolio_state(store, "p")
    assert not state.lots and state.settlements == 3
    staked = 100 * implied_price(D("4.4")) + 100 * implied_price(D("1.25")) + 5
    assert state.cash == D("1000") - staked + 100 + 5  # away won; the tie returned its stake
    assert sportsbook_pass(store, monitor)["markets"] == 0 and source.fetches == 3


def test_odds_reads_stop_at_the_reserve_but_results_may_spend_it(tmp_path):
    source = Paid([oddsapi_event()], credits=41)
    book = DraftKings(source, cache_dir=tmp_path, reserve=40)
    assert book.selections("basketball_nba", fresh=True)  # 41 -> 40, exactly the reserve
    with pytest.raises(OutOfCredits):
        book.selections("basketball_nba", fresh=True)  # a further read would dip into the reserve
    assert book.selections("basketball_nba", fetch=False)  # the cached read is free
    assert DraftKings(source, cache_dir=tmp_path, reserve=40).credits() == 40  # shared through the cache
    book.results("basketball_nba", ["e1"])
    assert source.remaining == 38
    store, broker = bet(tmp_path / "s", source)
    broker.adapter.client.reserve = 40
    broker.adapter.client.source = source
    assert back(broker, T_AWAY, 1, "0.3")["reason"] == "no_credits"


def test_operator_settles_a_void_with_evidence(tmp_path):
    store, broker = bet(tmp_path, Source([oddsapi_event()]))
    back(broker, T_AWAY, 10, "0.3")
    with pytest.raises(ValueError):
        operator_settle(store, T_AWAY, "void", " ")
    assert operator_settle(store, T_AWAY, "void", "postponed; DraftKings voided the moneyline") == 1
    state = portfolio_state(store, "p")
    assert state.cash == 1000 and state.realized == 0 and not state.lots
    assert operator_settle(store, T_AWAY, "void", "repeat") == 0
