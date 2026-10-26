"""Independent REST reference collection, paper settlement, and account recovery."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .accounting import portfolio_state, replay
from .broker import Broker
from .kalshi import Kalshi
from .book import levels
from .paper import update_consumed
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
        sub = p["metadata"]["subaccount"]
        for order in api.pages("/portfolio/orders", "orders", subaccount=sub, limit=1000):
            client_id = order.get("client_order_id")
            store.db.execute("UPDATE orders SET venue_id=? WHERE id=? AND portfolio_id=?",
                             (order["order_id"], client_id, p["id"]))
        fills += list(api.pages("/portfolio/fills", "fills", subaccount=sub, min_ts=since, limit=1000))
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
    if live:
        for transfer in api.pages("/portfolio/subaccounts/transfers", "transfers", limit=1000):
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
        for settlement in api.pages("/portfolio/settlements","settlements",subaccount=sub,min_ts=since,limit=1000):
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
        actual = api.get("/portfolio/balance", {"subaccount": sub})
        state = portfolio_state(store, p["id"])
        cash = decimal(actual["balance_dollars"]) if "balance_dollars" in actual else decimal(actual["balance"]) / 100
        observed_positions = list(api.pages("/portfolio/positions", "market_positions", subaccount=sub, limit=1000))
        expected_positions = {}
        for lot in state.lots.values():
            expected_positions[lot["market"]] = expected_positions.get(lot["market"],Decimal(0)) + lot["quantity"]*(1 if lot["side"]=="yes" else -1)
        actual_positions = {row["ticker"]:decimal(row["position_fp"]) for row in observed_positions if decimal(row["position_fp"])}
        if actual_positions != {k:v for k,v in expected_positions.items() if v}:
            discrepancies.append({"portfolio":p["id"],"kind":"inventory_mismatch"})
        # Unknown collateral/transfer effects are never converted to invented profit.
        if cash != state.cash:
            discrepancies.append({"portfolio": p["id"], "kind": "cash_mismatch", "recorded": str(state.cash), "observed": str(cash)})
        for order in api.pages("/portfolio/orders", "orders", subaccount=sub, limit=1000):
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


def run_monitor(root: str, component: str, interval: float = 60, once: bool = False) -> None:
    store = Store(root)
    api = Kalshi(authenticated=component == "account")
    while True:
        start = time.monotonic()
        try:
            status = market_pass(store, api) if component == "market" else account_pass(store, api)
            log.info("%s", json.dumps(status))
        except Exception as exc:
            store.append("health", {"component": component, "status": "failed", "error": type(exc).__name__})
            raise  # The service manager restarts it; never conceal dead collector tasks.
        if once:
            store.close()
            return
        time.sleep(max(1, interval - (time.monotonic() - start)))
