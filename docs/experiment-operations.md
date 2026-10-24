# Experiment operations

The rollout uses four independent paper portfolios with $1,000 each and a
$1,000 cash reference. No real order or transfer is part of the rollout.
The two sum presets run continuously. The two election strategies run every
15 minutes with seed 20261024. Separate market and authenticated account
services continue after strategy processes stop.

The trial state directory is outside the worktree. Its trial.json records the
actual common UTC start, cutoff, code revision, portfolio IDs, and services.
A private runtime.env supplies exported variables to systemd. Keep credentials
out of the trial manifest and Git. STRATEGY_RUNS_DIR selects this state root.
CZQUANT_BUCKET selects the private archive prefix. CZQUANT_START and
CZQUANT_CUTOFF define the fixed comparison window. CZQUANT_PR identifies the
implementation PR for its scheduled results comment.

## Install and inspect

After installing all four uv projects, create the five paper portfolios with
czquant portfolio. Write runtime.env, including absolute key paths and the
production API URL. Do not rely on a relative private-key path or the demo URL.

```bash
python3 scripts/experiments/install.py --environment /absolute/trial/runtime.env
systemctl --user enable --now czq-market.service czq-account.service
systemctl --user enable --now czq-sum-low.service czq-sum-high.service
systemctl --user enable --now czq-election-independent.timer czq-election-dependence.timer
systemctl --user enable --now czq-archive.timer czq-sunday.timer
systemctl --user list-timers 'czq-*'
systemctl --user status 'czq-*'
journalctl --user -u czq-market -u czq-account --since '10 minutes ago'
```

The installer writes unit files and reloads systemd; it does not start a run or
create portfolios. Persistent service operation requires user lingering.
Services use private file permissions. A strategy failure is a failed service;
continuous services restart after 15 seconds. Election timers prevent overlap.
The sum run records task failures rather than waiting out a successful-looking
duration. Restart resumes the same portfolio; it never reinitializes funding.

## Sunday computation

The persistent one-shot timer fires on Sunday 2026-10-25 at 18:05 UTC, with an
18:00 UTC data cutoff. scripts/experiments/sunday.sh generates the offline
comparison, archives the private artifacts, and posts only comparison.md to
the implementation PR. The report does not stop monitoring or liquidate open
holdings. Inspect its completion with:

```bash
systemctl --user status czq-sunday.service
journalctl --user -u czq-sunday.service
```

Test report generation before launch with an immediate cutoff and a separate
output directory. Do not run sunday.sh early with the final cutoff and publish
an incomplete future-window report. If the VM is unavailable, Persistent=true
runs the overdue job when it returns; the manifest still records the fixed
cutoff and the later ingestion time.

## Backup and restore

The private archive bucket is gs://pambot-1613449701904-cz-quant-private, in the
pambot infrastructure project. Uniform bucket-level access and public-access
prevention are enabled. Hourly archives use SQLite's consistent backup API,
include trial.json and checksums, and write timestamped directories. They do
not copy an open database/WAL pair. Raw account evidence is never published.

```bash
# With STRATEGY_RUNS_DIR and CZQUANT_BUCKET exported:
scripts/experiments/archive.sh
# Restore into a new directory, never over an active database:
gcloud storage cp --recursive gs://BUCKET/PREFIX/archives/STAMP /absolute/restore
cd /absolute/restore/STAMP
sha256sum -c SHA256SUMS
```

Point czquant report at the restored directory to reproduce results without
network or credentials. Restarting execution requires the same database,
strategy revision, and portfolio identifiers. Preserve old worktrees while
services use them. To stop this trial, stop the named czq strategy services and
election timers; keep monitors active while any positions remain open.

## Validation and known limits

Shared tests cover exact fees/cash, FIFO closes, partial fills, nonbinary
settlement without a strategy process, duplicate and conflicting evidence,
concurrent cash limits, uncertain live writes, restart consumption, corrections,
collateral accounting, funding-adjusted returns, stale marks, and offline
reporting. Sum tests cover the source pricing/classification plus durable halt
and timed-task failure propagation. Election tests compare candidate decisions
against existing fixtures and verify common-ledger execution.

Type checks cover the complete shared package, imported sum source, and both
changed election execution modules. The pre-existing election code outside
those modules is not newly claimed to be type-clean. All existing strategy
tests remain in CI.

The production key supports authenticated reads. Advanced upgrade returned
HTTP 403 with an empty body; the account remains basic. No eligibility order
was placed. Live fills/transfers/cancels are fixture-tested only. Full netting
and pre-existing live inventory require additional normalization; account
mismatches are explicit gaps and exclude live complete-period comparisons.
Resting/maker paper execution is not implemented. These limits do not alter the
schema or require a strategy base class.
