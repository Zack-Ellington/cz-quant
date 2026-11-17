from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from matching import EASTERN, kalshi_game, match, name_matches


@pytest.mark.parametrize("kalshi,abbr,book,ok", [
    ("Boston", "BOS", "Boston Celtics", True),
    ("Boston", "BOS", "BOS Celtics", True),           # nash style, by abbreviation
    ("Brooklyn", "BKN", "BKN Nets", True),
    ("New York", "NYK", "New York Knicks", True),
    ("New York", "NYK", "NY Knicks", True),
    ("New York", "NYK", "Brooklyn Nets", False),
    ("Los Angeles L", "LAL", "Los Angeles Lakers", True),
    ("Los Angeles L", "LAL", "LA Clippers", False),
    ("Chicago WS", "CWS", "Chicago White Sox", True),
    ("Chicago WS", "CWS", "Chicago Cubs", False),
    ("Tampa Bay", "TB", "Tampa Bay Rays", True),
    ("Columbus", "CBJ", "COL Avalanche", False),       # no reverse prefixes
    ("Columbus", "CBJ", "CBJ Blue Jackets", True),
    ("Montreal", "MTL", "Montréal Canadiens", True),    # accents are ignored
    ("St. Louis", "STL", "St Louis Blues", True),
    ("", "X", "Boston Celtics", False),
])
def test_name_matches(kalshi, abbr, book, ok):
    assert name_matches(kalshi, abbr, book) is ok


def kalshi_event(ticker, teams, tie=False):
    markets = [{"ticker": f"{ticker}-{abbr}", "yes_sub_title": name, "status": "active"} for name, abbr in teams]
    if tie:
        markets.append({"ticker": f"{ticker}-TIE", "yes_sub_title": "Tie", "status": "active"})
    return {"event_ticker": ticker, "series_ticker": ticker.split("-")[0], "markets": markets}


def selections(event, home, away, start, draw=False):
    roles = {"home": home, "away": away, **({"draw": "Draw"} if draw else {})}
    return [SimpleNamespace(event=event, role=r, home=home, away=away, start=start.isoformat(),
                            ticker=f"DK:oddsapi:x:{event}:ML2:{r}") for r in roles]


def test_kalshi_game_reads_date_time_and_teams():
    game = kalshi_game(kalshi_event("KXMLBGAME-26OCT272000MILTB", [("Milwaukee", "MIL"), ("Tampa Bay", "TB")]))
    assert game is not None and str(game.day) == "2026-10-27"
    assert game.start == datetime(2026, 10, 27, 20, 0, tzinfo=EASTERN)
    assert [t["abbr"] for t in game.teams] == ["MIL", "TB"]
    assert kalshi_game(kalshi_event("KXNBAGAME-BAD", [("A", "A"), ("B", "B")])) is None
    assert kalshi_game(kalshi_event("KXNBAGAME-26OCT27BOSX", [("Boston", "BOS")])) is None


def test_match_uses_the_eastern_date_and_assigns_roles():
    game = kalshi_game(kalshi_event("KXNBAGAME-26OCT27BKNBOS", [("Brooklyn", "BKN"), ("Boston", "BOS")]))
    late = datetime(2026, 10, 28, 1, 30, tzinfo=timezone.utc)  # 21:30 ET on Oct 27
    rows = (selections("e1", "Boston Celtics", "Brooklyn Nets", late)
            + selections("e2", "Boston Celtics", "Brooklyn Nets", late + timedelta(days=2))
            + selections("e3", "Detroit Pistons", "New York Knicks", late))
    pair, why = match(game, rows, draw=False)
    assert why == "ok" and pair is not None and pair.book_event == "e1"
    assert pair.roles == {"KXNBAGAME-26OCT27BKNBOS-BKN": "away", "KXNBAGAME-26OCT27BKNBOS-BOS": "home"}
    assert set(pair.selections) == {"home", "away"}


def test_doubleheaders_need_the_start_time_or_are_ambiguous():
    first = datetime(2026, 7, 4, 17, 5, tzinfo=timezone.utc)   # 13:05 ET
    second = datetime(2026, 7, 4, 23, 10, tzinfo=timezone.utc)  # 19:10 ET
    rows = (selections("g1", "New York Yankees", "Boston Red Sox", first)
            + selections("g2", "New York Yankees", "Boston Red Sox", second))
    timed = kalshi_game(kalshi_event("KXMLBGAME-26JUL041910BOSNYY", [("Boston", "BOS"), ("New York Y", "NYY")]))
    pair, why = match(timed, rows, draw=False)
    assert why == "ok" and pair is not None and pair.book_event == "g2"
    untimed = kalshi_game(kalshi_event("KXMLBGAME-26JUL04BOSNYY", [("Boston", "BOS"), ("New York Y", "NYY")]))
    assert match(untimed, rows, draw=False) == (None, "ambiguous")


def test_same_city_both_sides_is_never_guessed():
    game = kalshi_game(kalshi_event("KXNHLGAME-26NOV03NYINYR", [("New York", "NYI"), ("New York", "NYR")]))
    rows = selections("e", "New York Rangers", "New York Islanders", datetime(2026, 11, 4, 0, 0, tzinfo=timezone.utc))
    assert match(game, rows, draw=False) == (None, "unmatched")


def test_outcome_sets_must_agree_and_pins_override():
    start = datetime(2026, 10, 28, 0, 0, tzinfo=timezone.utc)
    two_way = kalshi_game(kalshi_event("KXEPLGAME-26OCT27ARSCHE", [("Arsenal", "ARS"), ("Chelsea", "CHE")]))
    rows = selections("s", "Arsenal", "Chelsea", start, draw=True)
    assert match(two_way, rows, draw=True) == (None, "outcome_set_mismatch")
    three_way = kalshi_game(kalshi_event("KXEPLGAME-26OCT27ARSCHE", [("Arsenal", "ARS"), ("Chelsea", "CHE")], tie=True))
    pair, why = match(three_way, rows, draw=True)
    assert why == "ok" and pair is not None and pair.roles["KXEPLGAME-26OCT27ARSCHE-TIE"] == "draw"
    pin = {"event": "s", "roles": {"KXEPLGAME-26OCT27ARSCHE-ARS": "away", "KXEPLGAME-26OCT27ARSCHE-CHE": "home",
                                   "KXEPLGAME-26OCT27ARSCHE-TIE": "draw"}}
    pinned, why = match(three_way, rows, draw=True, pinned=pin)
    assert why == "ok" and pinned is not None and pinned.evidence == {"method": "pinned"}
    assert pinned.roles == pin["roles"]
