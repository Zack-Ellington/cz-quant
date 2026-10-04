"""Single CLI entry point for the strategy.

    uv run strategy [options]                   scan, check, model, and paper trade once
    uv run strategy --duration H:MM [options]   the same, back to back, for H:MM
    uv run strategy ledger [options]            aggregate the paper ledger across runs
"""

import argparse
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from strategy.estimators import ESTIMATORS
from strategy.fees import DEFAULT_CONTRACTS
from strategy.ledger import default_runs_dir
from strategy.output import ledger_report
from strategy.pnl import SIMULATIONS
from strategy.runner import DEFAULT_MODEL, MODELS, SIBLINGS, STRATEGY_ID, run
from strategy.session import parse_duration, run_session
from strategy.trading import Policy


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "ledger":
        ledger_main(argv[1:])
    else:
        run_main(argv)


def _duration(text: str):
    try:
        return parse_duration(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def run_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="strategy",
        description="2026 Congress control vs. Kalshi's combo market: checks, model, "
        "and paper trading. Paper mode is the default and sends nothing. "
        "`strategy ledger` aggregates past runs.",
    )
    parser.add_argument("--model", choices=MODELS, default=DEFAULT_MODEL,
                        help=f"model to price and trade with (default: {DEFAULT_MODEL})")
    parser.add_argument("--estimator", choices=sorted(ESTIMATORS), default="midpoint",
                        help="how race books become probabilities (default: midpoint)")
    parser.add_argument("--snapshot-in", metavar="FILE", default=None,
                        help="replay a saved snapshot instead of reading the live API")
    parser.add_argument("--snapshot-out", metavar="FILE", default=None,
                        help="save every quote this run reads to FILE (saved before fitting); "
                        "with --duration, one file per pass, FILE-<session>-<pass>")
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
    parser.add_argument("--duration", type=_duration, default=None, metavar="H:MM",
                        help="run back to back, as often as possible, for this long (e.g. 24:00, 1:30); "
                        "each pass paper-trades and is marked in the ledger with the session id")
    parser.add_argument("--bankroll", type=Decimal, default=Policy.bankroll,
                        help=f"paper bankroll in dollars (default: {Policy.bankroll})")
    parser.add_argument("--max-position", type=Decimal, default=Policy.max_position,
                        help=f"entry capital cap per position (default: {Policy.max_position})")
    parser.add_argument("--min-edge", type=Decimal, default=Policy.min_edge,
                        help=f"minimum edge per set after fees, in dollars (default: {Policy.min_edge})")
    parser.add_argument("--kelly-fraction", type=Decimal, default=Policy.kelly_fraction,
                        help="fraction of the Kelly stake, on the model's edge after its uncertainty; "
                        f"1 is full Kelly (default: {Policy.kelly_fraction})")
    parser.add_argument("--runs-dir", default=None,
                        help=f"where run directories go (default: {default_runs_dir()})")
    parser.add_argument("--confirm-delay", type=float, default=None,
                        help="seconds before the confirmation scan (default: 5 live, 0 on a snapshot)")
    args = parser.parse_args(argv)
    if args.duration is not None and args.snapshot_in is not None:
        parser.error("--duration needs live quotes: a snapshot replays the same quotes on every pass")

    options = dict(
        simulations=args.simulations,
        seed=args.seed,
        model=args.model,
        estimator=args.estimator,
        snapshot_in=args.snapshot_in,
        contracts=args.contracts,
        mode=args.mode,
        trading=not args.no_trade,
        bankroll=args.bankroll,
        max_position=args.max_position,
        min_edge=args.min_edge,
        kelly_fraction=args.kelly_fraction,
        confirm_delay=args.confirm_delay,
    )
    if args.duration is None:
        run(snapshot_out=args.snapshot_out, runs_dir=args.runs_dir, **options)
        return

    runs_dir = Path(args.runs_dir) if args.runs_dir is not None else default_runs_dir()

    def one_pass(info: dict) -> Path:
        return run(snapshot_out=_per_pass(args.snapshot_out, info), runs_dir=runs_dir,
                   session=info, quiet=True, **options)

    session = run_session(one_pass, args.duration, runs_dir, STRATEGY_ID, args.bankroll)
    print(f"Session {session.session_id} ended: {session.stopped}; {len(session.passes)} pass(es), "
          f"{session.failed} failed")
    print()
    print(ledger_report(runs_dir, STRATEGY_ID, siblings=SIBLINGS, session=session.session_id))
    if session.stopped.startswith("stopped after"):
        raise SystemExit(1)


def _per_pass(path: str | None, info: dict) -> str | None:
    """``quotes.json`` -> ``quotes-<session>-0001.json``: one snapshot per pass."""
    if path is None:
        return None
    p = Path(path)
    return str(p.with_name(f"{p.stem}-{info['id']}-{info['pass']:04d}{p.suffix}"))


def ledger_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="strategy ledger",
        description="Volume, capital, P&L, and return on committed capital, joined "
        "across runs by position id, plus the simulated P&L of open positions under "
        "the model, the market, and the sibling strategy's model.",
    )
    parser.add_argument("--strategy", default=STRATEGY_ID,
                        help=f"strategy id (default: {STRATEGY_ID})")
    parser.add_argument("--since", type=date.fromisoformat, default=None,
                        help="first entry date to include, YYYY-MM-DD")
    parser.add_argument("--until", type=date.fromisoformat, default=None,
                        help="last entry date to include, YYYY-MM-DD")
    parser.add_argument("--session", default=None, metavar="ID",
                        help="only positions entered in this --duration session")
    parser.add_argument("--runs-dir", default=None,
                        help=f"run directories to read (default: {default_runs_dir()})")
    parser.add_argument("--simulations", type=int, default=SIMULATIONS,
                        help=f"draws for the simulated P&L (default: {SIMULATIONS})")
    parser.add_argument("--seed", type=int, default=0, help="seed for the simulated P&L (default: 0)")
    args = parser.parse_args(argv)
    runs_dir = args.runs_dir if args.runs_dir is not None else default_runs_dir()
    siblings = [s for s in (STRATEGY_ID, *SIBLINGS) if s != args.strategy]
    print(ledger_report(runs_dir, args.strategy, args.since, args.until, args.simulations, args.seed,
                        siblings, args.session))
