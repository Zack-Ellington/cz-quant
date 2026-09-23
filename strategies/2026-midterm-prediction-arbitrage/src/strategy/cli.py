"""Single CLI entry point for the strategy."""

import argparse

from strategy.estimators import ESTIMATORS
from strategy.fees import DEFAULT_CONTRACTS
from strategy.runner import MODELS, run


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="strategy",
        description="2026 Congress control: model-free checks and a calibrated "
        "factor model vs. Kalshi's combo market.",
    )
    parser.add_argument(
        "--model",
        choices=MODELS,
        default="auto",
        help="auto (default): calibrate and add a second factor only if needed; "
        "one-factor / two-factor: force it; independent: no calibration",
    )
    parser.add_argument(
        "--estimator",
        choices=sorted(ESTIMATORS),
        default="midpoint",
        help="how race books become probabilities (default: midpoint)",
    )
    parser.add_argument(
        "--snapshot-in",
        metavar="FILE",
        default=None,
        help="replay a saved snapshot instead of reading the live API",
    )
    parser.add_argument(
        "--snapshot-out",
        metavar="FILE",
        default=None,
        help="save every quote this run reads to FILE",
    )
    parser.add_argument(
        "--simulations",
        type=int,
        default=100_000,
        help="Monte Carlo runs for the independent model (default: 100000)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="random seed for the independent model (default: unseeded)",
    )
    parser.add_argument(
        "--contracts",
        type=int,
        default=DEFAULT_CONTRACTS,
        help=f"order size per leg for fee rounding (default: {DEFAULT_CONTRACTS})",
    )
    args = parser.parse_args()

    run(
        simulations=args.simulations,
        seed=args.seed,
        model=args.model,
        estimator=args.estimator,
        snapshot_in=args.snapshot_in,
        snapshot_out=args.snapshot_out,
        contracts=args.contracts,
    )
