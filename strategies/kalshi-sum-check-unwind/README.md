# Kalshi sum check: unwind

The [Kalshi sum check](../kalshi_sum_check/) paper trader, with one change: what
happens when a basket fills only some of its legs.

The base strategy keeps a partial basket as it is. On 2026-10-28 that left the
Brewers–Rays Game 4 basket holding 106 Rays contracts with no Milwaukee hedge. The
Rays won, so the trade made $90.71, but a Brewers win would have lost $15.29.

This variant sells the exposure back straight away:

1. **The complete sets are kept.** That is the smallest fill across the legs. They
   still pay out like a full basket.
2. **Every contract above that is sold** with reduce-only IOC orders. A YES leg sells
   no lower than its break-even price (entry plus fee) minus `--unwind-slippage`. A
   NO leg buys YES back no higher than its break-even YES price plus the slippage.
3. **It tries up to `--unwind-attempts` times**, `--unwind-delay` seconds apart, and
   each try reads a fresh book. Anything still unsold stays open and settles normally.

Every unwind is recorded as a `basket_unwind` observation that lists, per leg, the
excess contracts and how many were sold. The selling legs are fills on the same
`trade_id`, so the shared report and dashboard show the unwind's cost in realized P&L.

## Run

Use a separate paper cash pool, so this strategy's results can be compared with
the base sum check and the retry variant.

```bash
uv sync
uv run czquant pool sum-unwind --venue kalshi --cash 1000
export STRATEGY_RUNS_DIR=... CZQUANT_PORTFOLIO=sum-unwind
uv run python unwind.py --preset low --feed rest
```

Every flag of the base trader works here (`--help` lists them). The engine is a
local copy of the base strategy's modules, presets and `approvals.json`; edit
them here without affecting the base strategy.

| Flag | Default | Meaning |
|---|---|---|
| `--unwind-slippage` | 0.10 | worst price per contract below break-even when selling back |
| `--unwind-attempts` | 3 | tries to sell back the unhedged legs |
| `--unwind-delay` | 1 | seconds between tries |

## Trade-off

Unwinding turns a coin-flip outcome into a small, nearly certain loss: the
spread plus a second taker fee. Over many partial fills it should cost far less
than leaving them open, but any single case can look worse than holding, as the
Brewers–Rays trade would have.

```bash
uv run pytest -q
```
