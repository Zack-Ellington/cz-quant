import pytest

from types import SimpleNamespace

import json
import threading
import time

from odds_monitor import Monitor, Watch, edges, kalshi_fee, poly_fee, windows

SPORTS = {"enabled": True, "rate": 0.05, "exponent": 1}


def test_fees():
    # Kalshi: 0.07 * 100 * 0.40 * 0.60 = $1.68 on 100 contracts, rounded up per order
    assert kalshi_fee(0.40, 1.0, 100) == pytest.approx(0.0168)
    assert kalshi_fee(0.50, 1.0, 1) == pytest.approx(0.02)
    # Polymarket: 0.05 * 0.55 * 0.45 per share
    assert poly_fee(0.55, SPORTS) == pytest.approx(0.012375)
    assert poly_fee(0.55, {"enabled": False, "rate": 0.05}) == 0


def test_edges_pick_the_cheaper_cross_venue_assignment():
    # team 0 on Kalshi: 0.40 + 0.0168; team 1 on Polymarket: 0.55 + 0.012375
    out = edges([0.40, 0.70], [0.65, 0.55], 1.0, SPORTS, 100)
    assert out["kalshi"] == 0
    assert out["edge"] == pytest.approx(1 - (0.4168 + 0.562375))
    assert out["kalshi_only"] < 0 and out["poly_only"] < 0


def test_edges_need_both_venues():
    assert edges([0.40, 0.70], [None, None], 1.0, SPORTS, 100)["edge"] is None
    assert edges([None, 1.0], [0.5, 0.5], 1.0, SPORTS, 100)["edge"] is None  # a $1 ask is no offer


def point(t, edge, live=True):
    return {"t": t, "live": live, "k": [[0.38, 0.40, None], [0.6, 0.7, None]],
            "p": [[0.6, 0.65, None], [0.5, 0.55, None]],
            "arb": {"edge": edge, "kalshi": 0 if edge is not None else None}}


def test_windows_split_on_threshold_and_gaps():
    pts = [point(0, -0.01), point(10, 0.02), point(20, 0.03), point(30, -0.01),
           point(40, 0.01), point(1000, 0.01)]
    out = windows(pts, 0.0, gap=60)
    assert [(w["start"], w["end"]) for w in out] == [(10, 30), (40, 40), (1000, None)]
    assert out[0]["best"] == 0.03 and out[0]["at"] == 20
    assert out[0]["kalshi_ask"] == 0.40 and out[0]["poly_ask"] == 0.55
    assert windows(pts, 0.025, gap=60)[0]["start"] == 20


def test_a_window_touching_history_is_an_estimate():
    out = windows([point(0, 0.02, live=False), point(10, 0.02), point(20, 0.02)], 0.0, gap=60)
    assert len(out) == 1 and out[0]["estimate"] is True
    assert windows([point(0, 0.02), point(10, 0.02)], 0.0, gap=60)[0]["estimate"] is False


def _q(bid, ask):
    return [bid, ask, None]


def test_points_are_extended_incrementally_and_match_a_full_recompute(tmp_path):
    game = SimpleNamespace(event="KXNFLGAME-X", teams=[{"market": {"ticker": "A"}}, {"market": {"ticker": "B"}}])
    w = Watch(game, tmp_path / "x.jsonl")
    w.schedule = SPORTS
    m = object.__new__(Monitor)
    m.fee_multiplier, m.sets = 1.0, 100

    def fresh():
        copy = Watch(game, tmp_path / "none.jsonl")
        copy.schedule, copy.history, copy.live = w.schedule, dict(w.history), dict(w.live)
        return [(p["t"], p["live"], p["arb"]["edge"]) for p in m.points(copy)]

    w.history = {1.0: {"k": [_q(.3, .32), _q(.6, .62)], "p": [_q(.3, .31), _q(.6, .61)]},
                 50.0: {"k": [_q(.3, .33), _q(.6, .63)], "p": [_q(.3, .31), _q(.6, .61)]}}
    w.add_live(20.0, [_q(.3, .32), _q(.6, .62)], [_q(None, None), _q(.6, .61)])
    assert [(p["t"], p["live"]) for p in m.points(w)] == [(1.0, False), (20.0, True)]  # history after live starts is dropped
    first = m.points(w)
    w.add_live(30.0, [_q(.2, .25), _q(.7, .7)], [_q(.25, .3), _q(.6, .6)])
    after = m.points(w)
    assert after is first and len(after) == 3  # extended in place, not rebuilt
    assert [(p["t"], p["live"], p["arb"]["edge"]) for p in after] == fresh()
    w.history = {0.5: {"k": [_q(.3, .32), _q(.6, .62)], "p": [_q(.3, .31), _q(.6, .61)]}}  # a backfill starts over
    assert [p["t"] for p in m.points(w)] == [0.5, 20.0, 30.0]
    assert [(p["t"], p["live"], p["arb"]["edge"]) for p in m.points(w)] == fresh()


def test_a_closed_game_is_restored_from_its_saved_pairing(tmp_path):
    event = {"event_ticker": "KXNFLGAME-26NOV16LACBAL", "series_ticker": "KXNFLGAME", "title": "LAC at BAL",
             "markets": [{"ticker": "KXNFLGAME-26NOV16LACBAL-LAC", "yes_sub_title": "Los Angeles C", "status": "finalized"},
                         {"ticker": "KXNFLGAME-26NOV16LACBAL-BAL", "yes_sub_title": "Baltimore", "status": "finalized"}]}
    (tmp_path / "KXNFLGAME-26NOV16LACBAL.meta.json").write_text(json.dumps({"event": event, "schedule": SPORTS, "poly": None}))
    now = time.time()
    rows = [{"t": now - 60, "k": [_q(.15, .16), _q(.84, .85)], "p": [_q(.32, .33), _q(.67, .68)]}]
    (tmp_path / "KXNFLGAME-26NOV16LACBAL.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    old = {"t": now - 10 * 86400, "k": rows[0]["k"], "p": rows[0]["p"]}
    (tmp_path / "KXNFLGAME-26NOV01AAABBB.jsonl").write_text(json.dumps(old) + "\n")  # too old: left out
    m = object.__new__(Monitor)
    m.dir, m.watches, m.finished, m.lock, m.interval = tmp_path, {}, {}, threading.Lock(), 10.0
    m.fee_multiplier, m.sets, m._ledger, m._ledger_lock = 1.0, 100, {}, threading.Lock()
    m.restore_finished()
    assert list(m.finished) == ["KXNFLGAME-26NOV16LACBAL"]
    m.ledger = lambda: {}
    arbs = m.arbs_view(0.01)
    assert [(a["event"], a["source"], a["kalshi_team"], a["poly_team"]) for a in arbs] == \
        [("KXNFLGAME-26NOV16LACBAL", "live", "LAC", "BAL")]
