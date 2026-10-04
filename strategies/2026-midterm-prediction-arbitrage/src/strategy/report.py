"""The independent strategy's report: checks and the outcome table.

Two sections before the paper section:

1. **Checks** -- the model-free baskets (``output.checks_section``).
2. **Outcomes** -- each control outcome under the independent model (every race
   drawn on its own) and the combo market, with the model's edge over the
   midpoint and the expected value per contract of trading on the model at the
   quantity the book offers, after the taker fee.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from strategy.checks import Check
from strategy.fees import FeeSchedule
from strategy.markets import AggregateMarkets
from strategy.output import LABELS, bid_ask, checks_section, edge, ev_text, pct
from strategy.simulation import OUTCOMES, SimulationResult


@dataclass(frozen=True)
class Report:
    source: str  # "live" or a snapshot description
    estimator: str
    contracts: int
    checks: list[Check]
    markets: AggregateMarkets
    fees: Mapping[str, FeeSchedule]
    independent: SimulationResult

    @property
    def model_name(self) -> str:
        return "independent"


def format_report(report: Report) -> str:
    lines = [
        "2026 Midterm - Congress balance of power",
        f"{report.source} - estimator {report.estimator} - model {report.model_name}",
        "",
    ]
    lines += checks_section(report.checks, report.contracts)
    lines += [""] + _outcomes_section(report)
    return "\n".join(lines)


def _outcomes_section(report: Report) -> list[str]:
    ind = report.independent
    name_w = max(len(v) for v in LABELS.values())
    header = (
        f"{'Outcome'.ljust(name_w)}  {'Model':>6}  {'Market':>6}  "
        f"{'Bid/Ask':>11}  {'Edge':>7}  {'EV after fees':>17}"
    )
    lines = [header, "-" * len(header)]
    for code in OUTCOMES:
        quote = report.markets.combo[code]
        lines.append(
            f"{LABELS[code].ljust(name_w)}  {pct(ind.combo[code]):>6}  {pct(quote.mid):>6}  "
            f"{bid_ask(quote):>11}  {edge(ind.combo[code], quote.mid):>7}  "
            f"{ev_text(ind.combo[code], quote, report.fees, report.contracts):>17}"
        )
    lines.append("-" * len(header))
    for chamber, p, control in (
        ("House", ind.p_house_dem, report.markets.house_control.dem),
        ("Senate", ind.p_senate_dem, report.markets.senate_control.dem),
    ):
        label = f"D {chamber} control"
        lines.append(f"{label.ljust(name_w)}  {pct(p):>6}  {pct(control.mid):>6}  {bid_ask(control):>11}")
    worst_se = max(ind.standard_error.values())
    lines.append(f"Independent model: {ind.n:,} simulations, SE <= {worst_se * 100:.2f}pp")
    return lines
