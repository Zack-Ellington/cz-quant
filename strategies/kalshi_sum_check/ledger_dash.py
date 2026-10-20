"""Local dashboard for the WebSocket paper trader, read straight from its ledgers.

Serves dashboard/ledger.html and /api/state, which re-reads
runs/kalshi_sum_check/ws/*/ledger.jsonl (new bytes only) on every request. The page
polls every 2 seconds, so a trade shows up as soon as the engine fsyncs it.

Usage: uv run python ledger_dash.py            # http://localhost:8780
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from paper_ws import WS_RUNS

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "dashboard", "ledger.html")
MAX_POINTS = 600
MAX_RECENT = 40


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


class LedgerReader:
    """Tails every run's ledger.jsonl, remembering how far it has read."""

    def __init__(self, root=WS_RUNS):
        self.root = root
        self.offsets: dict[str, int] = {}
        self.partial: dict[str, str] = {}
        self.records: list[dict] = []
        self.lock = threading.Lock()

    def poll(self):
        with self.lock:
            if not os.path.isdir(self.root):
                return self.records
            if any(not os.path.isfile(p) for p in self.offsets):  # a run was deleted: start over
                self.offsets, self.partial, self.records = {}, {}, []
            for run in sorted(os.listdir(self.root)):
                path = os.path.join(self.root, run, "ledger.jsonl")
                if not os.path.isfile(path):
                    continue
                with open(path, "rb") as f:
                    f.seek(self.offsets.get(path, 0))
                    chunk = f.read()
                    self.offsets[path] = f.tell()
                text = self.partial.pop(path, "") + chunk.decode("utf-8", "replace")
                lines = text.split("\n")
                if lines and lines[-1]:
                    self.partial[path] = lines.pop()  # a line still being written
                for line in lines:
                    if line.strip():
                        try:
                            self.records.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            return self.records


def build_state(records: list[dict]) -> dict:
    runs, trades, settles, voids = {}, {}, {}, {}
    rejects, opp_events, opp_classes = Counter(), set(), Counter()
    heartbeats, recent, halts, warnings = [], [], [], []
    config = {}
    for r in records:
        t = r.get("type")
        run = runs.setdefault(r["run"], {"run": r["run"], "started": r["ts"], "stopped": None, "feed": None, "last_seen": r["ts"]})
        run["last_seen"] = r["ts"]
        if t == "run_start":
            run["feed"] = r.get("feed")
            config = r.get("config") or config
        elif t == "run_stop":
            run["stopped"] = r["ts"]
        elif t == "trade":
            trades[r["trade_id"]] = r
        elif t == "settle":
            settles[r["trade_id"]] = r
        elif t == "void":
            voids[r["trade_id"]] = r
        elif t == "reject":
            rejects[r.get("reason")] += 1
        elif t == "opportunity":
            opp_events.add(r.get("event"))
            opp_classes[r.get("cls")] += 1
        elif t == "heartbeat":
            heartbeats.append(r)
        elif t == "halt":
            halts.append(r)
        elif t == "warning":
            warnings.append(r)
        if t not in ("heartbeat",):
            recent.append(r)

    # Voided fills never happened: keep them out of positions, totals and charts.
    trade_list = sorted((t for k, t in trades.items() if k not in voids), key=lambda r: r["ts"])
    for tr in trade_list:
        s = settles.get(tr["trade_id"])
        tr["settled"] = s is not None
        tr["pnl"] = s["pnl"] if s else None
        tr["settled_at"] = s["ts"] if s else None
        tr["verified"] = s.get("verified") if s else None  # None on settles from before the cross-check
    open_trades = [t for t in trade_list if not t["settled"]]
    closed = [t for t in trade_list if t["settled"]]
    cap_all = sum(t["capital"] for t in trade_list)

    # Cumulative series, one point per trade or settlement.
    series, deployed, expected, realized = [], 0.0, 0.0, 0.0
    events = [(t["ts"], "trade", t) for t in trade_list] + [(s["ts"], "settle", s) for s in settles.values()
                                                             if s["trade_id"] not in voids]
    for ts, kind, rec in sorted(events, key=lambda e: e[0]):
        if kind == "trade":
            deployed += rec["capital"]
            expected += rec["net"]
        else:
            tr = trades.get(rec["trade_id"])
            if tr:
                deployed -= tr["capital"]
                expected -= tr["net"]
            realized += rec["pnl"]
        series.append({"t": ts, "deployed": round(deployed, 2), "expected": round(expected, 2),
                       "realized": round(realized, 2), "total": round(expected + realized, 2)})

    hb = heartbeats[-1] if heartbeats else None
    last_run = max(runs.values(), key=lambda r: r["started"]) if runs else None
    if not last_run:
        status = "no_data"
    elif last_run["stopped"]:
        status = "stopped"
    elif hb and time.time() - _ts(hb["ts"]) < 3 * config.get("heartbeat_seconds", 30) + 15:
        status = "halted" if hb.get("halted") else "live"
    elif not hb and time.time() - _ts(last_run["last_seen"]) < 120:
        status = "starting"
    else:
        status = "stale"

    step = max(1, len(heartbeats) // MAX_POINTS)
    hb_series = [{"t": h["ts"], "opps": h.get("opps_now", 0), "msgs": h.get("msgs_per_s", 0),
                  "fresh": h.get("books_fresh", 0), "markets": h.get("markets", 0)} for h in heartbeats[::step]]
    if heartbeats and (not hb_series or hb_series[-1]["t"] != heartbeats[-1]["ts"]):
        h = heartbeats[-1]
        hb_series.append({"t": h["ts"], "opps": h.get("opps_now", 0), "msgs": h.get("msgs_per_s", 0),
                          "fresh": h.get("books_fresh", 0), "markets": h.get("markets", 0)})

    def slim(t):
        keys = ("trade_id", "ts", "event", "title", "category", "cls", "side", "sets", "cost", "fees", "capital",
                "payout", "net", "gross", "apr", "days", "settle_eta", "latency_ms", "decided_apr", "feed",
                "settled", "pnl", "settled_at", "verified", "pool")
        out = {k: t.get(k) for k in keys}
        out["legs"] = [{"ticker": l["ticker"], "outcome": l.get("outcome"), "contract": l["contract"],
                        "vwap": l["vwap"], "fee": l["fee"]} for l in t.get("legs", [])]
        return out

    return {
        "now": time.time(),
        "status": status,
        "config": config,
        "runs": sorted(runs.values(), key=lambda r: r["started"], reverse=True)[:10],
        "heartbeat": hb,
        "totals": {
            "trades": len(trade_list),
            "open": len(open_trades),
            "settled": len(closed),
            "wins": sum(1 for t in closed if t["pnl"] >= -0.005),
            "deployed": round(sum(t["capital"] for t in open_trades), 2),
            "expected_open": round(sum(t["net"] for t in open_trades), 2),
            "realized": round(sum(t["pnl"] for t in closed), 2),
            "gross_all": round(sum(t["gross"] for t in trade_list), 2),
            "fees_all": round(sum(t["fees"] for t in trade_list), 2),
            "net_all": round(sum(t["net"] for t in trade_list), 2),
            "capital_all": round(cap_all, 2),
            "apr_weighted": (sum(t["apr"] * t["capital"] for t in trade_list) / cap_all) if cap_all else None,
            "days_weighted": (sum(t["days"] * t["capital"] for t in trade_list) / cap_all) if cap_all else None,
            "latency_ms": (sum(t.get("latency_ms") or 0 for t in trade_list) / len(trade_list)) if trade_list else None,
            "opp_events": len(opp_events),
            "rejects": sum(rejects.values()),
            "voided": len(voids),
        },
        "rejects": rejects.most_common(),
        "opp_classes": dict(opp_classes),
        "open_trades": [slim(t) for t in sorted(open_trades, key=lambda t: t["apr"], reverse=True)],
        "trades": [slim(t) for t in reversed(trade_list[-100:])],
        "series": series[-MAX_POINTS:],
        "hb_series": hb_series,
        "halts": halts[-5:],
        "warnings": warnings[-10:],
        "recent": [{k: v for k, v in r.items() if k in ("ts", "type", "event", "title", "reason", "side", "sets",
                                                          "net", "apr", "pnl", "events", "markets", "feed", "kind",
                                                          "detail", "error", "where", "capital")}
                   for r in recent[-MAX_RECENT:]][::-1],
    }


class Handler(BaseHTTPRequestHandler):
    reader: LedgerReader = None

    def log_message(self, *args):
        pass

    def _send(self, code, body: bytes, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            state = build_state(self.reader.poll())
            self._send(200, json.dumps(state, default=str).encode(), "application/json")
        elif self.path in ("/", "/index.html"):
            with open(PAGE, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8780)
    p.add_argument("--host", default="127.0.0.1")
    a = p.parse_args(argv)
    Handler.reader = LedgerReader()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"Paper trade dashboard on http://{a.host}:{a.port} (reading {WS_RUNS})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
