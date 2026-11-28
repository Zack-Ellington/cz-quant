# Polymarket broker

International Polymarket and Polymarket US have separate broker adapters.
International Polymarket supports paper outcome-token orders using public
Gamma metadata and CLOB books. Polymarket US supports paper and live binary
orders. Their tickers, clients, and credentials are distinct. Each broker
accepts orders only for its own venue.

Follow the [shared setup](../../README.md#shared-services). Run the CLI commands
from packages/czquant and use the same STRATEGY_RUNS_DIR as the monitors.
The [design principles](../design-principles.md) own the common interface and
schema.

Both clients log terminal HTTP errors with the venue, request method, path,
status, and up to 8,192 characters of the response body, with a truncation
flag. Request headers, request bodies, and query parameters are excluded.
The original exception and retry behavior are preserved. Read failures that
recover during retry do not emit this terminal error log.

## Paper execution

Create a venue-specific pool with explicit hypothetical cash:

```bash
uv run czquant pool example-polymarket --venue polymarket --cash 1000
```

Set EXAMPLE_TOKEN_ID to an outcome token's CLOB ID, not an event or market
slug. This example opens one venue-specific paper broker and requests ten
tokens at a limit of $0.42. Partial or zero fills are possible.

```python
import os
import subprocess

from czquant import Broker, Store
from czquant.polymarket import Polymarket, PolymarketPaper, ticker

store = Store()
client = Polymarket.from_env()
broker = Broker(store, "example-polymarket", PolymarketPaper(client))
revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
broker.start("example-strategy", revision, {"limit": "0.42"})
result = broker.submit({
    "ticker": ticker(os.environ["EXAMPLE_TOKEN_ID"]),
    "side": "bid",
    "count": "10.00",
    "price": "0.4200",
    "time_in_force": "immediate_or_cancel",
}, context={"research": {"signal": "example", "features": {"score": 0.7}}})
print(result["status"], result["filled"])
print(broker.state().lots)
store.close()
```

Each outcome token is recorded as a YES contract with a PM: ticker prefix.
A bid buys the token. To sell a held token, submit an ask with reduce_only
set to true. Buying the opposing outcome means buying its separate token;
do not model an unsupported naked short by treating it as the token's NO side.

Books are normalized to the existing binary layout: token bids become YES
bids and token asks at price p become NO bids at 1 minus p. The paper policy
is polymarket-ioc-depth-v1, with a default 500 ms latency, IOC/FOK depth
simulation, persistent consumed liquidity, and per-pool cash checks.
Its implemented fee formula uses each market's fee schedule and rounds to
five decimal places. Fee assumptions and observed books are retained in
execution evidence.

## Market reads and settlement

The client accepts POLYMARKET_GAMMA_URL and POLYMARKET_CLOB_URL overrides.
Without overrides it uses the public endpoints defined in
[polymarket.py](../../packages/czquant/src/czquant/polymarket.py). No credentials
are required by this adapter.

Start the public collector in a separate process:

```bash
uv run czquant monitor polymarket
```

It records marks for held or watched tokens and outstanding orders, and
settles paper lots when final results are available. Recorded payouts can be
1, 0, or a nonbinary value such as 0.5. Strategy code need not consume this
collector's API. See [market monitoring](market-monitor.md) and
[reporting](reporting.md) for reference coverage and valuation.

Brokers share the logging directory, not spendable balances. Each pool belongs
to one venue in paper and live mode. A large hypothetical paper pool is valid,
but another venue cannot spend its cash. Shared reports can aggregate pools
through a reporting-only portfolio.

## Polymarket US

A US binary market uses a PMUS: ticker followed by its market slug. YES is
long and NO is short. Orders use the common YES-book convention; the adapter
converts bid/ask and existing holdings into the supported venue intent.
International PM: token IDs are rejected by this broker.

For public paper execution, create a separate US pool:

```bash
uv run czquant pool example-polymarket-us --venue polymarket-us --cash 1000
```

Then open its venue-specific broker:

```python
import os

from czquant import Broker, Store
from czquant.polymarket_us import PolymarketUS, PolymarketUSPaper

store = Store()
client = PolymarketUS.from_env(authenticated=False)
broker = Broker(store, "example-polymarket-us", PolymarketUSPaper(client))
result = broker.submit({
    "ticker": "PMUS:" + os.environ["EXAMPLE_MARKET_SLUG"],
    "side": "bid",
    "count": "10",
    "price": "0.42",
    "time_in_force": "immediate_or_cancel",
}, context={"experiment": {"signal": "example"}})
print(broker.venue, result["status"], result["filled"])
store.close()
```

The paper adapter supports IOC/FOK, reads the market's feeCoefficient, and
records its polymarket-us-ioc-depth-v1 policy with the execution evidence.
The theta constructor argument overrides the paper fee coefficient. It does
not change a live venue fee.

## US live mapping and execution

Set POLYMARKET_US_KEY_ID and POLYMARKET_US_SECRET_KEY_PATH. The latter names
a private file containing the base64 Ed25519 secret. The client also accepts
POLYMARKET_US_GATEWAY_URL and POLYMARKET_US_API_URL overrides. Read the balance
without placing an order:

```bash
uv run czquant check-polymarket-us
```

The map-live command maps a flat account with no open orders to one Polymarket
US cash pool. For several pools on the same account, use account registration
and open-sub as described in [virtual subaccounts](virtual-subaccounts.md).
The account monitor reconciles those registered allocations together.

```bash
uv run czquant map-live example-us-live --venue polymarket-us
```

A two-venue experiment also maps a Kalshi pool and can define a reporting group:

```bash
uv run czquant map-live example-kalshi-live --venue kalshi --subaccount 1
uv run czquant portfolios create --config portfolio.yaml
```

Use this portfolio.yaml to group the existing live pools:

```yaml
id: example-live
pools: [example-kalshi-live, example-us-live]
```

See the [operator CLI guide](../cli.md) for inspection and retirement.

Use --subaccount 0 --primary-account for the Kalshi primary account, or
--share-account to join an already reconciled Kalshi account. Mapping records
opening balances without moving money or placing orders. See the
[Kalshi guide](kalshi-broker.md#live-cash-pools) for allocation and recovery.

Construct one broker per venue cash pool. This example sends nothing;
its adapters also have dry-run enabled if an order is subsequently submitted.

```python
from czquant import Broker, Store
from czquant.kalshi import Kalshi
from czquant.live import KalshiLive
from czquant.polymarket_us import PolymarketUS, PolymarketUSLive

store = Store()
kalshi = Broker(store, "example-kalshi-live", KalshiLive(Kalshi(), dry_run=True), "example-run")
polymarket = Broker(store, "example-us-live", PolymarketUSLive(
    PolymarketUS.from_env(dry_run=True)), "example-run")
print(kalshi.available, polymarket.available)  # each venue's unreserved cash
print(kalshi.state().cash)  # only the Kalshi pool; reporting aggregates separately
store.close()
```

Strategies select the broker for each leg and own sequencing and repairs.
US live orders support integer-quantity taker IOC/FOK. The adapter books fills
from synchronous execution reports, including their reported commissions.
When fills are present, it also returns an execution summary with decimal-string
quantity, yes_notional, fee, yes_price_min, and yes_price_max. These aggregate
the fills already booked; they do not create additional accounting entries.
YES notional is the sum of YES price times quantity, and price bounds are the
minimum and maximum reported YES fill prices. The shared paper execution path
returns the same fields from its simulated fills. Consumers must allow an
absent summary rather than infer priced execution from an acknowledgement.

Cancel and amend are not exposed by this adapter. Missing final state retains
an unknown order and its reservation. The
[account monitor](account-monitor.md#polymarket-us-recovery) can recover an
order with a recorded venue ID. A lost reply without that ID remains an
explicit recovery gap; submitting a new client identity is not recovery.

Fixture tests validate the implemented response shapes. They do not establish
live short-side execution, commission-field availability, or rebate handling
for every venue response. Unreadable account activity remains a discrepancy.
