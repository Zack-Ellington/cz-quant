"""Single CLI entry point for the strategy."""

import argparse

from strategy.runner import run


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="strategy",
        description="Monte Carlo of 2026 Congress control vs. Kalshi combo prices.",
    )
    parser.add_argument(
        "--simulations",
        type=int,
        default=100_000,
        help="number of Monte Carlo simulations (default: 100000)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="random seed for a reproducible run (default: unseeded)",
    )
    parser.add_argument(
        "--snapshot",
        default=None,
        help="directory of saved Kalshi responses to read instead of the live "
        "API; makes the run reproducible",
    )
    args = parser.parse_args()

    run(simulations=args.simulations, seed=args.seed, snapshot=args.snapshot)
