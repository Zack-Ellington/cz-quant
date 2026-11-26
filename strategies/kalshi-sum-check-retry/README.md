# Kalshi sum check: retry

The [Kalshi sum check](../kalshi_sum_check/) paper trader, with one change: what
happens when a basket fills only some of its legs.

The base strategy keeps a partial basket as it is. On 2026-10-28 that left the
Brewers–Rays Game 4 basket holding 106 Rays contracts with no Milwaukee hedge. The
Rays won, so the trade made $90.71, but a Brewers win would have lost $15.29.

This variant tries to finish the basket instead:

1. **Budget.** The completed basket may cost at most its payout (sets × $1 long,
   sets × $(n−1) short) plus `--retry-max-loss`. What the filled legs actually cost,
   fees included, comes off first.
2. **Limits.** Whatever budget is left, after estimated fees, is split evenly across
   the missing contracts. Each missing leg is bid at its intended price plus that
   share, rounded to the cent that stays inside the budget. If the budget is already
   negative, nothing is sent.
3. **Attempts.** It tries up to `--retry-attempts` times, `--retry-delay` seconds
   apart. Each try reads a fresh book and recomputes the budget from the fills so far.

With the default `--retry-max-loss 0`, the Brewers–Rays retry would have bid
Milwaukee at $0.85 again, because the basket's whole edge was $0.13. Allowing a $5
loss lifts that bid to $0.89. Raise the limit if you'd rather be hedged at a small
cost than stay exposed.

Every retry is recorded as a `basket_retry` observation with each leg's fills, the
amount retried, the dollars paid and why it stopped (`complete`, `budget` or
`attempts`). Retry fills use the basket's `trade_id`, so a completed basket settles
as `complete` and is held to the base strategy's loss halt.

## Run

Use a separate paper cash pool, so this strategy's results can be compared with
the base sum check and the unwind variant.

```bash
uv sync
uv run czquant pool sum-retry --venue kalshi --cash 1000
export STRATEGY_RUNS_DIR=... CZQUANT_PORTFOLIO=sum-retry
uv run python retry.py --preset low --feed rest
```

Every flag of the base trader works here (`--help` lists them). The engine is a
local copy of the base strategy's modules, presets and `approvals.json`; edit
them here without affecting the base strategy.

| Flag | Default | Meaning |
|---|---|---|
| `--retry-max-loss` | 0 | dollars the completed basket may lose to get hedged |
| `--retry-attempts` | 3 | tries to buy the missing legs |
| `--retry-delay` | 1 | seconds between tries |

```bash
uv run pytest -q
```
