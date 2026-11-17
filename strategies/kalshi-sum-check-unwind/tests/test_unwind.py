import asyncio

from czquant import Store
import paper_ws
from paper_ws import Quote
import unwind
from unwind import UnwindConfig, UnwindEngine, unwind_orders


def order(ticker, side, price):
    return {"ticker": ticker, "side": side, "count": "106", "price": price,
            "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross"}


def result(filled, levels=(), fee="0"):
    return {"status": "filled" if filled else "canceled", "filled": str(filled), "fee": fee,
            "evidence": {"levels": [[str(p), str(q)] for p, q in levels]}}


def test_brewers_rays_sells_the_lone_rays_leg():
    # 2026-10-28: Milwaukee at 0.85 missed, Tampa Bay filled 106 at 0.14 plus a $0.45 fee.
    orders = [order("MIL", "bid", "0.85"), order("TB", "bid", "0.14")]
    out = unwind_orders(orders, [result(0), result(106, [(0.14, 106)], "0.45")], slippage=0.10)
    assert len(out) == 1
    o = out[0]
    # Break-even is 15.29 / 106 = 0.1442; the floor is 0.10 below that.
    assert (o["ticker"], o["side"], o["count"], o["price"], o["reduce_only"]) == ("TB", "ask", "106", "0.04", True)


def test_complete_sets_are_kept_and_no_legs_are_bought_back_higher():
    orders = [order("A", "ask", "0.30"), order("B", "ask", "0.70"), order("C", "ask", "0.10")]
    results = [result(106, [(0.30, 106)]), result(40, [(0.70, 40)]), result(106, [(0.10, 106)])]
    out = {o["ticker"]: o for o in unwind_orders(orders, results, slippage=0.05)}
    assert set(out) == {"A", "C"} and out["A"]["count"] == out["C"]["count"] == "66"
    # NO on A cost 0.70; it is closed by buying YES back at no more than 0.30 + 0.05.
    assert (out["A"]["side"], out["A"]["price"]) == ("bid", "0.35")


def test_repair_retries_the_unsold_remainder_and_records_it(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("CZQUANT_PORTFOLIO", "unwind")
    monkeypatch.setattr(paper_ws, "WS_RUNS", str(tmp_path / "diagnostics"))
    store = Store(tmp_path)
    store.create("unwind")
    engine = UnwindEngine(UnwindConfig(unwind_delay=0))
    sent = []

    async def submit(pending, context):
        sent.append([(o["ticker"], o["count"]) for o in pending])
        return [result(60 if len(sent) == 1 else int(o["count"])) for o in pending]

    monkeypatch.setattr(engine, "submit_legs", submit)
    q = Quote("E", "long", 106, 0, 0, 0, 106, 0, 0, 1, 0, 0, 0, [])
    orders = [order("MIL", "bid", "0.85"), order("TB", "bid", "0.14")]
    asyncio.run(engine.repair(q, {"trade_id": "t"}, orders, [result(0), result(106, [(0.14, 106)], "0.45")]))
    assert sent == [[("TB", "106")], [("TB", "46")]]
    record = [e["data"] for e in store.events() if e["data"].get("kind") == "basket_unwind"][-1]
    assert record["complete"] and record["legs"] == [{"ticker": "TB", "excess": 106, "sold": 106}]
    assert engine.broker.state() and unwind.UnwindEngine.strategy == "kalshi_sum_check_unwind"
