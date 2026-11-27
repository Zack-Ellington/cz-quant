from decimal import Decimal as D

from slippage import cost, split_limits, second_limit


def legs():
    return [{"limit": "0.50", "tick": "0.01", "fee_coefficient": "0.07"},
            {"limit": "0.39", "tick": "0.0025", "fee_coefficient": "0.11"}]


def test_split_preserves_floor_for_every_partial_quantity():
    original = legs()
    result = split_limits(original, 36, D("0.01"), lambda leg, p: True)
    assert result is not None
    assert all(D(a["limit"]) > D(b["limit"]) for a, b in zip(result, original))
    for n in range(1, 37):
        assert sum(cost(l, D(l["limit"]), n) for l in result) <= n * D("0.99")


def test_second_reuses_price_improvement_and_actual_fees():
    leg = legs()[1]
    expensive = second_limit(leg, 20, D("0.01"), D("0.54"), D("0.0175"), lambda p: True)
    improved = second_limit(leg, 20, D("0.01"), D("0.50"), D("0.01"), lambda p: True)
    assert expensive is not None and improved is not None and improved > expensive
    for n in range(1, 21):
        assert n * D("0.51") + cost(leg, improved, n) <= n * D("0.99")


def test_pool_limit_caps_slippage():
    result = split_limits(legs(), 36, D("0.01"), lambda leg, p: p <= D(leg["limit"]))
    assert result is not None
    assert [l["limit"] for l in result] == ["0.50", "0.3900"]


def test_small_partial_fee_rounding_can_refuse_basket():
    pair = legs()
    pair[1]["limit"] = "0.45"
    assert split_limits(pair, 36, D("0.01"), lambda leg, p: True) is None


def test_no_second_leg_when_actual_fee_exhausts_budget():
    assert second_limit(legs()[1], 1, D("0.01"), D("0.60"), D("0.40"), lambda p: True) is None


from tests.test_experiments import setup
import pytest


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_engine_reprices_after_recorded_fill_and_keeps_matched_basket(setup, mode):
    store, create, build = setup
    engine = build(create(mode=mode), profit_slippage=True, max_basket_dollars=50)
    engine.step()
    reprices = store.events(observations=("slippage_reprice",), kinds=("observation",))
    assert len(reprices) == 1
    quantities = [sum(l["quantity"] for l in b.state().lots.values()) for b in engine.brokers.values()]
    assert quantities == [D(10), D(10)]
    assert not engine.halted
    spent = D(100) - engine.cash
    assert D(10) - spent >= D("0.10")
    assert D(reprices[0]["data"]["first_fees"]) > 0


def test_no_safe_second_limit_repairs_first_fill(setup, monkeypatch):
    store, create, build = setup
    engine = build(create(), profit_slippage=True, max_basket_dollars=50)
    monkeypatch.setattr("trader.second_limit", lambda *args: None)
    repairs = []
    original = engine.unwind
    def record(leg, quantity, context):
        repairs.append((leg, quantity))
        return original(leg, quantity, context)
    monkeypatch.setattr(engine, "unwind", record)
    engine.step()
    assert len(repairs) == 1 and repairs[0][1] == 10
    records = store.events(kinds=("observation",), observations=("basket_execution",))
    assert records[-1]["data"]["outcomes"][1]["status"] == "profit_floor"



def test_unused_kalshi_tick_allowance_moves_to_first_us_leg():
    pair = [{"limit": "0.55", "tick": "0.01", "fee_coefficient": "0.07", "depth": "1000"},
            {"limit": "0.3775", "tick": "0.0025", "fee_coefficient": "0.11", "depth": "100"}]
    result = split_limits(pair, 35, D("0.01"), lambda l, p: 35 * (p + D("0.02") if l["fee_coefficient"] == "0.07" else p + D("0.025")) <= 20)
    assert result is not None
    assert D(result[1]["limit"]) > D("0.3825")
    for n in range(1, 36):
        assert sum(cost(l, D(l["limit"]), n) for l in result) <= n * D("0.99")


@pytest.mark.parametrize("status,expected", [("canceled", 1), ("rejected", 300), ("unknown", 300)])
def test_only_confirmed_empty_ioc_gets_short_cooldown(setup, monkeypatch, status, expected):
    store, create, build = setup
    engine = build(create(), profit_slippage=True, max_basket_dollars=50, cooldown=300)
    import time
    now = time.time()
    monkeypatch.setattr("trader.time.time", lambda: now)
    for broker in engine.brokers.values():
        monkeypatch.setattr(broker, "submit", lambda *a, **k: {"status": status, "filled": "0"})
    engine.step()
    assert engine.cooldown_until == now + expected
    assert not store.events(kinds=("execution",))



def test_empty_ioc_cooldown_survives_ledger_resume_and_restart(setup, monkeypatch):
    import time
    store, create, build = setup
    spec = create()
    engine = build(spec, profit_slippage=True, max_basket_dollars=50)
    def empty(order, broker):
        broker.finish(order["client_order_id"], "canceled")
        return {"status": "canceled", "filled": "0"}
    for broker in engine.brokers.values():
        monkeypatch.setattr(broker.adapter, "create_order", empty)
    before = time.time()
    engine.step()
    assert store.events(kinds=("order",))
    assert before < engine.cooldown_until <= time.time() + 1.01
    deadline = engine.cooldown_until
    engine.resume()
    assert engine.cooldown_until == deadline
    restarted = build(spec, profit_slippage=True, max_basket_dollars=50)
    assert restarted.cooldown_until == deadline
    monkeypatch.setattr("trader.time.time", lambda: deadline + .01)
    assert restarted.risk_block() is None



@pytest.mark.parametrize("block", ["recheck", "profit_floor", "preflight", "capital"])
def test_pre_submission_rejection_does_not_start_cooldown(setup, monkeypatch, block):
    store, create, build = setup
    engine = build(create(), profit_slippage=True, max_basket_dollars=50)
    basket = engine.scan()
    assert basket is not None
    if block == "recheck":
        monkeypatch.setattr(engine, "price_pair", lambda *_, **__: (None, "no_edge"))
    elif block == "profit_floor":
        monkeypatch.setattr("trader.split_limits", lambda *a: None)
    elif block == "preflight":
        monkeypatch.setattr(engine, "check_basket", lambda *a: "insufficient pool cash")
    else:
        monkeypatch.setattr("trader.split_limits", lambda legs, *a: [{**l, "fee": "100"} for l in legs])
    engine.execute(basket)
    assert engine.cooldown_until == 0
    assert engine.risk_block() is None
    assert not store.events(kinds=("order", "execution"))
    assert store.events(kinds=("observation",), observations=("reject",))


def test_kalshi_fill_booked_later_is_bounded_by_its_limit_not_halted(setup, monkeypatch):
    # Live Kalshi fills reach the ledger when the account monitor polls, after submit returns.
    store, create, build = setup
    engine = build(create(), profit_slippage=True, max_basket_dollars=50)
    basket = engine.scan()
    assert basket is not None
    kalshi = next(l for l in basket.legs if l["venue"] == "kalshi")
    other = next(l for l in basket.legs if l["venue"] != "kalshi")
    monkeypatch.setattr(engine, "price_pair", lambda *_, **__: (basket, "ok"))
    # Kalshi goes first: the thinner book leads.
    kalshi["depth"], other["depth"] = "1", "1000"
    deferred, book = [], engine.kalshi_broker.fill
    monkeypatch.setattr(engine.kalshi_broker, "fill", lambda *a, **k: deferred.append((a, k)))
    monkeypatch.setattr(engine, "resume", lambda: None)  # the monitor has not polled yet
    engine.execute(basket)
    assert not engine.halted
    intents = [e["data"]["order"] for e in store.events(kinds=("order",)) if e["data"]["status"] == "intent"]
    assert [o["ticker"] for o in intents] == [kalshi["ticker"], other["ticker"]]
    assert deferred, "the Kalshi leg filled with its receipt deferred"
    reprice = [e["data"] for e in store.events(kinds=("observation",)) if e["data"].get("kind") == "slippage_reprice"]
    assert reprice and D(reprice[0]["first_price_bound"]) >= D(kalshi["limit"])
    assert engine.fill_bound(other, "no-such-order", D(1)) is None  # Polymarket US needs its receipts
    for a, k in deferred:  # the account monitor books the Kalshi fill
        book(*a, **k)
    monkeypatch.undo()
    engine.resume()
    assert not engine.halted


def test_a_basket_narrowed_below_min_edge_goes_at_book_prices(setup, monkeypatch):
    from dataclasses import replace
    store, create, build = setup
    engine = build(create(), profit_slippage=True, max_basket_dollars=50)
    basket = engine.scan()
    assert basket is not None
    limits = {l["ticker"]: l["limit"] for l in basket.legs}
    # The recheck kept it (above recheck_min_edge), but it no longer clears min_edge.
    engine.cfg = replace(engine.cfg, min_edge=float(basket.edge) + 0.01)
    monkeypatch.setattr(engine, "price_pair", lambda *_, **__: (basket, "ok"))
    engine.execute(basket)
    rejects = [e["data"]["reason"] for e in store.events(kinds=("observation",)) if e["data"].get("kind") == "reject"]
    assert "slippage_profit_floor" not in rejects
    intents = [e["data"] for e in store.events(kinds=("order",)) if e["data"]["status"] == "intent"]
    assert len(intents) == 2
    from trader import buy
    assert {i["order"]["ticker"]: i["order"]["price"] for i in intents} == {
        l["ticker"]: buy({**l, "limit": limits[l["ticker"]]})["price"] for l in basket.legs}
