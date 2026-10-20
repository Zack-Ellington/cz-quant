"""Void a paper fill that couldn't have happened.

Writes a request to runs/kalshi_sum_check/ws/VOID/. The paper engine applies it
within one heartbeat (or at its next start): the trade leaves the open book, its
capital goes back to cash, and a `void` record with your reason goes in the
ledger. The dashboard drops voided trades from positions, totals and charts.

Usage: uv run python void_trade.py TRADE_ID --reason "why the fill wasn't real"
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

from paper_ws import VOID_DIR, read_ledgers


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("trade_id")
    p.add_argument("--reason", required=True)
    a = p.parse_args(argv)
    recs = list(read_ledgers())
    trade = next((r for r in recs if r.get("type") == "trade" and r.get("trade_id") == a.trade_id), None)
    if trade is None:
        raise SystemExit(f"No trade {a.trade_id} in the ledgers.")
    done = {r.get("trade_id") for r in recs if r.get("type") in ("settle", "void")}
    if a.trade_id in done:
        raise SystemExit(f"Trade {a.trade_id} is already settled or voided.")
    os.makedirs(VOID_DIR, exist_ok=True)
    path = os.path.join(VOID_DIR, f"{a.trade_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"trade_id": a.trade_id, "reason": a.reason,
                   "requested": datetime.now(timezone.utc).isoformat(timespec="seconds")}, f)
    print(f"Void requested for {a.trade_id} ({trade['event']}, ${trade['capital']:.2f} capital, "
          f"${trade['net']:.2f} expected). The engine applies it within one heartbeat.")


if __name__ == "__main__":
    main()
