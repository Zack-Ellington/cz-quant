"""Paper-buy favored teams in Kalshi women's college basketball games."""

import argparse

from strategy.runner import run


def main() -> None:
    parser = argparse.ArgumentParser(prog="strategy", description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one scan and exit")
    args = parser.parse_args()
    run(once=args.once)
