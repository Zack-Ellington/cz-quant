# Account monitor

The account collector records authoritative account evidence, recovers live
executions, and reconciles recorded allocations with the venue. Authenticated
components support Kalshi and Polymarket US. Paper positions are reconstructed
and settled by the [market monitor](market-monitor.md); they are not evidence
from a real account.

Follow the [shared setup](../../README.md#shared-services) and
[Kalshi credential setup](kalshi-broker.md#credentials-and-account-setup).
Use the same STRATEGY_RUNS_DIR as the live brokers. Strategies need not read
monitor health or use a monitor API; evidence ingestion is infrastructure.

## Run and inspect

From packages/czquant, run one authenticated pass:

```bash
uv run czquant monitor account --once
```

The command logs a status containing mapped, unattributed, and quarantined
fill counts plus reconciliation discrepancies. It retains account snapshots
and evidence even when no live cash pool is mapped. Successful public paper
collection does not establish that account authentication works.

Run continuously under a separate process supervisor:

```bash
uv run czquant monitor account --interval 60
```

The collector polls REST for orders, fills, balances, positions, settlements, and
supported subaccount transfers. It uses a persisted checkpoint and overlap
when requesting history. It joins fills to broker-owned orders and calls
Broker.fill, so fills retain strategy ownership and context. It releases an
order's reservation only after terminal venue status and recorded fill
counts agree. Restart it with the same database.

## Reconciliation and recovery

Kalshi cash pools can share an account/subaccount through explicit mappings
and allocations. The collector compares their aggregate cash and inventory
with the account. Cash discrepancies apply to all its allocations; position
discrepancies identify the holders of the affected market. The collector
records these facts without imposing a universal strategy halt.

Kalshi settlement checks compare net YES minus NO quantity and expected revenue.
A closed round trip can report equal gross YES and NO counts with zero revenue.
Unexplained settlements are attributed to current holders, then pools that traded
the market, then the registered reserve. Legacy unregistered groups retain the
account-wide discrepancy when no owner can be identified.

Supported funding and settlements become economic transactions;
raw venue evidence remains available for audit. See the
[schema](../design-principles.md#event-envelope-and-economic-schema).

An invalid or conflicting fill is quarantined rather than silently accepted.
Unmatched fills are retained as unattributed evidence. Unknown collateral,
manual trading, or other unsupported account effects remain discrepancies.
Resolve their source before treating the cash pool as fully reconciled.
Do not insert a balancing profit or delete the original facts.

If an order acknowledgement was lost, the collector can recover its venue
ID and fills by the recorded client order identity. Do not resubmit under a
new client ID simply because no fill has reached local accounting yet. The
broker's local state can lag the account until ingestion completes.

The collector records health; individual strategies decide whether health
or discrepancy observations should gate their trading. A healthy process
does not prove complete market-reference coverage. The
[reporting service](reporting.md) exposes valuation coverage separately.

Strategies that write standing halt observations can be inspected with:

```bash
uv run czquant halts example-live
```

After investigating a halt, clear only the reviewed record:

```bash
uv run czquant clear-halt example-live --halt-id RECORD_ID --reason 'Evidence reconciled'
```

Replace RECORD_ID and the reason with the actual reviewed evidence. This adds
a halt_cleared observation; it does not delete the halt, change cash, or force
a strategy to resume. Resume behavior belongs to that strategy.

Account responses and logs are private. Send operational logs under the shared
root, keep credentials outside Git, and use a full database backup for
recovery. Implementation: [monitor.py](../../packages/czquant/src/czquant/monitor.py).

### Cross-market run policy

The [cross-market strategy](../../strategies/cross-market-polymarket/#live-preparation-and-recovery)
distinguishes configuration errors, runtime refusals and uncertain execution.
Invalid pool, scope or pair configuration raises before submission. Insufficient
funding, opposite inventory or a reconciliation pause records a rejection and
leaves the process running. These are strategy policies; the monitor records
evidence without imposing them on other strategies.

Acknowledged orders pause new entries until the monitor books their fills and
releases their reservations. This pause survives restart without creating a
persistent halt. Inventory completeness is checked after the trade's outstanding
orders reconcile, so delayed accounting alone is not treated as an incomplete
basket. Missing or stale monitor health also pauses entries.

Pending or unknown writes halt new entries and retain reservations for recovery.
A known later-leg failure permits repair of known excess fills; an uncertain
execution outcome stops further entry and repair submissions until reconciliation.
The cross-market strategy then automatically sells unmatched inventory at fresh
visible bids, without an entry-price loss floor. Partial or unavailable exits
remain pending for later scans. Uncertain repair orders must reconcile before
another sale. Only execution-recovery halts clear automatically when quantities
match; monitor discrepancies and other hard halts require explicit resolution.
A new order identity or a new pool is not a substitute for recovering an
uncertain write.


## Polymarket US recovery

Run its component alongside the Kalshi collector when both venues are mapped:

```bash
uv run czquant monitor polymarket-us --interval 60
```

It needs US credentials when a live cash pool is mapped. Without live mappings,
it can use public data to mark and settle paper positions. It records held-market
books for live valuation as well as public outcomes.

For live orders with a recorded venue ID, the collector reads cumulative fills
and fees, books missing executions through the US broker, and releases terminal
reservations. The implemented API path cannot recover a lost acknowledgement
without its venue ID. That order remains a discrepancy for investigation.

Supported deposits, withdrawals, rebates, and deposit advances are logged as
separate account effects. Pending advances and their reversals change funding,
not trading profit. Balance and position snapshots retain the account evidence.
On registered accounts, funding goes to the reserve. Rebates without pool ownership
evidence remain quarantined and nonspendable; they are reported as unattributed_rebate
rather than assigned to the reserve. See [virtual subaccounts](virtual-subaccounts.md).

Pending deposits can carry an explicit advance or buying power above settled
cash. The latter is bounded by the deposit reservation and pending deposit
amounts. The collector books the advance once, including deposits pending at
registration. Completion books only the remainder; failure reverses the advance
and records a deposit_failed discrepancy. Allocation checks count recorded
pending advances only up to the venue's current deposit reservation.

A public resolution can retire live lots only when the observed account no
longer holds them and its cash matches the recorded balance plus their payout.
If those observations disagree, the lots remain and reconciliation reports
the difference. Public paper settlement does not need this account evidence.

The US component emits polymarket_us health. Report completeness for a
reporting portfolio containing pools at both venues requires fresh healthy evidence from both
account components, independently of a strategy's chosen halt policy.
