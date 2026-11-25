# Polymarket broker

The available adapter simulates international Polymarket outcome-token orders
through the common broker. It uses public Gamma market metadata and CLOB books;
it does not sign or send exchange orders. Polymarket US is a different venue
and has no adapter in this checkout. Do not use international token IDs or
credentials as substitutes for a US venue implementation.

Follow the [shared setup](../../README.md#shared-services). Run the CLI commands
from packages/czquant and use the same STRATEGY_RUNS_DIR as the monitors.
The [design principles](../design-principles.md) own the common interface and
schema.

## Paper execution

Create a portfolio with hypothetical cash:

```bash
uv run czquant portfolio example-polymarket --cash 1000
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
simulation, persistent consumed liquidity, and per-portfolio cash checks.
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

Each broker can share the logging directory with brokers for other venues.
The current paper cash model is portfolio-scoped; use distinct paper
portfolios when experiments need separate cash pools. Do not infer separate
venue balances merely from creating two broker objects on one portfolio.
