"""Runner for the strategy. Configuration comes from exported environment variables.

The pipeline is: discover the races, read each race's Democratic win probability
from Kalshi, run the Monte Carlo, read the combo-market prices, and print the
model-vs-market table.

By default it reads live prices from Kalshi's public API (no key required). Pass
a ``snapshot`` directory to read saved fixtures instead; that is what the tests
and the README sample use, and it is the only way to get a reproducible table,
since the live combo market is not always quoted.
"""

from __future__ import annotations

import os
from pathlib import Path

from strategy import constants
from strategy.api import Client, FixtureClient, KalshiClient
from strategy.output import print_table
from strategy.probabilities import extract_probabilities, read_combo_prices
from strategy.races import discover_races
from strategy.simulation import simulate


def run(
    simulations: int = 100_000,
    seed: int | None = None,
    snapshot: str | None = None,
) -> None:
    """Run the full pipeline and print the comparison table.

    ``snapshot`` is an optional directory of saved Kalshi responses; when given,
    no network calls are made and the run is reproducible.
    """
    if snapshot is not None:
        client: Client = FixtureClient(snapshot)
        label = Path(snapshot).name
        _run_with_client(client, simulations, seed, label)
        return

    base_url = os.environ.get("KALSHI_API_BASE_URL", constants.DEFAULT_BASE_URL)
    with KalshiClient(base_url=base_url) as client:
        _run_with_client(client, simulations, seed, "live")


def _run_with_client(
    client: Client, simulations: int, seed: int | None, label: str
) -> None:
    races = discover_races()
    probs = extract_probabilities(races, client)
    result = simulate(probs, n=simulations, seed=seed)
    combo_prices = read_combo_prices(client)
    print_table(result, combo_prices, snapshot_label=label)
