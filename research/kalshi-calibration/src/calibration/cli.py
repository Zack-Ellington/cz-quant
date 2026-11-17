"""calibration pull | calibration analyze"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DATA = Path("data")


def main() -> None:
    p = argparse.ArgumentParser(prog="calibration")
    sub = p.add_subparsers(dest="cmd", required=True)

    pull = sub.add_parser("pull", help="download events and price history (resumable)")
    pull.add_argument("--data", type=Path, default=DEFAULT_DATA)
    pull.add_argument("--months", type=int, default=6)
    pull.add_argument("--rate", type=float, default=3.0, help="max requests per second (slows itself on 429s)")
    pull.add_argument("--workers", type=int, default=4)
    pull.add_argument("--series", nargs="*", default=None,
                      help="only these series tickers; a trailing * matches a prefix (e.g. KXNCAAWB*)")

    an = sub.add_parser("analyze", help="calibration tables and t-tests")
    an.add_argument("--data", type=Path, default=DEFAULT_DATA)
    an.add_argument("--out", type=Path, default=Path("reports"))
    an.add_argument("--alpha", type=float, default=0.02)
    an.add_argument("--min-volume", type=float, default=100.0, help="contracts traded, chosen side")

    args = p.parse_args()
    if args.cmd == "pull":
        from .pull import Puller

        since_dt = datetime.now(timezone.utc) - timedelta(days=round(args.months * 30.44))
        since = int(since_dt.timestamp())
        meta = args.data / "meta.json"
        args.data.mkdir(parents=True, exist_ok=True)
        if meta.exists():
            since = json.loads(meta.read_text())["since"]  # keep the window fixed across resumes
        else:
            meta.write_text(json.dumps({"since": since, "since_iso": since_dt.isoformat()}))
        puller = Puller(args.data, since, rate=args.rate, workers=args.workers)
        series = puller.series()
        if args.series:
            series = [s for s in series if any(
                s["ticker"].startswith(pat[:-1]) if pat.endswith("*") else s["ticker"] == pat
                for pat in args.series)]
        print(f"window since {datetime.fromtimestamp(since, timezone.utc):%Y-%m-%d}; {len(series)} series")
        puller.events(series)
        puller.archive()
        markets = puller.qualifying()
        (args.data / "qualifying.json").write_text(json.dumps(markets), encoding="utf-8")
        print(f"qualifying two-outcome events: {len(markets)}")
        puller.candles(markets)
    else:
        from .report import run

        run(args.data, args.out, alpha=args.alpha, min_volume=args.min_volume)
