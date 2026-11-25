# Account monitor

The account collector records authoritative account evidence, recovers live
executions, and reconciles the recorded portfolio with the venue. The available
authenticated component supports Kalshi. Paper positions are reconstructed
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
and evidence even when no live portfolio is mapped. Successful public paper
collection does not establish that account authentication works.

Run continuously under a separate process supervisor:

```bash
uv run czquant monitor account --interval 60
```

The collector retrieves orders, fills, balances, positions, settlements, and
supported subaccount transfers. It uses a persisted checkpoint and overlap
when requesting history. It joins fills to broker-owned orders and calls
Broker.fill, so fills retain strategy ownership and context. It releases an
order's reservation only after terminal venue status and recorded fill
counts agree. Restart it with the same database.

## Reconciliation and recovery

The available live mapping assigns each account/subaccount to one portfolio.
The collector compares that portfolio's reconstructed cash and inventory with
the account. Supported funding and settlements become economic transactions;
raw venue evidence remains available for audit. See the
[schema](../design-principles.md#event-envelope-and-economic-schema).

An invalid or conflicting fill is quarantined rather than silently accepted.
Unmatched fills are retained as unattributed evidence. Unknown collateral,
manual trading, or other unsupported account effects remain discrepancies.
Resolve their source before treating the portfolio as fully reconciled.
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
