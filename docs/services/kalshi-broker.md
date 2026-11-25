# Kalshi broker

The Kalshi adapter submits native venue orders through the common broker and
records execution ownership in the shared store. Its paper adapter simulates
orders against public books. The [design principles](../design-principles.md)
define the interface, arbitrary submission context, and economic schema.

Run the commands below from packages/czquant after the
[shared setup](../../README.md#shared-services). Use the same STRATEGY_RUNS_DIR
for the broker, monitors, and reports. A strategy installs czquant in its own
environment, for example with uv add ../../packages/czquant from a strategy
directory two levels below the repository root.

## Paper execution

Create a portfolio once. This records hypothetical funding; opening its broker
again preserves its cash, reservations, and positions.

```bash
uv run czquant portfolio example-kalshi --cash 1000
```

Set EXAMPLE_MARKET to an open Kalshi market ticker. Run this Python example
in an environment with czquant installed. It requests ten paper contracts at
a maximum YES price of $0.42. Actual simulated fills depend on the book.
No exchange order is sent.

```python
import os
import subprocess

from czquant import Broker, Store
from czquant.kalshi import Kalshi
from czquant.paper import Paper

store = Store()
broker = Broker(store, "example-kalshi", Paper(Kalshi(authenticated=False)))
revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
broker.start("example-strategy", revision, {"limit": "0.42"})
result = broker.submit({
    "ticker": os.environ["EXAMPLE_MARKET"],
    "side": "bid",
    "count": "10.00",
    "price": "0.4200",
    "time_in_force": "immediate_or_cancel",
    "self_trade_prevention_type": "taker_at_cross",
}, context={"research": {"hypothesis": "example", "scores": [0.3, 0.7]}})
print(result["status"], result["filled"])
print(broker.available)
store.close()
```

Orders use V2 YES-book direction and decimal strings. A bid buys YES or
closes held NO. An ask buys NO or closes held YES. Use reduce_only when an
order is intended only to close existing inventory. The current paper
policy supports immediate_or_cancel and fill_or_kill; it does not simulate
resting or maker orders. FOK applies to one order, not an entire basket.

The ioc-depth-v1 policy reads depth after a default 300 ms latency, respects
limits and available cash, and applies the market series' supported quadratic
taker fee rounded upward to cents. Consumed liquidity persists across
restarts. A price level must be absent for 300 seconds before its consumption
is reset. These are simulation assumptions, not a queue-position model.
The [market monitor](market-monitor.md) collects marks and settles paper lots
after confirmed payouts, including after the strategy stops.

## Credentials and account setup

Paper market reads need no key. Authenticated account reads and live orders
use KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH. The latter names a private
key file outside Git. KALSHI_API_BASE_URL selects the environment; the client
defaults to https://external-api.kalshi.com/trade-api/v2. Demo and production
use different credentials and account histories. Export environment variables
explicitly; the application does not load .env files.

Check credentials and the account's reported access before mapping it:

```bash
uv run czquant setup
```

The command prints account limits and verifies a balance read. It does not
place an order. setup --upgrade additionally requests an API access upgrade
and rereads the limits. Resolve any venue-reported prerequisite explicitly;
setup does not place a qualifying trade. Keep account-specific responses in
private operational records.

## Live portfolios

A new mapping assigns a flat account or numbered subaccount to a portfolio.
Existing inventory needs an opening ledger. With --share-account, a portfolio
can join an already mapped Kalshi account if its positions and orders reconcile
with the existing allocations. Its opening cash is the account balance less
cash already owned by those allocations.

Map an existing, funded subaccount to a new portfolio:

```bash
uv run czquant map-live example-live --subaccount 1
```

If the account has no numbered-subaccount access, the explicit primary-account
alternative is:

```bash
uv run czquant map-live example-live --subaccount 0 --primary-account
```

Choose one mapping. Both record opening cash without placing an order or
transferring money. Shared portfolios retain separate ownership in the ledger;
the account monitor reconciles their combined cash and positions. Unattributed
trades or unsupported deposits cause reconciliation differences. Reopening
the broker retains the mapping.

To allocate recorded Kalshi cash between portfolios already sharing an account:

```bash
uv run czquant allocate --from example-live --to another-live --dollars 100
```

This changes ledger ownership without transferring money at the venue. It
refuses an allocation larger than the source portfolio's unreserved cash.
retire-live retires a reconciled portfolio with no remaining open positions
or orders; --to names the sharing portfolio receiving its Kalshi cash.
--book-difference explicitly records an unexplained Kalshi cash difference
as an external adjustment. Such an adjustment is not trading profit.
The map-live-cross command can also remap an existing portfolio's flat,
reconciled Kalshi side to another account while retaining its history.

Construct a live broker with the authenticated transport. This example only
opens the broker and reads its local state; a subsequent submit sends a real
order, and the transport has no generic dry-run flag.

```python
from czquant import Broker, Store
from czquant.kalshi import Kalshi

store = Store()
broker = Broker(store, "example-live", Kalshi(authenticated=True))
print(broker.state().cash, broker.available)
store.close()
```

Live order requests are scoped to the mapped account. The venue determines
live cash sufficiency. The broker logs intent and acknowledgements; the
[account monitor](account-monitor.md) ingests authoritative fills and releases
reservations after reconciliation. cancel and amend use the broker's client
order ID and require a recovered venue order ID. Amendments resize the
reservation and cannot change the instrument or side.

create-subaccount creates a real venue subaccount. transfer moves real cash
between subaccounts and requires --client-id, --from-subaccount,
--to-subaccount, and --amount-cents. Retain the UUID supplied as --client-id
for recovery of that same transfer. These are explicit operator actions;
broker construction never allocates or moves money automatically.

## Recovery

Retain the portfolio and client order identities after a timeout. An unknown
write is not an unfilled order; do not retry it under a new identity. Run the
account monitor to recover evidence before interpreting available cash or
positions. Missing venue IDs prevent cancellation and amendment until recovered.
Use the [reporting guide](reporting.md#backup-and-restore) for database recovery.

For normalized IOC/FOK results, use KalshiLive(Kalshi(authenticated=True)).
The adapter reports status and filled quantity from the response while the
account monitor books authoritative fills. KalshiLive(..., dry_run=True) logs
the built request and sends nothing. Unknown responses persist as unknown
and keep their reservations. This adapter does not expose cancel or amend;
the raw authenticated Kalshi transport above supports those operations.

The source is [broker.py](../../packages/czquant/src/czquant/broker.py),
[kalshi.py](../../packages/czquant/src/czquant/kalshi.py),
[paper.py](../../packages/czquant/src/czquant/paper.py), and
[operator.py](../../packages/czquant/src/czquant/operator.py).
