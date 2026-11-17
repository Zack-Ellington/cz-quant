"""Independent REST reference collection, paper settlement, and account recovery."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from .accounting import portfolio_state, replay
from .broker import PRIMARY, Broker, scope
from .kalshi import Kalshi
from .book import levels
from .draftkings import DraftKings, is_sportsbook, parse_ticker
from .paper import update_consumed
from .polymarket import Polymarket, is_polymarket
from .sportsbook import settle_lots
from .store import Store, decimal, utc

log = logging.getLogger(__name__)


def market_pass(store: Store, api: Any) -> dict[str, Any]:
    portfolios = [p for p in store.portfolios() if p["mode"] == "paper"]
    states = {p["id"]: replay(store.events(p["id"], kinds=("transaction", "correction"))) for p in portfolios}
    tickers = {lot["market"] for state in states.values() for lot in state.lots.values()}
    for row in store.db.execute("SELECT request FROM orders WHERE status IN ('pending','unknown','acknowledged')"):
        tickers.add(json.loads(row[0])["ticker"])
    for p in portfolios:
        tickers.update(p["metadata"].get("watch", []))
    # Sportsbook selections and Polymarket tokens have their own readers and monitors.
    tickers = {t for t in tickers if not is_sportsbook(t) and not is_polymarket(t)}
    if not tickers:
        # A real public-data probe, even for no-trade portfolios.
        probe = api.get("/markets", {"limit": 1, "status": "open"}).get("markets", [])
        # Listing may include short-lived generated markets that disappear before
        # their detail request. With no exposure, the listing itself is the probe.
        store.append("market_probe", {"returned": len(probe)})
    errors = []
    for ticker in sorted(tickers):
        try:
            market = api.market(ticker)
            raw = api.book(ticker) if market.get("status") not in ("settled", "finalized") else {}
            observed = utc()
            store.append("market", {"ticker": ticker, "market": market, "book": raw}, at=observed)
            with store.transaction():
                update_consumed(store, ticker, levels(raw), time.time())
            final = market.get("status") in ("settled", "finalized")
            payout = market.get("settlement_value_dollars")
            if final and payout is not None:
                for portfolio, state in states.items():
                    broker = Broker(store, portfolio, run_id="paper-account-monitor")
                    for lot_id, lot in state.lots.items():
                        if lot["market"] == ticker:
                            broker.settle(lot_id, str(payout), {"market": market, "observed_at": observed})
            elif final:
                errors.append({"market": ticker, "reason": "missing_final_payout"})
            time.sleep(0.1)
        except Exception as exc:
            errors.append({"market": ticker, "reason": type(exc).__name__})
            log.exception("market observation failed for %s", ticker)
    for p in portfolios:
        state = replay(store.events(p["id"], kinds=("transaction", "correction")))
        store.append("snapshot", {"source": "paper_replay", "cash": state.cash, "realized": state.realized,
            "reserved": store.reserved(p["id"]), "lots": list(state.lots.values())}, p["id"])
    status = {"component": "market", "status": "healthy" if not errors else "degraded",
              "markets": len(tickers), "errors": errors}
    store.append("health", status)
    return status


def sportsbook_pass(store: Store, book: DraftKings) -> dict[str, Any]:
    """Reference odds and paper settlement for held or pending sportsbook selections.

    Odds cost credits, so this pass never buys them: it marks held selections from
    the newest read any process cached (the trader's), dated when that read was
    made, so a mark is as stale as it really is. It records a mark only when a newer
    read or a status change arrives. Results are the one paid read: once per league
    every `results_minutes`, only for games that started at least `settle_after_hours`
    ago, all of a league's events in one call. Finalized selections settle every
    paper lot: 1 or 0, or the lot's own price on a void, which returns the stake.
    """
    portfolios = [p for p in store.portfolios() if p["mode"] == "paper"]
    tickers = {lot["market"] for p in portfolios
               for lot in replay(store.events(p["id"], kinds=("transaction", "correction"))).lots.values()}
    for row in store.db.execute("SELECT request FROM orders WHERE status IN ('pending','unknown','acknowledged')"):
        tickers.add(json.loads(row[0])["ticker"])
    for p in portfolios:
        tickers.update(p["metadata"].get("watch", []))
    tickers = {t for t in tickers if is_sportsbook(t)}
    errors: list[dict[str, Any]] = []
    now = time.time()
    due: set[str] = set()
    for ticker in tickers:
        parts = parse_ticker(ticker)
        if now - book.results_checked.get(parts["league"], -1e18) < book.results_minutes * 60:
            continue
        start = _start(store, book, ticker)
        if start is not None and now - start >= book.settle_after_hours * 3600:
            due.add(parts["event"])
    try:
        markets = book.markets(sorted(tickers), results_for=due, fetch=False) if tickers else {}
        for ticker in tickers:
            if parse_ticker(ticker)["event"] in due:
                book.results_checked[parse_ticker(ticker)["league"]] = now
    except Exception as exc:
        log.exception("sportsbook read failed")
        markets = {}
        errors.append({"reason": type(exc).__name__, "detail": str(exc)[:300]})
    settled = 0
    for ticker in sorted(tickers):
        if ticker not in markets:
            if not errors:
                errors.append({"market": ticker, "reason": "missing"})
            continue
        market, raw = markets[ticker]
        key = (market.get("fetched_at"), market["status"])
        if market["status"] == "finalized" or book.last_marked.get(ticker) != key:
            observed = utc()
            # Date the mark by its read, not by this pass, so report staleness rules apply honestly.
            at = market.get("fetched_at") if market["status"] != "finalized" and market.get("fetched_at") else observed
            store.append("market", {"ticker": ticker, "market": market, "book": raw}, at=at)
            book.last_marked[ticker] = key
        if market["status"] == "finalized":
            settled += settle_lots(store, ticker, market, {"market": market, "observed_at": utc()},
                                   "sportsbook-monitor")
        elif market["status"] == "closed":
            errors.append({"market": ticker, "reason": "awaiting_result"})
    status = {"component": "draftkings", "source": book.source.name,
              "status": "healthy" if not errors else "degraded", "markets": len(tickers),
              "settled_lots": settled, "results_checked": sorted(due), "credits_remaining": book.credits(),
              "errors": errors}
    store.append("health", status)
    return status


def _start(store: Store, book: DraftKings, ticker: str) -> float | None:
    """A selection's start time: from the newest cached read, else its last recorded mark."""
    parts = parse_ticker(ticker)
    selection = next((s for s in book.selections(parts["league"], fetch=False) if s.ticker == ticker), None)
    if selection is not None:
        return datetime.fromisoformat(selection.start).timestamp()
    row = store.db.execute("SELECT json_extract(data,'$.market.start') FROM events WHERE type='market' AND "
                           "json_extract(data,'$.ticker')=? AND json_extract(data,'$.market.start') IS NOT NULL "
                           "ORDER BY sequence DESC LIMIT 1", (ticker,)).fetchone()
    return datetime.fromisoformat(row[0]).timestamp() if row else None


def fill_time(fill: dict[str, Any]) -> str:
    raw = fill.get("created_time")
    if raw:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat(timespec="microseconds")
    return datetime.fromtimestamp(float(fill["ts"]), timezone.utc).isoformat(timespec="microseconds")


def account_pass(store: Store, api: Any) -> dict[str, Any]:
    if not api.key_id:
        raise ValueError("account monitor requires authenticated credentials")
    checkpoint = store.db.execute("SELECT value FROM checkpoints WHERE name='account' ").fetchone()
    # Bootstrap captures current state; history ingests since this collector's startup.
    since = int(float(checkpoint[0])) - 300 if checkpoint else int(time.time()) - 300
    watermark = int(time.time())
    cutoffs = api.get("/historical/cutoff")
    fills = list(api.pages("/portfolio/fills", "fills", min_ts=since, limit=1000))
    cutoff = cutoffs.get("trades_created_ts")
    if cutoff:
        epoch = datetime.fromisoformat(cutoff.replace("Z", "+00:00")).timestamp() if isinstance(cutoff, str) else float(cutoff)
        if since < epoch:
            fills += list(api.pages("/historical/fills", "fills", min_ts=since, limit=1000))
    live = [p for p in store.portfolios() if p["mode"] == "live"]
    # Recover venue IDs after lost acknowledgements before joining fills.
    for p in live:
        for order in api.pages("/portfolio/orders", "orders", **scope(p["metadata"]), limit=1000):
            client_id = order.get("client_order_id")
            store.db.execute("UPDATE orders SET venue_id=? WHERE id=? AND portfolio_id=?",
                             (order["order_id"], client_id, p["id"]))
        fills += list(api.pages("/portfolio/fills", "fills", **scope(p["metadata"]), min_ts=since, limit=1000))
    fills = sorted({(f.get("exchange_index",0),f["fill_id"]):f for f in fills}.values(), key=fill_time)
    mapped, unattributed, quarantined = 0, 0, 0
    discrepancies: list[dict[str, Any]] = []
    for fill in fills:
        # REST and stream adapters must normalize to this trade/order/shard identity.
        key = f"kalshi-fill:{fill.get('exchange_index',0)}:{fill['order_id']}:{fill.get('trade_id') or fill['fill_id']}"
        row = store.db.execute("SELECT * FROM orders WHERE venue_id=? OR id=?",
                               (fill["order_id"], fill.get("client_order_id", ""))).fetchone()
        if row:
            side = json.loads(row["request"])["side"]
            price = fill.get("yes_price_dollars")
            try:
                if price is None:
                    raise ValueError("fill lacks a precise YES price")
                broker = Broker(store, row["portfolio_id"], run_id=row["run_id"])
                broker.fill(row["id"], fill["count_fp"], price, fill["fee_cost"], key,
                            {"venue": "kalshi", "trade_id": fill.get("trade_id"), "book_side": side}, fill_time(fill))
                mapped += 1
            except ValueError as exc:
                # One fill the ledger can't accept (an overfill, a conflicting receipt, a
                # missing price) must not stop reconciliation for every other fill and
                # portfolio. It is set aside, once, for an operator to resolve.
                quarantined += 1
                discrepancies.append({"portfolio": row["portfolio_id"], "kind": "fill_quarantined",
                                      "order": row["id"], "fill": key, "reason": str(exc)})
                store.append("quarantine", {"kind": "fill", "reason": str(exc), "order": row["id"], "raw": fill},
                             row["portfolio_id"], source=key + ":quarantine", at=fill_time(fill))
        else:
            unattributed += 1
        store.append("account_evidence", {"kind": "fill", "raw": fill}, source=key + ":raw", at=fill_time(fill))
    # Collect authoritative snapshots and settlement/transfer evidence even without mapped live portfolios.
    balance = api.get("/portfolio/balance")
    positions = list(api.pages("/portfolio/positions", "market_positions", limit=1000))
    settlements = list(api.pages("/portfolio/settlements", "settlements", min_ts=since, limit=1000))
    orders = list(api.pages("/portfolio/orders", "orders", limit=1000))
    store.append("account_snapshot", {"balance": balance, "positions": positions, "orders": orders,
                                     "settlements": settlements, "history_from": since})
    transfers: list[dict[str, Any]] = []
    if live:
        try:
            transfers = list(api.pages("/portfolio/subaccounts/transfers", "transfers", limit=1000))
        except httpx.HTTPStatusError:
            # An account without subaccount access can't list transfers. That only
            # matters when a numbered subaccount is mapped; the primary has none.
            if any(p["metadata"]["subaccount"] != PRIMARY for p in live):
                raise
        for transfer in transfers:
            at = datetime.fromtimestamp(transfer["created_ts"],timezone.utc).isoformat()
            for p in live:
                if fill_time({"created_time":at}) < p["created_at"]:
                    continue
                sub = p["metadata"]["subaccount"]
                if transfer.get("exchange_index",0) != p["metadata"].get("exchange_index",0):
                    continue
                direction = int(transfer["to_subaccount"]==sub)-int(transfer["from_subaccount"]==sub)
                if direction:
                    cash = direction*decimal(transfer["amount_cents"])/100
                    store.append("transaction", {"reason":"funding","cash_delta":str(cash),
                        "external_delta":str(cash),"fee":"0","legs":[],"evidence":transfer},p["id"],
                        source=f"transfer:{transfer['exchange_index']}:{transfer['transfer_id']}:{sub}",at=at)
    for p in store.portfolios():
        if p["mode"] != "live":
            continue
        sub = p["metadata"]["subaccount"]
        for settlement in api.pages("/portfolio/settlements","settlements",**scope(p["metadata"]),min_ts=since,limit=1000):
            source = f"settlement:{settlement.get('exchange_index',0)}:{sub}:{settlement['ticker']}:{settlement['settled_time']}"
            if store.db.execute("SELECT 1 FROM events WHERE source_key=?",(source,)).fetchone():
                continue
            state = portfolio_state(store, p["id"])
            lots = [lot for lot in state.lots.values() if lot["market"]==settlement["ticker"]]
            yes = decimal(settlement['value'])/100 if settlement.get('value') is not None else (Decimal(1) if settlement['market_result']=='yes' else Decimal(0))
            expected = sum((lot['quantity']*(yes if lot['side']=='yes' else 1-yes) for lot in lots),Decimal(0))
            revenue = decimal(settlement['revenue'])/100
            quantities = {side:sum((l['quantity'] for l in lots if l['side']==side),Decimal(0)) for side in ('yes','no')}
            if expected != revenue or any(quantities[side]!=decimal(settlement[side+'_count_fp']) for side in ('yes','no')):
                discrepancies.append({'portfolio':p['id'],'kind':'unresolved_settlement','market':settlement['ticker']})
                continue
            with store.transaction():
                for lot in lots:
                    payout = yes if lot['side']=='yes' else 1-yes
                    store.append('transaction',{'reason':'settlement','cash_delta':str(lot['quantity']*payout),
                        'fee':'0','context':lot['context'],'evidence':settlement,
                        'legs':[{'market':lot['market'],'side':lot['side'],'quantity_delta':str(-lot['quantity']),
                                 'price':str(payout),'lot_id':lot['record_id']}]},p['id'],
                        source=source+':'+lot['record_id'],at=settlement['settled_time'])
                store.append('account_evidence',{'kind':'settlement','raw':settlement},p['id'],source=source)
        actual = api.get("/portfolio/balance", scope(p["metadata"]))
        state = portfolio_state(store, p["id"])
        cash = decimal(actual["balance_dollars"]) if "balance_dollars" in actual else decimal(actual["balance"]) / 100
        observed_positions = list(api.pages("/portfolio/positions", "market_positions", **scope(p["metadata"]), limit=1000))
        expected_positions = {}
        for lot in state.lots.values():
            expected_positions[lot["market"]] = expected_positions.get(lot["market"],Decimal(0)) + lot["quantity"]*(1 if lot["side"]=="yes" else -1)
        actual_positions = {row["ticker"]:decimal(row["position_fp"]) for row in observed_positions if decimal(row["position_fp"])}
        if actual_positions != {k:v for k,v in expected_positions.items() if v}:
            discrepancies.append({"portfolio":p["id"],"kind":"inventory_mismatch"})
        # Unknown collateral/transfer effects are never converted to invented profit.
        if cash != state.cash:
            discrepancies.append({"portfolio": p["id"], "kind": "cash_mismatch", "recorded": str(state.cash), "observed": str(cash)})
        for order in api.pages("/portfolio/orders", "orders", **scope(p["metadata"]), limit=1000):
            row = store.db.execute("SELECT id,filled FROM orders WHERE venue_id=?", (order["order_id"],)).fetchone()
            if row and order.get("status") in ("canceled", "executed"):
                if decimal(order.get("fill_count_fp", "0")) == decimal(row["filled"]):
                    Broker(store, p["id"]).finish(row["id"], order["status"])
    store.db.execute("INSERT OR REPLACE INTO checkpoints VALUES('account',?)", (str(watermark),))
    status = {"component": "account", "status": "healthy" if not discrepancies else "degraded",
              "mapped_fills": mapped, "unattributed_fills": unattributed, "quarantined_fills": quarantined,
              "discrepancies": discrepancies}
    store.append("health", status)
    return status


def polymarket_pass(store: Store, client: Polymarket) -> dict[str, Any]:
    """Mark every Polymarket token a paper portfolio holds or watches, and settle the
    lots of any whose result is final. Public reads only: no credentials."""
    portfolios = [p for p in store.portfolios() if p["mode"] == "paper"]
    tickers = {lot["market"] for p in portfolios
               for lot in replay(store.events(p["id"], kinds=("transaction", "correction"))).lots.values()}
    for row in store.db.execute("SELECT request FROM orders WHERE status IN ('pending','unknown','acknowledged')"):
        tickers.add(json.loads(row[0])["ticker"])
    for p in portfolios:
        tickers.update(p["metadata"].get("watch", []))
    tickers = {t for t in tickers if is_polymarket(t)}
    errors, settled = [], 0
    for ticker in sorted(tickers):
        try:
            market = client.market(ticker, fresh=True)
            raw = client.book(ticker) if market["status"] == "active" else {}
            observed = utc()
            store.append("market", {"ticker": ticker, "market": market, "book": raw}, at=observed)
            with store.transaction():
                update_consumed(store, ticker, levels(raw), time.time())
            if market["status"] == "finalized":
                settled += settle_lots(store, ticker, market, {"market": market, "observed_at": observed},
                                       "paper-polymarket-monitor")
        except Exception as exc:
            errors.append({"market": ticker, "reason": type(exc).__name__})
            log.exception("polymarket observation failed for %s", ticker)
    status = {"component": "polymarket", "status": "healthy" if not errors else "degraded",
              "markets": len(tickers), "settled_lots": settled, "errors": errors}
    store.append("health", status)
    return status


def run_monitor(root: str, component: str, interval: float = 60, once: bool = False) -> None:
    store = Store(root)
    api: Any = (DraftKings.from_env() if component == "draftkings" else Polymarket.from_env()
                if component == "polymarket" else Kalshi(authenticated=component == "account"))
    passes = {"market": market_pass, "account": account_pass, "draftkings": sportsbook_pass,
              "polymarket": polymarket_pass}
    while True:
        start = time.monotonic()
        try:
            status = passes[component](store, api)
            log.info("%s", json.dumps(status))
        except Exception as exc:
            store.append("health", {"component": component, "status": "failed", "error": type(exc).__name__})
            raise  # The service manager restarts it; never conceal dead collector tasks.
        if once:
            store.close()
            return
        time.sleep(max(1, interval - (time.monotonic() - start)))
