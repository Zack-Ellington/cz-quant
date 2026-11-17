# wcbb-favorites

Paper-buys the favored team in Kalshi women's college basketball games
(`KXNCAAWBGAME`) when its price is inside a fixed range. Paper only: the
runner refuses a live portfolio.

## Rule

Every scan (default 120 s) lists open games. For each game with two open
markets and two-sided quotes, the favorite is the team with the higher YES
midpoint. If that midpoint is in **[80%, 90%]**, the spread is **at most 2¢**, and
the portfolio has never ordered on the game, it sends one immediate-or-cancel
bid for YES at the current ask, sized to **$25** of contracts. One order per
game, ever, including after a partial fill. Settlement is booked by the shared
market monitor.

| Variable | Default | Meaning |
|---|---|---|
| `CZQUANT_PORTFOLIO` | (required) | paper portfolio on the shared ledger |
| `WCBB_LOW`, `WCBB_HIGH` | 0.80, 0.90 | favorite midpoint range, inclusive |
| `WCBB_MAX_SPREAD` | 0.02 | widest YES spread accepted |
| `WCBB_STAKE` | 25 | dollars of contracts per game |
| `WCBB_INTERVAL` | 120 | seconds between scans |

## Why this range, and the expected result

From `research/kalshi-calibration` (4,596 games, Nov 20 2025 to Nov 15 2026;
`reports-wcbb/` there). Favorites won slightly more often than priced at 70–95%,
and the gap was largest at 80–90% (+1.0 to +1.3 points). **No level is
significantly miscalibrated after the Holm correction**, and the +5.8-point gap
seen in November 2026 alone did not hold over the full season.

After paying the ask and Kalshi's taker fee, buying at the first touch lost money
at every level (−1.5 to −1.7¢ per contract at 80–90%, p < 0.01). Restricting to
1¢ spreads cut the loss to about −0.5 to −0.7¢, not significant. 80–90% with a
tight spread is the least-bad configuration, not a demonstrated edge; this
portfolio tests it forward on paper.

## Run

```bash
uv sync
cd ../../packages/czquant && uv run czquant portfolio wcbb-favorites --cash 1000 && cd -
export STRATEGY_RUNS_DIR=/absolute/runs CZQUANT_PORTFOLIO=wcbb-favorites
uv run strategy --once   # one scan
uv run strategy          # continuous
```

`scripts/experiments/install.py` writes a `czq-wcbb-favorites.service` that runs
it continuously with `CZQUANT_PORTFOLIO=wcbb-favorites`. Run the market monitor
too, or paper positions never settle.
