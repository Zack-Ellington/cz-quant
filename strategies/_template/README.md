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

For portfolio accounting, add czquant as a local dependency from
../../packages/czquant. Create a portfolio explicitly, then bind its broker in
your strategy. Pass native Kalshi order fields and arbitrary JSON context.
Your strategy can use any market reader or execution schedule. Run the market
and account monitors independently. See [the shared guide](../../docs/broker-and-reporting.md)
for the complete example and offline report commands.
