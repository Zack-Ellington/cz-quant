"""Single CLI entry point for the strategy."""

import argparse
from pathlib import Path

from dotenv import load_dotenv

from strategy.runner import run

STRATEGY_DIR = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(prog="strategy", description=__doc__)
    parser.parse_args()

    load_dotenv(STRATEGY_DIR / ".env")
    run()
