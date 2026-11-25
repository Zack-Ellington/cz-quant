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
write. The report's opening time must include the portfolios being compared;
portfolios created after that time are not included by the current evaluator.

## Portfolio lifetime and reporting identity

A portfolio is a persistent accounting identity, not a process run or a broker.
The cross-market strategy uses one portfolio with two brokers, one per venue.
Both brokers record the same portfolio ID and run ID. Live cash is accounted
for separately by venue; paper execution currently shares one hypothetical
cash pool across both brokers. Two broker objects do not double the funding.

Create a paper portfolio once, from packages/czquant with STRATEGY_RUNS_DIR
set to a persistent directory outside the checkout:

```bash
uv run czquant portfolio cross-market-paper --cash 1000
```

This records initial funding. Repeating the creation command fails rather than
resetting or refunding the portfolio. Live portfolios are created through
map-live or map-live-cross, which record the mapped accounts and their opening
cash; see the [live mapping guide](polymarket-broker.md#us-live-mapping-and-execution).
Do not first create a paper portfolio for a live mapping.

In each strategy shell, use the same STRATEGY_RUNS_DIR and set
CZQUANT_PORTFOLIO to the existing ID. Starting the strategy creates a new run
ID and records its revision and configuration. It reconstructs existing cash,
positions, fees, and losses, and retains outstanding orders and reservations.
Use a new portfolio ID when an experiment needs separate accounting; changing
the run ID alone does not isolate its economics.

The shared root contains accounting.sqlite3. Its portfolios table stores
identity, mode, creation time, and account metadata. Its events table stores
funding, economic transactions, execution evidence, and run provenance. Its
orders table retains requests, context, status, fills, and reservations;
checkpoints retain collector progress. Cash and positions are reconstructed
from economic events. Keep this root across code updates and process restarts;
use the full database [backup](#backup-and-restore) for recovery.

Reports group by portfolio, combining both venues and every run contributing
to the evaluation window. Earlier events establish opening cash and positions;
window events determine subsequent cash flows and holdings. Run IDs remain
available for attribution, but the current comparison does not produce separate
per-run or per-venue performance rows. Reusing a portfolio across different
strategy revisions combines their performance in that portfolio's history.

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

The output contains metrics.csv, equity.csv, a standalone report.html,
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

Open http://127.0.0.1:8790/. The read-only dashboard streams portfolio cash,
P&L, fees, positions, coverage, monitor health, and recorded halts. It uses the
report's valuation rules and retains unmarked cost separately. No venue
credentials or requests are needed. --host and --port change the binding;
--interval controls refresh and --step controls the history grid.

The dashboard reads the common store and uses a rebuildable replay cache
under the shared root. It is an operational view, not the frozen comparison
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
