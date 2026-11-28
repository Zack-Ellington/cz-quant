# Market monitor

Market collectors retain reference prices, contract metadata, lifecycle
changes, and final payouts for independent reporting. They also settle
supported paper positions. They run outside strategy processes and continue
after strategies stop. Strategy authors may use their own market readers;
they need not call these collectors.

The current shared collectors poll REST. Their marks support valuation and
settlement, not a replay of a strategy decision. Cross-market streaming
execution owns separate authenticated subscriptions and records its input books
in scan observations. Its dashboard reads those observations without an exchange
connection. See the [strategy's execution readers](../../strategies/cross-market-polymarket/README.md#streaming-execution-quotes).

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

For Polymarket US, use its own component:

```bash
uv run czquant monitor polymarket-us --interval 60
```

It marks held paper and live markets and settles paper lots from final results.
A mapped live account also enables authenticated recovery and reconciliation;
see the [account guide](account-monitor.md#polymarket-us-recovery).

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

The Kalshi collector discovers both paper and live holdings, outstanding
orders, and cash pool watchlists. It continues marking positions after live
orders finish, but public outcomes settle only paper lots. International
Polymarket collection covers its supported paper inventory.
The Polymarket US component marks held paper and live markets; it combines
public references with authenticated recovery when a live account is mapped.
None of these discovery paths guarantees uninterrupted reference coverage.

Paper watchlists can be supplied when creating a cash pool with Store.create_pool
and its watch metadata. Held positions do not require strategy-specific
registration. A missing final payout remains an error rather than an invented
settlement. Consumed paper liquidity is updated as book levels disappear.

## Operation

Supervise each collector independently and send its stdout/stderr to a log
under STRATEGY_RUNS_DIR. Use absolute environment and key paths. Restart with
the same state directory; never recreate cash pools to recover a collector.
Keep collectors running while positions need marks or settlement evidence.
An outage leaves a historical gap even after new observations resume.

Implementation: [monitor.py](../../packages/czquant/src/czquant/monitor.py).
