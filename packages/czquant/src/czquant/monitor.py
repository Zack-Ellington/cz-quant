"""Independent REST reference collection, paper settlement, and account recovery."""

from __future__ import annotations

import json
import logging
import time
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from .accounting import portfolio_state, replay, venue_of
from .broker import PRIMARY, Broker, scope
from .kalshi import Kalshi
from .book import levels
from .paper import update_consumed
from .polymarket import Polymarket, is_polymarket
from .polymarket_us import PolymarketUS, amount, is_polymarket_us, position_quantity, slug_of
from .operator import account_of, active_live, kalshi_cash, kalshi_positions
from .store import Store, decimal, utc

log = logging.getLogger(__name__)


def market_pass(store: Store, api: Any) -> dict[str, Any]:
    portfolios = store.pools("kalshi")
    paper = {p["id"] for p in portfolios if p["mode"] == "paper"}
    states = {p["id"]: replay(store.events(p["id"], kinds=("transaction", "correction"))) for p in portfolios}
    tickers = {lot["market"] for state in states.values() for lot in state.lots.values()}
    for row in store.db.execute("SELECT request FROM orders WHERE status IN ('pending','unknown','acknowledged')"):
        tickers.add(json.loads(row[0])["ticker"])
    for p in portfolios:
        tickers.update(p["metadata"].get("watch", []))
    # Collect only this venue; other or retired namespaces are never sent to Kalshi.
    tickers = {t for t in tickers if venue_of(t) == "kalshi"}
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
                    if portfolio not in paper:
                        continue
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
        if p["id"] not in paper:
            continue
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
    live = [p for p in active_live(store) if p["venue"] == "kalshi"]
    # Portfolios sharing one Kalshi account are reconciled against it together.
    groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for p in live:
        groups.setdefault(account_of(p["metadata"]), []).append(p)
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
                group = groups[account_of(p["metadata"])]
                reserve = next((q for q in group if q["metadata"].get("reserve")), None)
                if direction and reserve is not None and p is not reserve:
                    continue  # booked once, to the account's reserve
                if direction and len(group) > 1 and reserve is None:
                    # Whose share of a shared account moved is the operator's call: `allocate`.
                    discrepancies.append({"portfolio": p["id"], "kind": "shared_account_transfer",
                                          "transfer": transfer.get("transfer_id")})
                elif direction:
                    cash = direction*decimal(transfer["amount_cents"])/100
                    store.append("transaction", {"reason":"funding","cash_delta":str(cash),
                        "external_delta":str(cash),"fee":"0","legs":[],"evidence":transfer},p["id"],
                        source=f"transfer:{transfer['exchange_index']}:{transfer['transfer_id']}:{sub}",at=at)
    balances: list[dict[str, Any]] = []
    for (sub, exchange), group in groups.items():
        lead = group[0]["metadata"]
        ids = [p["id"] for p in group]
        for settlement in api.pages("/portfolio/settlements","settlements",**scope(lead),min_ts=since,limit=1000):
            source = f"settlement:{settlement.get('exchange_index',0)}:{sub}:{settlement['ticker']}:{settlement['settled_time']}"
            if store.db.execute("SELECT 1 FROM events WHERE source_key=?",(source,)).fetchone():
                continue
            lots = [(p["id"], lot) for p in group for lot in portfolio_state(store, p["id"]).lots.values()
                    if lot["market"]==settlement["ticker"]]
            # Attribute discrepancies to holders, past traders, or the reserve.
            # Unregistered accounts without an owner retain an account-wide gap.
            traders = sorted({r[0] for r in store.db.execute("SELECT portfolio_id, request FROM orders")
                              if r[0] in ids and json.loads(r[1]).get("ticker") == settlement["ticker"]})
            reserve = [p["id"] for p in group if p["metadata"].get("reserve")]
            holders = sorted({pid for pid, _ in lots}) or traders or reserve or ids
            yes = decimal(settlement['value'])/100 if settlement.get('value') is not None else (Decimal(1) if settlement['market_result']=='yes' else Decimal(0))
            expected = sum((lot['quantity']*(yes if lot['side']=='yes' else 1-yes) for _, lot in lots),Decimal(0))
            revenue = decimal(settlement['revenue'])/100
            # Kalshi reports gross YES and NO counts: a bought-then-sold position settles as
            # equal YES and NO with no revenue. Positions net (as Kalshi nets them), so the
            # ledger's net lots are compared with the settlement's net count.
            ledger_net = sum((l['quantity']*(1 if l['side']=='yes' else -1) for _, l in lots),Decimal(0))
            settled_net = decimal(settlement['yes_count_fp']) - decimal(settlement['no_count_fp'])
            if expected != revenue or ledger_net != settled_net:
                discrepancies.extend({'portfolio':pid,'kind':'unresolved_settlement','market':settlement['ticker']} for pid in holders)
                continue
            with store.transaction():
                for pid, lot in lots:
                    payout = yes if lot['side']=='yes' else 1-yes
                    store.append('transaction',{'reason':'settlement','cash_delta':str(lot['quantity']*payout),
                        'fee':'0','context':lot['context'],'evidence':settlement,
                        'legs':[{'market':lot['market'],'side':lot['side'],'quantity_delta':str(-lot['quantity']),
                                 'price':str(payout),'lot_id':lot['record_id']}]},pid,
                        source=source+':'+lot['record_id'],at=settlement['settled_time'])
                store.append('account_evidence',{'kind':'settlement','raw':settlement},
                             holders[0] if len(holders)==1 else None,source=source)
        actual = api.get("/portfolio/balance", scope(lead))
        cash = decimal(actual["balance_dollars"]) if "balance_dollars" in actual else decimal(actual["balance"]) / 100
        observed_positions = list(api.pages("/portfolio/positions", "market_positions", **scope(lead), limit=1000))
        # Another venue's lots are reconciled by that venue's monitor.
        expected_positions = kalshi_positions(store, group)
        actual_positions = {row["ticker"]:decimal(row["position_fp"]) for row in observed_positions if decimal(row["position_fp"])}
        wrong = sorted(m for m in set(actual_positions) | set(expected_positions)
                       if actual_positions.get(m) != expected_positions.get(m))
        if wrong:
            owners = sorted({p["id"] for p in group for lot in portfolio_state(store, p["id"]).lots.values()
                             if lot["market"] in wrong}) or ids
            discrepancies.extend({"portfolio": pid, "kind": "inventory_mismatch", "markets": wrong} for pid in owners)
        # Unknown collateral/transfer effects are never converted to invented profit.
        # On a shared account a cash difference can't be attributed: every sharer sees it.
        recorded = sum((kalshi_cash(store, p) for p in group), Decimal(0))
        balances.append({"subaccount": sub, "exchange_index": exchange, "cash": str(cash),
                         "recorded": {p["id"]: str(kalshi_cash(store, p)) for p in group},
                         "positions": len(actual_positions)})
        if cash != recorded:
            discrepancies.extend({"portfolio": pid, "kind": "cash_mismatch", "venue": "kalshi",
                                  "recorded": str(recorded), "observed": str(cash),
                                  **({"shared_with": ids} if len(ids) > 1 else {})} for pid in ids)
        for order in api.pages("/portfolio/orders", "orders", **scope(lead), limit=1000):
            row = store.db.execute("SELECT id,filled,portfolio_id FROM orders WHERE venue_id=? "
                                     "AND status IN ('pending','unknown','acknowledged')", (order["order_id"],)).fetchone()
            if row and order.get("status") in ("canceled", "executed"):
                if decimal(order.get("fill_count_fp", "0")) == decimal(row["filled"]):
                    Broker(store, row["portfolio_id"]).finish(row["id"], order["status"])
    store.db.execute("INSERT OR REPLACE INTO checkpoints VALUES('account',?)", (str(watermark),))
    status = {"component": "account", "status": "healthy" if not discrepancies else "degraded",
              "mapped_fills": mapped, "unattributed_fills": unattributed, "quarantined_fills": quarantined,
              "balances": balances, "discrepancies": discrepancies}
    store.append("health", status)
    return status


def polymarket_pass(store: Store, client: Polymarket) -> dict[str, Any]:
    """Mark every Polymarket token a paper portfolio holds or watches, and settle the
    lots of any whose result is final. Public reads only: no credentials."""
    portfolios = [p for p in store.pools("polymarket") if p["mode"] == "paper"]
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
                for p in portfolios:
                    broker = Broker(store, p["id"], venue="polymarket", run_id="paper-polymarket-monitor")
                    for lot_id, lot in list(broker.state().lots.items()):
                        if lot["market"] == ticker:
                            broker.settle(lot_id, str(market["settlement_value_dollars"]),
                                          {"market": market, "observed_at": observed})
                            settled += 1
        except Exception as exc:
            errors.append({"market": ticker, "reason": type(exc).__name__})
            log.exception("polymarket observation failed for %s", ticker)
    status = {"component": "polymarket", "status": "healthy" if not errors else "degraded",
              "markets": len(tickers), "settled_lots": settled, "errors": errors}
    store.append("health", status)
    return status


FUNDING_ACTIVITIES = {"ACTIVITY_TYPE_ACCOUNT_DEPOSIT": 1, "ACTIVITY_TYPE_ACCOUNT_ADVANCED_DEPOSIT": 1,
                      "ACTIVITY_TYPE_ACCOUNT_WITHDRAWAL": -1}
ORDER_TERMINAL = {"ORDER_STATE_FILLED": "filled", "ORDER_STATE_CANCELED": "canceled",
                  "ORDER_STATE_REJECTED": "rejected", "ORDER_STATE_EXPIRED": "canceled"}


def _recover_orders(store: Store, client: PolymarketUS, p: dict[str, Any], broker: Broker,
                    discrepancies: list[dict[str, Any]]) -> int:
    recovered = 0
    rows = store.db.execute("SELECT * FROM orders WHERE portfolio_id=? AND status IN ('pending','acknowledged','unknown')",
                            (p["id"],)).fetchall()
    for row in rows:
        if not is_polymarket_us(json.loads(row["request"])["ticker"]):
            continue
        if not row["venue_id"]:
            discrepancies.append({"portfolio": p["id"], "kind": "order_without_venue_id", "order": row["id"]})
            continue
        order = client.order(row["venue_id"])
        cumulative = decimal(order.get("cumQuantity") or "0")
        missing = cumulative - decimal(row["filled"])
        if missing > 0:
            average = amount(order.get("avgPx"))
            if average is None:
                discrepancies.append({"portfolio": p["id"], "kind": "fill_without_price", "order": row["id"]})
                continue
            booked = [e["data"] for e in store.events(p["id"], kinds=("execution",)) if e["data"].get("order_id") == row["id"]]
            notional = sum((decimal(b["quantity"]) * decimal(b["yes_price"]) for b in booked), Decimal(0))
            fees = sum((decimal(b["fee"]) for b in booked), Decimal(0))
            total_fee = amount(order.get("commissionNotionalTotalCollected")) or Decimal(0)
            # The unbooked part's price: the order's notional less what's booked, per missing contract.
            price = (average * cumulative - notional) / missing
            broker.fill(row["id"], str(missing), str(price), str(max(Decimal(0), total_fee - fees)),
                        f"pmus-order:{row['venue_id']}:{cumulative}", {"venue": "polymarket_us", "order": order})
            recovered += 1
        status = ORDER_TERMINAL.get(order.get("state", ""))
        if status:
            broker.finish(row["id"], status)
    return recovered


DEPOSIT_FAILED = ("FAILED", "CANCELED", "CANCELLED", "REJECTED", "RETURNED", "REVERSED")


def _booked(store: Store, source: str) -> Decimal | None:
    row = store.db.execute("SELECT data FROM events WHERE source_key=?", (source,)).fetchone()
    return decimal(json.loads(row["data"])["cash_delta"]) if row else None


def _advance(store: Store, p: dict[str, Any], activity: dict[str, Any], body: dict[str, Any],
             discrepancies: list[dict[str, Any]], spendable: Decimal = Decimal(0)) -> tuple[bool, Decimal]:
    """A deposit Polymarket US advanced as buying power before the bank transfer settled.

    The advance is booked as cash while the deposit is pending, so the trader can spend it;
    on completion only the remainder is booked, and a failed transfer books the advance
    back out and is reported (Polymarket takes it back). Returns whether the activity was
    handled here, and the advance still pending (cash the balance holds in reserve).

    A deposit with no explicit advancedAmount can still be spendable: Polymarket counts
    it in buyingPower while it clears. `spendable` is the part of buying power beyond
    settled cash not yet claimed by another pending deposit; up to the deposit's amount
    of it is booked as a buying-power advance, with the same reversal if it fails."""
    status = body.get("status") or ""
    txid = body.get("transactionId")
    source = f"pmus-advance:{txid}"
    booked = _booked(store, source)
    advanced = sum((amount(t.get("advancedAmount")) or Decimal(0) for t in body.get("transactions") or []), Decimal(0))
    at = body.get("createTime") or body.get("updateTime")
    if at and at < p["created_at"]:
        at = p["created_at"]  # pending when the account was registered: not in its opening cash
    if status.endswith("PENDING"):
        purpose = "advanced_deposit"
        if advanced <= 0:
            advanced = min(amount(body.get("amount")) or Decimal(0), max(Decimal(0), spendable))
            purpose = "buying_power_advance"
        if booked is None and advanced > 0 and at:
            store.append("transaction", {"reason": "funding", "purpose": purpose, "venue": "polymarket_us",
                         "cash_delta": str(advanced), "external_delta": str(advanced), "fee": "0", "legs": [],
                         "evidence": activity}, p["id"], source=source, at=at)
            booked = advanced
        return True, booked or Decimal(0)
    if booked is None:
        return False, Decimal(0)
    if status.endswith(DEPOSIT_FAILED):
        store.append("transaction", {"reason": "funding", "purpose": "advanced_deposit_reversed",
                     "venue": "polymarket_us", "cash_delta": str(-booked), "external_delta": str(-booked), "fee": "0",
                     "legs": [], "evidence": activity}, p["id"], source=source + ":reversed",
                     at=body.get("updateTime") or at)
        discrepancies.append({"portfolio": p["id"], "kind": "deposit_failed", "transaction": txid,
                              "advanced": str(booked), "status": status})
        return True, Decimal(0)
    return False, Decimal(0)


def _book_activities(store: Store, client: PolymarketUS, p: dict[str, Any],
                     discrepancies: list[dict[str, Any]], balance: dict[str, Any] | None = None) -> Decimal:
    """Book funding and rebates; returns advanced deposits still pending.

    With `balance`, buying power on deposits still clearing is booked as an advance
    (see `_advance`): settled cash is currentBalance less depositReservation, and only
    buying power above it, up to the reservation, counts. Advances already booked are
    claimed first so a deposit is never counted twice."""
    pending_advances = Decimal(0)
    activities = list(client.activities())
    spendable = Decimal(0)
    if balance is not None:
        clearing = decimal(balance.get("depositReservation") or 0)
        settled = decimal(balance.get("currentBalance") or 0) - clearing
        spendable = min(clearing, max(Decimal(0), decimal(balance.get("buyingPower") or 0) - settled))
        for activity in activities:
            body = activity.get("accountBalanceChange") or {}
            if (body.get("status") or "").endswith("PENDING"):
                spendable -= _booked(store, f"pmus-advance:{body.get('transactionId')}") or Decimal(0)
    for activity in activities:
        kind = activity.get("type", "")
        if kind not in FUNDING_ACTIVITIES and kind != "ACTIVITY_TYPE_TAKER_FEE_REBATE":
            continue
        body = activity.get("accountBalanceChange") or {}
        at = body.get("updateTime") or body.get("createTime")
        value = amount(body.get("amount"))
        # A deposit still clearing at registration, or one whose advance was booked, is
        # this account's to book or reverse whatever its dates.
        pending_deposit = FUNDING_ACTIVITIES.get(kind) == 1 and (
            (body.get("status") or "").endswith("PENDING")
            or _booked(store, f"pmus-advance:{body.get('transactionId')}") is not None)
        if at and at < p["created_at"] and not pending_deposit:
            continue
        if FUNDING_ACTIVITIES.get(kind) == 1:
            before = _booked(store, f"pmus-advance:{body.get('transactionId')}")
            handled, pending = _advance(store, p, activity, body, discrepancies, spendable)
            if before is None and pending:
                spendable -= pending
            pending_advances += pending
            if handled:
                continue
        if not at or value is None or body.get("status") != "ACCOUNT_BALANCE_CHANGE_STATUS_COMPLETED":
            if kind == "ACTIVITY_TYPE_TAKER_FEE_REBATE" or (body.get("status") or "").endswith("COMPLETED"):
                discrepancies.append({"portfolio": p["id"], "kind": "unbooked_activity", "raw": activity})
            continue
        source = f"pmus-activity:{kind}:{body.get('transactionId')}"
        if _booked(store, source) is not None:
            continue
        if kind == "ACTIVITY_TYPE_TAKER_FEE_REBATE":
            if p["metadata"].get("reserve"):
                # Without pool ownership evidence, preserve the rebate without
                # making a trading return available to
                # fund another experiment. A sole legacy owner is unambiguous.
                discrepancies.append({"portfolio": p["id"], "kind": "unattributed_rebate",
                                      "transaction": body.get("transactionId")})
                store.append("quarantine", {"kind": "rebate", "raw": activity}, p["id"],
                             source=source + ":quarantine", at=at)
                continue
            store.append("transaction", {"reason": "rebate", "venue": "polymarket_us", "cash_delta": str(abs(value)),
                         "fee": "0", "legs": [], "evidence": activity}, p["id"], source=source, at=at)
        else:
            cash = abs(value) * FUNDING_ACTIVITIES[kind]
            if cash > 0:  # a deposit whose advance was booked while pending: only the rest is new
                cash -= _booked(store, f"pmus-advance:{body.get('transactionId')}") or Decimal(0)
            store.append("transaction", {"reason": "funding", "venue": "polymarket_us", "cash_delta": str(cash),
                         "external_delta": str(cash), "fee": "0", "legs": [], "evidence": activity},
                         p["id"], source=source, at=at)
    return pending_advances


def _settle(store: Store, client: PolymarketUS, pools: list[dict[str, Any]], observed_cash: Decimal,
            positions: dict[str, Any], evidence: dict[str, Any]) -> int:
    """Book resolved lots only when account cash and retired inventory support them.
    Every pool on the account is checked together: they share one balance."""
    settled = 0
    states = {p["id"]: deepcopy(portfolio_state(store, p["id"])) for p in pools}
    candidates = []
    held = {lot["market"] for s in states.values() for lot in s.lots.values() if is_polymarket_us(lot["market"])}
    for market in sorted(held):
        normalized = client.market(market)
        observed = utc()
        store.append("market", {"ticker": market, "market": normalized,
                     "book": client.book(market) if normalized["status"] == "active" else {}}, at=observed)
        if normalized["status"] != "finalized":
            continue
        if position_quantity(positions.get(slug_of(market), {})):
            continue
        yes = decimal(normalized["settlement_value_dollars"])
        for pid, state in states.items():
            for lot in state.lots.values():
                if lot["market"] == market:
                    payout = yes if lot["side"] == "yes" else 1 - yes
                    candidates.append((pid, lot, payout, normalized, observed))
    with store.transaction():
        # A public outcome alone does not establish an account payout. Unexplained
        # differences remain reconciliation gaps, including reads racing a new fill.
        for pid, state in states.items():
            current = portfolio_state(store, pid)
            if current.lots != state.lots or current.venue_cash != state.venue_cash:
                return 0
        expected = sum((s.venue_cash.get("polymarket_us", Decimal(0)) for s in states.values()), Decimal(0))
        expected += sum((lot["quantity"] * payout for _, lot, payout, _, _ in candidates), Decimal(0))
        if expected != observed_cash:
            return 0
        for pid, lot, payout, normalized, observed in candidates:
            market = lot["market"]
            store.append("transaction", {"reason": "settlement", "venue": "polymarket_us",
                "cash_delta": str(lot["quantity"] * payout), "fee": "0", "context": lot["context"],
                "evidence": {"market": normalized, "observed_at": observed, "account": evidence},
                "legs": [{"market": market, "side": lot["side"], "quantity_delta": str(-lot["quantity"]),
                          "price": str(payout), "lot_id": lot["record_id"]}]},
                pid, source=f"pmus-settlement:{pid}:{lot['record_id']}", at=observed)
            settled += 1
    return settled


def _settle_paper(store: Store, client: PolymarketUS) -> int:
    """Mark the Polymarket US markets paper portfolios hold, and settle their lots once
    a market's settlement is final. Public reads only."""
    settled = 0
    for p in store.pools("polymarket_us"):
        if p["mode"] != "paper":
            continue
        broker = Broker(store, p["id"], run_id="polymarket-us-monitor", venue="polymarket_us")
        markets = {lot["market"] for lot in broker.state().lots.values() if is_polymarket_us(lot["market"])}
        for market in sorted(markets):
            normalized = client.market(market)
            observed = utc()
            store.append("market", {"ticker": market, "market": normalized, "book": client.book(market)
                                    if normalized["status"] == "active" else {}}, at=observed)
            if normalized["status"] != "finalized":
                continue
            for lot_id, lot in list(broker.state().lots.items()):
                if lot["market"] == market:
                    broker.settle(lot_id, normalized["settlement_value_dollars"],
                                  {"market": normalized, "observed_at": observed})
                    settled += 1
    return settled


def polymarket_us_pass(store: Store, client: PolymarketUS) -> dict[str, Any]:
    """Settle paper portfolios' Polymarket US lots, and reconcile the live portfolio
    mapped to the Polymarket US account (which needs its credentials).

    1. Orders left pending/acknowledged/unknown with a venue ID are read back; fills the
       ledger lacks are booked (source `pmus-order:<id>:<cumulative>`) and terminal
       orders finished. An order with no venue ID (a lost acknowledgement) can't be
       looked up, since the venue has no client order ID: it's reported.
    2. Completed deposits and withdrawals after the mapping become funding, and taker
       fee rebates with an amount become rebates. One the monitor can't read is reported.
    3. Finalized lots settle when authenticated cash and retired positions reconcile
       with the payout. Public resolution alone does not create live cash.
    4. The account's cash and positions are compared with the ledger. Any difference is
       a discrepancy (never booked as profit), and the trader halts on one.
    """
    discrepancies: list[dict[str, Any]] = []
    mapped = [p for p in active_live(store) if p["venue"] == "polymarket_us"]
    if mapped and not client.authenticated:
        raise ValueError("a live portfolio is mapped to Polymarket US: the monitor needs its credentials")
    settled, recovered = _settle_paper(store, client), 0
    balances: list[dict[str, Any]] = []
    for p in mapped:
        broker = Broker(store, p["id"], run_id="polymarket-us-monitor", venue="polymarket_us")
        recovered += _recover_orders(store, client, p, broker, discrepancies)
    if mapped:
        # One account: venue-level activity goes to its reserve (or its only pool), and
        # cash and positions are reconciled against every pool on it together.
        reserve = next((p for p in mapped if p["metadata"].get("reserve")), None)
        owner = reserve or mapped[0]
        if reserve is None and len(mapped) > 1:
            discrepancies.extend({"portfolio": p["id"], "kind": "unregistered_shared_account"} for p in mapped)
        account = {**owner, "created_at": min(p["created_at"] for p in mapped)}
        balance = client.balance()
        advanced = _book_activities(store, client, account, discrepancies, balance)
        positions = client.positions()
        evidence = {"kind": "polymarket_us_snapshot", "balance": balance, "positions": positions,
                    "observed_at": utc()}
        store.append("account_evidence", evidence, owner["id"])
        # A deposit still clearing is in currentBalance (and its buying power) but held as
        # depositReservation. The part Polymarket advanced is booked as cash already; any
        # rest is booked once the deposit completes, so until then it's left out.
        observed_cash = (decimal(balance["currentBalance"]) - decimal(balance.get("depositReservation") or 0)
                         + advanced)
        settled += _settle(store, client, mapped, observed_cash, positions, evidence)
        states = {p["id"]: portfolio_state(store, p["id"]) for p in mapped}
        recorded = sum((s.venue_cash.get("polymarket_us", Decimal(0)) for s in states.values()), Decimal(0))
        ids = [p["id"] for p in mapped]
        balances.append({"portfolio": owner["id"], **({"pools": sorted(ids, key=lambda i: i != owner["id"])} if len(ids) > 1 else {}), "cash": str(observed_cash), "recorded": str(recorded),
                         "clearing_deposits": str(decimal(balance.get("depositReservation") or 0)),
                         "advanced": str(advanced),
                         "buying_power": str(balance.get("buyingPower", "0"))})
        if observed_cash != recorded:
            discrepancies.extend({"portfolio": pid, "kind": "cash_mismatch", "venue": "polymarket_us",
                                  "recorded": str(recorded), "observed": str(observed_cash)} for pid in ids)
        expected: dict[str, Decimal] = {}
        for lot in (lot for s in states.values() for lot in s.lots.values()):
            if is_polymarket_us(lot["market"]):
                slug = slug_of(lot["market"])
                expected[slug] = expected.get(slug, Decimal(0)) + lot["quantity"] * (1 if lot["side"] == "yes" else -1)
        observed = {slug: position_quantity(row) for slug, row in positions.items() if position_quantity(row)}
        expected = {k: v for k, v in expected.items() if v}
        if observed != expected:
            discrepancies.extend({"portfolio": pid, "kind": "inventory_mismatch", "venue": "polymarket_us",
                                  "recorded": {k: str(v) for k, v in expected.items()},
                                  "observed": {k: str(v) for k, v in observed.items()}} for pid in ids)
    status = {"component": "polymarket_us", "status": "healthy" if not discrepancies else "degraded",
              "portfolios": [p["id"] for p in mapped], "recovered_orders": recovered, "settled_lots": settled,
              "balances": balances, "discrepancies": discrepancies}
    store.append("health", status)
    return status


def run_monitor(root: str, component: str, interval: float = 60, once: bool = False) -> None:
    store = Store(root)
    api: Any = (Polymarket.from_env() if component == "polymarket" else
                PolymarketUS.from_env() if component == "polymarket-us" else
                Kalshi(authenticated=component == "account"))
    passes = {"market": market_pass, "account": account_pass,
              "polymarket": polymarket_pass, "polymarket-us": polymarket_us_pass}
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
