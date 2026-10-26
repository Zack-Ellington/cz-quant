"""Single CLI entry point for the strategy.

    uv run strategy [options]   scan, check, model, and paper trade once
"""

import argparse
import sys
from decimal import Decimal

from strategy.estimators import ESTIMATORS
from strategy.fees import DEFAULT_CONTRACTS
from strategy.ledger import default_runs_dir
from strategy.runner import DEFAULT_MODEL, MODELS, run
from strategy.trading import Policy


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        prog="strategy",
        description="2026 Congress control vs. Kalshi's combo market: checks, model, "
        "and paper trading. Paper mode is the default and sends nothing.",
    )
    parser.add_argument("--model", choices=MODELS, default=DEFAULT_MODEL,
                        help=f"model to price and trade with (default: {DEFAULT_MODEL})")
    parser.add_argument("--estimator", choices=sorted(ESTIMATORS), default="midpoint",
                        help="how race books become probabilities (default: midpoint)")
    parser.add_argument("--snapshot-in", metavar="FILE", default=None,
                        help="replay a saved snapshot instead of reading the live API")
    parser.add_argument("--snapshot-out", metavar="FILE", default=None,
                        help="save every quote this run reads to FILE (saved before fitting)")
    parser.add_argument("--simulations", type=int, default=100_000,
                        help="Monte Carlo runs for the independent model (default: 100000)")
    parser.add_argument("--seed", type=int, default=None,
                        help="random seed for the simulations (default: unseeded)")
    parser.add_argument("--contracts", type=int, default=DEFAULT_CONTRACTS,
                        help=f"largest order per leg shown in checks and EV (default: {DEFAULT_CONTRACTS})")
    parser.add_argument("--mode", choices=("paper", "live"), default="paper",
                        help="paper (default) simulates fills; live is not implemented (issue #5)")
    parser.add_argument("--no-trade", action="store_true",
                        help="scan and report only; still writes the run directory")
    parser.add_argument("--bankroll", type=Decimal, default=Policy.bankroll,
                        help=f"paper bankroll in dollars (default: {Policy.bankroll})")
    parser.add_argument("--max-position", type=Decimal, default=Policy.max_position,
                        help=f"entry capital cap per position (default: {Policy.max_position})")
    parser.add_argument("--min-edge", type=Decimal, default=Policy.min_edge,
                        help=f"minimum edge per set after fees, in dollars (default: {Policy.min_edge})")
    parser.add_argument("--runs-dir", default=None,
                        help=f"where run directories go (default: {default_runs_dir()})")
    parser.add_argument("--confirm-delay", type=float, default=None,
                        help="seconds before the confirmation scan (default: 5 live, 0 on a snapshot)")
    args = parser.parse_args(argv)
    run(
        simulations=args.simulations,
        seed=args.seed,
        model=args.model,
        estimator=args.estimator,
        snapshot_in=args.snapshot_in,
        snapshot_out=args.snapshot_out,
        contracts=args.contracts,
        mode=args.mode,
        trading=not args.no_trade,
        bankroll=args.bankroll,
        max_position=args.max_position,
        min_edge=args.min_edge,
        runs_dir=args.runs_dir,
        confirm_delay=args.confirm_delay,
    )
