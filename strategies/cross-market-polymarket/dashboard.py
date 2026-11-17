"""This strategy's own dashboard: one self-contained HTML page (dashboard.html).

  P&L       net P&L over time (cash plus open positions at their marks, less funding),
            replayed from the shared store with the same valuation as `czquant dashboard`
  Baskets   every basket the trader sent: pair, how it was matched (reviewed or auto,
            with its score), sets, expected net, fills, and P&L so far
  Approved  markets.json and markets.auto.json, and whether each was traded
  Candidates  candidates.json from find_pairs.py, marked approved / traded

Reads only: the store, candidates.json, markets.json, markets.auto.json.

Usage
  uv run python dashboard.py                  # write dashboard.html once
  uv run python dashboard.py --watch 60       # rewrite every 60 s; the open page reloads itself
"""

from __future__ import annotations

import argparse
import html
import json
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from czquant import Store
from czquant.accounting import State, active_events
from czquant.dashboard import measure
from czquant.report import timestamp, value

from markets import AUTO, MARKETS

HERE = os.path.dirname(os.path.abspath(__file__))
CANDIDATES = os.path.join(HERE, "candidates.json")
OUT = os.path.join(HERE, "dashboard.html")
MAX_POINTS = 600


def read_json(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def money(value: Any) -> str:
    if value is None:
        return "—"
    v = float(value)
    return f"{'-' if v < 0 else ''}${abs(v):,.2f}"


_MARKS: dict[str, Any] = {"after": 0, "events": [], "tickers": set()}  # kept between --watch passes


def marks_for(store: Store, tickers: set[str], since: int) -> list[dict[str, Any]]:
    """Market events for these tickers recorded after sequence `since`.

    The store holds every strategy's marks (over a million rows), so this reads only
    sequences after this portfolio's first trade, and in --watch only the new ones."""
    if _MARKS["after"] < since or not tickers <= _MARKS["tickers"]:  # a new market: read again from `since`
        _MARKS.update(after=since, events=[], tickers=set(tickers))
    after = _MARKS["after"]
    rows = store.db.execute(
        "SELECT sequence, record_id, portfolio_id, type, effective_at, recorded_at, data FROM events "
        "WHERE type='market' AND sequence > ? ORDER BY sequence", (after,)).fetchall()
    for r in rows:
        _MARKS["after"] = r[0]
        data = json.loads(r[6])
        if data.get("ticker") in tickers:
            _MARKS["events"].append({"sequence": r[0], "record_id": r[1], "portfolio_id": r[2], "type": r[3],
                                     "effective_at": r[4], "recorded_at": r[5], "data": data})
    return [e for e in _MARKS["events"] if e["data"].get("ticker") in tickers]


def pnl_series(store: Store, portfolio: str) -> tuple[list[dict[str, Any]], State, dict[str, Any]]:
    """Net P&L on an even grid from the portfolio's creation to now."""
    created = next((p["created_at"] for p in store.portfolios() if p["id"] == portfolio), None)
    now = datetime.now(timezone.utc)
    economic = active_events(store.events(portfolio, kinds=("transaction", "correction"), until=now.isoformat()))
    tickers = {leg["market"] for e in economic if e["type"] == "transaction" for leg in e["data"].get("legs", [])}
    traded = [e["sequence"] for e in economic if e["type"] == "transaction" and e["data"].get("legs")]
    marks = marks_for(store, tickers, min(traded) - 1) if traded else []
    events = sorted(economic + marks, key=lambda e: (e["effective_at"], e["sequence"]))
    start = timestamp(created) if created else now
    step = timedelta(seconds=max(300.0, (now - start).total_seconds() / MAX_POINTS))
    state, marks, points, cursor, at = State(), {}, [], 0, start
    while True:
        at = min(at, now)
        while cursor < len(events) and timestamp(events[cursor]["effective_at"]) <= at:
            e = events[cursor]
            if e["type"] == "market":
                t = e["data"].get("ticker")
                if t and (t not in marks or e["effective_at"] >= marks[t]["effective_at"]):
                    marks[t] = e
            else:
                state.apply(e)
            cursor += 1
        m = measure(state, marks, at)
        points.append({"t": m["t"], "net": m["net_pnl"], "lo": m["net_pnl_lower"], "hi": m["net_pnl_upper"],
                       "realized": m["realized"], "missing": m["missing"],
                       # the shared ledger's unrealized columns, so both pages show the same figures
                       "unrealized": m["unrealized"], "unrealized_marked": m["unrealized_marked"],
                       "unmarked_cost": m["unmarked_cost"]})
        if at >= now:
            break
        at += step
    return points, state, marks


def baskets(store: Store, portfolio: str, state: State, marks: dict[str, Any]) -> list[dict[str, Any]]:
    events = store.events(portfolio, kinds=("transaction", "observation"),
                          observations=("basket_execution", "basket_settled"))
    cash: dict[str, Decimal] = {}
    settled: dict[str, dict[str, Any]] = {}
    for e in active_events(events):
        d = e["data"]
        tid = (d.get("context") or {}).get("trade_id")
        if e["type"] == "transaction" and tid:
            cash[tid] = cash.get(tid, Decimal(0)) + Decimal(str(d["cash_delta"]))
        elif d.get("kind") == "basket_settled":
            settled[d["trade_id"]] = d
    # Open lots valued as the shared report does (mid of each book), per trade.
    by_trade: dict[str, dict[str, Any]] = {}
    for key, lot in state.lots.items():
        tid = (lot.get("context") or {}).get("trade_id")
        if tid:
            by_trade.setdefault(tid, {})[key] = lot
    now = datetime.now(timezone.utc)
    open_value: dict[str, Decimal] = {}
    for tid, lots in by_trade.items():
        equity = value(State(lots=lots), marks, now)[0]
        if equity is not None:
            open_value[tid] = equity
    out = []
    for e in events:
        d = e["data"]
        if d.get("kind") != "basket_execution":
            continue
        c = d["context"]
        tid = c["trade_id"]
        filled = {o["ticker"]: o.get("filled") for o in d["outcomes"] if "filled" in o}
        holding = any((lot.get("context") or {}).get("trade_id") == tid for lot in state.lots.values())
        status = (settled[tid]["fill_status"] + ", settled") if tid in settled else ("open" if holding else "closed")
        unpriced = tid in by_trade and tid not in open_value
        pnl = None if tid not in cash or unpriced else cash[tid] + open_value.get(tid, Decimal(0))
        out.append({"at": e["effective_at"], "trade_id": tid, "kalshi": c.get("kalshi_market") or c.get("kalshi_event"),
                    "polymarket": c.get("polymarket_event"), "league": c.get("league"),
                    "method": (c.get("match") or {}).get("method", "game"), "score": (c.get("match") or {}).get("score"),
                    "sets": c.get("sets"), "expected": c.get("expected_net"), "edge": c.get("edge"),
                    # A Polymarket leg buys an outcome token outright; show which answer it is.
                    "legs": [{"venue": leg["venue"], "vwap": leg["vwap"],
                              "contract": leg["contract"] if leg["venue"] == "kalshi" or leg.get("role") not in ("yes", "no")
                              else leg["role"].capitalize(),
                              "qty": leg["qty"], "filled": filled.get(leg["ticker"])} for leg in c["intended_legs"]],
                    "unwinds": [o for o in d["outcomes"] if "unwind" in o],
                    "status": status, "pnl": pnl})
    return sorted(out, key=lambda b: b["at"], reverse=True)


def chart(points: list[dict[str, Any]]) -> str:
    vals = [(timestamp(p["t"]), float(p["net"])) for p in points if p["net"] is not None]
    if len(vals) < 2:
        return '<p class="muted">Not enough marked history for a chart yet.</p>'
    band = [(timestamp(p["t"]), float(p["lo"]), float(p["hi"])) for p in points
            if p["lo"] is not None and p["hi"] is not None]
    w, h, l, r, t, b = 960, 280, 64, 16, 16, 28
    t0, t1 = vals[0][0], vals[-1][0]
    ys = [v for _, v in vals] + [x for _, lo, hi in band for x in (lo, hi)] + [0.0]
    y0, y1 = min(ys), max(ys)
    pad = (y1 - y0) * 0.08 or 1
    y0, y1 = y0 - pad, y1 + pad
    span = (t1 - t0).total_seconds() or 1

    def X(at: datetime) -> float:
        return l + (w - l - r) * (at - t0).total_seconds() / span

    def Y(v: float) -> float:
        return t + (h - t - b) * (1 - (v - y0) / (y1 - y0))

    line = " ".join(f"{X(a):.1f},{Y(v):.1f}" for a, v in vals)
    area = ""
    if len(band) > 1:
        top = " ".join(f"{X(a):.1f},{Y(hi):.1f}" for a, _, hi in band)
        bottom = " ".join(f"{X(a):.1f},{Y(lo):.1f}" for a, lo, _ in reversed(band))
        area = f'<polygon class="band" points="{top} {bottom}"/>'
    ticks = []
    for i in range(5):
        v = y0 + (y1 - y0) * i / 4
        ticks.append(f'<line class="grid" x1="{l}" x2="{w - r}" y1="{Y(v):.1f}" y2="{Y(v):.1f}"/>'
                     f'<text class="axis" x="{l - 8}" y="{Y(v) + 4:.1f}" text-anchor="end">{money(v)}</text>')
    for i in range(5):
        at = t0 + (t1 - t0) * i / 4
        ticks.append(f'<text class="axis" x="{X(at):.1f}" y="{h - 8}" text-anchor="middle">'
                     f'{at.astimezone().strftime("%b %d %H:%M")}</text>')
    zero = f'<line class="zero" x1="{l}" x2="{w - r}" y1="{Y(0):.1f}" y2="{Y(0):.1f}"/>'
    last = vals[-1]
    dot = f'<circle class="dot" cx="{X(last[0]):.1f}" cy="{Y(last[1]):.1f}" r="4"/>'
    return (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Net P&amp;L over time">{"".join(ticks)}{area}{zero}'
            f'<polyline class="line" points="{line}"/>{dot}</svg>')


def esc(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def short(token: str | None, n: int = 46) -> str:
    s = token or ""
    return s if len(s) <= n else s[:n - 1] + "…"


def render(portfolio: str, root: str) -> str:
    store = Store(root)
    points, state, marks = pnl_series(store, portfolio)
    trades = baskets(store, portfolio, state, marks)
    reviewed = read_json(MARKETS).get("markets") or []
    auto = read_json(AUTO)
    cands = read_json(CANDIDATES)
    traded = {}
    for tr in trades:
        traded.setdefault(tr["kalshi"], []).append(tr)
    approved = [{**m, "source": "reviewed"} for m in reviewed] + \
               [{**m, "source": "auto"} for m in auto.get("markets") or []
                if m["kalshi"] not in {r["kalshi"] for r in reviewed}]
    approved_keys = {(m["kalshi"], m["polymarket"]) for m in approved if m.get("enabled", True)}
    now = points[-1] if points else {}
    net = now.get("net")
    realized = now.get("realized")
    settled = [t for t in trades if "settled" in t["status"]]
    wins = sum(1 for t in settled if t["pnl"] is not None and t["pnl"] > 0)

    def pnl_cell(v: Any) -> str:
        if v is None:
            return '<td class="num muted">—</td>'
        cls = "pos" if float(v) > 0 else "neg" if float(v) < 0 else ""
        return f'<td class="num {cls}">{money(v)}</td>'

    rows_b = []
    for t in trades:
        legs = "<br>".join(f'{esc(l["venue"])} {esc(l["contract"])} {esc(l["filled"] or 0)}/{esc(l["qty"])} '
                           f'@ {float(l["vwap"]):.3f}' for l in t["legs"])
        unwound = f'<br><span class="muted">sold back {esc(sum(float(u.get("sold") or 0) for u in t["unwinds"]))}</span>' \
            if t["unwinds"] else ""
        score = f' {t["score"]:.2f}' if isinstance(t["score"], (int, float)) else ""
        rows_b.append(
            f'<tr><td>{esc(timestamp(t["at"]).astimezone().strftime("%b %d %H:%M"))}</td>'
            f'<td><div>{esc(t["kalshi"])}</div><div class="muted">{esc(short(t["polymarket"]))}</div></td>'
            f'<td><span class="tag {esc(t["method"])}">{esc(t["method"])}{score}</span></td>'
            f'<td class="num">{esc(t["sets"])}</td><td class="legs">{legs}{unwound}</td>'
            f'<td class="num">{money(t["expected"])}</td>{pnl_cell(t["pnl"])}<td>{esc(t["status"])}</td></tr>')
    rows_a = []
    for m in approved:
        tr = traded.get(m["kalshi"], [])
        score = m.get("score")
        rows_a.append(
            f'<tr class="{"" if m.get("enabled", True) else "off"}"><td><span class="tag {esc(m["source"])}">{esc(m["source"])}</span></td>'
            f'<td class="num">{"" if score is None else f"{score:.2f}"}</td>'
            f'<td><div>{esc(m.get("name"))}</div><div class="muted">{esc(m["kalshi"])} ↔ {esc(short(m["polymarket"]))}</div></td>'
            f'<td>{"inverted" if m.get("invert") else ""}{"" if m.get("enabled", True) else " disabled"}</td>'
            f'<td class="num">{len(tr) or ""}</td>{pnl_cell(sum((x["pnl"] for x in tr if x["pnl"] is not None), Decimal(0)) if tr else None)}</tr>')
    rows_c = []
    for c in cands.get("candidates") or []:
        key = (c["kalshi"], c["polymarket"])
        state_label = "traded" if c["kalshi"] in traded and key in approved_keys else \
            "approved" if key in approved_keys else ""
        cost = "" if c.get("rough_cost") is None else f"{c['rough_cost']:.3f}"
        differ = " ".join(c.get("only_kalshi") or []) + (" | " if c.get("only_polymarket") else "") + \
            " ".join(c.get("only_polymarket") or [])
        rows_c.append(
            f'<tr><td class="num">{c["score"]:.2f}</td>'
            f'<td class="num">{cost}</td>'
            f'<td><div>{esc(c.get("kalshi_question"))}</div><div class="muted">{esc(c.get("polymarket_question"))}</div></td>'
            f'<td><div>{esc(c["kalshi"])}</div><div class="muted">{esc(short(c["polymarket"]))}</div></td>'
            f'<td class="warn">{esc(differ)}</td><td>{esc(c.get("category"))}</td>'
            f'<td>{f"<span class=tag>{state_label}</span>" if state_label else ""}</td></tr>')

    generated = datetime.now().strftime("%b %d, %Y %H:%M:%S")
    stats = [("Net P&L", money(net), "pos" if net and net > 0 else "neg" if net and net < 0 else ""),
             ("Realized", money(realized), ""),
             ("Unrealized", money(now.get("unrealized")), ""),
             ("Unrealized, marked lots", money(now.get("unrealized_marked")), ""),
             ("Cost not marked", money(now.get("unmarked_cost")), ""),
             ("Baskets", f"{len(trades)}", ""),
             ("Settled", f"{len(settled)} ({wins} won)", ""),
             ("Approved", f"{sum(1 for m in approved if m.get('enabled', True))}", ""),
             ("Candidates", f"{len(cands.get('candidates') or [])}", "")]
    missing = now.get("missing") or []
    venues = sorted({"Polymarket" if t.startswith("PM:") else "Kalshi" for t in missing})
    monitors = " and ".join(f"<code>czquant monitor {'polymarket' if v == 'Polymarket' else 'market'}</code>"
                            for v in venues)
    banner = (f'<div class="banner">{len(missing)} open {" and ".join(venues)} market'
              f'{"s have" if len(missing) != 1 else " has"} no mark from the last 2 minutes, so net P&amp;L '
              f'can&#39;t be valued. Is {monitors} running with this STRATEGY_RUNS_DIR?</div>') if missing else ""
    tiles = "".join(f'<div class="tile"><div class="label">{k}</div><div class="value {cls}">{v}</div></div>'
                    for k, v, cls in stats)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60"><title>Cross-Market Polymarket</title>
<style>
:root {{ --bg:#f7f7f5; --panel:#fff; --ink:#1d1d1b; --muted:#6b6b66; --line:#e3e2dc; --accent:#2f5bd3;
  --pos:#1f7a4d; --neg:#b3261e; --band:rgba(47,91,211,.12); --warn:#8a5a00; --tag:#eef1fb; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141414; --panel:#1c1c1b; --ink:#ecebe6; --muted:#9a9a93;
  --line:#2e2e2b; --accent:#7ea2ff; --pos:#5cc98f; --neg:#ff7a70; --band:rgba(126,162,255,.16); --warn:#e0b25c; --tag:#24283a; }} }}
* {{ box-sizing:border-box }} body {{ margin:0; background:var(--bg); color:var(--ink);
  font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif }}
main {{ max-width:1200px; margin:0 auto; padding:24px 16px 64px }}
h1 {{ font-size:22px; margin:0 }} h2 {{ font-size:16px; margin:32px 0 10px }}
.sub {{ color:var(--muted); margin:4px 0 20px }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px }}
.tile,.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:10px }}
.tile {{ padding:12px 14px }} .label {{ color:var(--muted); font-size:12px }} .value {{ font-size:20px; font-weight:600 }}
.panel {{ padding:14px; overflow-x:auto }}
svg {{ width:100%; height:auto; display:block }} .line {{ fill:none; stroke:var(--accent); stroke-width:2 }}
.band {{ fill:var(--band) }} .grid {{ stroke:var(--line) }} .zero {{ stroke:var(--muted); stroke-dasharray:4 4 }}
.axis {{ fill:var(--muted); font-size:11px }} .dot {{ fill:var(--accent) }}
table {{ width:100%; border-collapse:collapse }} th,td {{ text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); vertical-align:top }}
th {{ font-size:12px; color:var(--muted); font-weight:500; position:sticky; top:0; background:var(--panel); cursor:pointer }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap }}
.muted {{ color:var(--muted); font-size:12px }} .pos {{ color:var(--pos) }} .neg {{ color:var(--neg) }} .warn {{ color:var(--warn); font-size:12px }}
.legs {{ font-size:12px; white-space:nowrap }} tr.off {{ opacity:.5 }}
.tag {{ display:inline-block; padding:1px 7px; border-radius:99px; background:var(--tag); font-size:12px; white-space:nowrap }}
.tag.auto {{ color:var(--warn) }} .tag.reviewed {{ color:var(--pos) }}
.bar {{ display:flex; gap:10px; align-items:center; justify-content:space-between; margin:32px 0 10px; flex-wrap:wrap }}
.bar h2 {{ margin:0 }} input {{ font:inherit; padding:6px 10px; border:1px solid var(--line); border-radius:8px;
  background:var(--panel); color:var(--ink); min-width:220px }}
.scroll {{ max-height:560px; overflow:auto }}
.banner {{ border:1px solid var(--warn); color:var(--warn); border-radius:10px; padding:10px 14px; margin:0 0 12px }}
</style></head><body><main>
<h1>Cross-Market Polymarket</h1>
<p class="sub">Paper portfolio <b>{esc(portfolio)}</b> · updated {generated} · reloads every 60 s
 · band: P&amp;L range when a book is one-sided</p>
<div class="banner" id="stale" hidden></div>
{banner}<div class="tiles">{tiles}</div>
<h2>Net P&amp;L</h2><div class="panel">{chart(points)}</div>
<div class="bar"><h2>Baskets committed ({len(trades)})</h2><input data-filter="baskets" placeholder="Filter baskets"></div>
<div class="panel scroll"><table id="baskets"><thead><tr><th>Time</th><th>Pair</th><th>Match</th><th class="num">Sets</th>
<th>Legs (filled/asked @ avg)</th><th class="num">Expected</th><th class="num">P&amp;L</th><th>Status</th></tr></thead>
<tbody>{"".join(rows_b) or '<tr><td colspan="8" class="muted">No baskets yet.</td></tr>'}</tbody></table></div>
<div class="bar"><h2>Approved markets ({len(approved)})</h2><input data-filter="approved" placeholder="Filter approved"></div>
<div class="panel scroll"><table id="approved"><thead><tr><th>Source</th><th class="num">Score</th><th>Pair</th><th>Flags</th>
<th class="num">Baskets</th><th class="num">P&amp;L</th></tr></thead>
<tbody>{"".join(rows_a) or '<tr><td colspan="6" class="muted">markets.json and markets.auto.json are empty.</td></tr>'}</tbody></table></div>
<div class="bar"><h2>Candidates ({len(cands.get("candidates") or [])})</h2><input data-filter="candidates" placeholder="Filter candidates"></div>
<p class="muted">From find_pairs.py, {esc(cands.get("generated") or "never run")} ({esc(cands.get("source") or "")}).</p>
<div class="panel scroll"><table id="candidates"><thead><tr><th class="num">Score</th><th class="num">Cost</th><th>Questions (Kalshi / Polymarket)</th>
<th>Markets</th><th>Words on one side only</th><th>Category</th><th>State</th></tr></thead>
<tbody>{"".join(rows_c) or '<tr><td colspan="7" class="muted">No candidates.json yet.</td></tr>'}</tbody></table></div>
</main><script>
document.querySelectorAll("input[data-filter]").forEach(function (box) {{
  var key = "filter-" + box.dataset.filter;
  try {{ box.value = localStorage.getItem(key) || ""; }} catch (e) {{}}
  function apply() {{
    var q = box.value.toLowerCase();
    try {{ localStorage.setItem(key, box.value); }} catch (e) {{}}
    document.querySelectorAll("#" + box.dataset.filter + " tbody tr").forEach(function (tr) {{
      tr.style.display = tr.textContent.toLowerCase().indexOf(q) >= 0 ? "" : "none";
    }});
  }}
  box.addEventListener("input", apply); apply();
}});
document.querySelectorAll("th").forEach(function (th) {{
  th.addEventListener("click", function () {{
    var table = th.closest("table"), i = Array.prototype.indexOf.call(th.parentNode.children, th);
    var rows = Array.prototype.slice.call(table.tBodies[0].rows), dir = th.dataset.dir === "asc" ? -1 : 1;
    th.dataset.dir = dir === 1 ? "asc" : "desc";
    function val(r) {{ var t = r.cells[i] ? r.cells[i].textContent.trim() : ""; var n = parseFloat(t.replace(/[$,]/g, ""));
      return isNaN(n) ? t.toLowerCase() : n; }}
    rows.sort(function (a, b) {{ var x = val(a), y = val(b); return (x > y ? 1 : x < y ? -1 : 0) * dir; }});
    rows.forEach(function (r) {{ table.tBodies[0].appendChild(r); }});
  }});
}});
// The page reloads itself, but only `dashboard.py --watch` rewrites it: flag an old snapshot.
(function () {{
  var made = {int(datetime.now().timestamp() * 1000)}, el = document.getElementById("stale");
  var mins = Math.floor((Date.now() - made) / 60000);
  if (mins >= 5) {{ el.hidden = false; el.textContent = "This snapshot is " + mins +
    " minutes old, so its figures can differ from the ledger. Run dashboard.py --watch 60 to keep it current."; }}
}})();
</script></body></html>"""


def write(portfolio: str, root: str, out: str) -> None:
    page = render(portfolio, root)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(page)
    os.replace(tmp, out)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--portfolio", default=os.environ.get("CZQUANT_PORTFOLIO") or "cross-market-polymarket")
    p.add_argument("--out", default=OUT)
    p.add_argument("--watch", type=float, default=None, metavar="SECONDS", help="rewrite the page every SECONDS")
    a = p.parse_args(argv)
    root = os.environ.get("STRATEGY_RUNS_DIR")
    if not root:
        p.error("STRATEGY_RUNS_DIR is not set; load .env first (set -a; source .env; set +a)")
    while True:
        started = time.monotonic()
        write(a.portfolio, root, a.out)
        print(f"{datetime.now():%H:%M:%S} wrote {a.out} ({time.monotonic() - started:.1f} s)")
        if a.watch is None:
            return
        time.sleep(max(1.0, a.watch - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
