"""Runner for the strategy. Configuration comes from exported environment variables.

Pipeline: read quotes -> model-free checks -> calibrate -> simulate -> table.

1. Read every quote the run needs: each race's legs, the combo, the control,
   same-party, and seat-count markets, and the fee schedule of each traded
   series.
2. Run the model-free arbitrage checks on the aggregate markets.
3. Turn each race's quotes into probabilities with the chosen estimator, and
   (unless ``model="independent"``) calibrate the swing, the unpriced-seat flip
   rates, and the caucus share to the seat-count and control markets.
4. Simulate the independent model on the same race probabilities, and print the
   independent model, the calibrated factor model, and the market side by side.

Quotes come from Kalshi's public API (no key) or, with ``snapshot_in``, from a
saved snapshot, which makes the run reproducible. ``snapshot_out`` records every
quote a live run reads so it can be replayed later.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from strategy import constants
from strategy.api import Client, KalshiClient
from strategy.calibration import calibrate
from strategy.checks import run_checks
from strategy.fees import DEFAULT_CONTRACTS, fee_schedules
from strategy.markets import fetch_markets, traded_series
from strategy.output import Report, print_report
from strategy.probabilities import apply_estimator, caucus_probs, load_race_quotes
from strategy.races import discover_races
from strategy.simulation import simulate
from strategy.snapshot import RecordingClient, SnapshotClient

MODELS = ("auto", "one-factor", "two-factor", "independent")


def run(
    simulations: int = 100_000,
    seed: int | None = None,
    model: str = "auto",
    estimator: str = "midpoint",
    snapshot_in: str | None = None,
    snapshot_out: str | None = None,
    contracts: int = DEFAULT_CONTRACTS,
) -> None:
    """Run the full pipeline and print the report."""
    if model not in MODELS:
        raise ValueError(f"model must be one of {MODELS}, got {model!r}")

    live: KalshiClient | None = None
    if snapshot_in is not None:
        client: Client = SnapshotClient(snapshot_in)
        source = f"snapshot {Path(snapshot_in).name} (captured {client.captured_at})"
    else:
        base_url = os.environ.get("KALSHI_API_BASE_URL", constants.DEFAULT_BASE_URL)
        live = KalshiClient(base_url=base_url)
        client = live
        source = "live"
    recorder = None
    if snapshot_out is not None:
        recorder = RecordingClient(client, source=getattr(client, "source", "") or source_url())
        client = recorder

    try:
        report = build_report(client, source, simulations, seed, model, estimator, contracts)
    finally:
        if live is not None:
            live.close()

    print_report(report)
    if recorder is not None:
        path = recorder.save(snapshot_out)
        print(f"Saved {len(recorder.records)} quotes to {path}", file=sys.stderr)


def build_report(
    client: Client,
    source: str,
    simulations: int,
    seed: int | None,
    model: str,
    estimator: str,
    contracts: int,
) -> Report:
    races = discover_races()
    race_probs = apply_estimator(races, load_race_quotes(races, client), estimator)
    markets = fetch_markets(client)
    fees = fee_schedules(client, traded_series(markets))
    checks = run_checks(markets, fees, contracts)

    if model == "independent":
        calibration = None
        probs = caucus_probs(race_probs, 0.0)
    else:
        calibration = calibrate(race_probs, markets, model)
        probs = calibration.chosen.probs

    return Report(
        source=source,
        estimator=estimator,
        contracts=contracts,
        checks=checks,
        markets=markets,
        fees=fees,
        independent=simulate(probs, n=simulations, seed=seed),
        calibration=calibration,
    )


def source_url() -> str:
    return os.environ.get("KALSHI_API_BASE_URL", constants.DEFAULT_BASE_URL)
