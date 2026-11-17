import asyncio

from czquant import Store
import paper_ws
from paper_ws import Quote
from retry import RetryConfig, RetryEngine, retry_orders


def order(ticker, side, price):
    return {"ticker": ticker, "side": side, "count": "106", "price": price,
            "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross"}


def result(filled, levels=(), fee="0"):
    return {"status": "filled" if filled else "canceled", "filled": str(filled), "fee": fee,
            "evidence": {"levels": [[str(p), str(q)] for p, q in levels]}}


def brewers_rays(side="long"):
    # 2026-10-28: Milwaukee at 0.85 missed, Tampa Bay filled 106 at 0.14 plus a $0.45 fee.
    legs = [{"ticker": "MIL", "vwap": 0.85, "qty": 106}, {"ticker": "TB", "vwap": 0.14, "qty": 106}]
    return Quote("KXMLBGAME-26OCT272000MILTB", side, 106, 105.94, 0.93, 106.87 - 0.94, 106, 0.13, 1.06, 2.8, 0, 0, 0.17, legs)


def test_brewers_rays_retries_milwaukee_only_within_break_even():
    orders = [order("MIL", "bid", "0.85"), order("TB", "bid", "0.14")]
    # Sports fees are half rate: MIL's estimated fee is ceil(0.035 * 106 * .85 * .15) = $0.48.
    out = retry_orders(brewers_rays(), orders, [0, 106], [0, 15.29], "quadratic", 0.5, max_loss=0.0)
    # Slack is (106 - 15.29 - 0.48 - 90.10) / 106 = $0.0012 a contract: still the 0.85 cent.
    assert [(o["ticker"], o["side"], o["count"], o["price"]) for o in out] == [("MIL", "bid", "106", "0.85")]
    # Allowing a $5 loss to get hedged lifts the limit by 4.8 cents.
    out = retry_orders(brewers_rays(), orders, [0, 106], [0, 15.29], "quadratic", 0.5, max_loss=5.0)
    assert out[0]["price"] == "0.89"
    # Paying more for the filled leg leaves no budget: nothing is sent.
    assert retry_orders(brewers_rays(), orders, [0, 106], [0, 16.00], "quadratic", 0.5, max_loss=0.0) == []


def test_short_legs_are_priced_on_the_no_side():
    q = brewers_rays("short")
    q.legs = [{"ticker": "A", "vwap": 0.60, "qty": 10}, {"ticker": "B", "vwap": 0.38, "qty": 10}]
    q.sets, q.payout = 10, 10
    orders = [order("A", "ask", "0.40"), order("B", "ask", "0.62")]
    out = retry_orders(q, orders, [10, 0], [6.0, 0], "quadratic", 0.0, max_loss=0.0)
    # NO on B may cost up to 0.38 + 0.02 = 0.40, so YES is sold no lower than 0.60.
    assert [(o["ticker"], o["side"], o["count"], o["price"]) for o in out] == [("B", "ask", "10", "0.60")]


def test_repair_completes_the_basket_and_records_it(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("CZQUANT_PORTFOLIO", "retry")
    monkeypatch.setattr(paper_ws, "WS_RUNS", str(tmp_path / "diagnostics"))
    store = Store(tmp_path)
    store.create("retry")
    engine = RetryEngine(RetryConfig(retry_delay=0, retry_max_loss=5))
    engine.universe = {"KXMLBGAME-26OCT272000MILTB": {"fee_type": "quadratic", "fee_mult": 0.5}}
    sent = []

    async def submit(pending, context):
        sent.append([(o["ticker"], o["count"], o["price"]) for o in pending])
        n = 50 if len(sent) == 1 else int(pending[0]["count"])
        return [result(n, [(0.86, n)], "0.20")]

    monkeypatch.setattr(engine, "submit_legs", submit)
    orders = [order("MIL", "bid", "0.85"), order("TB", "bid", "0.14")]
    asyncio.run(engine.repair(brewers_rays(), {"trade_id": "t"}, orders,
                              [result(0), result(106, [(0.14, 106)], "0.45")]))
    assert [s[0][:2] for s in sent] == [("MIL", "106"), ("MIL", "56")]
    record = [e["data"] for e in store.events() if e["data"].get("kind") == "basket_retry"][-1]
    assert record["complete"] and record["stopped"] == "complete"
    assert record["legs"][0] == {"ticker": "MIL", "filled": 106, "retried": 106, "paid": 91.56}
