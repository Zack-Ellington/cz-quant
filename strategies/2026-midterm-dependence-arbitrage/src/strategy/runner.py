"""Runner for the dependence strategy. Configuration comes from the environment.

Pipeline: acquire quotes -> save snapshot -> checks -> calibrate -> simulate ->
report -> paper trade -> paper section.

1. Acquire every quote the run needs: each race's legs, the combo, House and
   Senate control, the same-party market, the seat-count buckets, and each
   traded series' fees and contract terms.
2. With ``snapshot_out``, save the snapshot *now*, before any fitting, so a run
   that fails later still leaves its quotes on disk for replay.
3. Run the model-free checks, calibrate the latent swing (unless
   ``model="independent"``), and simulate the independent model on the same
   race probabilities.
4. Print the report, then trade on paper: settle positions whose markets have
   resolved, confirm candidates on a second scan, size them within the caps, and
   fill. The paper section is read back from the ledger.

Every run writes ``runs/<strategy-id>/<UTC timestamp>/`` (see ``ledger.py``).
Paper mode is the only mode; nothing can send an order (issue #5).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from strategy import constants
from strategy.api import Client, KalshiClient
from strategy.calibration import calibrate
from strategy.checks import run_checks
from strategy.estimators import RaceProb
from strategy.fees import DEFAULT_ACCOUNT, DEFAULT_CONTRACTS, FeeSchedule, fee_schedules
from strategy.ledger import Run, Tee, default_runs_dir, read_events, read_ledger, utc_now
from strategy.markets import AggregateMarkets, fetch_markets, traded_series
from strategy.output import paper_section
from strategy.probabilities import apply_estimator, caucus_probs, load_race_quotes
from strategy.races import discover_races
from strategy.report import Report, format_report
from strategy.simulation import OUTCOMES, simulate
from strategy.snapshot import RecordingClient, SnapshotClient
from strategy.trading import ModelView, Policy, record_scan, trade

STRATEGY_ID = "2026-midterm-dependence-arbitrage"
MODELS = ("auto", "one-factor", "two-factor", "independent")
DEFAULT_MODEL = "auto"
LIVE_CONFIRM_DELAY = 5.0  # seconds between the scan and its confirmation, live


@dataclass(frozen=True)
class Acquired:
    race_probs: dict[str, RaceProb]
    markets: AggregateMarkets
    fees: dict[str, FeeSchedule]


def acquire(client: Client, estimator: str, account: str = DEFAULT_ACCOUNT) -> Acquired:
    """Every quote the model and the checks need."""
    races = discover_races()
    race_probs = apply_estimator(races, load_race_quotes(races, client), estimator)
    markets = fetch_markets(client)
    fees = fee_schedules(client, traded_series(markets), account)
    return Acquired(race_probs, markets, fees)


def build_report(
    acquired: Acquired,
    source: str,
    simulations: int,
    seed: int | None,
    model: str,
    estimator: str,
    contracts: int,
) -> Report:
    checks = run_checks(acquired.markets, acquired.fees, contracts)
    if model == "independent":
        calibration = None
        probs = caucus_probs(acquired.race_probs, 0.0)
    else:
        calibration = calibrate(acquired.race_probs, acquired.markets, model)
        probs = calibration.chosen.probs
    return Report(
        source=source,
        estimator=estimator,
        contracts=contracts,
        checks=checks,
        markets=acquired.markets,
        fees=acquired.fees,
        independent=simulate(probs, n=simulations, seed=seed),
        calibration=calibration,
    )


def model_view(report: Report) -> ModelView:
    """The selected model's prices and uncertainty, for trading.

    The factor model's uncertainty on each outcome is how far the one- and
    two-factor fits disagree -- the part of the answer the markets do not pin
    down. The independent model's is three Monte Carlo standard errors.
    """
    cal = report.calibration
    if cal is None:
        ind = report.independent
        return ModelView(
            "independent model",
            dict(ind.combo),
            {k: 3 * ind.standard_error[k] for k in OUTCOMES},
        )
    chosen = cal.chosen.result
    return ModelView(
        f"{report.model_name} model",
        dict(chosen.combo),
        {k: abs(cal.one.result.combo[k] - cal.two.result.combo[k]) for k in OUTCOMES},
    )


def run(
    simulations: int = 100_000,
    seed: int | None = None,
    model: str = DEFAULT_MODEL,
    estimator: str = "midpoint",
    snapshot_in: str | None = None,
    snapshot_out: str | None = None,
    contracts: int = DEFAULT_CONTRACTS,
    mode: str = "paper",
    trading: bool = True,
    bankroll: Decimal = Policy.bankroll,
    max_position: Decimal = Policy.max_position,
    min_edge: Decimal = Policy.min_edge,
    runs_dir: str | Path | None = None,
    confirm_delay: float | None = None,
    clock=utc_now,
) -> Path:
    """Run the pipeline, print the report and paper section; return the run dir."""
    if model not in MODELS:
        raise ValueError(f"model must be one of {MODELS}, got {model!r}")
    if mode != "paper":
        raise SystemExit("Only paper mode exists; live orders are issue #5. Nothing was sent.")
    policy = Policy(Decimal(bankroll), Decimal(max_position), Decimal(min_edge))

    live: KalshiClient | None = None
    if snapshot_in is not None:
        client: Client = SnapshotClient(snapshot_in)
        source = f"snapshot {Path(snapshot_in).name} (captured {client.captured_at})"
        base_url = client.source
    else:
        base_url = os.environ.get("KALSHI_API_BASE_URL", constants.DEFAULT_BASE_URL)
        live = KalshiClient(base_url=base_url)
        client = live
        source = "live"
    recorder = RecordingClient(client, source=base_url) if snapshot_out is not None else None
    if recorder is not None:
        client = recorder
    if confirm_delay is None:
        confirm_delay = LIVE_CONFIRM_DELAY if live is not None else 0.0

    runs_dir = Path(runs_dir) if runs_dir is not None else default_runs_dir()
    run_ = Run.start(
        runs_dir,
        STRATEGY_ID,
        {
            "mode": mode,
            "api_base_url": base_url,
            "snapshot_in": snapshot_in,
            "snapshot_out": snapshot_out,
            "model": model,
            "estimator": estimator,
            "simulations": simulations,
            "seed": seed,
            "contracts": contracts,
            "account": DEFAULT_ACCOUNT,
            "policy": {"bankroll": policy.bankroll, "max_position": policy.max_position,
                       "min_edge": policy.min_edge, "trading": trading},
        },
        clock,
    )
    stdout, tee = sys.stdout, Tee(sys.stdout, run_.log_path)
    sys.stdout = tee
    try:
        acquired = acquire(client, estimator)
        if recorder is not None:
            recorder.save(snapshot_out)  # before any fitting
        report = build_report(acquired, source, simulations, seed, model, estimator, contracts)
        view = model_view(report)
        record_scan(run_, acquired.markets, view, source)
        print(format_report(report))
        print()
        if trading:
            trade(run_, client, acquired.markets, report.checks, view, acquired.fees, policy,
                  contracts, confirm_delay)
        rows, events = read_ledger(runs_dir, STRATEGY_ID), read_events(runs_dir, STRATEGY_ID)
        print("\n".join(paper_section(run_.label, run_.run_id, rows, events, policy.bankroll)))
        if recorder is not None:
            path = recorder.save(snapshot_out)  # adds the confirmation scan
            print(f"Saved {len(recorder.records)} quotes to {path}", file=sys.stderr)
        run_.finish("ok")
    except BaseException as exc:
        if recorder is not None and recorder.records:
            recorder.save(snapshot_out)
        run_.event("note", message=f"run failed: {type(exc).__name__}: {exc}")
        run_.finish("failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        sys.stdout = stdout
        tee.close()
        if live is not None:
            live.close()
    return run_.directory
