"""Render the run: model-free checks, calibration, and the side-by-side table.

Three sections:

1. **Checks** -- the model-free baskets from ``checks.py`` with their cost and
   edge before and after taker fees. These need no model; a positive net edge on
   an exact check is an arbitrage.
2. **Calibration** -- the baseline (no swing), one-factor, and two-factor fits
   with their parameters, loss, and chamber-control odds against the control
   markets, and which model was selected and why.
3. **Outcomes** -- each control outcome under the independent model, the
   selected factor model, and the combo market (midpoint and bid/ask), with the
   model's edge over the midpoint and the expected value per contract of trading
   on the model after crossing the spread and paying the taker fee.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from strategy.calibration import Calibration, ModelFit
from strategy.checks import Check
from strategy.fees import FeeSchedule
from strategy.markets import AggregateMarkets, Quote
from strategy.simulation import OUTCOMES, SimulationResult

LABELS = {
    "DD": "Democrats sweep",
    "DR": "D House / R Senate",
    "RD": "R House / D Senate",
    "RR": "Republicans sweep",
}
_MODEL_NAMES = {"one": "one factor", "two": "two factor"}


@dataclass(frozen=True)
class Report:
    source: str  # "live" or a snapshot description
    estimator: str
    contracts: int
    checks: list[Check]
    markets: AggregateMarkets
    fees: Mapping[str, FeeSchedule]
    independent: SimulationResult
    calibration: Calibration | None  # None for --model independent


def format_report(report: Report) -> str:
    cal = report.calibration
    model = "independent" if cal is None else _MODEL_NAMES[cal.selected]
    lines = [
        "2026 Midterm - Congress balance of power",
        f"{report.source} - estimator {report.estimator} - model {model}",
        "",
    ]
    lines += _checks_section(report)
    if cal is not None:
        lines += [""] + _calibration_section(cal)
    lines += [""] + _outcomes_section(report)
    return "\n".join(lines)


def print_report(report: Report) -> None:
    print(format_report(report))


# --- Checks ------------------------------------------------------------------


def _checks_section(report: Report) -> list[str]:
    name_w = max((len(c.name) for c in report.checks), default=10)
    header = f"{'Check'.ljust(name_w)}  {'Cost':>6}  {'Raw':>7}  {'Net':>7}  {'Size':>7}  Verdict"
    lines = [
        f"Model-free checks (taker; edge per set in cents after fees on up to "
        f"{report.contracts} sets; size = sets available at these prices)",
        header,
        "-" * len(header),
    ]
    for c in report.checks:
        lines.append(
            f"{c.name.ljust(name_w)}  {c.cost:>6.3f}  {_cents(c.raw_edge):>7}  "
            f"{_cents(c.net_edge):>7}  {_size(c.size):>7}  {c.verdict}"
        )
    arbs = [c for c in report.checks if c.verdict == "ARB"]
    raw_only = [c for c in report.checks if c.exact and c.raw_edge > 0 and c.net_edge <= 0]
    if arbs:
        best = max(arbs, key=lambda c: c.net_edge * min(c.size, report.contracts))
        summary = (
            f"{len(arbs)} arbitrage(s) survive fees; largest: {best.name}, "
            f"{_cents(best.net_edge)} on {_size(best.size)} sets."
        )
    elif raw_only:
        summary = f"No arbitrage after fees; {len(raw_only)} exact check(s) positive before fees."
    else:
        summary = "No arbitrage before or after fees."
    lines.append(summary)
    return lines


# --- Calibration ---------------------------------------------------------------


def _calibration_section(cal: Calibration) -> list[str]:
    header = (
        f"{'Model':<16}{'sigma H':>8}{'sigma S':>8}{'caucus':>8}{'flip R/D':>13}"
        f"{'loss':>8}{'House':>8}{'Senate':>8}"
    )
    lines = [
        "Calibration: seat-count and control markets (combo held out)",
        header,
        "-" * len(header),
    ]
    for name, fit in (("no swing", cal.baseline), ("one factor", cal.one), ("two factor", cal.two)):
        lines.append(_fit_row(name, fit))
    house = cal.targets.house.control if cal.targets.house else None
    senate = cal.targets.senate.control if cal.targets.senate else None
    lines.append(f"{'control markets':<16}{'':>45}{_pct(house):>8}{_pct(senate):>8}")
    lines.append(f"Selected {_MODEL_NAMES[cal.selected]}: {cal.reason}")
    return lines


def _fit_row(name: str, fit: ModelFit) -> str:
    p = fit.params
    flips = f"{p.flip_rep * 100:.1f}%/{p.flip_dem * 100:.1f}%"
    return (
        f"{name:<16}{p.sigma_house:>8.3f}{p.sigma_senate:>8.3f}{fit.caucus_share:>8.2f}"
        f"{flips:>13}{fit.loss:>8.4f}{_pct(fit.result.p_house_dem):>8}"
        f"{_pct(fit.result.p_senate_dem):>8}"
    )


# --- Outcomes --------------------------------------------------------------------


def _outcomes_section(report: Report) -> list[str]:
    cal = report.calibration
    factor = cal.chosen.result if cal is not None else None
    name_w = max(len(v) for v in LABELS.values())
    header = (
        f"{'Outcome'.ljust(name_w)}  {'Indep':>6}  {'Factor':>6}  {'Market':>6}  "
        f"{'Bid/Ask':>11}  {'Edge':>7}  {'EV after fees':>13}"
    )
    lines = [header, "-" * len(header)]
    for code in OUTCOMES:
        quote = report.markets.combo[code]
        model_p = factor.combo[code] if factor is not None else report.independent.combo[code]
        lines.append(
            f"{LABELS[code].ljust(name_w)}  {_pct(report.independent.combo[code]):>6}  "
            f"{_pct(factor.combo[code] if factor else None):>6}  {_pct(quote.mid):>6}  "
            f"{_bid_ask(quote):>11}  {_edge(model_p, quote.mid):>7}  "
            f"{_trade(model_p, quote, report.fees, report.contracts):>13}"
        )
    lines.append("-" * len(header))
    for chamber, ind, fac, control in (
        ("House", report.independent.p_house_dem, factor.p_house_dem if factor else None,
         report.markets.house_control.dem),
        ("Senate", report.independent.p_senate_dem, factor.p_senate_dem if factor else None,
         report.markets.senate_control.dem),
    ):
        label = f"D {chamber} control"
        lines.append(
            f"{label.ljust(name_w)}  {_pct(ind):>6}  {_pct(fac):>6}  {_pct(control.mid):>6}  "
            f"{_bid_ask(control):>11}"
        )
    if factor is not None:
        h_mean, h_sd = factor.house_moments()
        s_mean, s_sd = factor.senate_moments()
        lines.append(
            f"Factor model Democratic seats: House {h_mean:.1f} +/- {h_sd:.1f}, "
            f"Senate {s_mean:.1f} +/- {s_sd:.1f}"
        )
    worst_se = max(report.independent.standard_error.values())
    lines.append(
        f"Independent: {report.independent.n:,} simulations, SE <= {worst_se * 100:.2f}pp"
    )
    return lines


def _trade(model: float, quote: Quote, fees: Mapping[str, FeeSchedule], contracts: int) -> str:
    """Expected value per contract of trading on the model, after spread and fee."""
    schedule = fees.get(quote.series, FeeSchedule())
    if quote.ask is not None and model > quote.ask:
        ev = model - quote.ask - schedule.taker_per_contract(quote.ask, contracts)
        if ev > 0:
            return f"buy {_cents(ev)}"
    if quote.bid is not None and model < quote.bid:
        price = 1.0 - quote.bid
        ev = quote.bid - model - schedule.taker_per_contract(price, contracts)
        if ev > 0:
            return f"sell {_cents(ev)}"
    return "-"


def _size(sets: float) -> str:
    return f"{sets:,.0f}" if sets >= 1 else f"{sets:.2f}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _cents(value: float) -> str:
    return f"{value * 100:+.1f}c"


def _edge(model: float, market: float | None) -> str:
    return "n/a" if market is None else f"{(model - market) * 100:+.1f}pp"


def _bid_ask(quote: Quote) -> str:
    bid = "-" if quote.bid is None else f"{quote.bid * 100:.1f}"
    ask = "-" if quote.ask is None else f"{quote.ask * 100:.1f}"
    return f"{bid}/{ask}"
