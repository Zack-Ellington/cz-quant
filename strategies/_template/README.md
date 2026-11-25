# strategy-template

TODO: describe the strategy. State the market, the edge, and the risks.

## Install

```bash
uv sync
cp .env.example .env
```

Fill in `.env`. Then export the variables into your shell. Do this in each new
shell, and again after you change `.env`.

```bash
set -a; source .env; set +a
```

## Run

```bash
uv run strategy
```

The `strategy` command calls `run()` in `src/strategy/runner.py`. The code reads
configuration from the environment only. It does not read `.env`.

## Shared execution and comparison

Follow the [design principles](../../docs/design-principles.md). Every trade
must go through a broker bound to one venue and cash pool, including paper
trades, exits, and retries. A cross-venue strategy uses a broker for each venue;
both write to the shared logging directory. The scaffold is optional, and
research code must not depend on another strategy's implementation.

For the current broker API, add czquant as a local dependency from
../../packages/czquant. Create a portfolio explicitly, then bind its broker in
your strategy. Pass native Kalshi order fields and arbitrary JSON context.
Your strategy can use any market reader or execution schedule. Run the market
and account monitors independently for reporting; your strategy need not
consume their APIs or health records. See [the Kalshi example](../../docs/services/kalshi-broker.md#paper-execution)
and [report commands](../../docs/services/reporting.md).
