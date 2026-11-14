"""Build observations from the cached pull and write the calibration report."""

from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .analysis import (BUCKETS, HOUR, THRESHOLDS, TTest, bucket_of, holm, mids, slope_test,
                       snapshot, touches, ttest)

PRE_CLOSE_LEAD = 6 * HOUR
SNAPSHOTS = [("24h", 24 * HOUR, 24 * HOUR), ("7d", 7 * 24 * HOUR, 48 * HOUR)]


def load(data: Path, min_volume: float) -> tuple[list[dict[str, Any]], Counter]:
    markets = json.loads((data / "qualifying.json").read_text(encoding="utf-8"))
    drops: Counter = Counter()
    rows = []
    for m in markets:
        f = data / "candles" / f"{m['ticker']}.json"
        if not f.exists():
            drops["no candle file"] += 1
            continue
        if m["volume"] < min_volume:
            drops[f"volume < {min_volume:g}"] += 1
            continue
        try:
            candles = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:  # being written by a running pull
            drops["no candle file"] += 1
            continue
        path = mids(candles, until=m["close_ts"])
        if not path:
            drops["no usable quote"] += 1
            continue
        rows.append({**m, "y": 1.0 if m["result"] == "yes" else 0.0, "path": path})
    return rows, drops


def touch_table(rows: list[dict[str, Any]], lead: int, alpha: float) -> list[tuple[float, TTest]]:
    obs: dict[float, list[tuple[float, float]]] = defaultdict(list)
    for r in rows:
        path = [(t, p) for t, p in r["path"] if t <= r["close_ts"] - lead]
        for x, (_, p) in touches(path).items():
            obs[x].append((r["y"], p))
    table = [(x, ttest([o[0] for o in obs[x]], [o[1] for o in obs[x]], alpha)) for x in THRESHOLDS]
    holm([t for _, t in table])
    return table


def snapshot_obs(rows: list[dict[str, Any]], horizon: int, max_age: int) -> list[tuple[dict[str, Any], float]]:
    out = []
    for r in rows:
        p = snapshot(r["path"], r["close_ts"] - horizon, max_age)
        if p is not None:
            out.append((r, p))
    return out


def bucket_table(obs: list[tuple[dict[str, Any], float]], alpha: float) -> list[tuple[tuple[float, float], TTest]]:
    groups: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for r, p in obs:
        groups[bucket_of(p)].append((r["y"], p))
    table = [(b, ttest([o[0] for o in groups[i]], [o[1] for o in groups[i]], alpha)) for i, b in enumerate(BUCKETS)]
    holm([t for _, t in table])
    return table


def pct(v: float, digits: int = 1) -> str:
    return "–" if v is None or math.isnan(v) else f"{100 * v:.{digits}f}%"


def num(v: float, fmt: str = ".2f") -> str:
    return "–" if v is None or math.isnan(v) else format(v, fmt)


def pval(v: float) -> str:
    if v is None or math.isnan(v):
        return "–"
    return "<0.0001" if v < 1e-4 else f"{v:.4f}"


def verdict(t: TTest, alpha: float) -> str:
    if t.n < 2 or math.isnan(t.p):
        return "too few"
    if t.significant(alpha, adjusted=True):
        return "**yes (Holm)**"
    if t.significant(alpha):
        return "yes (raw only)"
    return "no"


def ttest_rows(label_rows: list[tuple[str, TTest]], alpha: float) -> list[str]:
    ci = f"{100 * (1 - alpha):g}% CI"
    lines = [f"| Level | n | Avg price | Won | Won − price | {ci} | t | p | Holm p | Miscalibrated at {alpha:g}? |",
             "|---|---:|---:|---:|---:|---|---:|---:|---:|---|"]
    for label, t in label_rows:
        lines.append(f"| {label} | {t.n:,} | {pct(t.mean_price)} | {pct(t.win_rate)} | {pct(t.diff)} | "
                     f"{pct(t.ci_low)} to {pct(t.ci_high)} | {num(t.t)} | {pval(t.p)} | {pval(t.p_holm)} | {verdict(t, alpha)} |")
    return lines


def run(data: Path, out: Path, alpha: float, min_volume: float) -> None:
    out.mkdir(parents=True, exist_ok=True)
    meta = json.loads((data / "meta.json").read_text())
    rows, drops = load(data, min_volume)
    since = datetime.fromtimestamp(meta["since"], timezone.utc)
    last = datetime.fromtimestamp(max(r["close_ts"] for r in rows), timezone.utc)
    md: list[str] = []
    summary: dict[str, Any] = {"alpha": alpha, "since": since.isoformat(), "n_events": len(rows)}

    md += ["# How well do Kalshi prices predict two-outcome events?", "",
           f"Events closing {since:%Y-%m-%d} to {last:%Y-%m-%d}. Each event is mutually exclusive with exactly two "
           "binary markets that settled yes/no; one side per event is picked by a fixed coin flip, so each event "
           "counts once. Price = hourly YES bid/ask midpoint (spread ≤ 10¢). "
           f"Markets with under {min_volume:g} contracts traded are dropped. Tests are two-sided t-tests at "
           f"α = {alpha:g}.", "",
           f"**Events analyzed: {len(rows):,}.** Dropped: " +
           (", ".join(f"{k} {v:,}" for k, v in drops.most_common()) or "none") + ".", ""]

    cats = Counter(r["category"] for r in rows)
    md += ["| Category | Events |", "|---|---:|"] + [f"| {c} | {n:,} |" for c, n in cats.most_common()] + [""]
    series = Counter(r["series"] for r in rows)
    md += ["Largest series: " + ", ".join(f"{s} ({n:,})" for s, n in series.most_common(10)) + ".", ""]

    # --- touches ---
    for title, lead, key in [("all touches before close", 0, "touch_all"),
                             (f"touches at least {PRE_CLOSE_LEAD // HOUR}h before close", PRE_CLOSE_LEAD, "touch_pre")]:
        table = touch_table(rows, lead, alpha)
        summary[key] = [{"level": x, **vars(t)} for x, t in table]
        md += [f"## Touched a level: {title}", "",
               "A market touched 90% at the first hour its midpoint reached ≥ 90% (for levels under 50%: fell to "
               "≤ the level). *Avg price* is the midpoint at that hour; if prices are accurate, *Won* should "
               "equal it.", ""]
        md += ttest_rows([(f"{'≥' if x >= 0.5 else '≤'} {pct(x, 0)}", t) for x, t in table], alpha) + [""]

    # --- snapshots ---
    for label, horizon, max_age in SNAPSHOTS:
        obs = snapshot_obs(rows, horizon, max_age)
        table = bucket_table(obs, alpha)
        st = slope_test([r["y"] for r, _ in obs], [p for _, p in obs])
        summary[f"snap_{label}"] = {"buckets": [{"lo": b[0], "hi": b[1], **vars(t)} for b, t in table],
                                    "slope": vars(st)}
        md += [f"## Price {label} before close", "",
               f"{st.n:,} events had a quote within {max_age // HOUR}h before that point. Brier score "
               f"{num(st.brier, '.4f')} (0 = perfect, 0.25 = coin flip).", ""]
        md += ttest_rows([(f"{pct(b[0], 0)}–{pct(b[1], 0)}", t) for b, t in table], alpha) + [""]
        md += [f"Slope of outcome on price: **{num(st.slope, '.3f')}** (SE {num(st.se, '.3f')}); "
               f"t-test of slope = 1: t = {num(st.t)}, p = {pval(st.p)} → "
               + ("**significantly different from 1**" if st.p < alpha else "not significantly different from 1")
               + f" at α = {alpha:g}. Slope > 1 means favorites win more often than priced and longshots less "
               "(favorite-longshot bias); slope < 1 means prices are too extreme.", ""]

    # --- by category (24h snapshot) ---
    obs = snapshot_obs(rows, 24 * HOUR, 24 * HOUR)
    by_cat: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for r, p in obs:
        by_cat[r["category"]].append((r["y"], p))
    md += ["## By category (price 24h before close)", "",
           "| Category | n | Brier | Slope | p (slope = 1) | Significant? |", "|---|---:|---:|---:|---:|---|"]
    summary["by_category"] = []
    for cat, o in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        st = slope_test([a for a, _ in o], [b for _, b in o])
        summary["by_category"].append({"category": cat, **vars(st)})
        sig = "too few" if math.isnan(st.p) else ("yes" if st.p < alpha else "no")
        md += [f"| {cat} | {st.n:,} | {num(st.brier, '.4f')} | {num(st.slope, '.3f')} | {pval(st.p)} | {sig} |"]
    md += [""]

    (out / "calibration.md").write_text("\n".join(md), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    with (out / "observations.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["event_ticker", "ticker", "series", "category", "result", "volume", "close_utc",
                    "price_24h", "price_7d"] + [f"touch_{int(100 * x)}" for x in THRESHOLDS])
        for r in rows:
            tc = touches(r["path"])
            w.writerow([r["event_ticker"], r["ticker"], r["series"], r["category"], r["result"], r["volume"],
                        datetime.fromtimestamp(r["close_ts"], timezone.utc).isoformat(),
                        snapshot(r["path"], r["close_ts"] - 24 * HOUR, 24 * HOUR),
                        snapshot(r["path"], r["close_ts"] - 7 * 24 * HOUR, 48 * HOUR)]
                       + [round(tc[x][1], 4) if x in tc else "" for x in THRESHOLDS])
    print(f"wrote {out / 'calibration.md'}, summary.json, observations.csv ({len(rows):,} events)")
