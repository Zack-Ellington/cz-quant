# Market monitor

Market collectors retain reference prices, contract metadata, lifecycle
changes, and final payouts for independent reporting. They also settle
supported paper positions. They run outside strategy processes and continue
after strategies stop. Strategy authors may use their own market readers;
they need not call these collectors.

Use the [shared setup](../../README.md#shared-services). Run commands from
packages/czquant with the same STRATEGY_RUNS_DIR as execution. The common
record types and time semantics are in the
[design principles](../design-principles.md#event-envelope-and-economic-schema).

## Start a collector

Choose the venue component and run it in a separate process. The default
interval is 60 seconds.

```bash
uv run czquant monitor market --interval 60
```

market collects Kalshi public metadata and books. It writes market records,
paper snapshots, and health. Confirmed outcomes settle paper lots through
the broker. With no exposure to collect, it records a market-list probe.

For international Polymarket tokens, use:

```bash
uv run czquant monitor polymarket --interval 60
```

The polymarket component reads public Gamma/CLOB data and records marks and
final payouts. Neither public collector needs an account key. They collect
their own venue's tickers; running one does not cover the other venue.

For DraftKings paper bets, use the configured source:

```bash
export DRAFTKINGS_SOURCE=oddsapi
uv run czquant monitor draftkings
```

The oddsapi source needs ODDS_API_KEY. The collector reuses odds already in
the shared odds_cache directory, preserving their read times. It requests
results for eligible held games, paced by its configured result interval.
It does not independently refresh odds, so quote coverage depends on another
process refreshing that cache. See the
[sportsbook strategy's source configuration](../../strategies/cross-market-sportbook/README.md#draftkings-data).

When a supported sportsbook result needs operator evidence, the explicit
settle-sportsbook command accepts a ticker, --result yes, no, or void, and
--note describing the source. It records the result and settles paper lots.
A void returns their stake. This operation changes the paper ledger.

## Coverage and verification

Add --once to run one collection pass and exit:

```bash
uv run czquant monitor market --once
```

Read the emitted status and the health records through the
[dashboard](reporting.md#dashboard). Per-market errors produce degraded
health. An outer collector failure writes failed health and exits so a
process supervisor can restart it. A successful command exit alone does not
prove every market was collected; inspect errors and reference coverage.

The current collectors derive inventory from paper portfolios, then add
outstanding orders and paper watchlists. In particular, Kalshi positions held
after live orders finish are not discovered through a live-inventory scan.
Do not assume this collector provides complete live valuation coverage.

Paper watchlists can be supplied when creating a portfolio with Store.create
and its watch metadata. Held positions do not require strategy-specific
registration. A missing final payout remains an error rather than an invented
settlement. Consumed paper liquidity is updated as book levels disappear.

## Operation

Supervise each collector independently and send its stdout/stderr to a log
under STRATEGY_RUNS_DIR. Use absolute environment and key paths. Restart with
the same state directory; never recreate portfolios to recover a collector.
Keep collectors running while positions need marks or settlement evidence.
An outage leaves a historical gap even after new observations resume.

Implementation: [monitor.py](../../packages/czquant/src/czquant/monitor.py).
