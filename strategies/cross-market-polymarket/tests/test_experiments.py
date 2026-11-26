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
from tests.test_trader import EVENT, LEAGUES, FakeKalshi, experiment
from tests.test_trader_us import FakeUSVenue, LiveKalshi, SLUG
from trader import Config, Engine, load_experiment


class GameKalshi(FakeKalshi):
    def __init__(self, event=EVENT):
        super().__init__()
        self.event = event

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
        for venue, pool in (("kalshi", spec.kalshi_pool), (spec.venue, spec.polymarket_pool)):
            store.create_pool(pool, venue, "50", mode=mode, subaccount=1,
                              markets=(scopes or {}).get(venue, sorted({m for m, _ in spec.contracts(venue)})))
            if mode == "live":
                store.append("transaction", {"reason": "funding", "cash_delta": "50", "external_delta": "50", "legs": []}, pool)
        return spec

    def build(spec, **parameters):
        cfg = Config(experiment=spec, venue=spec.venue, live=spec.mode == "live", kalshi_latency=0,
                     polymarket_latency=0, max_event_frac=1, **parameters)
        engine = Engine(cfg, GameKalshi(spec.pair.kalshi_event), leagues=LEAGUES, revision="fixture-revision",
                        us=GameUS(spec.pair.polymarket_event, spec.pair.polymarket_market, depth="10", authenticated=cfg.live),
                        kalshi_live=LiveKalshi())
        if cfg.live:
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
        b.poly.book_raw = lambda slug: {"marketData": {"marketSlug": slug,
            "bids": [{"px": {"value": "0.80"}, "qty": "10"}],
            "offers": [{"px": {"value": "0.82"}, "qty": "10"}]}}
    a.discover()
    b.discover()
    assert b.scan()[0][0].apr > a.scan()[0][0].apr
    # An unrelated, higher-scoring game is visible to A's discovery.
    a.games["nba"] += b.games["nba"]
    a.polys["nba"] += b.polys["nba"]
    assert [pair.game.event for pair in a.pair_league("nba", {})] == [a.experiment.pair.kalshi_event]
    assert [pair.game.event for _, pair, _ in a.scan()] == [a.experiment.pair.kalshi_event]
    before_b = [broker.available for broker in b.brokers.values()]
    a.step()
    assert [broker.available for broker in b.brokers.values()] == before_b
    b.step()
    for engine in (a, b):
        for broker in engine.brokers.values():
            assert broker.state().lots
            assert {(lot["market"], lot["side"].upper()) for lot in broker.state().lots.values()} <= engine.experiment.contracts(broker.venue)
    for pool in (a.experiment.kalshi_pool, a.experiment.polymarket_pool):
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
        build(spec)
    assert not store.events(kinds=("order", "execution"))


@pytest.mark.parametrize("mismatch", ["missing", "venue", "mode", "retired", "reserve"])
def test_invalid_allocation_submits_nothing(setup, mismatch):
    store, create, build = setup
    spec = create()
    if mismatch == "missing":
        spec = replace(spec, polymarket_pool="absent")
    elif mismatch == "venue":
        spec = replace(spec, polymarket_pool=spec.kalshi_pool, kalshi_pool=spec.polymarket_pool)
    elif mismatch == "mode":
        spec = replace(spec, mode="live")
    else:
        store.db.execute("UPDATE portfolios SET metadata=? WHERE id=?",
                         (json.dumps({"retired_at": "today"} if mismatch == "retired" else {"reserve": True}), spec.polymarket_pool))
    with pytest.raises(ValueError):
        build(spec)
    assert not store.events(kinds=("order", "execution"))


def test_invalid_outcome_mapping_fails_before_both_legs(setup):
    store, create, build = setup
    spec = create()
    outcomes = tuple(replace(o, polymarket_side="NO" if o.polymarket_side == "YES" else "YES") for o in spec.pair.outcomes)
    engine = build(replace(spec, pair=replace(spec.pair, outcomes=outcomes)))
    with pytest.raises(ValueError, match="outcome mapping"):
        engine.step()
    assert not store.events(kinds=("order", "execution"))


def test_reuse_by_another_experiment_is_rejected(setup):
    store, create, build = setup
    spec = create()
    build(spec)
    with pytest.raises(ValueError, match="another experiment"):
        build(replace(spec, id="another"))
    assert len(store.events(kinds=("run",))) == 2


def test_full_basket_is_checked_again_after_repricing(setup, monkeypatch):
    store, create, build = setup
    engine = build(create())
    engine.discover()
    basket, pair, league = engine.scan()[0]
    # Scope changes between discovery and execution must fail before the first fill.
    store.db.execute("UPDATE portfolios SET metadata=? WHERE id=?",
                     (json.dumps({"markets": ["OTHER*"]}), engine.kalshi_broker.pool_id))
    with pytest.raises(ValueError, match="outside cash pool"):
        engine.execute(basket, pair, league)
    assert not store.events(kinds=("order", "execution"))


def test_complete_basket_limit_cost_must_fit_both_allocations(setup, monkeypatch):
    store, create, build = setup
    engine = build(create())
    engine.discover()
    basket, pair, league = engine.scan()[0]
    # At VWAP the cash would suffice, but not at the complete basket's limit prices.
    for leg in basket.legs:
        if leg["venue"] == "kalshi":
            leg["limit"] = "0.99"
    store.append("transaction", {"reason": "funding", "cash_delta": "-44", "external_delta": "-44", "legs": []}, engine.kalshi_broker.pool_id)
    monkeypatch.setattr(engine, "recheck", lambda *_: (basket, "ok"))
    assert engine.venue_shortfall(basket) is None
    engine.execute(basket, pair, league)
    assert not store.events(kinds=("order", "execution"))
    rejects = store.events(kinds=("observation",), observations=("reject",))
    assert "full basket" in rejects[-1]["data"]["reason"]
    # A later scan remains usable once funding is restored.
    store.append("transaction", {"reason": "funding", "cash_delta": "44", "external_delta": "44", "legs": []}, engine.kalshi_broker.pool_id)
    engine.cooldown_until.clear()
    engine.step()
    assert engine.counters["baskets"] == 1


@pytest.mark.parametrize("block", ["halted", "stale_account_monitor"])
def test_preflight_reconciliation_change_rejects_without_crashing(setup, monkeypatch, block):
    store, create, build = setup
    engine = build(create())
    engine.discover()
    basket, pair, league = engine.scan()[0]
    monkeypatch.setattr(engine, "live_block", lambda: block)
    engine.execute(basket, pair, league)
    assert not store.events(kinds=("order", "execution"))
    assert store.events(kinds=("observation",), observations=("reject",))[-1]["data"]["reason"] == block


@pytest.mark.parametrize("reconcile_quantity", ["10", "9"])
def test_live_kalshi_acknowledgement_pauses_until_monitor_books_fills(setup, reconcile_quantity):
    store, create, build = setup
    spec = create(mode="live")
    engine = build(spec, cooldown=0)

    class FilledKalshi:
        def create_order(self, order):
            return {"order": {"order_id": "venue-order", "status": "executed", "fill_count_fp": order["count"]}}

    engine.kalshi_broker.adapter = KalshiLive(FilledKalshi())
    engine.step()
    order = engine.kalshi_broker.orders()[0]
    assert order["status"] == "acknowledged" and order["filled"] == 0
    reserved = store.reserved(spec.kalshi_pool)
    assert reserved > 0 and not engine.halted
    assert engine.live_block() == "awaiting_reconciliation"
    orders = store.events(kinds=("order",))
    engine.step()
    restarted = build(spec, cooldown=0)
    restarted.step()
    assert not restarted.halted
    assert store.events(kinds=("order",)) == orders
    assert store.reserved(spec.kalshi_pool) == reserved
    # Emulate authoritative account-monitor ingestion, not a strategy fill.
    broker = restarted.kalshi_broker
    broker.fill(order["client_order_id"], reconcile_quantity, "0.55", "0", "fixture-fill", {"fixture": "account monitor"})
    broker.finish(order["client_order_id"], "filled")
    restarted.resume()
    if reconcile_quantity == "9":
        assert restarted.halted and "incomplete inventory" in restarted.halted
    else:
        assert not restarted.halted and restarted.live_block() is None
        restarted.poly.depth = "20"  # replenish the fixture's consumed paper liquidity
        restarted.step()
        assert restarted.counters["baskets"] == 1


def test_same_market_opposite_basket_legs_are_rejected_before_submission(setup, monkeypatch):
    store, create, build = setup
    engine = build(create(), require_cross=False)
    engine.discover()
    basket, pair, league = engine.scan()[0]
    for leg in basket.legs:
        leg.update(ticker="PMUS:" + SLUG, venue="polymarket_us", contract=pair.legs[leg["role"]])
    monkeypatch.setattr(engine, "recheck", lambda *_: (basket, "ok"))
    engine.execute(basket, pair, league)
    assert not store.events(kinds=("order", "execution"))
    assert "opposite" in store.events(kinds=("observation",), observations=("reject",))[-1]["data"]["reason"]


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_known_second_leg_failure_repairs_first_fill(setup, monkeypatch, mode):
    store, create, build = setup
    engine = build(create(mode=mode))
    def reject(*_):
        response = httpx.Response(400, request=httpx.Request("POST", "https://fixture.invalid/orders"))
        raise httpx.HTTPStatusError("known rejection", request=response.request, response=response)
    monkeypatch.setattr(engine.kalshi_broker.adapter, "create_order", reject)
    with pytest.raises(httpx.HTTPStatusError):
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
    reservation = store.reserved(spec.polymarket_pool)
    orders = store.events(kinds=("order",))
    assert reservation > 0 and engine.halted
    restarted = build(spec)
    restarted.step()
    assert store.reserved(spec.polymarket_pool) == reservation
    assert store.events(kinds=("order",)) == orders
    assert [b.available for b in restarted.brokers.values()] == [before[0], before[1] - reservation]


def test_unknown_later_leg_keeps_known_fill_and_halts_for_reconciliation(setup, monkeypatch):
    store, create, build = setup
    spec = create(mode="live")
    engine = build(spec)
    def timeout(*_):
        raise TimeoutError("reply lost")
    monkeypatch.setattr(engine.kalshi_broker.adapter, "create_order", timeout)
    with pytest.raises(TimeoutError):
        engine.step()
    assert engine.poly_broker.state().lots and engine.halted
    assert engine.counters["unwinds"] == 0
    assert store.reserved(spec.kalshi_pool) > 0
    restarted = build(spec)
    orders = store.events(kinds=("order",))
    restarted.step()
    assert store.events(kinds=("order",)) == orders


def test_restart_keeps_inventory_cash_and_cooldown_without_funding(setup):
    store, create, build = setup
    spec = create()
    engine = build(spec)
    engine.step()
    orders = store.events(kinds=("order",))
    balances = [b.available for b in engine.brokers.values()]
    restarted = build(spec)
    restarted.step()
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
                 "legs": [{"market": EVENT + "-BOS", "side": "yes", "quantity_delta": "1", "price": "1"}]}, spec.kalshi_pool)
    with pytest.raises(ValueError, match="incompatible existing holdings"):
        build(spec)
    assert not store.events(kinds=("run", "order"))


def test_incomplete_repair_halts_and_survives_restart(setup, monkeypatch):
    store, create, build = setup
    spec = create()
    engine = build(spec)
    monkeypatch.setattr(engine.kalshi_broker.adapter, "create_order", lambda *_: (_ for _ in ()).throw(ValueError("rejected")))
    monkeypatch.setattr(engine, "unwind", lambda *args: {"status": "canceled", "sold": "0"})
    with pytest.raises(ValueError, match="rejected"):
        engine.step()
    assert engine.halted and engine.poly_broker.state().lots
    restarted = build(spec)
    orders = store.events(kinds=("order",))
    restarted.step()
    assert restarted.halted and store.events(kinds=("order",)) == orders


def test_loader_records_all_resolved_parameters_and_rejects_unknown_keys(tmp_path):
    path = tmp_path / "experiment.json"
    data = {**asdict(experiment()), "parameters": {"max_sets": 7, "cooldown": 12}}
    path.write_text(json.dumps(data))
    cfg = load_experiment(str(path))
    assert cfg.max_sets == 7 and cfg.cooldown == 12 and cfg.max_basket_dollars == 25
    data["parameters"]["max_setz"] = 8
    path.write_text(json.dumps(data))
    with pytest.raises(TypeError):
        load_experiment(str(path))


@pytest.mark.parametrize("parameters", [
    {"max_sets": 1.5}, {"max_sets": 0}, {"cooldown": -1}, {"min_edge": float("nan")},
    {"require_cross": "false"}, {"unwind_slippage": 2}, {"live": True}, {"dry_run": True},
])
def test_invalid_parameters_fail_before_any_engine_exists(tmp_path, parameters):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({**asdict(experiment()), "parameters": parameters}))
    with pytest.raises(ValueError):
        load_experiment(str(path))


def test_selected_basket_cannot_substitute_an_unrelated_contract(setup, monkeypatch):
    store, create, build = setup
    engine = build(create())
    engine.discover()
    basket, pair, league = engine.scan()[0]
    basket.legs[0]["ticker"] = "KXOTHER-GAME-BOS"
    monkeypatch.setattr(engine, "recheck", lambda *_: (basket, "ok"))
    with pytest.raises(ValueError, match="outside the selected pair"):
        engine.execute(basket, pair, league)
    assert not store.events(kinds=("order", "execution"))


def test_run_provenance_contains_full_pin_mapping(setup):
    store, create, _ = setup
    spec = create()
    pins = {"pairs": {spec.pair.kalshi_event: {"slug": spec.pair.polymarket_event,
            "legs": {spec.pair.outcomes[0].kalshi: "bos", spec.pair.outcomes[1].kalshi: "sac"}}}, "blocked": set()}
    Engine(Config(experiment=spec, venue=spec.venue), GameKalshi(), leagues=LEAGUES,
           pairs=pins, revision="full-config", us=GameUS(spec.pair.polymarket_event, spec.pair.polymarket_market))
    assert store.events(kinds=("run",))[0]["data"]["config"]["pairs"]["pinned"] == pins["pairs"]


@pytest.mark.parametrize("swapped", [False, True])
def test_pin_is_used_but_cannot_override_experiment_mapping(setup, swapped):
    _, create, build = setup
    engine = build(create())
    engine.discover()
    # Abbreviations differ, so this reviewed pair needs an explicit pin.
    for team in engine.games["nba"][0].teams:
        team["abbr"] = "unmatched-" + team["abbr"]
    assert engine.pair_league("nba", {}) == []
    outcomes = engine.experiment.pair.outcomes
    engine.pairs = {"pairs": {engine.experiment.pair.kalshi_event: {
        "slug": engine.experiment.pair.polymarket_event,
        "legs": {outcomes[0].kalshi: "sac" if swapped else "bos",
                 outcomes[1].kalshi: "bos" if swapped else "sac"}}}, "blocked": set()}
    if swapped:
        with pytest.raises(ValueError, match="outcome mapping"):
            engine.pair_league("nba", {})
    else:
        assert engine.pair_league("nba", {})[0].evidence["method"] == "pinned"
