# Cross-market experiment companion

This is an experiment companion, not a core reporting service. Run commands from
`scripts/cross-market-monitor/`; `uv sync --locked` installs its own environment.
It uses the shared czquant accounting library and existing ledger, without
importing the strategy or owning execution.

The game dashboard separates execution evidence from sampled price opportunities.
It reads the same ledger as the strategies. Start its public quote collector and
web server as separate processes, with STRATEGY_RUNS_DIR pointing to that ledger:

```bash
uv run python monitor.py quotes --interval 5
```

```bash
uv run python monitor.py dashboard
```

Open http://127.0.0.1:8791/. Both commands require no credentials. The collector
discovers explicit selections from recorded cross-market run configurations,
reads public Kalshi and Polymarket US books and fee schedules, and appends market
and quote observations. It does not submit orders, fund pools, or reconcile
accounts. The dashboard opens SQLite in read-only mode and refreshes every five
seconds. Existing strategy and authenticated account-monitor processes stay
independent.

The light dashboard has an Overall tab and one tab per game. Overall plots venue
cash, allocated game cash, open contract counts, and expected return over time.
Expected return divides conditional expected profit by contributed game capital.
Each game has one Plotly chart on a single cents axis. Four solid lines show
raw buy prices: dark/light blue for one team's Kalshi/Polymarket US prices,
and dark/light orange for the other team's prices. Two solid dark and light grey lines show the
maximum fee-adjusted profit per matched pair in each cross-venue direction.
For each sample, every integer size up to max_sets is priced through available
depth. Sizes must fit the recorded pools' available cash, broker fee buffers,
limit-price funding requirements, and the recorded total capital limit. The
chart selects the largest net profit per pair, breaking ties by total profit.
Hover text includes quantity, average leg costs, fees per pair, and basket profit.
A gap of zero is break-even; intervals with a positive gap in either direction have full-height green bands. The strategy's
minimum-profit hurdle and execution checks still apply. Attempts appear as small red vertical ticks on the time axis. Executed trades
appear as green ticks, dated by their first booked fill.
Gross gap is $1 minus the two purchase prices. Fee calculations use rounded taker fees and walk depth
for integer sizes up to the recorded max_sets. A sampled opportunity requires
positive net at a size funded by both venue pools and within the recorded capital
limits. Strategy edge, return, cooldown, and execution checks are separate;
an observed opportunity is not an execution recommendation or a guaranteed fill.

Windows report the first and last positive samples, sample count, and peak quoted
net. Errors and collection gaps break windows. History begins with collection;
earlier no-edge scan logs contain no leg prices. Charts grow with all collected
samples and retain zoom across refreshes. Green chart regions use the maximum
affordable gap per pair; the funded-window table reports the maximum total basket profit. Older samples are
reconstructed from their recorded books, fees, pool cash, and run configuration;
missing evidence leaves a gap instead of reusing stale books. Times are Pacific.

Trade attempts count recorded preflight/recheck refusals plus distinct submitted
baskets. Rate-limited diagnostic logs cannot establish every unrecorded retry.
Executed trades count baskets with at least one booked fill; order acknowledgements
alone do not count. Details retain actual execution prices, quantities, fees, and
repair orders. Expected profit is realized profit plus the conditional $1 payout
of complete open sets minus their FIFO cost, including fees. It is unavailable
for incomplete inventory. NFL tie and cancellation rules can invalidate the $1
assumption; quoted profits and expected profits are not realized returns.

Totals count each selected cash pool once. Venue-reported balances are shown
separately from recorded ledger cash, allocated game cash, and reserves. Polymarket
US buying power, pending deposits, bonuses, and withdrawable funds come from the
authenticated monitor's existing evidence. Missing or stale account observations
remain visible; the dashboard does not repair or reallocate balances.

Implementation: cross_market_dashboard.py, cross_market.html, and
cross_market_monitor.py in this bundle. Regression cases cover
fee rounding, depth, insufficient cash, missing samples, incomplete inventory,
actual fills, aggregate deduplication, and observation-only quote collection.

