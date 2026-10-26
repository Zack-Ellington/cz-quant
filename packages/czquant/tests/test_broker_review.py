"""Accuracy tests for czquant.broker: submitting, correcting and tracking orders.

Written in the review of the broker merge (#17). The amend, venue-ID, correction and
fill-quarantine tests pinned defects found there; they pass with the fixes.
"""
import json
import random
import threading
from decimal import Decimal as D, ROUND_CEILING

import pytest

from czquant import Broker, Store
from czquant.monitor import account_pass
from czquant.paper import Paper



# --------------------------------------------------------------------------- fixtures

class Venue:
    """Static public market data for the paper adapter."""

    def __init__(self, books, multiplier="1"):
        self.books, self.multiplier = books, multiplier

    def book(self, ticker):
        yes, no = self.books[ticker]
        return {"orderbook_fp": {"yes_dollars": [[str(p), str(q)] for p, q in yes],
                                 "no_dollars": [[str(p), str(q)] for p, q in no]}}

    def market(self, ticker):
        return {"ticker": ticker, "series_ticker": "S", "status": "active"}

    def get(self, path, params=None):
        return {"series": {"fee_type": "quadratic", "fee_multiplier": self.multiplier}}


def paper(tmp_path, books, cash="100000", multiplier="1"):
    store = Store(tmp_path)
    store.create("p", cash)
    venue = Venue(books, multiplier)
    return store, Broker(store, "p", Paper(venue, latency=0)), venue


# --------------------------------------------------------------------------- independent reference

class Reference:
    """Kalshi semantics written independently of the broker: buying YES costs p, buying
    NO costs 1-p, and every YES+NO pair held in one market is netted back to $1."""

    def __init__(self, cash, books, multiplier):
        self.cash, self.books, self.mult = D(cash), books, D(multiplier)
        self.pos = {}  # market -> [yes, no]
        self.used = {}  # (market, native side, native price) -> qty taken

    def order(self, ticker, side, count, limit, tif):
        yes, no = self.books[ticker]
        native = sorted(no if side == "bid" else yes, key=lambda l: -l[0])
        native_side = "no" if side == "bid" else "yes"
        fills, left = [], D(count)
        for price, qty in native:
            yes_price = 1 - price if side == "bid" else price
            if (side == "bid" and yes_price > limit) or (side == "ask" and yes_price < limit):
                continue
            avail = max(D(0), D(qty) - self.used.get((ticker, native_side, price), D(0)))
            take = min(left, avail)
            if take > 0:
                fills.append((price, yes_price, take))
                left -= take
            if not left:
                break
        if tif == "fill_or_kill" and left:
            return D(0), D(0)
        filled = D(count) - left
        if not filled:
            return D(0), D(0)
        fee = (D("0.07") * self.mult * sum(q * p * (1 - p) for _, p, q in fills)).quantize(D("0.01"), ROUND_CEILING)
        cost = sum((p if side == "bid" else 1 - p) * q for _, p, q in fills)
        self.cash -= cost + fee
        y, n = self.pos.setdefault(ticker, [D(0), D(0)])
        if side == "bid":
            y += filled
        else:
            n += filled
        pairs = min(y, n)
        self.cash += pairs
        self.pos[ticker] = [y - pairs, n - pairs]
        for price, _, q in fills:
            k = (ticker, native_side, price)
            self.used[k] = self.used.get(k, D(0)) + q
        return filled, fee


def random_book(rng):
    """Uncrossed YES and NO bid ladders in whole cents."""
    yes_top = rng.randint(5, 85)
    no_top = rng.randint(5, 98 - yes_top)
    yes = [(D(yes_top - i) / 100, D(rng.randint(1, 40))) for i in range(rng.randint(0, 4)) if yes_top - i > 0]
    no = [(D(no_top - i) / 100, D(rng.randint(1, 40))) for i in range(rng.randint(0, 4)) if no_top - i > 0]
    return yes, no


def positions(state):
    out = {}
    for lot in state.lots.values():
        y, n = out.setdefault(lot["market"], [D(0), D(0)])
        out[lot["market"]] = [y + lot["quantity"], n] if lot["side"] == "yes" else [y, n + lot["quantity"]]
    return {m: v for m, v in out.items() if any(v)}


# --------------------------------------------------------------------------- submitting

@pytest.mark.parametrize("seed", range(40))
def test_random_order_flow_matches_independent_reference(tmp_path, seed):
    rng = random.Random(seed)
    markets = [f"M{i}" for i in range(3)]
    books = {m: random_book(rng) for m in markets}
    mult = rng.choice(["1", "0.5"])
    store, broker, _ = paper(tmp_path, books, multiplier=mult)
    ref = Reference("100000", books, mult)
    for _ in range(rng.randint(5, 25)):
        m, side = rng.choice(markets), rng.choice(["bid", "ask"])
        limit = D(rng.randint(1, 99)) / 100
        count = rng.randint(1, 60)
        tif = rng.choice(["immediate_or_cancel", "fill_or_kill"])
        result = broker.submit({"ticker": m, "side": side, "count": str(count), "price": str(limit),
                                "time_in_force": tif})
        filled, fee = ref.order(m, side, count, limit, tif)
        assert D(result["filled"]) == filled, (m, side, count, limit, tif)
        if filled:
            assert D(result["fee"]) == fee
    state = broker.state()
    assert state.cash == ref.cash
    assert positions(state) == {m: v for m, v in ref.pos.items() if any(v)}
    assert store.reserved("p") == 0


def test_fee_allocation_sums_exactly_to_order_fee(tmp_path):
    # Seven price levels, so the per-level fee shares need rounding.
    no = [(D(60 - i) / 100, D(3)) for i in range(7)]
    store, broker, _ = paper(tmp_path, {"M": ([], no)})
    r = broker.submit({"ticker": "M", "side": "bid", "count": "21", "price": "0.99",
                       "time_in_force": "immediate_or_cancel"})
    receipts = [e["data"] for e in store.events() if e["type"] == "execution"]
    assert sum(D(x["fee"]) for x in receipts) == D(r["fee"])
    assert broker.state().fees == D(r["fee"])


def test_concurrent_duplicate_client_order_id_calls_venue_once(tmp_path):
    store = Store(tmp_path)
    store.create("live", mode="live", subaccount=3)
    store.close()
    calls, barrier = [], threading.Barrier(8)

    class Adapter:
        def create_order(self, order):
            calls.append(order["client_order_id"])
            return {"order": {"order_id": "venue-1"}}

    def go(_):
        s = Store(tmp_path)
        barrier.wait()
        out = Broker(s, "live", Adapter()).submit({"ticker": "M", "side": "bid", "count": "5", "price": "0.40",
                                                   "client_order_id": "same"}, {"basket": 1})
        s.close()
        return out

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert calls == ["same"]


# --------------------------------------------------------------------------- correcting

def live_order(tmp_path, count="10", price="0.40"):
    store = Store(tmp_path)
    store.create("live", mode="live", subaccount=3)

    class Adapter:
        amended, canceled = [], []

        def create_order(self, order):
            return {"order": {"order_id": "venue-1"}}

        def amend_order(self, venue_id, changes):
            self.amended.append((venue_id, changes))
            return {"order": {"order_id": venue_id}}

        def cancel_order(self, venue_id, params):
            self.canceled.append(venue_id)
            return {"order": {"order_id": venue_id}}

    adapter = Adapter()
    broker = Broker(store, "live", adapter)
    broker.submit({"ticker": "M", "side": "bid", "count": count, "price": price, "client_order_id": "o1"}, {})
    return store, broker, adapter


def test_amend_up_resizes_the_cash_reservation(tmp_path):
    store, broker, _ = live_order(tmp_path)
    before = store.reserved("live")
    broker.amend("o1", {"count": "100", "price": "0.60"})
    # 100 contracts at 0.60 plus the 0.0175/contract fee allowance.
    assert store.reserved("live") == D("60") + D("1.75") > before


def test_amend_or_cancel_refuses_an_order_without_a_venue_id(tmp_path):
    store = Store(tmp_path)
    store.create("live", mode="live", subaccount=3)

    class LostAck:
        amended = []

        def create_order(self, order):
            raise TimeoutError("acknowledgement lost")

        def amend_order(self, venue_id, changes):
            self.amended.append(venue_id)
            return {}

    adapter = LostAck()
    broker = Broker(store, "live", adapter)
    with pytest.raises(TimeoutError):
        broker.submit({"ticker": "M", "side": "bid", "count": "5", "price": "0.40", "client_order_id": "o1"}, {})
    with pytest.raises(ValueError):
        broker.amend("o1", {"count": "6"})  # venue_id is unknown until the account monitor recovers it
    assert adapter.amended == []


def test_amend_records_a_durable_audit_trail(tmp_path):
    store, broker, adapter = live_order(tmp_path)
    broker.amend("o1", {"price": "0.42"})
    kinds = [e["data"].get("kind") for e in store.events() if e["type"] == "observation"]
    assert kinds[-2:] == ["amend_intent", "amend_acknowledged"]
    assert json.loads(store.db.execute("SELECT request FROM orders WHERE id='o1'").fetchone()[0])["price"] == "0.42"


def test_correcting_a_fill_lets_the_real_fill_book(tmp_path):
    store, broker, _ = live_order(tmp_path)
    broker.fill("o1", "10", "0.40", "0.17", "bad-source", {})
    bad = next(iter(broker.state().lots))
    broker.correct([bad], "misattributed fill")
    # The genuine fill for the full order arrives afterwards.
    broker.fill("o1", "10", "0.40", "0.17", "kalshi-fill:0:venue-1:t1", {})
    assert sum(l["quantity"] for l in broker.state().lots.values()) == 10


def test_correction_that_breaks_later_inventory_is_rolled_back(tmp_path):
    store, broker, _ = live_order(tmp_path)
    broker.fill("o1", "10", "0.40", "0.17", "f1", {})
    opening = next(iter(broker.state().lots))
    store.append("transaction", {"reason": "fill", "cash_delta": "5", "fee": "0", "legs": [
        {"market": "M", "side": "yes", "quantity_delta": "-10", "price": "0.5"}]}, "live")
    with pytest.raises(ValueError):
        broker.correct([opening], "would orphan the later close")
    assert len([e for e in store.events() if e["type"] == "correction"]) == 0


# --------------------------------------------------------------------------- tracking

class Account:
    """Account-monitor fixture: fills, order list and balances as Kalshi returns them."""
    key_id = "fixture"

    def __init__(self, fills, orders, balance_cents=100000):
        self.fills, self.orders, self.balance = fills, orders, balance_cents

    def get(self, path, params=None):
        if path == "/portfolio/balance":
            return {"balance": self.balance}
        return {}

    def pages(self, path, key, **params):
        if path == "/portfolio/fills":
            return iter(self.fills if "subaccount" not in params else [f for f in self.fills if f.get("sub") == params["subaccount"]])
        if path == "/portfolio/orders":
            return iter(self.orders)
        return iter([])


def fill(fid, qty, price, minute, order_id="venue-1", sub=3, fee="0.01"):
    return {"fill_id": fid, "trade_id": fid, "order_id": order_id, "count_fp": str(qty), "yes_price_dollars": price,
            "fee_cost": fee, "created_time": f"2026-10-25T12:{minute:02d}:00Z", "exchange_index": 0, "sub": sub}


def test_out_of_order_and_duplicated_fills_book_once_in_time_order(tmp_path):
    store, broker, _ = live_order(tmp_path)
    fills = [fill("f3", 2, "0.40", 3), fill("f1", 5, "0.39", 1), fill("f2", 3, "0.40", 2)]
    # The same fills arrive again on the subaccount page: they must not double count.
    status = account_pass(store, Account(fills, [{"order_id": "venue-1", "client_order_id": "o1", "status": "executed",
                                                  "fill_count_fp": "10"}]))
    assert status["mapped_fills"] == 3  # duplicates across pages are dropped before booking
    lot_qty = sum(l["quantity"] for l in broker.state().lots.values())
    assert lot_qty == 10
    assert store.db.execute("SELECT filled,status,reserved FROM orders WHERE id='o1'").fetchone()[:] == ("10", "executed", "0")
    # A second pass with nothing new changes nothing.
    account_pass(store, Account(fills, []))
    assert sum(l["quantity"] for l in broker.state().lots.values()) == 10


def test_lost_acknowledgement_is_recovered_from_the_order_list(tmp_path):
    store = Store(tmp_path)
    store.create("live", mode="live", subaccount=3)

    class LostAck:
        def create_order(self, order):
            raise TimeoutError("acknowledgement lost")

    broker = Broker(store, "live", LostAck())
    with pytest.raises(TimeoutError):
        broker.submit({"ticker": "M", "side": "bid", "count": "4", "price": "0.40", "client_order_id": "o1"}, {"basket": 7})
    orders = [{"order_id": "venue-9", "client_order_id": "o1", "status": "executed", "fill_count_fp": "4"}]
    account_pass(store, Account([fill("f1", 4, "0.40", 1, order_id="venue-9")], orders))
    lot = next(iter(broker.state().lots.values()))
    assert lot["quantity"] == 4 and lot["context"] == {"basket": 7}


def test_partial_fill_then_cancel_releases_reservation_only_when_counts_agree(tmp_path):
    store, broker, adapter = live_order(tmp_path)
    broker.cancel("o1")
    assert store.reserved("live") > 0  # still waiting for the venue
    order = {"order_id": "venue-1", "client_order_id": "o1", "status": "canceled", "fill_count_fp": "6"}
    account_pass(store, Account([fill("f1", 4, "0.40", 1)], [order]))
    assert store.reserved("live") > 0  # venue says 6 filled, only 4 booked: keep the hold
    account_pass(store, Account([fill("f1", 4, "0.40", 1), fill("f2", 2, "0.41", 2)], [order]))
    assert store.reserved("live") == 0


def test_fill_exceeding_order_count_is_quarantined_not_crash_the_monitor(tmp_path):
    store, broker, _ = live_order(tmp_path, count="5")
    # Venue reports more than we asked for (amend raced, or a bad mapping).
    status = account_pass(store, Account([fill("f1", 8, "0.40", 1)], []))
    assert status["status"] == "degraded"


# --------------------------------------------------------------------------- cached state

def full(store, portfolio):
    from czquant.accounting import replay
    return replay(store.events(portfolio, kinds=("transaction", "correction")))


def same(a, b):
    return (a.cash, a.realized, a.fees, a.fills, a.settlements,
            sorted((l["market"], l["side"], l["quantity"], l["cost"]) for l in a.lots.values())) == \
           (b.cash, b.realized, b.fees, b.fills, b.settlements,
            sorted((l["market"], l["side"], l["quantity"], l["cost"]) for l in b.lots.values()))


@pytest.mark.parametrize("seed", range(15))
def test_cached_state_always_equals_a_full_replay(tmp_path, seed):
    """Random fills (some backdated, as venue fills are), corrections and a rolled-back
    write: after every step the cached state must equal a full replay."""
    rng = random.Random(seed)
    store = Store(tmp_path)
    store.create("p", "100000")
    broker = Broker(store, "p")
    other = Store(tmp_path)  # a second process writing to the same ledger
    for step in range(60):
        action = rng.random()
        if action < 0.6:
            side, qty = rng.choice(["yes", "no"]), rng.randint(1, 5)
            held = sum(l["quantity"] for l in full(store, "p").lots.values() if l["market"] == "M" and l["side"] == side)
            delta = qty if rng.random() < 0.6 or held < qty else -qty
            # Venue fills can arrive backdated. Only openings are backdated here: that
            # keeps the history valid while still changing which lot FIFO matches first.
            at = f"2026-10-{rng.randint(1, 25):02d}T12:00:00+00:00" if delta > 0 and rng.random() < 0.3 else None
            writer = other if rng.random() < 0.3 else store
            writer.append("transaction", {"reason": "fill", "cash_delta": str(-delta * D("0.4")), "fee": "0.01",
                                          "legs": [{"market": "M", "side": side, "quantity_delta": str(delta), "price": "0.4"}]},
                          "p", at=at)
        elif action < 0.75:
            opens = [e["record_id"] for e in store.events("p", kinds=("transaction",))
                     if e["data"].get("legs") and D(e["data"]["legs"][0]["quantity_delta"]) > 0]
            if opens:
                try:
                    broker.correct([rng.choice(opens)], "fixture")
                except ValueError:
                    pass  # would orphan a later close: rolled back
        else:
            with pytest.raises(RuntimeError):
                with store.transaction():
                    store.append("transaction", {"reason": "fill", "cash_delta": "-4", "fee": "0", "legs": [
                        {"market": "M", "side": "yes", "quantity_delta": "10", "price": "0.4"}]}, "p")
                    broker.state()  # caches the uncommitted row...
                    raise RuntimeError("abort")  # ...which the rollback must discard
        assert same(broker.state(), full(store, "p")), step


def test_repeated_quarantined_fill_is_recorded_once(tmp_path):
    store, broker, _ = live_order(tmp_path, count="5")
    bad = [fill("f1", 8, "0.40", 1)]
    account_pass(store, Account(bad, []))
    account_pass(store, Account(bad, []))
    assert len([e for e in store.events() if e["type"] == "quarantine"]) == 1
    assert not broker.state().lots
