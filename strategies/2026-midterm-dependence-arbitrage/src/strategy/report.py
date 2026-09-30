"""The dependence strategy's report: checks, calibration, and the outcome table.

Three sections before the paper section:

1. **Checks** -- the model-free baskets (``output.checks_section``).
2. **Calibration** -- the no-swing, one-factor, and two-factor fits: parameters,
   loss, and chamber-control odds against the control markets, and which model
   was selected and why.
3. **Outcomes** -- each control outcome under the independent model, the
   selected factor model, and the combo market, with the model's edge over the
   midpoint and the expected value per contract of trading on the model at the
   quantity the book offers, after the taker fee.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from strategy.calibration import Calibration, ModelFit
from strategy.checks import Check
from strategy.fees import FeeSchedule
from strategy.markets import AggregateMarkets
from strategy.output import LABELS, bid_ask, checks_section, edge, ev_text, pct
from strategy.simulation import OUTCOMES, SimulationResult

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

    @property
    def model_name(self) -> str:
        return "independent" if self.calibration is None else _MODEL_NAMES[self.calibration.selected]


def format_report(report: Report) -> str:
    lines = [
        "2026 Midterm - Congress balance of power",
        f"{report.source} - estimator {report.estimator} - model {report.model_name}",
        "",
    ]
    lines += checks_section(report.checks, report.contracts)
    if report.calibration is not None:
        lines += [""] + _calibration_section(report.calibration)
    lines += [""] + _outcomes_section(report)
    return "\n".join(lines)


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
    lines.append(f"{'control markets':<16}{'':>45}{pct(house):>8}{pct(senate):>8}")
    lines.append(f"Selected {_MODEL_NAMES[cal.selected]}: {cal.reason}")
    return lines


def _fit_row(name: str, fit: ModelFit) -> str:
    p = fit.params
    flips = f"{p.flip_rep * 100:.1f}%/{p.flip_dem * 100:.1f}%"
    return (
        f"{name:<16}{p.sigma_house:>8.3f}{p.sigma_senate:>8.3f}{fit.caucus_share:>8.2f}"
        f"{flips:>13}{fit.loss:>8.4f}{pct(fit.result.p_house_dem):>8}"
        f"{pct(fit.result.p_senate_dem):>8}"
    )


def _outcomes_section(report: Report) -> list[str]:
    cal = report.calibration
    factor = cal.chosen.result if cal is not None else None
    name_w = max(len(v) for v in LABELS.values())
    header = (
        f"{'Outcome'.ljust(name_w)}  {'Indep':>6}  {'Factor':>6}  {'Market':>6}  "
        f"{'Bid/Ask':>11}  {'Edge':>7}  {'EV after fees':>17}"
    )
    lines = [header, "-" * len(header)]
    for code in OUTCOMES:
        quote = report.markets.combo[code]
        model_p = factor.combo[code] if factor is not None else report.independent.combo[code]
        lines.append(
            f"{LABELS[code].ljust(name_w)}  {pct(report.independent.combo[code]):>6}  "
            f"{pct(factor.combo[code] if factor else None):>6}  {pct(quote.mid):>6}  "
            f"{bid_ask(quote):>11}  {edge(model_p, quote.mid):>7}  "
            f"{ev_text(model_p, quote, report.fees, report.contracts):>17}"
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
            f"{label.ljust(name_w)}  {pct(ind):>6}  {pct(fac):>6}  {pct(control.mid):>6}  "
            f"{bid_ask(control):>11}"
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
