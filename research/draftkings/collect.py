"""Save one read-only odds snapshot under the shared research directory."""
from __future__ import annotations

import argparse
import json
import os
import uuid
from dataclasses import asdict
from pathlib import Path

import httpx

from api import Blocked, Nash, OddsApi, OutOfCredits, utc


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("oddsapi", "nash"), default="oddsapi")
    parser.add_argument("--league", required=True, help="source-specific league ID")
    args = parser.parse_args(argv)
    root = os.environ.get("STRATEGY_RUNS_DIR")
    if not root:
        parser.error("export STRATEGY_RUNS_DIR for research snapshots")
    source = OddsApi() if args.source == "oddsapi" else Nash()
    try:
        raw = source.fetch(args.league)
        observed = utc()
        selections = source.parse(raw, args.league, observed)
    except (httpx.HTTPError, Blocked, OutOfCredits) as exc:
        # Some sources put credentials in URLs; do not print a request traceback.
        parser.exit(1, f"{args.source} read failed: {type(exc).__name__}\n")
    finally:
        source.http.close()
    directory = Path(root) / "research" / "draftkings"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{args.source}-{uuid.uuid4().hex}.json"
    record = {"schema_version": 1, "source": args.source, "league": args.league,
              "observed_at": observed, "remaining_credits": source.remaining,
              "selections": [asdict(row) for row in selections], "raw": raw}
    path.write_text(json.dumps(record, default=str, allow_nan=False) + "\n")
    print(path)


if __name__ == "__main__":
    main()
