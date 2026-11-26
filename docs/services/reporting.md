# Reporting

Reporting reconstructs portfolio economics from the shared log and values
remaining positions using recorded market references. It does not import
strategies, contact venues, or require execution credentials. The
[design principles](../design-principles.md) define the economic schema and
comparison requirements. This guide describes the current reporting policy.

The shared package provides the dashboard and frozen comparison reports.
Strategies record executions and optional diagnostics; they do not host
reporting views or maintain parallel performance ledgers. Reports and dashboards
continue to work after a strategy stops or its source is removed.

Use the [shared setup](../../README.md#shared-services) and run commands from
packages/czquant. Choose the same STRATEGY_RUNS_DIR that execution and monitors
write. The report's opening time must follow creation of every pool being compared;
later-created pools are not included by the current evaluator.

## Cash pools, portfolios, and runs

A cash pool owns the persistent cash, positions, orders and reservations for
one venue. A broker binds to that pool. A portfolio only groups existing pools
for reporting; creating one neither funds anything nor changes buying power.
The cross-market strategy uses two cash pools and two brokers in both modes.

From packages/czquant, create paper pools once with explicit venue funding:

```bash
uv run czquant pool cross-kalshi --venue kalshi --cash 600
uv run czquant pool cross-polymarket --venue polymarket-us --cash 400
```

These amounts are hypothetical and can be arbitrarily large. The Kalshi pool
can spend only its $600, and the Polymarket pool only its $400. Both use the
same budget and reservation rules as live pools. Repeating either creation
fails instead of resetting or refunding it. Live pools are created by map-live;
see the [live mapping guide](polymarket-broker.md#us-live-mapping-and-execution).

Create an optional portfolio to compare the combined experiment:

```bash
uv run czquant portfolio cross-market --pools cross-kalshi cross-polymarket
```

Membership is a flat, fixed list of distinct pool IDs in one mode. Multiple
pools at the same venue can be included. This selection can be defined after
trading; its report still reconstructs the members' histories. A report window
must start after all selected pools were created. Create a new portfolio ID
for a different selection. Portfolio totals overlap their constituent pool
rows; never sum all output rows as though they were independent holdings.

Reuse the same STRATEGY_RUNS_DIR and pool IDs on every strategy restart. The
cross-market strategy selects them through the portfolio venue-to-pool mapping
in its --experiment JSON file. Each invocation records a new run ID and revision;
existing cash, positions, fees, losses, orders and reservations persist.
Separate experiments need separate pools; a new run or reporting group does
not create an independent allocation.

The shared root contains accounting.sqlite3. For storage compatibility,
portfolios retains the ownership records and events/orders retain the
portfolio_id column; for new data these identify a cash pool. cash_pools binds
those identities to a venue. reporting_portfolios holds reporting-only
membership. Events reconstruct funding, positions and economic performance;
orders retain execution status and reservations, and checkpoints retain
collector progress. Keep this root across updates and restarts. Use a full
[database backup](#backup-and-restore) for recovery.

Reports contain pool rows and portfolio totals, identified by kind and member
pool IDs. They show cash, reservations, equity, fees and performance. Available
cash is meaningful only for a pool; a portfolio's total cash is not venue buying
power. Positions remain attributed to their owning pool. Earlier events establish
opening balances, and all runs contributing to the window remain included.
Returns and drawdown use the combined equity curve and funding boundaries;
percentages from individual pools are not added or averaged. Allocations inside
a reporting portfolio cancel as capital flows, while fees remain costs.

## Deprecated pooled-capital records

Historical portfolios without a venue cash-pool binding remain readable by
reports and the dashboard. They are labelled legacy and deprecated, with a
deprecation entry in the report manifest. Their original funding and execution
facts are preserved; reporting does not invent a historical venue cash split.
They cannot be reopened as execution brokers. Use explicitly funded new paper
pools for new trials. Live mapping requires reconciled venue allocations and
the account conditions in the venue guide. Existing immutable report artifacts
remain valid records of the policy that generated them.

The previous portfolio creation command with --cash and the cross-market
CZQUANT_PORTFOLIO setting are replaced by explicit pool creation and selection.
The Python Store.create spelling remains a deprecated single-venue helper for
older callers; new integrations use Store.create_pool with an explicit venue.
There is no scheduled removal date for historical report support.

## Generate a comparison

Set START and END to the intended UTC window in ISO 8601 form, with an offset
such as Z. Set REPORT_DIR to a new directory under the shared root for this
report. The command freezes a database snapshot and prints the metrics and
output location.

For example, select an archive directory beneath the shared root:

```bash
export REPORT_DIR="$STRATEGY_RUNS_DIR/reports/comparison-001"
```

```bash
uv run czquant report --since "$START" --until "$END" --output "$REPORT_DIR"
```

The output contains metrics.csv, equity.csv, pool-owned positions.csv, a standalone report.html,
comparison.md, accounting.sqlite3, and manifest.json with the window,
recorded-data cutoff, policies, health, source-module hashes, and snapshot checksum. Retain a separate
output directory for each report or restatement. The cutoff does not liquidate
positions or stop collectors.

Replay includes earlier economic history to establish opening cash and
holdings. Evaluation uses a common one-minute grid plus funding boundaries.
Metrics include net P&L, realized and unrealized components, return on opening
equity for fixed-capital periods, time-weighted return, flow-adjusted sampled
drawdown, utilization, fees, rewards, filled notional, and evidence coverage.
Net P&L is ending equity minus opening equity minus net external funding.

## Valuation policy

The implementation uses FIFO cost basis with entry fees included. A two-sided
binary book is marked at the midpoint of the best YES bid and implied YES ask.
NO values use the complement. Books older than 120 seconds, crossed books,
and absent or invalid references cannot supply a valid mark. A confirmed final
payout remains usable without a live quote.

Quote ranges use the best YES bid as the lower value, or zero if that side is
empty, and the implied YES ask as the upper value, or one if that side is
empty. NO uses the complementary range. Fresh one-sided books normally have a
range without a midpoint; when the range is at most $0.02 wide, the current
policy uses its middle as a point estimate. Both sides empty give the full
binary range. Stale references invalidate the range too.

Portfolio ranges sum cash and actual holdings. P&L ranges account for the
opposite opening bound and external flows. They are quote envelopes, not
executable liquidation values or guaranteed payouts. They do not model exit
fees or available liquidation depth. Missing midpoints prevent complete-period
point-return and drawdown ranking; realized results remain separately visible.

Metrics and comparison rows identify each portfolio's paper or live mode.
The manifest identifies the one-sided midpoint rule and hashes the report,
accounting, book, and store source modules. Retain the generating checkout and
environment alongside those hashes to reproduce the implementation.

A live comparison also requires a healthy account observation no older than
120 seconds at its cutoff for every mapped venue. A Kalshi and Polymarket US
portfolio therefore needs both account components. A missing or degraded
component makes complete-period returns unavailable even when public marks
are present. Per-component health coverage and
[market-reference coverage](market-monitor.md#coverage-and-verification)
remain separate measurements.

## Dashboard

```bash
uv run czquant dashboard
```

Open http://127.0.0.1:8790/. The read-only dashboard streams pool and portfolio cash,
P&L, fees, positions, coverage, monitor health, and recorded halts. It uses the
report's valuation rules and retains unmarked cost separately. No venue
credentials or requests are needed. --host and --port change the binding;
--interval controls refresh and --step controls the history grid.

The dashboard reads the common store and uses a rebuildable replay cache
under the shared root. Paper and live pools are computed by the same replay and metrics; differences
should arise only from execution evidence and recorded fill assumptions. It is
an operational view, not the frozen comparison
artifact produced by the report command. A heartbeat indicates activity,
not profitability or complete evidence.

## Export

Create an output directory first. Export produces the common JSONL event
envelope, including economic facts and observations:

```bash
mkdir -p "$STRATEGY_RUNS_DIR/exports"
uv run czquant export --output "$STRATEGY_RUNS_DIR/exports/events.jsonl"
```

This export does not contain every operational table needed to resume
execution. Use a database backup for recovery.

## Backup and restore

SQLite's backup API makes a consistent copy while writers are active. Set
BACKUP_DIR to a new private directory under the shared root:

```bash
mkdir -p "$BACKUP_DIR"
uv run czquant backup --output "$BACKUP_DIR/accounting.sqlite3"
```

Retain a checksum and the code/environment revision alongside the backup.
Archive the backup, required immutable artifacts, and operational metadata
in private storage. Do not copy an open database/WAL pair as a substitute for
the backup API. Raw account evidence can be present in the database and
export even when a report is focused on paper portfolios.

Restore into a separate directory, verify the checksum and SQLite integrity,
then point reporting at it. Set RESTORE_DIR to that directory and
RESTORED_REPORT_DIR to a different output directory:

```bash
uv run czquant --runs-dir "$RESTORE_DIR" report \
  --since "$START" --until "$END" --output "$RESTORED_REPORT_DIR"
```

Offline reporting needs no account credentials. Resuming execution also
requires the recorded portfolio identities, operational tables, and compatible
code/configuration. Stop writers before replacing active state. Preserve
losses, outstanding orders, and historical reference gaps across recovery.
Share only reviewed report artifacts that contain no private account evidence.

Implementation: [report.py](../../packages/czquant/src/czquant/report.py),
[dashboard.py](../../packages/czquant/src/czquant/dashboard.py), and
[store.py](../../packages/czquant/src/czquant/store.py).
