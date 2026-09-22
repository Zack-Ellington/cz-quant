"""End-to-end on the saved snapshot: the printed table must match byte for byte.

The pipeline is deterministic (fixed seed, PCG64 stream, saved prices), so the
whole run reduces to one golden string. If any stage changes the numbers or the
layout, this fails.
"""

from __future__ import annotations

from pathlib import Path

from strategy.output import format_table
from strategy.probabilities import extract_probabilities, read_combo_prices
from strategy.races import discover_races
from strategy.runner import run
from strategy.simulation import simulate

EXPECTED = (Path(__file__).resolve().parent / "fixtures" / "expected_output.txt").read_text(
    encoding="utf-8"
)


def test_pipeline_matches_golden_table(fixture_client):
    probs = extract_probabilities(discover_races(), fixture_client)
    result = simulate(probs, n=100_000, seed=12345)
    combo = read_combo_prices(fixture_client)
    table = format_table(result, combo, snapshot_label="fixtures")
    assert table + "\n" == EXPECTED


def test_runner_prints_the_same_table(capsys, fixtures_dir):
    run(simulations=100_000, seed=12345, snapshot=str(fixtures_dir))
    out = capsys.readouterr().out
    assert out == EXPECTED
