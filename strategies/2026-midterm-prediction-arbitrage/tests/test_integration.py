"""End to end on the committed snapshot of real quotes.

The snapshot replays every quote the run read, the calibration is deterministic,
and the independent Monte Carlo is seeded, so the whole report reduces to one
golden string. Beyond that byte-for-byte check, the selected model must meet
the criteria it was selected by:

* one factor is selected, because it reproduces both chamber-control markets
  within 0.01 (the rule in ``calibration.select_model``);
* the model still prices every race at its market probability: simulated win
  rates stay within 0.01 of the inputs, which is what the latent-margin scaling
  promises;
* the exact (quadrature) and simulated combo probabilities agree within 0.01.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from strategy.calibration import FACTOR_TOLERANCE, control_miss
from strategy.estimators import ESTIMATORS
from strategy.factor import simulate_factor
from strategy.output import format_report
from strategy.runner import build_report, run
from strategy.snapshot import SnapshotClient
from tests.conftest import committed_snapshot

GOLDEN = Path(__file__).resolve().parent / "expected_output.txt"
SNAPSHOT = committed_snapshot()
SEED = 12345


@pytest.fixture(scope="module")
def report():
    client = SnapshotClient(SNAPSHOT)
    source = f"snapshot {SNAPSHOT.name} (captured {client.captured_at})"
    return build_report(client, source, 100_000, SEED, "auto", "midpoint", 100)


def test_report_matches_golden(report):
    assert format_report(report) + "\n" == GOLDEN.read_text(encoding="utf-8")


def test_cli_run_prints_the_golden_report(capsys):
    run(seed=SEED, snapshot_in=str(SNAPSHOT))
    assert capsys.readouterr().out == GOLDEN.read_text(encoding="utf-8")


def test_one_factor_is_selected(report):
    assert report.calibration.selected == "one"
    assert report.calibration.chosen.params.factors == 1


def test_one_factor_matches_control_markets_within_one_cent(report):
    cal = report.calibration
    assert control_miss(cal.one, cal.targets) <= FACTOR_TOLERANCE


def test_race_marginals_are_preserved_within_one_cent(report):
    fit = report.calibration.chosen
    mc = simulate_factor(fit.probs, fit.params, n=100_000, seed=SEED)
    worst = max(abs(rate - fit.probs[rid]) for rid, rate in mc.race_win_rates.items())
    assert worst <= 0.01


def test_quadrature_and_simulation_agree(report):
    fit = report.calibration.chosen
    mc = simulate_factor(fit.probs, fit.params, n=100_000, seed=SEED)
    for code, p in fit.result.combo.items():
        assert mc.combo[code] == pytest.approx(p, abs=0.01)


def test_dependence_beats_independence_on_the_calibration_targets(report):
    cal = report.calibration
    assert cal.one.loss < cal.baseline.loss / 10


@pytest.mark.parametrize("estimator", sorted(ESTIMATORS))
def test_every_estimator_replays_from_the_snapshot(estimator):
    client = SnapshotClient(SNAPSHOT)
    r = build_report(client, "snapshot", 10_000, SEED, "independent", estimator, 100)
    assert r.calibration is None
    assert sum(r.independent.combo.values()) == pytest.approx(1.0)
