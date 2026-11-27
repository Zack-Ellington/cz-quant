"""Read-only cross-market operations dashboard; no credentials or venue requests."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from czquant.accounting import State, active_events, combine_states
from cross_market_monitor import measure_quotes, selections
from czquant.store import decimal, encode

ZERO = Decimal(0)


def seconds(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def reprice_history(events: list[dict]) -> None:
    """Reconstruct older gap samples from their recorded books, fees and pool cash."""
    configs, books = {}, {}
    for event in events:
        d = event['data']
        if event['type'] == 'run' and d.get('strategy') == 'cross-market-polymarket':
            cfg = d['config']
            configs[cfg['experiment']['id']] = cfg
        if event['type'] == 'market' and d.get('source') == 'cross_market_quotes':
            books[d['ticker']] = d['book']
        if event['type'] != 'observation' or d.get('kind') != 'cross_market_quote':
            continue
        cfg = configs.get(d['experiment_id'])
        if cfg and d['status'] == 'ok' and any('max_gap' not in r for r in d['routes']):
            pair = cfg['experiment']['pair']
            ktickers = [o['kalshi'] for o in pair['outcomes']]
            pticker = 'PMUS:' + pair['polymarket_market']
            if all(t in books for t in [*ktickers, pticker]):
                event['data'] = {**d, **measure_quotes(cfg, {t: books[t] for t in ktickers}, books[pticker],
                    decimal(d['fee_multiplier']), decimal(d['fee_coefficient']),
                    {v: decimal(c) for v, c in d['available'].items()}, decimal(d['capital_limit']))}
        # Only the books collected for this sample may reconstruct its gap.
        books.clear()


def windows(samples: list[dict], now: float) -> list[dict]:
    result, opened = [], {}
    for sample in samples:
        at, data = sample["effective_at"], sample["data"]
        present = {r["id"]: r for r in data.get("routes", [])}
        for key in list(opened):
            prior = opened[key]
            gap = seconds(at) - seconds(prior["last_seen"]) > max(15, data.get("interval", 5) * 3)
            route = present.get(key)
            if gap or data["status"] != "ok" or not route or not route["opportunity"]:
                prior["status"] = "sampling gap" if gap or data["status"] == "error" else "closed"
                result.append(opened.pop(key))
        if data["status"] != "ok":
            continue
        for route in present.values():
            if not route["opportunity"]:
                continue
            best = route["best"]
            entry = opened.setdefault(route["id"], {"route": route["name"], "start": at,
                                      "last_seen": at, "peak_net": ZERO, "samples": 0, "status": "open",
                                      "stale_after": max(15, data.get("interval", 5) * 3)})
            entry["last_seen"] = at
            entry["peak_net"] = max(decimal(entry["peak_net"]), decimal(best["net"]))
            entry["samples"] += 1
    for entry in opened.values():
        if now - seconds(entry["last_seen"]) > entry["stale_after"]:
            entry["status"] = "sampling gap"
        result.append(entry)
    for entry in result:
        entry["observed_seconds"] = seconds(entry["last_seen"]) - seconds(entry["start"])
    return sorted(result, key=lambda w: w["start"], reverse=True)


def conditional_profit(state: State, pair: dict) -> dict:
    quantities = [ZERO, ZERO]
    unmatched = False
    for lot in state.lots.values():
        found = False
        for index, outcome in enumerate(pair["outcomes"]):
            contracts = {(outcome["kalshi"], outcome["kalshi_side"].lower()),
                         (outcome["polymarket"], outcome["polymarket_side"].lower())}
            if (lot["market"], lot["side"]) in contracts:
                quantities[index] += lot["quantity"]
                found = True
        unmatched |= not found
    complete = not unmatched and quantities[0] == quantities[1]
    profit = min(quantities) - state.cost if complete else None
    return {"matched_sets": min(quantities), "unmatched_contracts": abs(quantities[0] - quantities[1]),
            "open_contracts": sum((lot['quantity'] for lot in state.lots.values()), ZERO),
            "open_expected_profit": profit,
            "expected_profit": state.realized + profit if profit is not None else None}


def trade_rows(orders: list[dict], events: list[dict], pools: set[str]) -> list[dict]:
    starts, trades = {}, {}
    for event in events:
        d = event["data"]
        if event["type"] == "order" and d.get("status") == "intent":
            starts[d["order"]["client_order_id"]] = event["effective_at"]
    for order in orders:
        if order["portfolio_id"] not in pools:
            continue
        context, request = order["context"], order["request"]
        key = context.get("trade_id") or order["id"]
        trade = trades.setdefault(key, {"id": key, "at": starts.get(order["id"]), "orders": [],
                                        "quote_expected_profit": context.get("expected_net"),
                                        "filled": False, "fees": ZERO, "fill_count": 0, "executions": [], "reason": None})
        at = starts.get(order["id"])
        if at and (not trade["at"] or at < trade["at"]):
            trade["at"] = at
        trade["orders"].append({"id": order["id"], "market": request["ticker"], "side": request["side"],
                                "requested": request["count"], "filled": order["filled"],
                                "limit": request["price"], "status": order["status"],
                                "repair": bool(context.get("repair"))})
    for event in active_events(events):
        d = event["data"]
        if (event['portfolio_id'] in pools and event['type'] == 'observation' and d.get('kind') == 'reject'
                and d.get('reason') in ('basket_preflight', 'recheck_failed')):
            key = event['record_id']
            trades[key] = {'id': key, 'at': event['effective_at'], 'orders': [], 'executions': [],
                           'quote_expected_profit': None, 'filled': False, 'fees': ZERO, 'fill_count': 0,
                           'reason': d['reason'] + ': ' + str(d.get('detail') or d.get('why') or '')}
        if event["portfolio_id"] not in pools or event["type"] != "transaction" or d.get("reason") != "fill":
            continue
        key = d.get("context", {}).get("trade_id") or d.get("order_id")
        if key not in trades:
            continue
        trade = trades[key]
        trade["filled"] = True
        trade["fill_count"] += 1
        trade["fees"] += decimal(d.get("fee", 0))
        for leg in d.get("legs", []):
            trade["executions"].append({"at": event["effective_at"], "market": leg["market"],
                                        "side": leg["side"], "quantity": leg["quantity_delta"],
                                        "price": leg["price"], "fee": d.get("fee", 0)})
    return sorted(trades.values(), key=lambda r: r["at"] or "", reverse=True)


def snapshot(root: Path) -> dict:
    now = datetime.now(timezone.utc)
    db = sqlite3.connect(f"file:{root / 'accounting.sqlite3'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")
        rows = db.execute("""SELECT * FROM events WHERE type IN ('run','transaction','correction','order','health','account_evidence')
            OR (type='market' AND json_extract(data,'$.source')='cross_market_quotes')
            OR (type='observation' AND json_extract(data,'$.kind') IN
            ('cross_market_quote','heartbeat','scan_complete','halt','halt_cleared','reject','error',
             'basket_execution','order_rejected')) ORDER BY sequence""").fetchall()
        events = [{**dict(r), "data": json.loads(r["data"])} for r in rows]
        pools = [{**dict(r), "metadata": json.loads(r["metadata"])} for r in db.execute(
            "SELECT p.*,c.venue FROM portfolios p JOIN cash_pools c ON c.id=p.id")]
        orders = [{**dict(r), "request": json.loads(r["request"]), "context": json.loads(r["context"] or "{}")}
                  for r in db.execute("SELECT * FROM orders")]
    finally:
        db.close()
    configs = selections([e for e in events if e["type"] == "run"], pools)
    reprice_history(events)
    states = {p["id"]: State() for p in pools}
    cash_history = {c["experiment"]["id"]: [] for c in configs}
    memberships = {c["experiment"]["id"]: set(c["experiment"]["portfolio"].values()) for c in configs}
    selected_pools = set().union(*memberships.values()) if memberships else set()
    overall_history = []

    def measure_overall(at: str) -> dict:
        combined = combine_states([states[p] for p in selected_pools if p in states])
        projections = [conditional_profit(combine_states([states[p] for p in memberships[c['experiment']['id']]
                                                          if p in states]), c['experiment']['pair']) for c in configs]
        expected = sum((p['expected_profit'] for p in projections), ZERO) if all(
            p['expected_profit'] is not None for p in projections) else None
        return {'at': at, 'cash': combined.cash, 'positions': sum((p['open_contracts'] for p in projections), ZERO),
                'expected_profit': expected, 'expected_return': expected / combined.contributed if
                expected is not None and combined.contributed > 0 else None}
    economic = active_events([e for e in events if e["type"] in ("transaction", "correction")])
    for event in sorted(economic, key=lambda e: (e["effective_at"], e["sequence"])):
        pid = event["portfolio_id"]
        if pid in states:
            states[pid].apply(event)
            for identifier, members in memberships.items():
                if pid in members:
                    cash_history[identifier].append({"at": event["effective_at"],
                        "cash": sum((states[p].cash for p in members if p in states), ZERO)})
            if pid in selected_pools:
                overall_history.append(measure_overall(event['effective_at']))
    health, venue_balances = {}, {}
    venue_history = []
    for event in events:
        d = event["data"]
        if event["type"] == "account_evidence" and d.get("kind") == "polymarket_us_snapshot":
            b = d['balance']
            venue_balances['polymarket_us'] = {'at': event['effective_at'],
                'venue_balance': decimal(b['currentBalance']), 'buying_power': b.get('buyingPower'),
                'pending_deposit': b.get('depositReservation', 0), 'bonus': b.get('displayedBonus', 0),
                'withdrawable': b.get('availableToWithdraw')}
        if event["type"] == "health":
            health[d["component"]] = {"at": event["effective_at"], "status": d["status"],
                                      "discrepancies": len(d.get("discrepancies", [])),
                                      "errors": len(d.get("errors", [])),
                                      "age": now.timestamp() - seconds(event["effective_at"])}
            if d['component'] == 'account' and d.get('balances'):
                venue_balances['kalshi'] = {'at': event['effective_at'],
                    'venue_balance': sum((decimal(b['cash']) for b in d['balances']), ZERO)}
        if (len(venue_balances) == 2 and (event['type'] == 'account_evidence'
                and d.get('kind') == 'polymarket_us_snapshot' or event['type'] == 'health' and d.get('component') == 'account')):
            venue_history.append({'at': event['effective_at'], 'cash': sum(
                (b['venue_balance'] for b in venue_balances.values()), ZERO)})
    games = []
    for config in configs:
        experiment = config["experiment"]
        identifier, pair = experiment["id"], experiment["pair"]
        members = memberships[identifier]
        state = combine_states([states[p] for p in members if p in states])
        obs = [e for e in events if e["type"] == "observation" and e["portfolio_id"] in members]
        quotes = [e for e in events if e["type"] == "observation" and e["data"].get("kind") == "cross_market_quote"
                  and e["data"].get("experiment_id") == identifier]
        last = {}
        for event in obs:
            last[event["data"]["kind"]] = {"at": event["effective_at"], **event["data"]}
        cash = []
        for venue, pid in experiment["portfolio"].items():
            s = states.get(pid, State())
            reserved = sum((decimal(o["reserved"]) for o in orders if o["portfolio_id"] == pid), ZERO)
            cash.append({"venue": venue, "pool": pid, "cash": s.cash, "reserved": reserved,
                         "available": s.cash - reserved, "fees": s.fees})
        trades = trade_rows(orders, events, members)
        names = [name for name, _ in pair.get("evidence", {}).get("kalshi", [])]
        latest = quotes[-1] if quotes else None
        quote = {**latest["data"], "at": latest["effective_at"],
                 "stale_after": max(15, latest['data'].get('interval', 5) * 3),
                 "age": now.timestamp() - seconds(latest["effective_at"])} if latest else None
        issues = [{"at": e["effective_at"], **e["data"]} for e in obs
                  if e["data"]["kind"] in ("reject", "error", "halt", "halt_cleared", "order_rejected")]
        games.append({"id": identifier, "name": " vs ".join(names) or pair["kalshi_event"],
                      "event": pair["kalshi_event"], "mode": experiment["mode"], "cash": cash,
                      "start": pair.get("evidence", {}).get("start"), "rules": pair.get("evidence", {}).get("rules", ""),
                      "total_cash": state.cash, "realized_profit": state.realized, "fees": state.fees,
                      **conditional_profit(state, pair), "attempts": len(trades),
                      "executed_trades": sum(t["filled"] for t in trades), "fills": state.fills,
                      "trades": trades, "issues": issues[-30:][::-1], "heartbeat": last.get("heartbeat"),
                      "positions": list(state.lots.values()),
                      "scan": last.get("scan_complete"), "quote": quote, "windows": windows(quotes, now.timestamp())[:100],
                      "route_labels": next(([{'id': r['id'], 'name': r['name']} for r in e['data']['routes']]
                                             for e in reversed(quotes) if e['data'].get('routes')), []),
                      "team_names": next(([l['name'] for l in e['data']['legs']]
                                            for e in reversed(quotes) if e['data'].get('legs')), []),
                      "price_history": [{"at": e["effective_at"], "status": e["data"]["status"],
                        "routes": [{"id": r['id'], "kalshi_price": e['data']['legs'][int(r['id'])]['kalshi_ask'],
                                    "polymarket_price": e['data']['legs'][1-int(r['id'])]['polymarket_ask'],
                                    "net_per_set": r['one_set']['net_per_set'] if r.get('one_set') else None,
                                    "fee_per_set": r['one_set']['fee_per_set'] if r.get('one_set') else None,
                                    "max_gap": r.get('max_gap'), "best": r.get('best')}
                                   for r in e['data']['routes']]} for e in quotes],
                      "cash_history": cash_history[identifier][-3000:] + [{"at": now.isoformat(), "cash": state.cash}],
                      "quote_history_start": quotes[0]["effective_at"] if quotes else None})
    accounts = []
    for venue in ("kalshi", "polymarket_us"):
        live = [p for p in pools if p["mode"] == "live" and p["venue"] == venue]
        accounts.append({"venue": venue, **venue_balances.get(venue, {}),
                         "cash": sum((states[p["id"]].cash for p in live), ZERO),
                         "allocated_cash": sum((states[p["id"]].cash for p in live if p["id"] in selected_pools), ZERO),
                         "reserve_cash": sum((states[p["id"]].cash for p in live if p["metadata"].get("reserve")), ZERO)})
    unique = combine_states([states[p] for p in selected_pools if p in states])
    all_trades = trade_rows(orders, events, selected_pools)
    expected = sum((g["expected_profit"] for g in games), ZERO) if all(g["expected_profit"] is not None for g in games) else None
    venue_total = sum((a['venue_balance'] for a in accounts), ZERO) if all('venue_balance' in a for a in accounts) else None
    overall_history.append(measure_overall(now.isoformat()))
    return {"at": now.isoformat(), "health": health, "games": games, "accounts": accounts,
            "overall_history": overall_history, "venue_history": venue_history,
            "overall": {"cash": unique.cash, "account_cash": sum((a["cash"] for a in accounts), ZERO),
                        "venue_balance": venue_total,
                        "positions": overall_history[-1]['positions'],
                        "expected_return": overall_history[-1]['expected_return'],
                        "attempts": len(all_trades), "executed_trades": sum(t["filled"] for t in all_trades),
                        "expected_profit": expected, "realized_profit": unique.realized, "fees": unique.fees}}


def serve(root: str, host: str = "127.0.0.1", port: int = 8791) -> None:
    from plotly.offline import get_plotlyjs
    root_path = Path(root).resolve()
    if not (root_path / "accounting.sqlite3").is_file():
        raise ValueError("the selected ledger does not exist")
    plotly = get_plotlyjs().encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            try:
                if self.path == "/api/snapshot":
                    body, mime = encode(snapshot(root_path)).encode(), "application/json"
                elif self.path == "/plotly.min.js":
                    body, mime = plotly, "application/javascript"
                elif self.path == "/":
                    body, mime = Path(__file__).with_name("cross_market.html").read_bytes(), "text/html; charset=utf-8"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                logging.exception("dashboard request failed")
                self.send_error(500, "Dashboard read failed; inspect the server log")

        def log_message(self, format: str, *args: Any) -> None:
            pass

    print(f"Cross-market dashboard: http://{host}:{port}/", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
