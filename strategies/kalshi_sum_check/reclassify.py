"""Re-score the monitor's history with the no-winner classifier.

state.json keeps per-event running stats but not rules text, so this fetches each
event's markets from Kalshi (rules don't change), classifies it, and reports how much
of the recorded edge came from events where exactly one outcome must win.

Usage: python reclassify.py [--state PATH]
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys

from classifier import EXHAUSTIVE, classify
from kalshi_client import KalshiClient
from monitor import DEFAULT_DIR


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state", default=os.path.join(DEFAULT_DIR, "state.json"))
    args = p.parse_args(argv)
    with open(args.state, encoding="utf-8") as f:
        state = json.load(f)
    events = state["events"]
    last = state["last_scan_at"]

    client = KalshiClient()
    fetched = {}
    print("Fetching open events...", file=sys.stderr)
    for ev in client.iter_open_events():
        if ev["event_ticker"] in events:
            fetched[ev["event_ticker"]] = ev
    # Events that closed during the week, but only the ones that ever showed an edge.
    missing = [k for k, e in events.items() if k not in fetched and (e["positive"] or e["under"] or e["over"])]
    print(f"Fetching {len(missing)} closed events individually...", file=sys.stderr)
    for k in missing:
        try:
            d = client.get(f"/events/{k}", {"with_nested_markets": "true"})
            ev = d["event"]
            ev["markets"] = ev.get("markets") or d.get("markets") or []
            fetched[k] = ev
        except RuntimeError:
            pass

    cls = {k: classify(ev) for k, ev in fetched.items()}
    out = {"scans": state["scans"], "from": state["first_scan_at"], "to": last, "classes": {}, "events": []}

    by_class = collections.defaultdict(lambda: {"tracked": 0, "ever_positive": 0, "positive_scans": 0, "net_now": 0.0, "under_scans": 0, "over_scans": 0})
    for k, e in events.items():
        c = cls.get(k, ("CLOSED_QUIET", ""))[0]  # closed during the week, never off $1
        b = by_class[c]
        b["tracked"] += 1
        b["under_scans"] += e["under"]
        b["over_scans"] += e["over"]
        if e["positive"]:
            b["ever_positive"] += 1
            b["positive_scans"] += e["positive"]
        # Only events last week's totals counted (they passed the old mid-sum check).
        if e["last_seen"] == last and e.get("exh", True) and (e.get("net") or 0) > 0:
            b["net_now"] = round(b["net_now"] + e["net"], 2)
        if e["positive"]:
            out["events"].append({"ticker": k, "title": e["title"], "class": c, "reason": cls.get(k, ("", ""))[1],
                                  "positive_scans": e["positive"], "obs": e["obs"], "ask_lo": e["ask_lo"], "bid_hi": e["bid_hi"],
                                  "net_now": e.get("net"), "apr_now": e.get("apr"), "days": e.get("days"), "open": e["last_seen"] == last})
    out["classes"] = dict(sorted(by_class.items(), key=lambda kv: -kv[1]["positive_scans"]))
    out["events"].sort(key=lambda r: (r["class"] != EXHAUSTIVE, -r["positive_scans"]))

    path = os.path.join(os.path.dirname(args.state), "reclassified.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)

    total_pos = sum(b["positive_scans"] for b in by_class.values()) or 1
    print(f"\n{state['scans']} scans, {out['from']} to {out['to']}")
    print(f"{'class':<16}{'tracked':>9}{'ever +':>8}{'+ event-scans':>15}{'share':>8}{'net now $':>11}")
    for c, b in out["classes"].items():
        print(f"{c:<16}{b['tracked']:>9}{b['ever_positive']:>8}{b['positive_scans']:>15}{b['positive_scans'] / total_pos:>8.1%}{b['net_now']:>11.2f}")
    print("\nEvents with an edge after fees at least once:")
    for r in out["events"]:
        print(f"  {r['class']:<15}{r['ticker'][:32]:<33}{r['title'][:44]:<45}+{r['positive_scans']:>4}/{r['obs']:<4}"
              f" askLo {r['ask_lo']}  bidHi {r['bid_hi']}  {'open' if r['open'] else 'closed'}  {r['reason'][:60]}")
    print(f"\nWrote {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
