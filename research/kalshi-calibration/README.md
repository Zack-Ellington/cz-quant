# kalshi-calibration

How accurately do Kalshi prices predict outcomes? This study takes every
**two-outcome event** (mutually exclusive, exactly two binary markets, e.g.
Team A wins / Team B wins) that closed in the last six months and checks
whether markets priced at X% won X% of the time.

Public market data only; no API key needed.

## Run

```bash
uv sync
uv run calibration pull      # resumable; rerun to retry failures
uv run calibration analyze   # writes reports/
```

`pull` options: `--months 6`, `--rate 3` (max requests/second; Kalshi allows about 3/s without an API key, and the client slows itself down on 429s), `--workers 4`.
`analyze` options: `--alpha 0.02`, `--min-volume 100`.

The window is fixed in `data/meta.json` on the first pull, so reruns resume the
same window. Delete `data/` to start over.

## Method

**Sample.** All 16k series are scanned for settled events with a market closing
in the window. An event qualifies if Kalshi flags it mutually exclusive, it has
exactly two binary markets, both settled yes/no, and both closed in the window.
Markets settled before Kalshi's historical cutoff come from the
`/historical` endpoints. Archived markets are fetched per series only if
the series' live events include two-market events (or it has no live events).

**One side per event.** The two markets of an event are near-mirror images,
so counting both would double the sample and make the 10% and 90% results
mirror each other. A fixed coin flip (hash of the event ticker) picks one.

**Price.** Hourly YES bid/ask midpoint at each candle's close. Hours with an
empty book or a spread wider than 10¢ are skipped.

**Touched a level.** For a level X ≥ 50%, the first hour the midpoint was at or
above X (below 50%: at or below X). The forecast recorded is the midpoint at
that hour, not X, since hourly data can jump past a level. If prices are
accurate, the win rate among markets that touched X equals the average
recorded price. This is reported for all touches and for touches at least 6h
before close (to exclude prices set after the result was known).

**Price at a fixed time.** The midpoint 24h and 7d before close, bucketed by
10-point price ranges.

**Significance (α = 0.02, two-sided).**

- Each level/bucket: one-sample t-test that the mean of (outcome − price) is 0.
  Holm-adjusted p-values are also shown, since ~19 tests at 2% will produce
  false positives by chance.
- Overall: OLS slope of outcome on price, with robust (HC1) standard errors,
  t-tested against 1. Slope > 1 = favorite-longshot bias; < 1 = prices too extreme.

## Caveats

- Events in a series (e.g. daily games) can share shocks, so the t-tests,
  which assume independent observations, may overstate significance somewhat.
- The 7d and 6h-before-close views only include events that were listed and
  quoted that early.
- Close time is used as the reference clock; some markets close well after the
  outcome is effectively known.

## Follow-up scripts

Run from this directory after `calibration analyze`:

- `uv run python scripts/groups.py reports/observations.csv` — gap by sport family and series vs. the rest (Welch t-test, Holm). Output: `reports/groups.txt`.
- `uv run python scripts/spread.py <obs.csv>` — whether the gap beats the bid-ask spread and Kalshi's taker fee at each touch level; caches per-observation bid/ask to `<obs.csv>`. Output: `reports/spread.txt`.
- `uv run python scripts/tight.py <obs.csv>` — same, restricted to 1¢-spread quotes.

`reports/observations.csv` (~20 MB) is not committed; `calibration analyze` regenerates it.
