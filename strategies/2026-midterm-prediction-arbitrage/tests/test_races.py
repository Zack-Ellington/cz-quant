"""Race discovery, especially the Kentucky/Louisiana ticker trap."""

from __future__ import annotations

from strategy.races import discover_races


def _by_id(races):
    return {r.race_id: r for r in races}


def test_chamber_counts():
    races = discover_races()
    senate = [r for r in races if r.chamber == "senate"]
    house = [r for r in races if r.chamber == "house"]
    assert len(senate) == 35
    assert len(house) >= 400
    assert len(house) == 435


def test_kentucky_louisiana_trap():
    races = _by_id(discover_races())
    # Kentucky's race is filed under the Louisiana-suffixed Kalshi event.
    assert races["senate:KY"].event == "SENATELA-26"
    assert races["senate:KY"].d_ticker == "SENATELA-26-D"
    # Louisiana itself has no market and is scored from its prior.
    assert races["senate:LA"].event is None
    assert races["senate:LA"].has_market is False
    # There is no straight-through Kentucky event anywhere.
    assert all(r.event != "SENATEKY-26" for r in races.values())


def test_special_elections_use_s_suffix():
    races = _by_id(discover_races())
    assert races["senate:FL"].event == "SENATEFLS-26"
    assert races["senate:OH"].event == "SENATEOHS-26"


def test_race_ids_unique_and_prefixed():
    races = discover_races()
    ids = [r.race_id for r in races]
    assert len(ids) == len(set(ids))
    assert all(r.race_id.startswith(("senate:", "house:")) for r in races)


def test_safe_house_seats_have_no_market():
    races = discover_races()
    safe = [r for r in races if r.is_safe and r.chamber == "house"]
    assert len(safe) == 435 - 67
    assert all(r.event is None for r in safe)
