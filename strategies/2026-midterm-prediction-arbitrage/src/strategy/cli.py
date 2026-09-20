"""Single CLI entry point for the strategy."""

import argparse

from strategy.runner import run


def main() -> None:
    parser = argparse.ArgumentParser(prog="strategy", description=__doc__)
    parser.parse_args()

    run()
