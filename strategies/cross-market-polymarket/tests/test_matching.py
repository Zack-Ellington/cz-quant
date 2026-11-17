import json
from datetime import datetime, timezone

from matching import kalshi_game, match, poly_game


def kalshi_event(ticker, teams, tie=False):
    markets = [{"ticker": f"{ticker}-{abbr}", "yes_sub_title": name, "status": "active"} for name, abbr in teams]
    if tie:
        markets.append({"ticker": f"{ticker}-TIE", "yes_sub_title": "Tie", "status": "active"})
    return {"event_ticker": ticker, "series_ticker": ticker.split("-")[0], "markets": markets}


def poly_event(slug, teams, day, start, outcomes=None, extra_markets=()):
    """A Polymarket game event: teams with abbreviations, and a two-outcome moneyline."""
    names = [n for n, _ in teams]
    return {"slug": slug, "eventDate": day, "startTime": start,
            "teams": [{"name": n, "abbreviation": a} for n, a in teams],
            "markets": [{"sportsMarketType": "moneyline", "outcomes": json.dumps(outcomes or names),
                         "clobTokenIds": json.dumps([f"tok-{a}" for _, a in teams])}, *extra_markets]}


CELTICS_KINGS = poly_event("nba-bos-sac-2026-11-21", [("Celtics", "bos"), ("Kings", "sac")],
                           "2026-11-21", "2026-11-22T03:00:00Z")


def test_poly_game_maps_each_team_abbreviation_to_its_outcome_token():
    game = poly_game(CELTICS_KINGS)
    assert game is not None and str(game.day) == "2026-11-21"
    assert game.tokens == {"bos": "tok-bos", "sac": "tok-sac"}
    assert game.start == datetime(2026, 11, 22, 3, tzinfo=timezone.utc)
    # The outcome order doesn't matter: tokens follow the outcome names.
    swapped = poly_game(poly_event("x", [("Celtics", "bos"), ("Kings", "sac")], "2026-11-21",
                                   "2026-11-22T03:00:00Z", outcomes=["Kings", "Celtics"]))
    assert swapped is not None and swapped.tokens == {"bos": "tok-sac", "sac": "tok-bos"}


def test_events_without_one_clean_moneyline_are_skipped():
    assert poly_game(poly_event("x", [("Celtics", "bos"), ("Kings", "sac")], "2026-11-21", None,
                                outcomes=["Yes", "No"])) is None  # outcomes aren't the teams
    no_line = dict(CELTICS_KINGS, markets=[{"sportsMarketType": "spreads"}])
    assert poly_game(no_line) is None


def test_match_needs_the_same_date_and_abbreviations():
    game = kalshi_game(kalshi_event("KXNBAGAME-26NOV21BOSSAC", [("Boston", "BOS"), ("Sacramento", "SAC")]))
    assert game is not None
    other_day = poly_game(poly_event("y", [("Celtics", "bos"), ("Kings", "sac")], "2026-12-21", "2026-12-22T03:00:00Z"))
    other_teams = poly_game(poly_event("z", [("Celtics", "bos"), ("Knicks", "ny")], "2026-11-21", "2026-11-22T00:00:00Z"))
    polys = [g for g in (poly_game(CELTICS_KINGS), other_day, other_teams) if g]
    pair, why = match(game, polys)
    assert why == "ok" and pair is not None and pair.poly.slug == "nba-bos-sac-2026-11-21"
    assert pair.legs == {"KXNBAGAME-26NOV21BOSSAC-BOS": "tok-bos", "KXNBAGAME-26NOV21BOSSAC-SAC": "tok-sac"}
    assert match(game, [g for g in (other_day, other_teams) if g]) == (None, "unmatched")
    assert match(game, polys * 2)[1] == "ambiguous"


def test_aliases_bridge_abbreviations_spelled_differently():
    game = kalshi_game(kalshi_event("KXNBAGAME-26NOV21GSWSAC", [("Golden State", "GSW"), ("Sacramento", "SAC")]))
    assert game is not None
    poly = poly_game(poly_event("nba-gs-sac", [("Warriors", "gs"), ("Kings", "sac")], "2026-11-21", "2026-11-22T03:00:00Z"))
    assert poly is not None
    assert match(game, [poly]) == (None, "unmatched")
    pair, why = match(game, [poly], {"GSW": "gs"})
    assert why == "ok" and pair is not None and pair.legs["KXNBAGAME-26NOV21GSWSAC-GSW"] == "tok-gs"


def test_doubleheaders_are_told_apart_by_start_time():
    first = poly_game(poly_event("g1", [("Yankees", "nyy"), ("Red Sox", "bos")], "2026-07-04", "2026-07-04T17:05:00Z"))
    second = poly_game(poly_event("g2", [("Yankees", "nyy"), ("Red Sox", "bos")], "2026-07-04", "2026-07-04T23:10:00Z"))
    game = kalshi_game(kalshi_event("KXMLBGAME-26JUL041910BOSNYY", [("Boston", "BOS"), ("New York Y", "NYY")]))
    assert game is not None and first and second
    pair, why = match(game, [first, second])
    assert why == "ok" and pair is not None and pair.poly.slug == "g2"


def test_three_way_kalshi_events_pair_on_their_team_markets():
    game = kalshi_game(kalshi_event("KXEPLGAME-26NOV21ARSCHE", [("Arsenal", "ARS"), ("Chelsea", "CHE")], tie=True))
    poly = poly_game(poly_event("e", [("Arsenal", "ars"), ("Chelsea", "che")], "2026-11-21", "2026-11-21T15:00:00Z"))
    assert game is not None and poly is not None
    pair, why = match(game, [poly])
    assert why == "ok" and pair is not None and len(pair.legs) == 2
    assert pair.evidence["kalshi_tie_market"] is True


def test_a_pin_overrides_the_abbreviations():
    game = kalshi_game(kalshi_event("KXNBAGAME-26NOV21BOSSAC", [("Boston", "BOS"), ("Sacramento", "SAC")]))
    poly = poly_game(CELTICS_KINGS)
    assert game is not None and poly is not None
    pin = {"slug": "nba-bos-sac-2026-11-21", "legs": {"KXNBAGAME-26NOV21BOSSAC-BOS": "BOS",
                                                      "KXNBAGAME-26NOV21BOSSAC-SAC": "sac"}}
    pair, why = match(game, [poly], pinned=pin)
    assert why == "ok" and pair is not None and pair.evidence["method"] == "pinned"
    assert match(game, [poly], pinned=dict(pin, slug="missing")) == (None, "pinned_event_not_offered")


def test_nfl_moneylines_name_outcomes_by_nickname():
    # Polymarket lists the teams in full but names the NFL moneyline's outcomes by nickname.
    event = poly_event("nfl-mia-buf-2026-11-22", [("Miami Dolphins", "mia"), ("Buffalo Bills", "buf")],
                       "2026-11-22", "2026-11-22T18:00:00Z", outcomes=["Dolphins", "Bills"])
    game = poly_game(event)
    assert game is not None and game.tokens == {"mia": "tok-mia", "buf": "tok-buf"}
    assert game.names == {"mia": "Miami Dolphins", "buf": "Buffalo Bills"}
    kalshi = kalshi_game(kalshi_event("KXNFLGAME-26NOV22MIABUF", [("Buffalo", "BUF"), ("Miami", "MIA")]))
    assert kalshi is not None
    pair, why = match(kalshi, [game])
    assert why == "ok" and pair is not None
    assert pair.legs == {"KXNFLGAME-26NOV22MIABUF-BUF": "tok-buf", "KXNFLGAME-26NOV22MIABUF-MIA": "tok-mia"}


def test_a_nickname_that_fits_both_teams_is_never_guessed():
    event = poly_event("x", [("New York Giants", "nyg"), ("San Francisco Giants", "sf")], "2026-11-22",
                       "2026-11-22T18:00:00Z", outcomes=["Giants", "Other"])
    assert poly_game(event) is None
