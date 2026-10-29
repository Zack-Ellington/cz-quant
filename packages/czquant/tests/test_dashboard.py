"""The dashboard's numbers follow the report's valuation; it reads only the store."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from czquant import Store
from czquant.dashboard import Ledger
from czquant.report import compare


def iso(seconds_ago=0.0):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def book(bid, ask, depth="100"):
    """A Kalshi-style book: YES bid, and a NO bid at 1 - ask."""
    return {"orderbook_fp": {"yes_dollars": [[bid, depth]] if bid else [], "no_dollars": [[str(1 - D(ask)), depth]] if ask else []}}


def ledger_fixture(tmp_path):
    store = Store(tmp_path)
    store.create("sum-low", "1000")
    store.create("cross", "500")
    store.append("run", {"status": "started", "strategy": "kalshi_sum_check", "revision": "r", "config": {}}, "sum-low")
    store.append("run", {"status": "started", "strategy": "cross-market-sportbook", "revision": "r", "config": {}}, "cross")
    # sum-low: a lot on K1 bought at 0.40, then 10 of 20 sold at 0.50: realized +1 before fees.
    store.append("transaction", {"reason": "fill", "cash_delta": "-8.10", "fee": "0.10", "context": {},
                 "legs": [{"market": "K1", "side": "yes", "quantity_delta": "20", "price": "0.40"}]}, "sum-low")
    store.append("transaction", {"reason": "fill", "cash_delta": "5", "fee": "0", "context": {},
                 "legs": [{"market": "K1", "side": "yes", "quantity_delta": "-10", "price": "0.50"}]}, "sum-low")
    # cross: a Kalshi lot and a DraftKings bet, which has no bid and is never midpoint-marked.
    store.append("transaction", {"reason": "fill", "cash_delta": "-7", "fee": "0", "context": {},
                 "legs": [{"market": "K2", "side": "yes", "quantity_delta": "10", "price": "0.70"}]}, "cross")
    store.append("transaction", {"reason": "fill", "cash_delta": "-2.5", "fee": "0", "context": {},
                 "legs": [{"market": "DK:oddsapi:nba:e:ML2:away", "side": "yes", "quantity_delta": "10", "price": "0.25"}]}, "cross")
    store.append("market", {"ticker": "K1", "market": {"status": "active"}, "book": book("0.55", "0.57")}, at=iso(5))
    store.append("market", {"ticker": "K2", "market": {"status": "active"}, "book": book("0.72", "0.76")}, at=iso(5))
    store.append("market", {"ticker": "DK:oddsapi:nba:e:ML2:away", "market": {"status": "open"},
                            "book": book(None, "0.25")}, at=iso(5))
    store.append("observation", {"kind": "heartbeat", "odds_credits_remaining": 450}, "cross")
    store.append("health", {"component": "market", "status": "healthy"})
    return store


def test_tick_reports_realized_unrealized_and_unmarked_cost(tmp_path):
    store = ledger_fixture(tmp_path)
    ledger = Ledger(store, step=60)
    ledger.backfill()
    rows = {r["id"]: r for r in ledger.tick()["portfolios"]}
    low, cross = rows["sum-low"], rows["cross"]
    assert low["strategy"] == "kalshi_sum_check" and cross["strategy"] == "cross-market-sportbook"
    n = low["now"]
    # Realized: proceeds 5 less half the 8.10 basis. Unrealized: 10 at mid 0.56 less basis 4.05.
    assert n["realized"] == D("0.95") and n["unrealized"] == D("1.55") and n["unrealized_marked"] == D("1.55")
    assert n["net_pnl"] == n["realized"] + n["unrealized"] and n["unmarked_cost"] == 0
    c = cross["now"]
    assert c["unrealized"] is None and c["net_pnl"] is None  # the bet has no midpoint
    assert c["unrealized_marked"] == D("0.40") and c["unmarked_cost"] == D("2.5")  # K2 mid 0.74 on 10
    # The quote range still bounds the bet at [0, 0.25] and K2 at [0.72, 0.76].
    assert c["unrealized_lower"] == D("7.2") - D("9.5") and c["unrealized_upper"] == D("7.6") + D("2.5") - D("9.5")
    assert ledger.health["market"]["status"] == "healthy"
    assert cross["activity"]["odds_credits_remaining"] == 450


def test_the_current_point_matches_the_offline_report(tmp_path):
    store = ledger_fixture(tmp_path)
    ledger = Ledger(store)
    ledger.backfill()
    point = {r["id"]: r for r in ledger.tick()["portfolios"]}["sum-low"]["now"]
    now = datetime.now(timezone.utc).isoformat()
    _, curves = compare(store, now, now)  # the report at one instant, after every fixture record
    last = [r for r in curves if r["portfolio"] == "sum-low"][-1]
    assert last["equity"] == point["equity"] and last["realized"] == point["realized"]
    assert last["equity_lower"] - last["contributed"] == point["net_pnl_lower"]


def test_stale_marks_are_not_valued(tmp_path):
    store = Store(tmp_path)
    store.create("p", "100")
    store.append("transaction", {"reason": "fill", "cash_delta": "-4", "fee": "0", "context": {},
                 "legs": [{"market": "K", "side": "yes", "quantity_delta": "10", "price": "0.40"}]}, "p")
    store.append("market", {"ticker": "K", "market": {"status": "active"}, "book": book("0.50", "0.52")}, at=iso(600))
    ledger = Ledger(store)
    ledger.backfill()
    n = ledger.tick()["portfolios"][0]["now"]
    assert n["unrealized"] is None and n["net_pnl_lower"] is None and n["unmarked_cost"] == D("4")
    assert n["missing"] == ["K"]


def test_new_portfolios_appear_without_configuration_and_history_is_gridded(tmp_path):
    store = ledger_fixture(tmp_path)
    ledger = Ledger(store, step=60)
    ledger.backfill()
    assert set(ledger.series) == {"sum-low", "cross"}
    store.create("brand-new", "250")
    store.append("run", {"status": "started", "strategy": "next-idea", "revision": "r", "config": {}}, "brand-new")
    tick = ledger.tick()
    assert {r["id"] for r in tick["portfolios"]} == {"sum-low", "cross", "brand-new"}
    assert "brand-new" in tick["append"]
    new = {r["id"]: r for r in tick["portfolios"]}["brand-new"]
    assert new["strategy"] == "next-idea" and new["now"]["net_pnl"] == 0
    assert ledger.tick()["append"] == {}  # within one step: no new grid point


def test_halts_show_until_cleared(tmp_path):
    store = ledger_fixture(tmp_path)
    halt = store.append("observation", {"kind": "halt", "reason": "settled trade t lost $1.00"}, "cross")
    ledger = Ledger(store)
    ledger.backfill()
    rows = {r["id"]: r for r in ledger.tick()["portfolios"]}
    assert rows["cross"]["halted"] == ["settled trade t lost $1.00"]
    store.append("observation", {"kind": "halt_cleared", "halt_id": halt, "reason": "reviewed"}, "cross")
    rows = {r["id"]: r for r in ledger.tick()["portfolios"]}
    assert rows["cross"]["halted"] == []


def test_odds_budget_comes_from_files_not_requests(tmp_path):
    store = ledger_fixture(tmp_path)
    (tmp_path / "odds_cache").mkdir()
    (tmp_path / "odds_cache" / "credits.json").write_text(json.dumps({"source": "oddsapi", "remaining": 431, "at": 1}))
    ledger = Ledger(store)
    ledger.backfill()
    assert ledger.tick()["odds"]["credits"]["remaining"] == 431


def test_server_serves_the_page_and_streams_snapshot_then_ticks(tmp_path):
    websockets = pytest.importorskip("websockets")
    import httpx
    from czquant import dashboard

    ledger_fixture(tmp_path)

    async def scenario():
        server = asyncio.create_task(dashboard.serve(str(tmp_path), "127.0.0.1", 8799, interval=0.2, step=60))
        for _ in range(50):
            await asyncio.sleep(0.1)
            try:
                async with httpx.AsyncClient() as client:
                    page = await client.get("http://127.0.0.1:8799/")
                break
            except httpx.ConnectError:
                continue
        assert page.status_code == 200 and "Strategy ledger" in page.text
        async with httpx.AsyncClient() as client:
            script = await client.get("http://127.0.0.1:8799/plotly.min.js")
        assert script.status_code == 200 and "Plotly" in script.text[:5000] and len(script.content) > 1_000_000
        async with websockets.connect("ws://127.0.0.1:8799/ws") as ws:
            first = json.loads(await ws.recv())
            second = json.loads(await ws.recv())
        server.cancel()
        return first, second

    first, second = asyncio.run(scenario())
    assert first["type"] == "snapshot" and set(first["series"]) == {"sum-low", "cross"}
    assert second["type"] == "tick" and len(second["portfolios"]) == 2


def test_a_nearly_decided_one_sided_book_is_marked_at_the_middle_of_its_range(tmp_path):
    store = Store(tmp_path)
    store.create("p", "100")
    store.append("transaction", {"reason": "fill", "cash_delta": "-9", "fee": "0", "context": {},
                 "legs": [{"market": "W", "side": "yes", "quantity_delta": "10", "price": "0.90"}]}, "p")
    store.append("transaction", {"reason": "fill", "cash_delta": "0", "fee": "0", "context": {},
                 "legs": [{"market": "L", "side": "no", "quantity_delta": "10", "price": "0"}]}, "p")
    store.append("market", {"ticker": "W", "market": {"status": "active"}, "book": book("0.99", None)}, at=iso(5))
    store.append("market", {"ticker": "L", "market": {"status": "active"}, "book": book(None, "0.01")}, at=iso(5))
    ledger = Ledger(store)
    ledger.backfill()
    n = ledger.tick()["portfolios"][0]["now"]
    # W: YES in [0.99, 1] marks 0.995. L: NO in [0.99, 1] marks 0.995. Basis 9.
    assert n["unrealized"] == D("19.9") - 9 and n["missing"] == []
    store.append("market", {"ticker": "W", "market": {"status": "active"}, "book": book("0.97", None)}, at=iso(1))
    n = ledger.tick()["portfolios"][0]["now"]
    assert n["unrealized"] is None and n["missing"] == ["W"]  # a 3-cent range is still unmarked
