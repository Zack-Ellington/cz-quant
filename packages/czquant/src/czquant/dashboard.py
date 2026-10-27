"""Live comparison of every portfolio over a websocket.

Reads the shared store only: no credentials, no venue requests, no odds credits.
Every portfolio in the store appears on its own, so a new strategy needs nothing
here; its name comes from the portfolio's latest run record.

Valuation is the report's (report.value): FIFO cost basis with fees, marks no older
than 120 seconds, a midpoint only from a two-sided book, and the quote range
otherwise. Per portfolio and time:

  realized            FIFO realized P&L (fills closed, settlements, rewards)
  unrealized          midpoint value of open lots minus their cost basis, only when
                      every open lot has a midpoint (as in the report)
  unrealized_marked   the same over the lots that have a midpoint; unmarked_cost is
                      the cost basis of the rest (DraftKings bets have no bid, so
                      they are never midpoint-marked; stale books are unmarked)
  unrealized range    quote envelope of all open lots, None if any mark is stale
  net P&L             equity minus net external funding (point and range)

History is replayed once at start on a grid of `step` seconds; afterwards each tick
reads only events recorded since the last one.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .accounting import State, active_events, portfolio_state
from .report import timestamp, value
from .store import Store, encode

log = logging.getLogger(__name__)
MAX_POINTS = 3000
ACTIVITY = ("heartbeat", "scan_complete", "halt", "halt_cleared", "basket_settled", "error")


def measure(state: State, marks: dict[str, dict[str, Any]], at: datetime) -> dict[str, Any]:
    equity, lower, upper, missing = value(state, marks, at)
    base, cost = state.cash + state.prepaid, state.cost
    unmarked = set(missing)
    marked = State(lots={k: lot for k, lot in state.lots.items() if lot["market"] not in unmarked})
    marked_cost = marked.cost
    marked_value = value(marked, marks, at)[0] if marked.lots else Decimal(0)
    return {"t": at.isoformat(timespec="seconds"), "cash": state.cash, "contributed": state.contributed,
            "realized": state.realized, "fees": state.fees, "cost": cost, "open_lots": len(state.lots),
            "fills": state.fills, "settlements": state.settlements,
            "unrealized": equity - base - cost if equity is not None else None,
            "unrealized_marked": marked_value - marked_cost if marked_value is not None else None,
            "unmarked_cost": cost - marked_cost,
            "unrealized_lower": lower - base - cost if lower is not None else None,
            "unrealized_upper": upper - base - cost if upper is not None else None,
            "equity": equity,
            "net_pnl": equity - state.contributed if equity is not None else None,
            "net_pnl_lower": lower - state.contributed if lower is not None else None,
            "net_pnl_upper": upper - state.contributed if upper is not None else None,
            "missing": missing}


class Ledger:
    def __init__(self, store: Store, step: float = 300.0):
        self.store, self.step = store, step
        self.marks: dict[str, dict[str, Any]] = {}
        self.sequence = 0  # newest event read for marks, runs, health and activity
        self.series: dict[str, list[dict[str, Any]]] = {}
        self.strategy: dict[str, str] = {}
        self.health: dict[str, dict[str, Any]] = {}
        self.activity: dict[str, dict[str, Any]] = {}
        self.halts: dict[str, dict[str, str]] = {}  # portfolio -> {halt record id: reason}
        self.latest: dict[str, Any] = {}

    def _mark(self, event: dict[str, Any]) -> None:
        ticker = event["data"].get("ticker")
        prior = self.marks.get(ticker)
        if ticker and (prior is None or event["effective_at"] >= prior["effective_at"]):
            self.marks[ticker] = event

    def _note(self, event: dict[str, Any]) -> None:
        d, pid = event["data"], event["portfolio_id"]
        if event["type"] == "run" and pid:
            self.strategy[pid] = d.get("strategy", "?")
        elif event["type"] == "health":
            self.health[d.get("component", "?")] = {"at": event["effective_at"], **d}
        elif event["type"] == "observation" and pid:
            kind = d.get("kind")
            if kind == "halt":
                self.halts.setdefault(pid, {})[event["record_id"]] = d.get("reason", "")
            elif kind == "halt_cleared":
                self.halts.get(pid, {}).pop(d.get("halt_id"), None)
            if kind in ("heartbeat", "scan_complete"):
                self.activity.setdefault(pid, {})["last"] = event["effective_at"]
                if d.get("odds_credits_remaining") is not None:
                    self.activity[pid]["odds_credits_remaining"] = d["odds_credits_remaining"]
            if kind == "error":
                self.activity.setdefault(pid, {})["last_error"] = {"at": event["effective_at"], **d}

    def _read_new(self) -> None:
        events = self.store.events(kinds=("market", "run", "health", "observation"), observations=ACTIVITY,
                                   after_sequence=self.sequence)
        for event in sorted(events, key=lambda e: e["sequence"]):
            if event["type"] == "market":
                self._mark(event)
            else:
                self._note(event)
            self.sequence = max(self.sequence, event["sequence"])

    def backfill(self) -> None:
        """Replay history once onto the grid; no more than MAX_POINTS per portfolio."""
        portfolios = self.store.portfolios()
        now = datetime.now(timezone.utc)
        if not portfolios:
            self._read_new()
            return
        first = min(timestamp(p["created_at"]) for p in portfolios)
        step = timedelta(seconds=max(self.step, (now - first).total_seconds() / MAX_POINTS))
        events = active_events(self.store.events(kinds=("transaction", "correction", "market"), until=now.isoformat()))
        states = {p["id"]: State() for p in portfolios}
        created = {p["id"]: timestamp(p["created_at"]) for p in portfolios}
        cursor, at = 0, first
        while True:
            at = min(at, now)
            while cursor < len(events) and timestamp(events[cursor]["effective_at"]) <= at:
                event = events[cursor]
                if event["type"] == "market":
                    self._mark(event)
                elif event["portfolio_id"] in states:
                    states[event["portfolio_id"]].apply(event)
                cursor += 1
            for pid, state in states.items():
                if created[pid] <= at:
                    self.series.setdefault(pid, []).append(measure(state, self.marks, at))
            if at >= now:
                break
            at += step
        self.marks.clear()
        self._read_new()  # marks, runs, health and activity, up to now

    def tick(self) -> dict[str, Any]:
        """The current measure of every portfolio; a new grid point when one is due."""
        self._read_new()
        now = datetime.now(timezone.utc)
        rows, appended = [], {}
        for p in self.store.portfolios():
            pid = p["id"]
            point = measure(portfolio_state(self.store, pid), self.marks, now)
            history = self.series.setdefault(pid, [])
            if not history or (now - timestamp(history[-1]["t"])).total_seconds() >= self.step:
                history.append(point)
                appended[pid] = point
            rows.append({"id": pid, "strategy": self.strategy.get(pid, "unknown"), "mode": p["mode"],
                         "label": p["metadata"].get("label") or "", "created_at": p["created_at"], "now": point,
                         "midpoint_coverage": _share(history, "equity"),
                         "range_coverage": _share(history, "net_pnl_lower"),
                         "activity": self.activity.get(pid, {}),
                         "halted": sorted(self.halts.get(pid, {}).values())})
        self.latest = {"generated_at": now.isoformat(timespec="seconds"), "step": self.step, "portfolios": rows,
                       "health": self.health, "odds": self.odds()}
        return {"type": "tick", **self.latest, "append": appended}

    def snapshot(self) -> dict[str, Any]:
        if not self.latest:
            self.tick()
        return {"type": "snapshot", **self.latest, "series": self.series}

    def odds(self) -> dict[str, Any]:
        """Credits and trial end, from the files the DraftKings flow writes. No request."""
        out: dict[str, Any] = {}
        for name in ("credits", "trial"):
            path = Path(self.store.root) / "odds_cache" / f"{name}.json"
            try:
                out[name] = json.loads(path.read_text())
            except (OSError, ValueError):
                pass
        return out


def _share(rows: list[dict[str, Any]], key: str) -> float | None:
    return sum(r[key] is not None for r in rows) / len(rows) if rows else None


async def serve(root: str | None, host: str = "127.0.0.1", port: int = 8790, interval: float = 5.0,
                step: float = 300.0) -> None:
    from websockets.asyncio.server import broadcast, serve as ws_serve
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    store = Store(root)
    ledger = Ledger(store, step)
    ledger.backfill()
    ledger.tick()
    clients: set[Any] = set()

    def process_request(connection: Any, request: Any) -> Any:
        if request.path in ("/", "/index.html"):
            return Response(200, "OK", Headers([("Content-Type", "text/html; charset=utf-8"),
                                                ("Cache-Control", "no-store")]), PAGE.encode())
        if request.path != "/ws":
            return connection.respond(404, "not found\n")
        return None

    async def handler(ws: Any) -> None:
        clients.add(ws)
        try:
            await ws.send(encode(ledger.snapshot()))
            await ws.wait_closed()
        finally:
            clients.discard(ws)

    async with ws_serve(handler, host, port, process_request=process_request, max_size=2**20):
        log.info("dashboard at http://%s:%s/ (store %s)", host, port, store.path)
        print(f"Dashboard: http://{host}:{port}/", flush=True)
        while True:
            await asyncio.sleep(interval)
            try:
                message = encode(ledger.tick())
            except Exception:
                log.exception("dashboard tick failed")
                continue
            broadcast(clients, message)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Strategy Ledger</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/plotly.js/2.35.2/plotly.min.js"></script>
<style>
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d1f21;--muted:#6b7075;--line:#e3e3df;--good:#1f7a4d;--bad:#b3261e;--warn:#9a6700}
@media (prefers-color-scheme:dark){:root{--bg:#141517;--panel:#1c1d20;--ink:#e8e8e6;--muted:#9a9ea3;--line:#2c2e32;--good:#5cc28d;--bad:#ff8a80;--warn:#e0b341}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:baseline;justify-content:space-between;padding:16px 20px 4px}
h1{font-size:18px;margin:0}.muted{color:var(--muted)}main{padding:8px 20px 28px;display:grid;gap:16px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;min-width:0}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.card b{display:block;font-size:12px;color:var(--muted);font-weight:500}.card span{font-size:15px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}th{font-size:12px;color:var(--muted);font-weight:500}
.scroll{overflow-x:auto}.pos{color:var(--good)}.neg{color:var(--bad)}.warn{color:var(--warn)}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:16px}.chart{height:340px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:var(--muted)}
.dot.healthy{background:var(--good)}.dot.degraded{background:var(--warn)}.dot.failed,.dot.stale{background:var(--bad)}
@media (max-width:520px){header,main{padding-left:16px;padding-right:16px}.charts{grid-template-columns:1fr}}
</style></head><body>
<header><h1>Strategy ledger</h1><span class="muted" id="status">connecting…</span></header>
<main>
<section class="panel"><div class="cards" id="health"></div></section>
<section class="panel scroll"><table id="table"><thead><tr>
<th>Strategy</th><th>Portfolio</th><th>Realized</th><th>Unrealized</th><th>Unrealized (marked)</th><th>Unmarked cost</th>
<th>Net P&amp;L</th><th>Net P&amp;L range</th><th>Fees</th><th>Fills</th><th>Settled</th><th>Open</th><th>Midpoint cov.</th><th>Last activity</th></tr></thead><tbody></tbody></table>
<p class="muted">Valuation follows the shared report: 120-second marks, a midpoint only from a two-sided book, quote ranges otherwise.
Unrealized is shown only when every open lot has a midpoint; "marked" covers the lots that do, and unmarked cost is the rest
(DraftKings bets have no bid, so they stay unmarked until settled). Ranges are quote envelopes, not liquidation values.</p></section>
<section class="charts">
<div class="panel"><div class="chart" id="realized"></div></div>
<div class="panel"><div class="chart" id="unrealized"></div></div>
<div class="panel"><div class="chart" id="net"></div></div>
</section>
</main>
<script>
const $=id=>document.getElementById(id);let series={},state=null;
const num=v=>v===null||v===undefined?null:Number(v);
const money=v=>{v=num(v);if(v===null)return'<span class="muted">—</span>';const c=v>0.0049?'pos':v<-0.0049?'neg':'';return`<span class="${c}">${v<0?'−':''}$${Math.abs(v).toFixed(2)}</span>`};
const pct=v=>v===null||v===undefined?'—':(100*v).toFixed(0)+'%';
const ago=t=>{if(!t)return'never';const s=(Date.now()-new Date(t))/1000;return s<90?`${s|0}s ago`:s<5400?`${(s/60)|0}m ago`:`${(s/3600).toFixed(1)}h ago`};
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
function layout(title){return{title:{text:title,font:{size:14}},margin:{l:56,r:12,t:36,b:36},paper_bgcolor:'rgba(0,0,0,0)',plot_bgcolor:'rgba(0,0,0,0)',
 font:{color:css('--ink')},xaxis:{gridcolor:css('--line')},yaxis:{gridcolor:css('--line'),tickprefix:'$',zerolinecolor:css('--muted')},legend:{orientation:'h',y:-0.18},hovermode:'x unified'}}
const name=p=>`${p.strategy} · ${p.id}`;
function draw(){if(!state)return;const ps=state.portfolios;
 const line=(key)=>ps.map(p=>({name:name(p),x:(series[p.id]||[]).map(r=>r.t),y:(series[p.id]||[]).map(r=>num(r[key])),mode:'lines',connectgaps:false}));
 Plotly.react('realized',line('realized'),layout('Realized P&L'),{displayModeBar:false,responsive:true});
 Plotly.react('unrealized',line('unrealized_marked'),layout('Unrealized P&L, marked lots'),{displayModeBar:false,responsive:true});
 const net=[];ps.forEach(p=>{const rows=series[p.id]||[];const x=rows.map(r=>r.t);
  net.push({name:name(p)+' range',x,y:rows.map(r=>num(r.net_pnl_upper)),mode:'lines',line:{width:0},showlegend:false,hoverinfo:'skip',legendgroup:p.id});
  net.push({name:name(p)+' range',x,y:rows.map(r=>num(r.net_pnl_lower)),mode:'lines',line:{width:0},fill:'tonexty',opacity:.2,showlegend:false,hoverinfo:'skip',legendgroup:p.id});
  net.push({name:name(p),x,y:rows.map(r=>num(r.net_pnl)),mode:'lines',connectgaps:false,legendgroup:p.id})});
 Plotly.react('net',net,layout('Net P&L: midpoint and quote range'),{displayModeBar:false,responsive:true})}
function render(){const ps=state.portfolios;
 $('table').tBodies[0].innerHTML=ps.map(p=>{const n=p.now;const range=num(n.net_pnl_lower)===null?'<span class="muted">stale or unmarked</span>':`${money(n.net_pnl_lower)} to ${money(n.net_pnl_upper)}`;
  const halt=p.halted.length?` <span class="neg" title="${p.halted.join('; ')}">halted</span>`:'';
  return`<tr><td>${p.strategy}${halt}</td><td>${p.id}</td><td>${money(n.realized)}</td><td>${money(n.unrealized)}</td><td>${money(n.unrealized_marked)}</td>
  <td>${num(n.unmarked_cost)?money(n.unmarked_cost):'<span class="muted">$0.00</span>'}</td><td>${money(n.net_pnl)}</td><td>${range}</td><td>${money(n.fees)}</td>
  <td>${n.fills}</td><td>${n.settlements}</td><td>${n.open_lots}</td><td>${pct(p.midpoint_coverage)}</td><td>${ago(p.activity.last)}</td></tr>`}).join('')||'<tr><td colspan="14" class="muted">No portfolios yet. Create one with czquant portfolio.</td></tr>';
 const cards=['market','account','draftkings'].map(c=>{const h=state.health[c];if(!h)return`<div class="card"><b>${c} monitor</b><span class="muted">not running</span></div>`;
  const age=(Date.now()-new Date(h.at))/1000;const st=age>300?'stale':h.status;
  return`<div class="card"><b>${c} monitor</b><span><i class="dot ${st}"></i>${st}, ${ago(h.at)}</span></div>`});
 const o=state.odds||{};const cr=o.credits;const tr=o.trial;
 cards.push(`<div class="card"><b>Odds API credits</b><span>${cr?cr.remaining+' left, '+ago(cr.observed_at):'<span class="muted">no reads yet</span>'}</span></div>`);
 if(tr)cards.push(`<div class="card"><b>Odds trial ends</b><span>${new Date(tr.end*1000).toLocaleString()}</span></div>`);
 $('health').innerHTML=cards.join('');$('status').textContent=`updated ${new Date(state.generated_at).toLocaleTimeString()} · grid ${state.step}s`}
function connect(){const ws=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws`);
 ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.type==='snapshot'){series=m.series}else{for(const[k,v]of Object.entries(m.append||{}))(series[k]=series[k]||[]).push(v)}
  state=m;render();draw()};
 ws.onclose=()=>{$('status').textContent='disconnected, retrying…';setTimeout(connect,3000)}}
connect();
</script></body></html>
"""
