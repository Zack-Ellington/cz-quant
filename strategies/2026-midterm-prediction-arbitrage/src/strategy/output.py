"""Render the model-vs-market comparison table.

The table is the whole point of the strategy: for each of the four control
outcomes it puts the simulation's probability next to the Kalshi combo-market
price and shows the edge (model minus market, in percentage points). A large
positive edge means the model thinks the outcome is underpriced.
"""

from __future__ import annotations

from strategy.simulation import OUTCOMES, SimulationResult

LABELS = {
    "DD": "Democrats sweep",
    "DR": "D House / R Senate",
    "RD": "R House / D Senate",
    "RR": "Republicans sweep",
}


def format_table(
    result: SimulationResult,
    combo_prices: dict[str, float | None],
    *,
    snapshot_label: str,
) -> str:
    """Return the comparison table as a string."""
    name_w = max(len(v) for v in LABELS.values())
    header_line = (
        f"{'Outcome'.ljust(name_w)}  {'Model':>7}  {'Market':>7}  {'Edge':>8}"
    )
    rule = "-" * len(header_line)

    lines = [
        "2026 Midterm - Congress balance of power",
        f"{result.n:,} simulations - snapshot {snapshot_label}",
        "",
        header_line,
        rule,
    ]
    for code in OUTCOMES:
        model = result.combo[code]
        market = combo_prices.get(code)
        lines.append(
            f"{LABELS[code].ljust(name_w)}  {_pct(model):>7}  "
            f"{_pct(market):>7}  {_edge(model, market):>8}"
        )
    lines.append(rule)
    lines.append(
        f"Democratic control:  House {_pct(result.p_house_dem)}  "
        f"Senate {_pct(result.p_senate_dem)}"
    )
    worst_se = max(result.standard_error.values())
    lines.append(f"Monte Carlo SE <= {worst_se * 100:.2f}pp")
    return "\n".join(lines)


def print_table(
    result: SimulationResult,
    combo_prices: dict[str, float | None],
    *,
    snapshot_label: str = "live",
) -> None:
    """Print the comparison table to stdout."""
    print(format_table(result, combo_prices, snapshot_label=snapshot_label))


def _pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def _edge(model: float, market: float | None) -> str:
    if market is None:
        return "n/a"
    return f"{(model - market) * 100:+.1f}pp"
