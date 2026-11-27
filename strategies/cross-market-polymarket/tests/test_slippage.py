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
