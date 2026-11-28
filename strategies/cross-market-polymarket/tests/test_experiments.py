from datetime import datetime, timezone
"""Isolated allocations and sequential failure recovery, entirely offline."""
import json
from dataclasses import asdict, replace
from decimal import Decimal as D

import httpx
import pytest
from czquant import Store
from czquant.live import KalshiLive
from czquant.paper import Paper
from czquant.polymarket_us import PolymarketUSPaper

from experiments import Outcome
from tests.test_trader import EVENT, FakeKalshi, experiment
from tests.test_trader_us import FakeUSVenue, LiveKalshi, SLUG
from trader import Config, Engine, load_experiment


class GameKalshi(FakeKalshi):
    def __init__(self, event=EVENT):
        super().__init__()
        self.event = event

    def market(self, ticker):
        return {**super().market(ticker), "event_ticker": self.event}

    def pages(self, *args, **kwargs):
        for row in super().pages(*args, **kwargs):
            yield {**row, "event_ticker": self.event, "markets": [
                {**m, "ticker": m["ticker"].replace(EVENT, self.event)} for m in row["markets"]]}


class GameUS(FakeUSVenue):
    def __init__(self, event, slug, **kwargs):
        super().__init__(**kwargs)
        self.event, self.slug = event, slug

    def events(self, **kwargs):
        for row in super().events(**kwargs):
            if row.get("teams"):
                yield {**row, "slug": self.event}

    def markets_by_id(self, ids):
        return [{**row, "slug": self.slug} for row in super().markets_by_id(ids)]

    def market_raw(self, slug):
        return {**super().market_raw(slug), "slug": self.slug}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_RUNS_DIR", str(tmp_path))
    # Neither environment selector may override explicit experiment ownership.
    monkeypatch.setenv("CZQUANT_KALSHI_POOL", "wrong-k")
    monkeypatch.setenv("CZQUANT_POLYMARKET_POOL", "wrong-p")
    store = Store(tmp_path)

    def create(name="a", mode="paper", scopes=None):
        base = experiment("polymarket_us", mode, name + "-k", name + "-p")
        event = EVENT if name == "a" else EVENT.replace("BOSSAC", "SACBOS")
        slug = SLUG if name == "a" else "aec-nba-sac-bos"
        selected = replace(base.pair, kalshi_event=event,
                           polymarket_event=base.pair.polymarket_event.replace("bos-sac", "sac-bos") if name != "a" else base.pair.polymarket_event,
                           polymarket_market=slug,
                           outcomes=tuple(Outcome(o.kalshi.replace(EVENT, event), o.kalshi_side,
                                                  "PMUS:" + slug, o.polymarket_side) for o in base.pair.outcomes))
        spec = replace(base, id=name, pair=selected)
        for venue, pool in (("kalshi", spec.portfolio["kalshi"]), (spec.pair.venue, spec.portfolio[spec.pair.venue])):
            store.create_pool(pool, venue, "50", mode=mode, subaccount=1,
                              markets=(scopes or {}).get(venue, sorted({m for m, _ in spec.pair.contracts(venue)})))
            if mode == "live":
                store.append("transaction", {"reason": "funding", "cash_delta": "50", "external_delta": "50", "legs": []}, pool)
        return spec

    def build(spec, **parameters):
        cfg = Config(experiment=spec, kalshi_latency=0,
                     polymarket_latency=0, max_capital_frac=1, **parameters)
        engine = Engine(cfg, GameKalshi(spec.pair.kalshi_event), revision="fixture-revision",
                        us=GameUS(spec.pair.polymarket_event, spec.pair.polymarket_market, depth="10", authenticated=spec.mode == "live"),
                        kalshi_live=LiveKalshi())
        if spec.mode == "live":
            # Fake live adapters use fixture execution evidence and real broker accounting.
            for broker, adapter in ((engine.kalshi_broker, Paper(engine.kalshi, latency=0)),
                                    (engine.poly_broker, PolymarketUSPaper(engine.poly, latency=0))):
                adapter.mode = "live"
                adapter.takes_broker = True
                broker.adapter = adapter
            store.append("health", {"component": "account", "status": "healthy", "discrepancies": []})
            store.append("health", {"component": "polymarket_us", "status": "healthy", "discrepancies": []})
        return engine

    return store, create, build


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_two_experiments_trade_only_their_pair_and_keep_separate_cash(setup, mode):
    store, create, build = setup
    a, b = build(create("a", mode)), build(create("b", mode))
    if b.experiment.id == "b":
        b.poly.book_raw = lambda slug: {"marketData": {"marketSlug": slug, "state": "MARKET_STATE_OPEN", "transactTime": datetime.now(timezone.utc).isoformat(),
            "bids": [{"px": {"value": "0.80"}, "qty": "10"}],
            "offers": [{"px": {"value": "0.82"}, "qty": "10"}]}}
    assert b.scan().apr > a.scan().apr
    # Runtime must not enumerate the unrelated, higher-scoring game at all.
    a.kalshi.pages = lambda *a, **kw: pytest.fail("runtime enumerated Kalshi events")
    a.poly.events = lambda *a, **kw: pytest.fail("runtime enumerated US events")
    before_b = [broker.available for broker in b.brokers.values()]
    a.step()
    assert [broker.available for broker in b.brokers.values()] == before_b
    b.step()
    for engine in (a, b):
        for broker in engine.brokers.values():
            assert broker.state().lots
            assert {(lot["market"], lot["side"].upper()) for lot in broker.state().lots.values()} <= engine.pair.contracts(broker.venue)
    for pool in (a.experiment.portfolio["kalshi"], a.experiment.portfolio[a.pair.venue]):
        record = store.events(pool, kinds=("run",))[0]["data"]
        assert record["config"]["experiment"] == json.loads(json.dumps(asdict(a.experiment)))
        assert record["revision"] == "fixture-revision"
        assert "max_sets" in record["config"] and len(record["config"]["experiment"]["pair"]["outcomes"]) == 2


@pytest.mark.parametrize("mode", ["paper", "live"])
@pytest.mark.parametrize("venue", ["kalshi", "polymarket_us"])
def test_invalid_scope_on_either_venue_submits_nothing(setup, mode, venue):
    store, create, build = setup
    spec = create(mode=mode, scopes={venue: ["UNRELATED*"]})
    with pytest.raises(ValueError, match="outside cash pool"):
        build(spec).step()
    assert not store.events(kinds=("order", "execution"))


@pytest.mark.parametrize("mismatch", ["missing", "venue", "mode", "retired", "reserve"])
def test_invalid_allocation_submits_nothing(setup, mismatch):
    store, create, build = setup
    spec = create()
    if mismatch == "missing":
        spec = replace(spec, portfolio={"kalshi": spec.portfolio["kalshi"], spec.pair.venue: "absent"})
    elif mismatch == "venue":
        spec = replace(spec, portfolio={"kalshi": spec.portfolio[spec.pair.venue], spec.pair.venue: spec.portfolio["kalshi"]})
    elif mismatch == "mode":
        spec = replace(spec, mode="live")
    else:
        store.db.execute("UPDATE portfolios SET metadata=? WHERE id=?",
                         (json.dumps({"retired_at": "today"} if mismatch == "retired" else {"reserve": True}), spec.portfolio[spec.pair.venue]))
    with pytest.raises(ValueError):
        build(spec).step()
    assert not store.events(kinds=("order", "execution"))


def test_invalid_outcome_mapping_fails_before_both_legs(setup):
    store, create, build = setup
    spec = create()
    outcomes = tuple(replace(o, polymarket_side="YES") for o in spec.pair.outcomes)
    with pytest.raises(ValueError, match="US outcomes"):
        build(replace(spec, pair=replace(spec.pair, outcomes=outcomes)))
    assert not store.events(kinds=("order", "execution"))


def test_portfolios_can_be_reused_without_refunding_or_resetting_inventory(setup):
    store, create, build = setup
    spec = create()
    first = build(spec)
    first.step()
    second = build(replace(spec, id="another-run"))
    assert second.cash == first.cash and second.positions == first.positions
    assert len([e for e in store.events(kinds=("transaction",)) if e["data"]["reason"] == "funding"]) == 2


def test_full_basket_is_checked_again_after_repricing(setup, monkeypatch):
    store, create, build = setup
    engine = build(create())
    basket = engine.scan()
    assert basket is not None
    # Scope changes between pricing and execution must fail before the first fill.
    store.db.execute("UPDATE portfolios SET metadata=? WHERE id=?",
                     (json.dumps({"markets": ["OTHER*"]}), engine.kalshi_broker.pool_id))
    with pytest.raises(ValueError, match="outside cash pool"):
        engine.execute(basket)
    assert not store.events(kinds=("order", "execution"))


def test_complete_basket_limit_cost_must_fit_both_allocations(setup, monkeypatch):
    store, create, build = setup
    engine = build(create())
    basket = engine.scan()
    assert basket is not None
    # At VWAP the cash would suffice, but not at the complete basket's limit prices.
    for leg in basket.legs:
        if leg["venue"] == "kalshi":
            leg["limit"] = "0.99"
    store.append("transaction", {"reason": "funding", "cash_delta": "-44", "external_delta": "-44", "legs": []}, engine.kalshi_broker.pool_id)
    monkeypatch.setattr(engine, "price_pair", lambda *_, **kw: (basket, "ok"))
    engine.execute(basket)
    rejected = [e["data"] for e in store.events(kinds=("observation",)) if e["data"].get("kind") == "reject"]
    assert rejected[-1]["reason"] == "basket_preflight"
    assert not store.events(kinds=("order", "execution"))

    store.append("transaction", {"reason": "funding", "cash_delta": "44", "external_delta": "44", "legs": []}, engine.kalshi_broker.pool_id)
    engine.step()
    assert engine.counters["baskets"] == 1


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_known_second_leg_failure_repairs_first_fill(setup, monkeypatch, mode):
    store, create, build = setup
    engine = build(create(mode=mode))
    def reject(*_):
        response = httpx.Response(400, request=httpx.Request("POST", "https://fixture.invalid/orders"))
        raise httpx.HTTPStatusError("known rejection", request=response.request, response=response)
    monkeypatch.setattr(engine.kalshi_broker.adapter, "create_order", reject)
    engine.step()
    assert not engine.poly_broker.state().lots and not engine.kalshi_broker.state().lots
    assert engine.counters["unwinds"] == 1
    assert engine.poly_broker.available < 50  # repair loss remains with this allocation
    assert not engine.halted


@pytest.mark.parametrize("status", ["unknown", "acknowledged"])
def test_restart_keeps_uncertain_order_and_reservation_without_duplicates(setup, status):
    store, create, build = setup
    spec = create(mode="live")
    engine = build(spec)
    before = [b.available for b in engine.brokers.values()]
    class Uncertain:
        venue, mode, takes_broker = "polymarket_us", "live", True
        def create_order(self, order, broker):
            broker.finish(order["client_order_id"], status, release=False)
            return {"status": "unknown", "filled": "0"}
    engine.poly_broker.adapter = Uncertain()
    engine.step()
    reservation = store.reserved(spec.portfolio[spec.pair.venue])
    orders = store.events(kinds=("order",))
    assert reservation > 0 and engine.halted
    restarted = build(spec)
    restarted.step()
    assert store.reserved(spec.portfolio[spec.pair.venue]) == reservation
    assert store.events(kinds=("order",)) == orders
    assert [b.available for b in restarted.brokers.values()] == [before[0], before[1] - reservation]


def test_unknown_later_leg_keeps_known_fill_and_halts_for_reconciliation(setup, monkeypatch):
    store, create, build = setup
    spec = create(mode="live")
    engine = build(spec)
    def timeout(*_):
        raise TimeoutError("reply lost")
    monkeypatch.setattr(engine.kalshi_broker.adapter, "create_order", timeout)
    engine.step()
    assert engine.poly_broker.state().lots and engine.halted
    assert engine.counters["unwinds"] == 0
    assert store.reserved(spec.portfolio["kalshi"]) > 0
    restarted = build(spec)
    orders = store.events(kinds=("order",))
    restarted.step()
    assert store.events(kinds=("order",)) == orders


def test_restart_keeps_inventory_cash_without_funding(setup):
    store, create, build = setup
    spec = create()
    engine = build(spec)
    engine.step()
    orders = store.events(kinds=("order",))
    balances = [b.available for b in engine.brokers.values()]
    restarted = build(spec)
    assert [b.available for b in restarted.brokers.values()] == balances
    assert restarted.positions == engine.positions
    assert store.events(kinds=("order",)) == orders
    assert len([e for e in store.events(kinds=("transaction",)) if e["data"]["reason"] == "funding"]) == 2


def test_other_experiment_keeps_cash_through_loss_profit_and_pending_order(setup, monkeypatch):
    store, create, build = setup
    a, b = build(create()), build(create("b"))
    before = [broker.available for broker in b.brokers.values()]
    a.step()
    for broker in a.brokers.values():
        for lot_id in list(broker.state().lots):
            broker.settle(lot_id, "0" if broker.venue == "kalshi" else "0", {"fixture": True})
    assert a.kalshi_broker.available < 50 and a.poly_broker.available > 50
    assert [broker.available for broker in b.brokers.values()] == before
    a.poly_broker.adapter.create_order = lambda order, broker: broker.finish(order["client_order_id"], "unknown", release=False) or {"status": "unknown"}
    a.poly_broker.submit({"ticker": "PMUS:" + SLUG, "side": "ask", "price": "0.62", "count": "1"}, {"experiment_id": "a"})
    assert store.reserved(a.poly_broker.pool_id) > 0
    assert [broker.available for broker in b.brokers.values()] == before


def test_foreign_inventory_is_rejected_before_run_start(setup):
    store, create, build = setup
    spec = create()
    store.append("transaction", {"reason": "fill", "cash_delta": "-1", "fee": "0", "context": {},
                 "legs": [{"market": "OTHER-GAME-BOS", "side": "yes", "quantity_delta": "1", "price": "1"}]}, spec.portfolio["kalshi"])
    with pytest.raises(ValueError, match="incompatible existing holdings"):
        build(spec)
    assert not store.events(kinds=("run", "order"))


def test_incomplete_repair_is_retried_after_restart(setup, monkeypatch):
    store, create, build = setup
    spec = create()
    engine = build(spec)
    monkeypatch.setattr(engine.kalshi_broker.adapter, "create_order", lambda *_: (_ for _ in ()).throw(ValueError("rejected")))
    monkeypatch.setattr(engine, "unwind", lambda *args: {"status": "canceled", "sold": "0"})
    engine.step()
    assert engine.halted and engine.poly_broker.state().lots
    restarted = build(spec)
    orders = store.events(kinds=("order",))
    restarted.step()
    assert not restarted.halted and len(store.events(kinds=("order",))) > len(orders)
    assert not restarted.poly_broker.state().lots


def test_loader_records_all_resolved_parameters_and_rejects_unknown_keys(tmp_path):
    path = tmp_path / "experiment.json"
    data = {**asdict(experiment()), "parameters": {"max_sets": 7, "interval_seconds": 0.05}}
    path.write_text(json.dumps(data))
    cfg = load_experiment(str(path))
    assert cfg.max_sets == 7 and cfg.interval_seconds == 0.05 and cfg.max_basket_dollars == 25
    data["parameters"]["max_setz"] = 8
    path.write_text(json.dumps(data))
    with pytest.raises(TypeError):
        load_experiment(str(path))


@pytest.mark.parametrize("parameters", [
    {"max_sets": 0}, {"interval_seconds": 0},
    {"unwind_slippage": 0.05}, {"live": True}, {"dry_run": True},
])
def test_invalid_parameters_fail_before_any_engine_exists(tmp_path, parameters):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({**asdict(experiment()), "parameters": parameters}))
    with pytest.raises((ValueError, TypeError)):
        load_experiment(str(path))


def test_selected_basket_cannot_substitute_an_unrelated_contract(setup, monkeypatch):
    store, create, build = setup
    engine = build(create())
    basket = engine.scan()
    assert basket is not None
    basket.legs[0]["ticker"] = "KXOTHER-GAME-BOS"
    monkeypatch.setattr(engine, "price_pair", lambda *_, **kw: (basket, "ok"))
    with pytest.raises(ValueError, match="outside the selected pair"):
        engine.execute(basket)
    assert not store.events(kinds=("order", "execution"))


def test_run_provenance_contains_preprocessing_evidence(setup):
    store, create, build = setup
    spec = create()
    evidence = {"method": "pinned", "legs": {o.kalshi: o.polymarket_side for o in spec.pair.outcomes}}
    build(replace(spec, pair=replace(spec.pair, evidence=evidence)))
    assert store.events(kinds=("run",))[0]["data"]["config"]["experiment"]["pair"]["evidence"] == evidence


@pytest.mark.parametrize("block", ["halted", "stale_account_monitor"])
def test_preflight_reconciliation_change_rejects_without_crashing(setup, monkeypatch, block):
    store, create, build = setup
    engine = build(create())
    basket = engine.scan()
    assert basket is not None
    monkeypatch.setattr(engine, "live_block", lambda: block)
    engine.execute(basket)
    assert not store.events(kinds=("order", "execution"))
    assert store.events(kinds=("observation",), observations=("reject",))[-1]["data"]["detail"] == block


@pytest.mark.parametrize("reconcile_quantity", ["10", "9"])
def test_live_kalshi_acknowledgement_pauses_until_monitor_books_fills(setup, reconcile_quantity):
    store, create, build = setup
    spec = create(mode="live")
    engine = build(spec)

    class FilledKalshi:
        def create_order(self, order):
            return {"order": {"order_id": "venue-order", "status": "executed", "fill_count_fp": order["count"]}}

    engine.kalshi_broker.adapter = KalshiLive(FilledKalshi())
    engine.step()
    order = engine.kalshi_broker.orders()[0]
    assert order["status"] == "acknowledged" and order["filled"] == 0
    reserved = store.reserved(spec.portfolio["kalshi"])
    assert reserved > 0 and not engine.halted
    assert engine.live_block() == "awaiting_reconciliation"
    orders = store.events(kinds=("order",))
    engine.step()
    restarted = build(spec)
    restarted.step()
    assert not restarted.halted
    assert store.events(kinds=("order",)) == orders
    assert store.reserved(spec.portfolio["kalshi"]) == reserved
    # Emulate authoritative account-monitor ingestion, not a strategy fill.
    broker = restarted.kalshi_broker
    broker.fill(order["client_order_id"], reconcile_quantity, "0.55", "0", "fixture-fill", {"fixture": "account monitor"})
    broker.finish(order["client_order_id"], "filled")
    restarted.resume()
    if reconcile_quantity == "9":
        assert restarted.halted and "unmatched inventory" in restarted.halted
        restarted.step()
        assert not restarted.halted
        assert sum(lot["quantity"] for lot in restarted.poly_broker.state().lots.values()) == 9
    else:
        assert not restarted.halted and restarted.live_block() is None
        restarted.poly.depth = "20"  # replenish the fixture's consumed paper liquidity
        restarted.step()
        assert restarted.counters["baskets"] == 1


def test_same_market_opposite_basket_legs_are_rejected_before_submission(setup, monkeypatch):
    store, create, build = setup
    engine = build(create(), require_cross=False)
    basket = engine.scan()
    assert basket is not None
    for leg in basket.legs:
        leg.update(ticker="PMUS:" + SLUG, venue="polymarket_us", contract=engine.pair.outcomes[int(leg["role"])].polymarket_side)
    monkeypatch.setattr(engine, "price_pair", lambda *_, **kw: (basket, "ok"))
    engine.execute(basket)
    assert not store.events(kinds=("order", "execution"))
    assert "opposite" in store.events(kinds=("observation",), observations=("reject",))[-1]["data"]["detail"]


def pool_sizing_engine(setup, mode='paper', kalshi_price='.16', us_price='.79', us_cash='10'):
    store, create, build = setup
    engine = build(create(mode=mode), max_sets=20, min_net=.01, pmus_theta=.11)
    for broker, cash in ((engine.kalshi_broker, '10'), (engine.poly_broker, us_cash)):
        delta = D(cash) - broker.state().cash
        store.append('transaction', {'reason': 'funding', 'cash_delta': str(delta),
                                    'external_delta': str(delta), 'legs': []}, broker.pool_id)
    engine.kalshi.asks = {'BOS': kalshi_price, 'SAC': '.99'}
    engine.poly.book_raw = lambda slug: {'marketData': {'marketSlug': slug, 'state': 'MARKET_STATE_OPEN', 'transactTime': datetime.now(timezone.utc).isoformat(),
        'bids': [{'px': {'value': str(1-D(us_price))}, 'qty': '100'}],
        'offers': [{'px': {'value': str(1-D(us_price)+D('.02'))}, 'qty': '100'}]}}
    return engine


@pytest.mark.parametrize('mode', ['paper', 'live'])
@pytest.mark.parametrize('kalshi_price,us_price', [('.16', '.79'), ('.79', '.16')])
def test_sizing_fits_each_pool_and_executes_smaller_profitable_basket(setup, mode, kalshi_price, us_price):
    engine = pool_sizing_engine(setup, mode, kalshi_price, us_price)
    engine.cfg.min_net = .25  # An explicit basket-profit floor remains enforceable.
    before = engine.store.events(kinds=('transaction', 'order', 'execution'))
    basket = engine.scan()
    assert basket is not None and basket.sets == 12 and basket.net >= D('.25')
    assert engine.check_basket(basket) is None
    assert engine.store.events(kinds=('transaction', 'order', 'execution')) == before
    assert all(b.available == 10 for b in engine.brokers.values())
    engine.execute(basket)  # Offline fixtures only, including the fake live adapters.
    assert engine.counters['baskets'] == 1
    assert all(b.available >= 0 for b in engine.brokers.values())
    assert all(sum(lot['quantity'] for lot in b.state().lots.values()) == 12 for b in engine.brokers.values())
    assert not engine.poly.sent


def test_sizing_uses_broker_fee_buffer_and_respects_profit_hurdle(setup):
    engine = pool_sizing_engine(setup, us_cash='9.70')
    # Twelve NO contracts cost 9.48 + quoted fee .22, but the broker reserves 9.78.
    basket = engine.scan()
    assert basket is not None and basket.sets == 11
    assert engine.check_basket(basket) is None
    engine.cfg.min_net = .26  # The affordable basket earns .24; do not relax the hurdle.
    assert engine.scan() is None


def test_sizing_subtracts_pending_reservations_and_ignores_other_pools(setup):
    store, create, build = setup
    engine = pool_sizing_engine(setup)
    other = build(create('b'))
    other_before = [b.available for b in other.brokers.values()]
    broker = engine.poly_broker
    # Reserve 1.80 with a fake acknowledged order, without a fill or cash transfer.
    broker.adapter.create_order = lambda order, b: b.finish(order['client_order_id'], 'acknowledged', release=False)
    broker.submit({'ticker': 'PMUS:'+engine.pair.polymarket_market, 'side': 'ask', 'price': '.125', 'count': '2'})
    assert broker.available == D('8.20')
    before = store.events(kinds=('transaction', 'order', 'execution'))
    basket = engine.scan()
    assert basket is not None and basket.sets == 10
    assert engine.budget_block(basket) is None
    assert store.events(kinds=('transaction', 'order', 'execution')) == before
    assert [b.available for b in other.brokers.values()] == other_before
    assert broker.available == D('8.20')


def test_sizing_uses_limit_cost_when_depth_crosses_a_price_level(setup):
    engine = pool_sizing_engine(setup, kalshi_price='.10', us_cash='5')
    engine.poly.book_raw = lambda slug: {'marketData': {'marketSlug': slug, 'state': 'MARKET_STATE_OPEN', 'transactTime': datetime.now(timezone.utc).isoformat(),
        'bids': [{'px': {'value': '.80'}, 'qty': '5'}, {'px': {'value': '.20'}, 'qty': '100'}],
        'offers': [{'px': {'value': '.82'}, 'qty': '100'}]}}
    # Seven cost only 2.60 at VWAP but require >5 at the .80 limit. Six fit.
    basket = engine.scan()
    assert basket is not None and basket.sets == 6
    assert engine.check_basket(basket) is None


def test_sizing_returns_no_basket_when_neither_route_can_fund_one_set(setup):
    engine = pool_sizing_engine(setup, us_cash='.01')
    basket, reason = engine.price_pair()
    assert basket is None and reason == 'capital_limit'
    assert not engine.store.events(kinds=('order', 'execution'))


def test_sizing_aggregates_multiple_legs_on_the_same_venue(setup):
    engine = pool_sizing_engine(setup)
    engine.cfg.require_cross = False
    engine.kalshi.asks = {'BOS': '.4', 'SAC': '.4'}
    engine.poly.book_raw = lambda slug: {'marketData': {'marketSlug': slug, 'state': 'MARKET_STATE_OPEN', 'transactTime': datetime.now(timezone.utc).isoformat(),
        'bids': [{'px': {'value': '.5'}, 'qty': '100'}],
        'offers': [{'px': {'value': '.5'}, 'qty': '100'}]}}
    basket = engine.scan()
    assert basket is not None and basket.sets == 11  # Twelve reserve 2 × 5.01 > 10.
    assert all(leg['venue'] == 'kalshi' for leg in basket.legs)
    assert engine.check_basket(basket) is None


def test_execution_reprices_size_after_selected_pool_cash_changes(setup):
    engine = pool_sizing_engine(setup)
    basket = engine.scan()
    assert basket is not None and basket.sets == 12
    engine.store.append('transaction', {'reason': 'funding', 'cash_delta': '-3',
                        'external_delta': '-3', 'legs': []}, engine.poly_broker.pool_id)
    engine.execute(basket)
    assert engine.counters['baskets'] == 1
    assert all(sum(lot['quantity'] for lot in b.state().lots.values()) == 8 for b in engine.brokers.values())


@pytest.mark.parametrize("second_leg", ["filled", "rejected"])
@pytest.mark.parametrize("first_fill", ["10.00", "4.00", "0.00"])
def test_kalshi_v2_first_leg_is_hedged_or_unwound(setup, monkeypatch, second_leg, first_fill):
    store, create, build = setup
    engine = build(create(mode="live"))
    basket = engine.scan()
    assert basket is not None and basket.sets == 10
    for leg in basket.legs:
        leg["depth"] = "10" if leg["venue"] == "kalshi" else "100"
    monkeypatch.setattr(engine, "price_pair", lambda **_: (basket, "ok"))
    sent = []

    class V2Kalshi:
        def create_order(self, order):
            sent.append(order)
            return {"order_id": "venue-" + order["client_order_id"],
                    "client_order_id": order["client_order_id"],
                    "fill_count": order["count"] if order.get("reduce_only") else first_fill,
                    "remaining_count": "0.00"}

    engine.kalshi_broker.adapter = KalshiLive(V2Kalshi())
    original_book = engine.kalshi.book
    monkeypatch.setattr(engine.kalshi, "book", lambda ticker: {"orderbook_fp": {
        **original_book(ticker)["orderbook_fp"], "yes_dollars": [["0.20", "100"]]}})
    if second_leg == "rejected":
        def reject(order, broker):
            broker.finish(order["client_order_id"], "rejected")
            return {"status": "rejected", "filled": "0"}
        monkeypatch.setattr(engine.poly_broker.adapter, "create_order", reject)
    engine.execute(basket)
    record = store.events(kinds=("observation",), observations=("basket_execution",))[-1]["data"]
    assert record["outcomes"][0]["venue"] == "kalshi"
    assert D(record["outcomes"][0]["filled"]) == D(first_fill)
    assert not engine.halted
    if D(first_fill) == 0:
        assert not engine.poly_broker.orders() and len(sent) == 1
    elif second_leg == "filled":
        assert sum(lot["quantity"] for lot in engine.poly_broker.state().lots.values()) == D(first_fill)
        assert len(sent) == 1
    else:
        assert len(sent) == 2 and sent[1]["reduce_only"] and D(sent[1]["count"]) == D(first_fill)
        assert sent[1]["side"] != sent[0]["side"]
        assert D(record["outcomes"][-1]["sold"]) == D(first_fill)
        assert not engine.poly_broker.state().lots
    # Kalshi's account monitor still owns authoritative fill booking.
    assert not engine.kalshi_broker.state().lots
