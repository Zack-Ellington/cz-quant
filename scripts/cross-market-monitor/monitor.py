"""Run the cross-market experiment companion against an existing ledger."""
import argparse
import logging
import os


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.environ.get("STRATEGY_RUNS_DIR"))
    commands = parser.add_subparsers(dest="command", required=True)
    dashboard = commands.add_parser("dashboard", help="serve read-only charts")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8791)
    args = parser.parse_args()
    if not args.root:
        parser.error("set STRATEGY_RUNS_DIR or pass --root for the existing ledger")
    logging.basicConfig(level=logging.INFO)
    from cross_market_dashboard import serve
    serve(args.root, args.host, args.port)



if __name__ == "__main__":
    main()
