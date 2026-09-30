"""Paper P&L across strategies, from the ``runs/`` ledgers.

    uv run pnl csv    [--runs-dir DIR] [-o FILE]                   every ledger row, one CSV
    uv run pnl report [--strategies A,B] [--since D] [--until D]   one line per strategy
    uv run pnl plot   [--strategies A,B] [--since D] [--until D] [-o pnl.html]
                                                                   P&L and return over time

The input is the accounting ledger every strategy writes: one flat row per
cash flow or mark in ``runs/<strategy>/<run>/ledger.jsonl`` (the schema is in
each strategy's ``ledger.py``). The rows of every run of every strategy
concatenate into one table, and that table is the only thing this tool reads.

Slicing: ``--strategies`` keeps the listed strategy ids (default: all).
``--since`` and ``--until`` (dates, inclusive) select **positions by entry
time**, the time of their first fill; every later row of a selected position
(marks, settlement) is kept, and the series stops at the end of ``--until``.

Transforms, all from the sliced rows, evaluated at the end of every run:

* capital committed = -sum of fill cash deltas (cost plus entry fees);
* cash = sum of fill and settlement cash deltas;
* value = quantity x latest mark price over every leg not yet settled
  (missing if an open leg has never been marked);
* P&L = cash + value;
* return on committed capital = P&L / capital committed.

``report`` prints the latest point per strategy. ``plot`` writes an HTML chart
of P&L and return over time for the selected strategies, and the same series
as CSV next to it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

COLUMNS = ["ts", "run_id", "strategy", "type", "position_id", "basket", "ticker", "side",
           "quantity", "price", "fee", "cash_delta", "simulated"]
SERIES = ["strategy", "ts", "run_id", "positions", "open", "capital", "cash", "value", "pnl", "return"]


# --- Load and slice --------------------------------------------------------------


def repo_root() -> Path:
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                             check=True, cwd=Path(__file__).resolve().parent).stdout.strip()
        return Path(out)
    except (OSError, subprocess.CalledProcessError):
        return Path(__file__).resolve().parents[1]


def load(runs_dir: Path) -> pd.DataFrame:
    """Every ledger row under ``runs_dir``, one DataFrame, sorted by time."""
    records = []
    for path in sorted(Path(runs_dir).glob("*/*/ledger.jsonl")):
        with path.open(encoding="utf-8") as handle:
            records.extend(json.loads(line) for line in handle if line.strip())
    df = pd.DataFrame.from_records(records, columns=COLUMNS)
    df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
    for col in ("price", "fee", "cash_delta"):
        df[col] = pd.to_numeric(df[col])
    df["quantity"] = df["quantity"].astype("int64")
    return df.sort_values(["ts", "run_id"], kind="stable").reset_index(drop=True)


def select(df: pd.DataFrame, strategies: list[str] | None = None, since: date | None = None,
           until: date | None = None) -> pd.DataFrame:
    """Rows of the chosen strategies whose position was entered in [since, until]."""
    if strategies:
        df = df[df["strategy"].isin(strategies)]
    entered = df[df["type"] == "fill"].groupby("position_id")["ts"].min()
    if since is not None:
        entered = entered[entered >= _start(since)]
    if until is not None:
        entered = entered[entered < _start(until) + timedelta(days=1)]
    df = df[df["position_id"].isin(entered.index)]
    if until is not None:
        df = df[df["ts"] < _start(until) + timedelta(days=1)]
    return df.reset_index(drop=True)


def _start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


# --- Transform ------------------------------------------------------------------


def point(rows: pd.DataFrame) -> dict:
    """Capital, cash, value, P&L, and return of one strategy's rows up to a time."""
    fills = rows[rows["type"] == "fill"]
    settled = set(rows.loc[rows["type"] == "settlement", "position_id"])
    capital = -fills["cash_delta"].sum()
    cash = rows.loc[rows["type"] != "mark", "cash_delta"].sum()
    open_legs = fills[~fills["position_id"].isin(settled)][["position_id", "ticker", "side", "quantity"]]
    marks = (rows[rows["type"] == "mark"]
             .drop_duplicates(["position_id", "ticker", "side"], keep="last")[["position_id", "ticker", "side", "price"]])
    legs = open_legs.merge(marks, on=["position_id", "ticker", "side"], how="left")
    value = float("nan") if legs["price"].isna().any() else float((legs["quantity"] * legs["price"]).sum())
    pnl = cash + value
    return {
        "positions": int(fills["position_id"].nunique()),
        "open": int(open_legs["position_id"].nunique()),
        "capital": float(capital),
        "cash": float(cash),
        "value": value,
        "pnl": float(pnl),
        "return": float(pnl / capital) if capital else float("nan"),
    }


def series(df: pd.DataFrame) -> pd.DataFrame:
    """One point per strategy per run, at the run's last row."""
    out = []
    for strategy, rows in df.groupby("strategy", sort=True):
        ends = rows.groupby("run_id")["ts"].max().sort_values()
        for run_id, ts in ends.items():
            out.append({"strategy": strategy, "ts": ts, "run_id": run_id, **point(rows[rows["ts"] <= ts])})
    return pd.DataFrame(out, columns=SERIES)


def report(df: pd.DataFrame) -> pd.DataFrame:
    """The latest point per strategy."""
    s = series(df)
    if s.empty:
        return s
    return s.sort_values("ts").groupby("strategy").tail(1).sort_values("strategy").reset_index(drop=True)


# --- Plot -----------------------------------------------------------------------

# Categorical slots in fixed order (see the workspace dataviz palette); a strategy
# keeps its slot by sorted name, so filtering never repaints the survivors.
SLOTS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SURFACE, INK, MUTED, GRID = "#fcfcfb", "#52514e", "#898781", "#e1e0d9"


def plot(s: pd.DataFrame, all_strategies: list[str], title: str) -> str:
    """Two panels, P&L in dollars and return on committed capital, one line per strategy."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    if len(all_strategies) > len(SLOTS):
        raise SystemExit(f"at most {len(SLOTS)} strategies per plot; facet or filter with --strategies")
    color = {name: SLOTS[i] for i, name in enumerate(sorted(all_strategies))}
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.12,
                        subplot_titles=("P&L, dollars", "Return on committed capital"))
    for name, rows in s.groupby("strategy", sort=True):
        custom = rows[["capital", "cash", "value", "open", "run_id"]].to_numpy()
        common = dict(name=name, legendgroup=name, mode="lines+markers",
                      line=dict(color=color[name], width=2, shape="linear"),
                      marker=dict(size=8, color=color[name], line=dict(color=SURFACE, width=2)),
                      customdata=custom)
        fig.add_trace(go.Scatter(x=rows["ts"], y=rows["pnl"],
                                 hovertemplate="P&L $%{y:,.2f}<br>capital $%{customdata[0]:,.2f}"
                                 "<br>cash $%{customdata[1]:,.2f}<br>value $%{customdata[2]:,.2f}"
                                 "<br>open %{customdata[3]}<br>run %{customdata[4]}<extra>%{fullData.name}</extra>",
                                 **common), row=1, col=1)
        fig.add_trace(go.Scatter(x=rows["ts"], y=rows["return"], showlegend=False,
                                 hovertemplate="return %{y:.1%}<extra>%{fullData.name}</extra>", **common),
                      row=2, col=1)
    axis = dict(showgrid=True, gridcolor=GRID, gridwidth=1, zeroline=True, zerolinecolor=GRID,
                zerolinewidth=1, linecolor=GRID, tickfont=dict(color=MUTED))
    fig.update_xaxes(**axis)
    fig.update_yaxes(**axis)
    fig.update_yaxes(tickprefix="$", tickformat=",.0f", row=1, col=1)
    fig.update_yaxes(tickformat=".0%", row=2, col=1)
    fig.update_layout(title=dict(text=title, font=dict(color=INK, size=16)), template="none",
                      paper_bgcolor=SURFACE, plot_bgcolor=SURFACE, font=dict(color=INK, size=12),
                      hovermode="x unified", legend=dict(orientation="h", y=1.08, x=0),
                      margin=dict(l=70, r=30, t=100, b=50), height=720)
    fig.update_annotations(font=dict(color=INK, size=13))
    return fig.to_html(include_plotlyjs="cdn", full_html=True)


# --- Command line ---------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pnl", description=__doc__.split("\n\n", 1)[1].split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--runs-dir", default=None, help="runs directory (default: <repo>/runs)")

    def window(p):
        p.add_argument("--strategies", default=None, help="comma-separated strategy ids (default: all)")
        p.add_argument("--since", type=date.fromisoformat, default=None, help="first entry date, YYYY-MM-DD")
        p.add_argument("--until", type=date.fromisoformat, default=None, help="last entry date, YYYY-MM-DD")

    csv = sub.add_parser("csv", help="every ledger row of every strategy as one CSV")
    common(csv)
    csv.add_argument("-o", "--output", default=None, help="CSV file (default: stdout)")
    rep = sub.add_parser("report", help="latest capital, cash, value, P&L, and return per strategy")
    common(rep); window(rep)
    plt = sub.add_parser("plot", help="P&L and return over time, HTML plus the series as CSV")
    common(plt); window(plt)
    plt.add_argument("-o", "--output", default="pnl.html", help="HTML file (default: pnl.html)")
    args = parser.parse_args(argv)

    runs_dir = Path(args.runs_dir) if args.runs_dir else repo_root() / "runs"
    df = load(runs_dir)
    if args.command == "csv":
        df.to_csv(args.output or sys.stdout, index=False)
        return
    strategies = args.strategies.split(",") if args.strategies else None
    chosen = select(df, strategies, args.since, args.until)
    if args.command == "report":
        table = report(chosen)
        if table.empty:
            print("no positions selected")
            return
        with pd.option_context("display.float_format", "{:,.4f}".format, "display.width", 200):
            print(table.to_string(index=False))
        return
    s = series(chosen)
    out = Path(args.output)
    names = strategies or sorted(df["strategy"].unique())
    window_text = f"{args.since or 'start'} to {args.until or 'now'}"
    out.write_text(plot(s, names, f"Paper P&L by strategy, positions entered {window_text}"), encoding="utf-8")
    s.to_csv(out.with_suffix(".csv"), index=False)
    print(f"wrote {out} and {out.with_suffix('.csv')}: {len(s)} point(s), {s['strategy'].nunique()} strategy(ies)")


if __name__ == "__main__":
    main()
