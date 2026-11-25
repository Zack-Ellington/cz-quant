"""Fixture coverage for read-only odds sources; no venue requests."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import httpx
import pytest

from api import (Blocked, Nash, OddsApi, implied_probability, make_selection_id,
                 parse_nash, parse_oddsapi, parse_selection_id)

T_HOME = make_selection_id("oddsapi", "basketball_nba", "e1", "ML2", "home")
T_AWAY = make_selection_id("oddsapi", "basketball_nba", "e1", "ML2", "away")

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


def test_selection_ids_round_trip_and_reject_malformed():
    assert parse_selection_id(T_HOME) == {"source": "oddsapi", "league": "basketball_nba", "event": "e1",
                                    "kind": "ML2", "role": "home"}
    for bad in ("KXNBAGAME-26OCT27BKNBOS-BOS", "DK:oddsapi:nba:e1:ML4:home", "DK:a:b:c"):
        with pytest.raises(ValueError):
            parse_selection_id(bad)
    with pytest.raises(ValueError):
        make_selection_id("oddsapi", "nba", "e:1", "ML2", "home")


def test_implied_probability_and_rejects_impossible_odds():
    assert implied_probability(D("2")) == D("0.5")
    assert implied_probability(D("1.91")) == D(1) / D("1.91")
    with pytest.raises(ValueError):
        implied_probability(D("1"))


def test_parse_oddsapi_keeps_only_draftkings_moneyline():
    rows = parse_oddsapi([oddsapi_event(), oddsapi_event("e2", draw="3.5")], "basketball_nba")
    by_id = {r.selection_id: r for r in rows}
    assert by_id[T_HOME].odds == D("1.25") and by_id[T_HOME].name == "Boston Celtics"
    assert by_id[T_AWAY].implied_probability == implied_probability(D("4.4"))
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
    assert rows[0].selection_id == "DK:nash:42648:31:ML2:home"


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
