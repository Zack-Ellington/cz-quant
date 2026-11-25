# Reporting

Reporting reconstructs portfolio economics from the shared log and values
remaining positions using recorded market references. It does not import
strategies, contact venues, or require execution credentials. The
[design principles](../design-principles.md) define the economic schema and
comparison requirements. This guide describes the current reporting policy.

Use the [shared setup](../../README.md#shared-services) and run commands from
packages/czquant. Choose the same STRATEGY_RUNS_DIR that execution and monitors
write. The report's opening time must include the portfolios being compared;
portfolios created after that time are not included by the current evaluator.

## Generate a comparison

Set START and END to the intended UTC window in ISO 8601 form, with an offset
such as Z. Set REPORT_DIR to a new directory under the shared root for this
report. The command freezes a database snapshot and prints the metrics and
output location.

```bash
uv run czquant report --since "$START" --until "$END" --output "$REPORT_DIR"
```

The output contains metrics.csv, equity.csv, a standalone report.html,
comparison.md, accounting.sqlite3, and manifest.json with the window,
recorded-data cutoff, policies, health, and snapshot checksum. Retain a separate
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

The current renderer uses paper-specific labels, and its manifest's policy
label does not fully identify the one-sided midpoint exception or report code
revision. Retain the generating Git revision with the report and disclose that
exception when sharing comparisons. Do not present current labels as evidence
that live evaluation is complete. Live coverage is also limited by the
[market collector's inventory discovery](market-monitor.md#coverage-and-verification).

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
